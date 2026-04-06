"""Claude AI service for real-time conversation and TDD generation."""

import asyncio
import json
from typing import Any

import anthropic
import structlog

from app.config import settings
from app.schemas import ClaudeResponse

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0

SYSTEM_PROMPT = """You are Tedi, a senior technical discovery specialist at BonEcho. You are sitting in on a live client call, actively listening and taking notes for a Technical Design Document (TDD).

Your behavior:
- You are a PASSIVE LISTENER by default. You silently observe the conversation and take structured notes.
- You only speak when someone addresses you directly by name (e.g. "Tedi, do you have any questions?" or "Hey Tedi, can you introduce yourself?").
- The conversation history shows all speakers as [Speaker Name]: message. You have full context of everything said.
- When addressed, respond naturally and concisely — then go back to listening.

You are only called upon to speak when someone addresses you. When you do speak, you may:
- Introduce yourself briefly ("Hi, I'm Tedi — I'm taking notes on the technical requirements as we go.")
- Answer questions about what you've captured so far
- Ask clarifying or probing questions about requirements, architecture, integrations, etc.
- Summarize what you've heard

You MUST respond with a JSON object containing exactly these fields:

{
  "spoken_response": "What you say out loud. Keep it conversational, 1-3 sentences max.",
  "tdd_updates": [
    {
      "section": "one of: project_overview, stakeholders, current_state, requirements, architecture, data_model, integrations, security, infrastructure, open_questions",
      "content": "The information to add to this TDD section",
      "action": "append or replace"
    }
  ],
  "internal_notes": "Optional notes to yourself about what to explore next",
  "should_leave": false
}

should_leave: Set to true ONLY when someone explicitly tells you to leave, hang up, or that the call is over. Say a brief goodbye in spoken_response when leaving.

Rules:
1. spoken_response must be natural speech — no markdown, no bullet points
2. Keep spoken_response SHORT — this is a real-time voice conversation
3. Update tdd_updates with any relevant information from the conversation, even from turns where you weren't addressed
4. When asked "do you have any questions?", pick the most important gap in your TDD notes and ask about it
5. Don't repeat back everything you've heard — be concise and targeted
6. Never make up or assume technical details — always ask
7. Be warm and professional but not overbearing — you're a helpful presence, not the main speaker

TDD sections to populate as you listen:
1. Project overview — what are they building and why?
2. Stakeholders — who are the users, admins, decision-makers?
3. Current state — what exists today? What are they replacing?
4. Requirements — functional and non-functional requirements
5. Architecture — preferred tech stack, hosting, scalability needs
6. Data model — what data entities exist, relationships, volumes
7. Integrations — third-party services, APIs, data feeds
8. Security — auth, authorization, compliance, data sensitivity
9. Infrastructure — deployment, environments, CI/CD preferences
10. Open questions — anything unclear that needs follow-up"""

POST_CALL_PROMPT = """You are Tedi, a technical documentation specialist. Given the following transcript and accumulated TDD notes from a technical discovery call, produce a complete, well-structured Technical Design Document.

Format each section with clear headers and professional language. Fill in gaps with reasonable inferences marked as [INFERRED]. Flag anything that needs follow-up as an open question.

Respond with a JSON object with these fields:
{
  "project_name": "string",
  "project_overview": "string",
  "stakeholders": [{"name": "string", "role": "string", "responsibilities": "string"}],
  "current_state": "string",
  "requirements": [{"id": "REQ-001", "type": "functional|non-functional", "description": "string", "priority": "high|medium|low"}],
  "architecture": "string",
  "data_model": "string",
  "integrations": [{"name": "string", "type": "string", "description": "string"}],
  "security": "string",
  "infrastructure": "string",
  "open_questions": ["string"]
}"""


GATE_PROMPT = """You are a conversation monitor. You are watching a live meeting where an AI assistant named "Tedi" is silently taking notes.

Given the last few lines of conversation, decide: is someone requesting Tedi to speak?

Answer YES if:
- Someone says Tedi's name and asks or directs something at Tedi
- Someone refers to "the note-taker", "the AI", "the assistant", "our agent" and expects a response
- Someone asks the room a question that clearly includes Tedi (e.g. "does anyone have questions?" directed at Tedi)
- Someone tells Tedi to leave, hang up, or that the call is over

Answer NO if:
- People are talking to each other
- Tedi is not referenced or addressed
- Someone just mentions the name "Tedi" in passing without expecting a response

Respond with ONLY "YES" or "NO". Nothing else."""


class ClaudeServiceError(Exception):
    """Raised when a Claude API call fails."""


class ClaudeService:
    """Client for Claude AI structured conversation responses."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def should_respond(self, recent_lines: list[str]) -> bool:
        """Use Haiku to quickly decide if Tedi is being addressed."""
        combined = "\n".join(line for line in recent_lines if line.strip())
        if not combined.strip():
            return False

        try:
            response = await self._client.messages.create(
                model=settings.ANTHROPIC_GATE_MODEL,
                max_tokens=8,
                system=GATE_PROMPT,
                messages=[{"role": "user", "content": combined}],
            )
            answer = response.content[0].text.strip().upper()
            return answer == "YES"
        except Exception:
            logger.exception("gate_check_failed")
            return False

    async def generate_response(
        self,
        conversation_history: list[dict[str, str]],
        tdd_context: dict[str, Any],
    ) -> ClaudeResponse:
        """Generate a structured response from Claude during a live call."""
        system_content = SYSTEM_PROMPT
        if tdd_context:
            system_content += (
                "\n\n--- CURRENT TDD STATE ---\n"
                + json.dumps(tdd_context, indent=2)
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
                    continue

            except anthropic.InternalServerError as exc:
                last_exc = exc
                logger.warning("claude_server_error", attempt=attempt, error=str(exc))
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

            except anthropic.APIConnectionError as exc:
                last_exc = exc
                logger.warning("claude_connection_error", attempt=attempt, error=str(exc))
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                logger.error("claude_auth_error", error=str(exc))
                raise ClaudeServiceError(f"Claude authentication failed: {exc}") from exc

            except anthropic.BadRequestError as exc:
                logger.error("claude_bad_request", error=str(exc))
                raise ClaudeServiceError(f"Claude bad request: {exc}") from exc

        raise ClaudeServiceError(
            f"Claude API failed after {_MAX_RETRIES} retries: {last_exc}"
        )

    async def generate_final_tdd(
        self,
        transcript: list[dict[str, Any]],
        tdd_notes: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a complete TDD document from the full call transcript and notes."""
        user_content = (
            "## Transcript\n"
            + json.dumps(transcript, indent=2)
            + "\n\n## Accumulated TDD Notes\n"
            + json.dumps(tdd_notes, indent=2)
        )

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await self._client.messages.create(
                    model=settings.ANTHROPIC_MODEL,
                    max_tokens=4096,
                    system=POST_CALL_PROMPT,
                    messages=[{"role": "user", "content": user_content}],
                )
                raw_text = response.content[0].text
                return self._parse_json(raw_text)

            except anthropic.RateLimitError as exc:
                last_exc = exc
                logger.warning("claude_tdd_rate_limited", attempt=attempt)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

            except anthropic.InternalServerError as exc:
                last_exc = exc
                logger.warning("claude_tdd_server_error", attempt=attempt)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

            except anthropic.APIConnectionError as exc:
                last_exc = exc
                logger.warning("claude_tdd_connection_error", attempt=attempt)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                logger.error("claude_tdd_auth_error", error=str(exc))
                raise ClaudeServiceError(f"Claude auth failed: {exc}") from exc

            except anthropic.BadRequestError as exc:
                logger.error("claude_tdd_bad_request", error=str(exc))
                raise ClaudeServiceError(f"Claude bad request: {exc}") from exc

        raise ClaudeServiceError(
            f"Claude TDD generation failed after {_MAX_RETRIES} retries: {last_exc}"
        )

    def _parse_response(self, raw_text: str) -> ClaudeResponse:
        """Parse Claude's raw text into a structured ClaudeResponse."""
        cleaned = self._extract_json_string(raw_text)
        try:
            data = json.loads(cleaned)
            return ClaudeResponse(**data)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "claude_response_parse_fallback",
                error=str(exc),
                raw_text=raw_text[:500],
            )
            spoken = self._extract_spoken_response(raw_text)
            return ClaudeResponse(
                spoken_response=spoken,
                tdd_updates=[],
                internal_notes="[Parse error - raw response could not be fully parsed]",
            )

    def _parse_json(self, raw_text: str) -> dict[str, Any]:
        """Parse raw text as JSON, handling markdown code block wrappers."""
        cleaned = self._extract_json_string(raw_text)
        try:
            result: dict[str, Any] = json.loads(cleaned)
            return result
        except json.JSONDecodeError as exc:
            logger.error("claude_json_parse_error", error=str(exc), raw_text=raw_text[:500])
            raise ClaudeServiceError(f"Failed to parse Claude JSON response: {exc}") from exc

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
            return "I'm sorry, could you repeat that? I had a brief technical issue."
        colon_idx = raw_text.find(":", idx + len(key))
        if colon_idx == -1:
            return "I'm sorry, could you repeat that? I had a brief technical issue."
        quote_start = raw_text.find('"', colon_idx + 1)
        if quote_start == -1:
            return "I'm sorry, could you repeat that? I had a brief technical issue."
        pos = quote_start + 1
        while pos < len(raw_text):
            if raw_text[pos] == "\\" and pos + 1 < len(raw_text):
                pos += 2
                continue
            if raw_text[pos] == '"':
                return raw_text[quote_start + 1:pos]
            pos += 1
        return "I'm sorry, could you repeat that? I had a brief technical issue."
