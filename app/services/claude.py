"""Claude AI service for TDD generation from session transcripts."""

import asyncio
import json
import re
from typing import Any

import anthropic
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0

TDD_GENERATION_PROMPT = """You are a technical documentation specialist at BonEcho. Given a session transcript and structured discovery notes, produce a complete Technical Design Document (TDD).

Your output must be a valid JSON object. Do NOT wrap it in markdown code fences. Output raw JSON only.

Rules:
- Extract facts directly from the transcript where possible
- Mark inferred or assumed details with [INFERRED] inline
- Leave fields as empty strings or empty arrays if truly no information was captured
- Do not hallucinate company names, project names, or technical details not present in the source

Output this exact JSON schema:
{
  "project_name": "string — name of the project or product being built",
  "company_name": "string — name of the client company",
  "project_overview": "string — what is being built and why, 2-4 sentences",
  "current_state": "string — what exists today, what they are replacing or extending",
  "pain_points": [
    "string — specific pain point or business problem driving this project"
  ],
  "recommended_agents": [
    {
      "name": "string — agent name (e.g. 'Onboarding Agent', 'Support Bot')",
      "purpose": "string — what this agent does and the problem it solves",
      "priority": "high | medium | low"
    }
  ],
  "integration_points": [
    {
      "system": "string — name of the system or service (e.g. 'Salesforce', 'Stripe')",
      "type": "string — type of integration (e.g. 'CRM', 'Payment', 'Communication')",
      "description": "string — what data or actions flow through this integration"
    }
  ],
  "open_questions": [
    "string — unanswered question that needs follow-up"
  ]
}"""


class ClaudeServiceError(Exception):
    """Raised when Claude API calls fail after retries."""


class ClaudeService:
    """Handles Claude API calls for TDD generation."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def generate_tdd(
        self,
        transcript: list[dict[str, str]],
        discovery_sections: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a structured TDD from a session transcript and discovery notes.

        Args:
            transcript: List of message dicts with 'role' and 'content' keys.
            discovery_sections: Dict of discovery section name -> accumulated notes.

        Returns:
            Parsed TDD dict matching the 6-section schema.

        Raises:
            ClaudeServiceError: If all retries are exhausted.
        """
        transcript_text = self._format_transcript(transcript)
        discovery_text = self._format_discovery_sections(discovery_sections)

        user_message = (
            f"## Session Transcript\n\n{transcript_text}\n\n"
            f"## Discovery Notes\n\n{discovery_text}\n\n"
            "Generate the TDD JSON now."
        )

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("claude_tdd_generation_attempt", attempt=attempt)
                response = await self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=TDD_GENERATION_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                raw = response.content[0].text.strip()
                tdd = self._parse_json(raw)
                logger.info("claude_tdd_generation_success", attempt=attempt)
                return tdd
            except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "claude_tdd_generation_retry",
                        attempt=attempt,
                        wait=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "claude_tdd_parse_error",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt >= _MAX_RETRIES:
                    break

        raise ClaudeServiceError(
            f"TDD generation failed after {_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _format_transcript(transcript: list[dict[str, str]]) -> str:
        lines = []
        for turn in transcript:
            role = turn.get("role", "unknown").capitalize()
            content = turn.get("content", "")
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines) if lines else "(empty transcript)"

    @staticmethod
    def _format_discovery_sections(sections: dict[str, Any]) -> str:
        if not sections:
            return "(no discovery notes)"
        parts = []
        for key, value in sections.items():
            label = key.replace("_", " ").title()
            if isinstance(value, list):
                body = "\n".join(f"- {item}" for item in value) if value else "(none)"
            else:
                body = str(value) if value else "(none)"
            parts.append(f"**{label}**\n{body}")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Extract and parse JSON from a Claude response, stripping any markdown fences."""
        # Strip ```json ... ``` or ``` ... ``` if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        return json.loads(cleaned.strip())
