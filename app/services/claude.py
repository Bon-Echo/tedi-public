"""Claude AI service for active discovery conversation."""

import asyncio
import json
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
You are Tedi, an active discovery interviewer for Bon Echo. Lead a structured 15-20 minute
discovery session covering: business_overview, dispatch_capacity, hiring_seasonality,
fleet_equipment, knowledge_transfer. Respond in JSON with spoken_response, discovery_updates,
coverage, internal_notes, session_phase, elapsed_minutes.
"""


class ClaudeServiceError(Exception):
    """Raised when a Claude API call fails."""


class ClaudeService:
    """Client for Claude AI discovery conversation responses."""

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

    def _parse_response(self, raw_text: str) -> DiscoveryResponse:
        """Parse Claude's raw text into a DiscoveryResponse.

        Handles JSON wrapped in markdown code blocks or partially malformed output.
        """
        cleaned = self._extract_json_string(raw_text)

        try:
            data = json.loads(cleaned)
            # Coerce nested models
            if "discovery_updates" in data and isinstance(data["discovery_updates"], list):
                data["discovery_updates"] = [
                    DiscoveryUpdate(**u) if isinstance(u, dict) else u
                    for u in data["discovery_updates"]
                ]
            if "coverage" in data and isinstance(data["coverage"], dict):
                data["coverage"] = Coverage(**data["coverage"])
            if "session_phase" in data:
                data["session_phase"] = SessionPhase(data["session_phase"])
            return DiscoveryResponse(**data)

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "claude_response_parse_fallback",
                error=str(exc),
                raw_text=raw_text[:500],
            )
            spoken = self._extract_spoken_response(raw_text)
            return DiscoveryResponse(
                spoken_response=spoken,
                discovery_updates=[],
                internal_notes="[Parse error — raw response could not be fully parsed]",
            )

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
