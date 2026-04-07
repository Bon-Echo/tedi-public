"""Claude AI service for active discovery conversation and session output generation."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import anthropic
import structlog

from app.config import settings
from app.schemas import Coverage, DiscoveryResponse, DiscoveryUpdate, SessionPhase

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0

# Load discovery system prompt from file, with inline fallback
_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "discovery_system.txt"

try:
    DISCOVERY_SYSTEM_PROMPT = _PROMPT_PATH.read_text()
except FileNotFoundError:
    logger.warning("discovery_prompt_file_not_found", path=str(_PROMPT_PATH))
    DISCOVERY_SYSTEM_PROMPT = """\
You are Tedi, an AI discovery guide for BonEcho. Lead a structured session to help the user \
discover where AI agents can work in their business. Cover: business_overview, dispatch_capacity, \
hiring_seasonality, fleet_equipment, knowledge_transfer. Respond in JSON with spoken_response, \
discovery_updates, coverage, internal_notes, session_phase, elapsed_minutes.
"""

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

CLAUDE_MD_GENERATION_PROMPT = """You are a technical writer at BonEcho. Given a discovery session transcript and structured discovery notes, produce a CLAUDE.md file for the client's project.

CLAUDE.md is a context file that developers place in their project root so that Claude Code understands the company and project. It must be concise, accurate, and LLM-optimized — no filler, no padding.

Rules:
- Output valid markdown only. No JSON, no code fences wrapping the whole output.
- Only include information explicitly mentioned in the transcript or discovery notes.
- Do not hallucinate names, tools, workflows, or terminology not grounded in the source.
- Omit any section entirely if no relevant information was captured for it — do not include empty sections or placeholder text like "N/A" or "Not discussed".
- Use short, declarative bullet points. Avoid prose paragraphs.
- Language should be clear and direct — suitable for an LLM reading it as context.

Output the following sections (omit any section with no content):

## Company Context
- Company name and what they do (1-2 bullet points)
- Approximate headcount or team size if mentioned
- Verticals or industries served
- Key stakeholders mentioned by name and role

## Workflows
- Core business workflows identified (e.g. job dispatch, hiring, equipment tracking)
- Step-by-step only if the session went into that level of detail
- Keep each workflow to 2-4 bullets maximum

## Tech Stack
- Software tools and platforms currently in use
- Integrations mentioned (APIs, third-party services)
- Known technical constraints or legacy systems

## Agent Goals
- Recommended AI agents and their primary objectives (one bullet per agent)
- Priority: high / medium / low for each
- Measurable success metric if mentioned

## Key Terminology
- Industry-specific or company-specific terms that appeared in the session
- Format: **Term** — definition or context"""

SKILLS_GENERATION_PROMPT = """You are an AI automation specialist at BonEcho. Given a discovery session transcript and structured discovery notes, extract concrete automation opportunities and produce a skills file in Agent Factory YAML format.

Your output must be valid YAML only. Do NOT wrap it in markdown code fences. Output raw YAML only.

Rules:
- Only include skills that are grounded in the session transcript or discovery notes — no generic or hallucinated skills
- Each skill must map to a specific pain point, workflow, or automation opportunity that was explicitly discussed or clearly implied
- Use lowercase-hyphenated names (e.g. "dispatch-optimization", "invoice-reconciliation")
- Priority is "high" if the problem was described as urgent or frequent, "medium" if notable, "low" if mentioned in passing
- Category should be one of: operations, finance, hiring, fleet, communication, knowledge, scheduling, reporting, compliance, other
- Inputs and outputs should be concrete data types or artifacts mentioned in the session
- Integrations should name specific systems mentioned (e.g. "ServiceTitan", "QuickBooks") or describe the type if no system was named (e.g. "GPS tracking system")
- Notes should capture the human context: who does it manually today, what the pain is, any caveats
- If no clear automation skills can be extracted, output: "skills: []"
- Do not include more than 10 skills — focus on the highest-value opportunities

Output this exact YAML structure:

skills:
  - name: "kebab-case-skill-name"
    description: "One sentence: what this skill does and the problem it solves"
    category: "operations | finance | hiring | fleet | communication | knowledge | scheduling | reporting | compliance | other"
    priority: "high | medium | low"
    inputs:
      - "specific input data or artifact"
    outputs:
      - "specific output data or artifact"
    integrations:
      - "system or service name"
    notes: "Human context: who does this today, what the manual pain is, any caveats"""

CONTEXT_GENERATION_PROMPT = """You are a business analyst at BonEcho. Given a discovery session transcript and structured discovery notes, produce a structured business context document.

Rules:
- Output valid markdown only. No JSON, no code fences wrapping the whole output.
- Only include information explicitly mentioned in the transcript or discovery notes.
- Do not hallucinate names, tools, roles, or facts not grounded in the source.
- Mark items that are uncertain or lightly implied with "(unconfirmed)".
- Omit any section or sub-item if no relevant information was captured — do not include placeholder text.
- Use concise bullet points. Avoid prose paragraphs.

Output the following sections (omit any section with no content):

# Business Context: {company_name}

## Background
- Industry and type of business
- Founding or ownership context if mentioned
- Approximate headcount or team size
- Service areas or geography

## Ideal Customer Profile (ICP)
- Who they serve (customer type, verticals)
- Typical deal or contract type if mentioned
- Volume or scale indicators

## Pain Points
- Specific operational challenges described
- Technology or tooling gaps
- Process bottlenecks and their business impact

## Current Tools & Systems
- Software platforms in active use
- Manual processes that have no tooling yet
- Known integrations between systems

## Key People
- Names and roles mentioned during the session
- Decision-makers identified
- Technical or operational contacts

## Notes
- Follow-up items or open questions
- Caveats, uncertainties, or items to validate
- Additional context that does not fit above sections"""


class ClaudeServiceError(Exception):
    """Raised when Claude API calls fail after retries."""


class ClaudeService:
    """Handles Claude API calls for active discovery conversation and session output generation."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def generate_response(
        self,
        conversation_history: list[dict[str, str]],
        discovery_context: dict[str, Any],
        elapsed_minutes: float,
    ) -> DiscoveryResponse:
        """Generate a structured discovery response from Claude.

        Args:
            conversation_history: Windowed list of message dicts with role/content.
            discovery_context: Current accumulated discovery sections and coverage.
            elapsed_minutes: Elapsed session time for pacing/phase injection.

        Returns:
            Parsed DiscoveryResponse.
        """
        context_block = json.dumps(
            {
                "elapsed_minutes": round(elapsed_minutes, 1),
                "discovery_sections": discovery_context.get("discovery_sections", {}),
                "coverage": discovery_context.get("coverage", {}),
            },
            indent=2,
        )
        system_content = DISCOVERY_SYSTEM_PROMPT.replace(
            "{discovery_context}", context_block
        )

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await self._client.messages.create(
                    model=settings.ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=system_content,
                    messages=conversation_history,
                )
                raw_text = response.content[0].text
                return self._parse_response(raw_text)

            except anthropic.RateLimitError as exc:
                last_exc = exc
                logger.warning("claude_rate_limited", attempt=attempt)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))

            except anthropic.InternalServerError as exc:
                last_exc = exc
                logger.warning("claude_server_error", attempt=attempt, error=str(exc))
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))

            except anthropic.APIConnectionError as exc:
                last_exc = exc
                logger.warning("claude_connection_error", attempt=attempt, error=str(exc))
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))

            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                logger.error("claude_auth_error", error=str(exc))
                raise ClaudeServiceError(f"Claude authentication failed: {exc}") from exc

            except anthropic.BadRequestError as exc:
                logger.error("claude_bad_request", error=str(exc))
                raise ClaudeServiceError(f"Claude bad request: {exc}") from exc

        raise ClaudeServiceError(
            f"Claude API failed after {_MAX_RETRIES} retries: {last_exc}"
        )

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

    async def generate_claude_md(
        self,
        transcript: list[dict[str, str]],
        discovery_sections: dict[str, Any],
    ) -> str:
        """Generate a CLAUDE.md file from a session transcript and discovery notes.

        Args:
            transcript: List of message dicts with 'role' and 'content' keys.
            discovery_sections: Dict of discovery section name -> accumulated notes.

        Returns:
            Valid markdown string ready to be written as CLAUDE.md.

        Raises:
            ClaudeServiceError: If all retries are exhausted.
        """
        transcript_text = self._format_transcript(transcript)
        discovery_text = self._format_discovery_sections(discovery_sections)

        user_message = (
            f"## Session Transcript\n\n{transcript_text}\n\n"
            f"## Discovery Notes\n\n{discovery_text}\n\n"
            "Generate the CLAUDE.md file now."
        )

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("claude_md_generation_attempt", attempt=attempt)
                response = await self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=CLAUDE_MD_GENERATION_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                content = response.content[0].text.strip()
                logger.info("claude_md_generation_success", attempt=attempt)
                return content
            except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "claude_md_generation_retry",
                        attempt=attempt,
                        wait=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)

        raise ClaudeServiceError(
            f"CLAUDE.md generation failed after {_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    async def generate_skills(
        self,
        transcript: list[dict[str, str]],
        discovery_sections: dict[str, Any],
    ) -> str:
        """Generate a skills YAML file from a session transcript and discovery notes.

        Args:
            transcript: List of message dicts with 'role' and 'content' keys.
            discovery_sections: Dict of discovery section name -> accumulated notes.

        Returns:
            Valid YAML string matching Agent Factory skills schema.

        Raises:
            ClaudeServiceError: If all retries are exhausted.
        """
        transcript_text = self._format_transcript(transcript)
        discovery_text = self._format_discovery_sections(discovery_sections)

        user_message = (
            f"## Session Transcript\n\n{transcript_text}\n\n"
            f"## Discovery Notes\n\n{discovery_text}\n\n"
            "Generate the skills YAML file now."
        )

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("claude_skills_generation_attempt", attempt=attempt)
                response = await self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=SKILLS_GENERATION_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                content = response.content[0].text.strip()
                content = re.sub(r"^```(?:yaml)?\s*", "", content, flags=re.MULTILINE)
                content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
                content = content.strip()
                self._validate_yaml(content)
                logger.info("claude_skills_generation_success", attempt=attempt)
                return content
            except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "claude_skills_generation_retry",
                        attempt=attempt,
                        wait=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
            except ValueError as exc:
                last_error = exc
                logger.warning(
                    "claude_skills_yaml_invalid",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt >= _MAX_RETRIES:
                    break

        raise ClaudeServiceError(
            f"Skills generation failed after {_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    async def generate_context(
        self,
        transcript: list[dict[str, str]],
        discovery_sections: dict[str, Any],
        company_name: str = "",
    ) -> str:
        """Generate a business context markdown file from a session transcript.

        Args:
            transcript: List of message dicts with 'role' and 'content' keys.
            discovery_sections: Dict of discovery section name -> accumulated notes.
            company_name: Client company name to substitute in the document header.

        Returns:
            Valid markdown string for the context document.

        Raises:
            ClaudeServiceError: If all retries are exhausted.
        """
        transcript_text = self._format_transcript(transcript)
        discovery_text = self._format_discovery_sections(discovery_sections)

        prompt = CONTEXT_GENERATION_PROMPT.replace(
            "{company_name}", company_name or "Unknown Company"
        )

        user_message = (
            f"## Session Transcript\n\n{transcript_text}\n\n"
            f"## Discovery Notes\n\n{discovery_text}\n\n"
            "Generate the business context document now."
        )

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("claude_context_generation_attempt", attempt=attempt)
                response = await self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=3000,
                    system=prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                content = response.content[0].text.strip()
                logger.info("claude_context_generation_success", attempt=attempt)
                return content
            except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "claude_context_generation_retry",
                        attempt=attempt,
                        wait=wait,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)

        raise ClaudeServiceError(
            f"Context generation failed after {_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    def _parse_response(self, raw_text: str) -> DiscoveryResponse:
        """Parse Claude's raw text into a DiscoveryResponse.

        Handles multiple non-conforming formats Claude may produce:
        - discovery_updates as dict-of-dicts instead of list
        - coverage as strings instead of integers
        - invalid session_phase values
        """
        cleaned = self._extract_json_string(raw_text)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("claude_response_json_failed", raw_text=raw_text[:300])
            spoken = self._extract_spoken_response(raw_text)
            return DiscoveryResponse(
                spoken_response=spoken,
                discovery_updates=[],
                internal_notes="[Parse error — JSON decode failed]",
            )

        # ── Normalize discovery_updates ────────────────────────────
        raw_updates = data.get("discovery_updates", [])
        updates: list[DiscoveryUpdate] = []

        if isinstance(raw_updates, dict):
            # Claude returned { "business_overview": { ... }, ... } instead of a list
            for area, content_val in raw_updates.items():
                if area not in ("business_overview", "dispatch_capacity",
                                "hiring_seasonality", "fleet_equipment",
                                "knowledge_transfer"):
                    continue
                if isinstance(content_val, dict):
                    # Flatten dict values into a text block
                    lines = [f"{k}: {v}" for k, v in content_val.items() if v]
                    text = "\n".join(lines)
                elif isinstance(content_val, str):
                    text = content_val
                else:
                    continue
                if text.strip():
                    updates.append(DiscoveryUpdate(area=area, content=text, action="append"))
        elif isinstance(raw_updates, list):
            for u in raw_updates:
                if isinstance(u, dict):
                    try:
                        updates.append(DiscoveryUpdate(**u))
                    except Exception:
                        # Try to salvage: might have area+content but wrong format
                        area = u.get("area", "")
                        content = u.get("content", "")
                        if area and content:
                            updates.append(DiscoveryUpdate(
                                area=area, content=str(content), action=u.get("action", "append")
                            ))

        data["discovery_updates"] = updates

        # ── Normalize coverage ─────────────────────────────────────
        raw_cov = data.get("coverage", {})
        if isinstance(raw_cov, dict):
            clean_cov = {}
            for k, v in raw_cov.items():
                if isinstance(v, int):
                    clean_cov[k] = max(0, min(100, v))
                elif isinstance(v, (float, str)):
                    # Try parsing; if string like "partial", map to a default
                    try:
                        clean_cov[k] = max(0, min(100, int(float(str(v)))))
                    except (ValueError, TypeError):
                        pass  # skip non-numeric values, keep existing coverage
            try:
                data["coverage"] = Coverage(**clean_cov)
            except Exception:
                data["coverage"] = Coverage()
        else:
            data["coverage"] = Coverage()

        # ── Normalize session_phase ────────────────────────────────
        raw_phase = data.get("session_phase", "opening")
        try:
            data["session_phase"] = SessionPhase(raw_phase)
        except ValueError:
            # Map common invalid values
            phase_str = str(raw_phase).lower()
            if "clos" in phase_str or "end" in phase_str or "conclu" in phase_str:
                data["session_phase"] = SessionPhase.CLOSING
            elif "wrap" in phase_str:
                data["session_phase"] = SessionPhase.WRAPPING_UP
            elif "discover" in phase_str or "accel" in phase_str:
                data["session_phase"] = SessionPhase.DISCOVERY
            else:
                data["session_phase"] = SessionPhase.OPENING

        try:
            return DiscoveryResponse(**data)
        except Exception as exc:
            logger.warning(
                "claude_response_construct_fallback",
                error=str(exc),
                raw_text=raw_text[:300],
            )
            spoken = data.get("spoken_response", "") or self._extract_spoken_response(raw_text)
            return DiscoveryResponse(
                spoken_response=spoken,
                discovery_updates=updates,
                coverage=data.get("coverage", Coverage()),
                internal_notes=data.get("internal_notes"),
                session_phase=data.get("session_phase", SessionPhase.OPENING),
            )

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
    def _extract_json_string(raw_text: str) -> str:
        """Strip markdown code block wrappers if present."""
        text = raw_text.strip()
        if text.startswith("```json"):
            text = text[len("```json"):].strip()
        elif text.startswith("```"):
            text = text[len("```"):].strip()
        if text.endswith("```"):
            text = text[:-len("```")].strip()
        return text

    @staticmethod
    def _extract_spoken_response(raw_text: str) -> str:
        """Best-effort extraction of spoken_response from malformed JSON."""
        key = '"spoken_response"'
        idx = raw_text.find(key)
        if idx == -1:
            return "I'm sorry, could you say that again? I had a brief technical issue."

        colon_idx = raw_text.find(":", idx + len(key))
        if colon_idx == -1:
            return "I'm sorry, could you say that again? I had a brief technical issue."

        quote_start = raw_text.find('"', colon_idx + 1)
        if quote_start == -1:
            return "I'm sorry, could you say that again? I had a brief technical issue."

        pos = quote_start + 1
        while pos < len(raw_text):
            if raw_text[pos] == "\\" and pos + 1 < len(raw_text):
                pos += 2
                continue
            if raw_text[pos] == '"':
                return raw_text[quote_start + 1:pos]
            pos += 1

        return "I'm sorry, could you say that again? I had a brief technical issue."

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Extract and parse JSON from a Claude response, stripping any markdown fences."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        return json.loads(cleaned.strip())

    @staticmethod
    def _validate_yaml(content: str) -> None:
        """Validate that content is parseable YAML with a top-level 'skills' key."""
        try:
            import yaml  # noqa: PLC0415
        except ImportError:
            return
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc
        if not isinstance(parsed, dict) or "skills" not in parsed:
            raise ValueError(
                "Skills YAML must have a top-level 'skills' key. "
                f"Got: {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed)}"
            )
