"""Post-session pipeline — parallel 2-file generation, S3 upload, email delivery."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Any

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.services.claude import ClaudeService, ClaudeServiceError
from app.services.notifications import notify_session_complete, send_session_output_email
from app.services.tdd_generator import TDDGenerator
from app.services.session_persistence import (
    persist_session_completion,
    SessionCompletionRecord,
)

logger = structlog.get_logger(__name__)


@dataclass
class PostSessionResult:
    """Outcome of a post-session pipeline run."""

    session_id: str
    company_name: str
    success: bool

    # Per-artifact results (None = not generated)
    tdd_s3_key: str | None = None
    claude_md_s3_key: str | None = None

    email_sent: bool = False
    slack_sent: bool = False
    errors: list[str] = field(default_factory=list)


async def run_post_session_pipeline(
    *,
    session_id: str,
    transcript: list[dict[str, str]],
    discovery_sections: dict[str, Any],
    company_name: str,
    user_email: str,
) -> PostSessionResult:
    """Run the post-session pipeline for a completed discovery session.

    Generates 2 output artifacts in parallel, uploads to S3, delivers
    via email, and sends a Slack notification. Partial failures are tolerated —
    whatever was generated is still delivered.

    Args:
        session_id: Unique session identifier (used for S3 key prefix and logs).
        transcript: Full conversation history as list of {role, content} dicts.
        discovery_sections: Dict of the 3 discovery areas and their accumulated notes.
        company_name: Client company name used in filenames and email copy.
        user_email: Recipient email address for the output files.

    Returns:
        PostSessionResult with per-artifact keys and success/error details.
    """
    result = PostSessionResult(
        session_id=session_id,
        company_name=company_name,
        success=False,
    )

    logger.info(
        "post_session_pipeline_started",
        session_id=session_id,
        company_name=company_name,
        user_email=user_email,
    )

    svc = ClaudeService()

    # -------------------------------------------------------------------------
    # Step 1 — Generate 2 artifacts in parallel
    # -------------------------------------------------------------------------
    tdd_task = asyncio.create_task(svc.generate_tdd(transcript, discovery_sections))
    claude_md_task = asyncio.create_task(svc.generate_claude_md(transcript, discovery_sections))

    tdd_result, claude_md_result = await asyncio.gather(
        tdd_task,
        claude_md_task,
        return_exceptions=True,
    )

    # -------------------------------------------------------------------------
    # Step 2 — Format TDD dict → DOCX bytes
    # -------------------------------------------------------------------------
    tdd_docx_bytes: bytes | None = None
    tdd_filename: str | None = None

    if isinstance(tdd_result, Exception):
        err = f"TDD generation failed: {tdd_result}"
        result.errors.append(err)
        logger.error("post_session_tdd_failed", session_id=session_id, error=str(tdd_result))
    else:
        try:
            gen = TDDGenerator()
            tdd_docx_bytes = gen.generate_docx(tdd_result)
            tdd_filename = gen.get_filename(tdd_result)
            logger.info("post_session_tdd_docx_generated", session_id=session_id)
        except Exception as exc:
            err = f"TDD DOCX formatting failed: {exc}"
            result.errors.append(err)
            logger.error("post_session_tdd_docx_failed", session_id=session_id, error=str(exc))

    safe_name = company_name.replace(" ", "_").replace("/", "-") or "Unknown_Company"

    if isinstance(claude_md_result, Exception):
        err = f"CLAUDE.md generation failed: {claude_md_result}"
        result.errors.append(err)
        logger.error("post_session_claude_md_failed", session_id=session_id, error=str(claude_md_result))
        claude_md_content: str | None = None
    else:
        claude_md_content = claude_md_result

    # -------------------------------------------------------------------------
    # Step 3 — Upload to S3 in parallel
    # -------------------------------------------------------------------------
    upload_tasks = []

    if tdd_docx_bytes is not None and tdd_filename:
        tdd_key = f"sessions/{session_id}/{tdd_filename}"
        upload_tasks.append(
            asyncio.create_task(_upload_to_s3(tdd_key, tdd_docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
        )
    else:
        upload_tasks.append(asyncio.create_task(_noop()))

    if claude_md_content is not None:
        claude_md_key = f"sessions/{session_id}/CLAUDE.md"
        upload_tasks.append(
            asyncio.create_task(_upload_to_s3(claude_md_key, claude_md_content.encode(), "text/markdown"))
        )
    else:
        upload_tasks.append(asyncio.create_task(_noop()))

    s3_results = await asyncio.gather(*upload_tasks, return_exceptions=True)

    # Record S3 keys on success
    if tdd_docx_bytes is not None and not isinstance(s3_results[0], Exception):
        result.tdd_s3_key = f"sessions/{session_id}/{tdd_filename}"
    elif isinstance(s3_results[0], Exception):
        result.errors.append(f"TDD S3 upload failed: {s3_results[0]}")

    if claude_md_content is not None and not isinstance(s3_results[1], Exception):
        result.claude_md_s3_key = f"sessions/{session_id}/CLAUDE.md"
    elif isinstance(s3_results[1], Exception):
        result.errors.append(f"CLAUDE.md S3 upload failed: {s3_results[1]}")

    # -------------------------------------------------------------------------
    # Step 4 — Email delivery (send whatever we have; skip if nothing generated)
    # -------------------------------------------------------------------------
    any_generated = any(x is not None for x in [tdd_docx_bytes, claude_md_content])

    if any_generated:
        tdd_doc_name = tdd_filename or f"{safe_name}_TDD.docx"
        project_name = (
            tdd_result.get("company_name") or company_name
            if not isinstance(tdd_result, Exception)
            else company_name
        )
        try:
            await send_session_output_email(
                user_email=user_email,
                project_name=project_name,
                tdd_docx_bytes=tdd_docx_bytes or _empty_docx(),
                claude_md_content=claude_md_content or "(CLAUDE.md generation failed)",
            )
            result.email_sent = True
            logger.info("post_session_email_sent", session_id=session_id, user_email=user_email)
        except Exception as exc:
            result.errors.append(f"Email delivery failed: {exc}")
            logger.error("post_session_email_failed", session_id=session_id, error=str(exc))
    else:
        result.errors.append("All generation tasks failed — no email sent")
        logger.error("post_session_all_generation_failed", session_id=session_id)

    # -------------------------------------------------------------------------
    # Step 5 — Slack notification
    # -------------------------------------------------------------------------
    try:
        business_summary = (
            tdd_result.get("business_overview", "")[:120]
            if not isinstance(tdd_result, Exception) and tdd_result.get("business_overview")
            else company_name
        )
        await notify_session_complete(
            user_email=user_email,
            business_summary=business_summary,
            session_id=session_id,
        )
        result.slack_sent = True
    except Exception as exc:
        # Non-fatal
        result.errors.append(f"Slack notification failed: {exc}")
        logger.error("post_session_slack_failed", session_id=session_id, error=str(exc))

    result.success = result.email_sent or result.slack_sent

    # -------------------------------------------------------------------------
    # Step 6 — Persist artifact + summary references onto the session row so
    # the admin dashboard can surface them later.
    # -------------------------------------------------------------------------
    summary_text: str | None = None
    business_summary_text: str | None = None
    if not isinstance(tdd_result, Exception):
        business_summary_text = (tdd_result.get("business_overview") or None)
        summary_text = (
            tdd_result.get("executive_summary")
            or tdd_result.get("summary")
            or business_summary_text
        )

    try:
        await persist_session_completion(
            SessionCompletionRecord(
                session_id=session_id,
                tdd_s3_key=result.tdd_s3_key,
                claude_md_s3_key=result.claude_md_s3_key,
                summary=summary_text,
                business_summary=business_summary_text,
                email_sent=result.email_sent,
                final_status="COMPLETED" if result.success else "ERROR",
            )
        )
    except Exception as exc:
        result.errors.append(f"Session row update failed: {exc}")
        logger.error(
            "post_session_db_persist_failed", session_id=session_id, error=str(exc)
        )

    logger.info(
        "post_session_pipeline_complete",
        session_id=session_id,
        success=result.success,
        errors=result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _upload_to_s3(key: str, data: bytes, content_type: str) -> None:
    """Upload bytes to S3 in a thread pool (boto3 is synchronous)."""

    def _put() -> None:
        s3 = boto3.client("s3", region_name=settings.AWS_REGION)
        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    try:
        await asyncio.to_thread(_put)
        logger.info("s3_upload_success", key=key, bytes=len(data))
    except (BotoCoreError, ClientError) as exc:
        logger.error("s3_upload_failed", key=key, error=str(exc))
        raise


async def _noop() -> None:
    """Placeholder for asyncio.gather when an artifact was not generated."""


def _empty_docx() -> bytes:
    """Return minimal DOCX bytes as fallback when TDD generation failed."""
    from docx import Document  # noqa: PLC0415

    doc = Document()
    doc.add_paragraph("TDD generation failed — no content available.")
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()
