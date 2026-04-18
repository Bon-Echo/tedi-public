"""Provider clients for the STT bakeoff.

Each provider exposes the same async `transcribe(audio_bytes, content_type)`
signature and returns a `TranscriptionResult`. HTTP is done via `httpx` —
already in the repo's dependency pin. The clients are intentionally thin:
they call the documented REST endpoints, read the transcript field, and
measure wall-clock latency for the request. They do **not** stream — the
bakeoff compares batch accuracy on prerecorded audio, so streaming latency
is a separate concern tracked in the memo.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx


def _truncate(text: str, limit: int = 120) -> str:
    """Shorten a body snippet so it fits in a single log/result cell."""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class TranscriptionResult:
    provider: str
    model: str
    transcript: str
    latency_ms: float
    raw: dict | None = None
    error: str | None = None


class Provider(Protocol):
    name: str
    model: str

    async def transcribe(
        self, audio_bytes: bytes, content_type: str
    ) -> TranscriptionResult: ...


class DeepgramNova3(Provider):
    """Deepgram `nova-3` prerecorded transcription.

    Docs: https://developers.deepgram.com/reference/listen-file
    Requires env var `DEEPGRAM_API_KEY`.
    """

    name = "deepgram"
    model = "nova-3"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_s: float = 60.0,
        base_url: str = "https://api.deepgram.com/v1/listen",
        smart_format: bool = True,
        punctuate: bool = True,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        self.timeout_s = timeout_s
        self.base_url = base_url
        self.params = {
            "model": self.model,
            "smart_format": "true" if smart_format else "false",
            "punctuate": "true" if punctuate else "false",
        }

    async def transcribe(
        self, audio_bytes: bytes, content_type: str
    ) -> TranscriptionResult:
        if not self.api_key:
            return TranscriptionResult(
                provider=self.name,
                model=self.model,
                transcript="",
                latency_ms=0.0,
                error="DEEPGRAM_API_KEY not set",
            )
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": content_type,
        }
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                resp = await client.post(
                    self.base_url,
                    params=self.params,
                    headers=headers,
                    content=audio_bytes,
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                return TranscriptionResult(
                    provider=self.name,
                    model=self.model,
                    transcript="",
                    latency_ms=(time.monotonic() - start) * 1000,
                    error=f"{type(e).__name__}: {e}",
                )
            latency_ms = (time.monotonic() - start) * 1000
            body_text = resp.text
            try:
                payload = resp.json()
            except (json.JSONDecodeError, ValueError):
                return TranscriptionResult(
                    provider=self.name,
                    model=self.model,
                    transcript="",
                    latency_ms=latency_ms,
                    error=f"invalid JSON body: {_truncate(body_text)!r}",
                )
        try:
            transcript = (
                payload["results"]["channels"][0]["alternatives"][0]["transcript"]
            )
        except (KeyError, IndexError, TypeError):
            return TranscriptionResult(
                provider=self.name,
                model=self.model,
                transcript="",
                latency_ms=latency_ms,
                raw=payload if isinstance(payload, dict) else None,
                error=f"unexpected response shape: {_truncate(body_text)!r}",
            )
        return TranscriptionResult(
            provider=self.name,
            model=self.model,
            transcript=transcript,
            latency_ms=latency_ms,
            raw=payload,
        )


class OpenAIGpt4oTranscribe(Provider):
    """OpenAI `gpt-4o-transcribe` via the Audio Transcriptions REST API.

    Docs: https://platform.openai.com/docs/api-reference/audio/createTranscription
    Requires env var `OPENAI_API_KEY`.
    """

    name = "openai"
    model = "gpt-4o-transcribe"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_s: float = 120.0,
        base_url: str = "https://api.openai.com/v1/audio/transcriptions",
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout_s = timeout_s
        self.base_url = base_url

    async def transcribe(
        self, audio_bytes: bytes, content_type: str
    ) -> TranscriptionResult:
        if not self.api_key:
            return TranscriptionResult(
                provider=self.name,
                model=self.model,
                transcript="",
                latency_ms=0.0,
                error="OPENAI_API_KEY not set",
            )
        filename = "audio." + _ext_for_content_type(content_type)
        files = {"file": (filename, audio_bytes, content_type)}
        data = {"model": self.model, "response_format": "json"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                resp = await client.post(
                    self.base_url, headers=headers, data=data, files=files
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                return TranscriptionResult(
                    provider=self.name,
                    model=self.model,
                    transcript="",
                    latency_ms=(time.monotonic() - start) * 1000,
                    error=f"{type(e).__name__}: {e}",
                )
            latency_ms = (time.monotonic() - start) * 1000
            body_text = resp.text
            try:
                payload = resp.json()
            except (json.JSONDecodeError, ValueError):
                return TranscriptionResult(
                    provider=self.name,
                    model=self.model,
                    transcript="",
                    latency_ms=latency_ms,
                    error=f"invalid JSON body: {_truncate(body_text)!r}",
                )
        if not isinstance(payload, dict) or "text" not in payload:
            return TranscriptionResult(
                provider=self.name,
                model=self.model,
                transcript="",
                latency_ms=latency_ms,
                raw=payload if isinstance(payload, dict) else None,
                error=f"unexpected response shape: {_truncate(body_text)!r}",
            )
        transcript = payload["text"] or ""
        return TranscriptionResult(
            provider=self.name,
            model=self.model,
            transcript=transcript,
            latency_ms=latency_ms,
            raw=payload,
        )


class SpeechmaticsEnhanced(Provider):
    """Speechmatics batch transcription (optional — add only if accent
    robustness becomes the root-cause hypothesis).

    Docs: https://docs.speechmatics.com/introduction/quickstart
    Requires env var `SPEECHMATICS_API_KEY`. The CLI runner additionally
    requires an explicit `--enable-speechmatics` flag; see
    `run.py::build_providers` and `docs/stt-bakeoff.md` §4.3 for the
    product rationale.
    """

    name = "speechmatics"
    model = "enhanced"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_s: float = 600.0,
        base_url: str = "https://asr.api.speechmatics.com/v2/jobs",
        language: str = "en",
    ) -> None:
        self.api_key = api_key or os.environ.get("SPEECHMATICS_API_KEY", "")
        self.timeout_s = timeout_s
        self.base_url = base_url
        self.language = language

    async def transcribe(
        self, audio_bytes: bytes, content_type: str
    ) -> TranscriptionResult:
        if not self.api_key:
            return TranscriptionResult(
                provider=self.name,
                model=self.model,
                transcript="",
                latency_ms=0.0,
                error="SPEECHMATICS_API_KEY not set",
            )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        config = (
            '{"type":"transcription","transcription_config":'
            f'{{"language":"{self.language}","operating_point":"enhanced"}}}}'
        )
        files = {
            "data_file": ("audio." + _ext_for_content_type(content_type),
                          audio_bytes, content_type),
            "config": (None, config, "application/json"),
        }
        start = time.monotonic()

        def _err(stage: str, message: str) -> TranscriptionResult:
            return TranscriptionResult(
                provider=self.name, model=self.model,
                transcript="", latency_ms=(time.monotonic() - start) * 1000,
                error=f"{stage}: {message}",
            )

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                submit = await client.post(
                    self.base_url, headers=headers, files=files
                )
                submit.raise_for_status()
            except httpx.HTTPError as e:
                return _err("submit", f"{type(e).__name__}: {e}")
            submit_text = submit.text
            try:
                submit_payload = submit.json()
            except (json.JSONDecodeError, ValueError):
                return _err("submit",
                            f"invalid JSON body: {_truncate(submit_text)!r}")
            if not isinstance(submit_payload, dict) or "id" not in submit_payload:
                return _err("submit",
                            f"unexpected response shape: "
                            f"{_truncate(submit_text)!r}")
            job_id = submit_payload["id"]

            # Poll until done. Speechmatics batch typically settles in
            # < 2x audio duration for enhanced.
            transcript_url = f"{self.base_url}/{job_id}/transcript"
            status_url = f"{self.base_url}/{job_id}"
            status = None
            for _ in range(120):
                await asyncio.sleep(2.0)
                try:
                    st = await client.get(status_url, headers=headers)
                    st.raise_for_status()
                except httpx.HTTPError as e:
                    return _err("status", f"{type(e).__name__}: {e}")
                status_text = st.text
                try:
                    status_payload = st.json()
                except (json.JSONDecodeError, ValueError):
                    return _err("status",
                                f"invalid JSON body: {_truncate(status_text)!r}")
                try:
                    status = status_payload["job"]["status"]
                except (KeyError, IndexError, TypeError):
                    return _err("status",
                                f"unexpected response shape: "
                                f"{_truncate(status_text)!r}")
                if status == "done":
                    break
                if status == "rejected":
                    return _err("status", "speechmatics job rejected")
            if status != "done":
                return _err("status", "polling timed out before job completed")

            try:
                tr = await client.get(
                    transcript_url, headers=headers, params={"format": "txt"}
                )
                tr.raise_for_status()
            except httpx.HTTPError as e:
                return _err("transcript", f"{type(e).__name__}: {e}")
            latency_ms = (time.monotonic() - start) * 1000
            transcript = tr.text.strip()
        return TranscriptionResult(
            provider=self.name, model=self.model,
            transcript=transcript, latency_ms=latency_ms,
        )


_EXT_BY_TYPE = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}


def _ext_for_content_type(content_type: str) -> str:
    return _EXT_BY_TYPE.get(content_type.lower().split(";", 1)[0].strip(), "bin")
