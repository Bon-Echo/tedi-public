"""ElevenLabs text-to-speech service."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ElevenLabsServiceError(Exception):
    """Raised when an ElevenLabs API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ElevenLabsService:
    """Client for ElevenLabs text-to-speech conversion."""

    def __init__(self) -> None:
        self.api_key: str = settings.ELEVENLABS_API_KEY
        self.voice_id: str = settings.ELEVENLABS_VOICE_ID
        self.model_id: str = settings.ELEVENLABS_MODEL_ID
        self.base_url: str = settings.ELEVENLABS_API_BASE_URL.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

    def _tts_body(self, text: str) -> dict[str, Any]:
        return {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.75,
                "style": 0.45,
            },
        }

    async def text_to_speech(self, text: str) -> bytes:
        """Convert text to speech audio bytes.

        Uses the ElevenLabs REST API to synthesize speech and returns
        MP3 audio suitable for playback.

        Args:
            text: The text to convert to speech.

        Returns:
            MP3 audio bytes.

        Raises:
            ElevenLabsServiceError: If the API call fails after retries.
        """
        url = (
            f"{self.base_url}/text-to-speech/{self.voice_id}"
            "?output_format=mp3_44100_128"
        )
        body = self._tts_body(text)
        headers = self._headers()

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(url, headers=headers, json=body)

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    logger.warning(
                        "elevenlabs_retryable_error",
                        status_code=response.status_code,
                        attempt=attempt,
                    )
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                        continue
                    raise ElevenLabsServiceError(
                        f"ElevenLabs API error: {response.status_code}",
                        status_code=response.status_code,
                    )

                if response.status_code >= 400:
                    raise ElevenLabsServiceError(
                        f"ElevenLabs API error: {response.status_code} - {response.text}",
                        status_code=response.status_code,
                    )

                logger.debug(
                    "elevenlabs_tts_complete",
                    text_length=len(text),
                    audio_bytes=len(response.content),
                )
                return response.content

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("elevenlabs_timeout", attempt=attempt)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

        raise ElevenLabsServiceError(
            f"ElevenLabs TTS failed after {_MAX_RETRIES} retries: {last_exc}"
        )

    async def text_to_speech_streamed(self, text: str) -> AsyncGenerator[bytes, None]:
        """Convert text to speech with streaming for lower latency.

        Uses the ElevenLabs streaming endpoint to yield audio chunks
        as they are generated, reducing time-to-first-byte.

        Args:
            text: The text to convert to speech.

        Yields:
            Audio data chunks in MP3 format.

        Raises:
            ElevenLabsServiceError: If the API call fails after retries.
        """
        url = (
            f"{self.base_url}/text-to-speech/{self.voice_id}/stream"
            "?output_format=mp3_44100_128"
        )
        body = self._tts_body(text)
        headers = self._headers()

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    async with client.stream(
                        "POST", url, headers=headers, json=body
                    ) as response:
                        if response.status_code in _RETRYABLE_STATUS_CODES:
                            await response.aread()
                            logger.warning(
                                "elevenlabs_stream_retryable_error",
                                status_code=response.status_code,
                                attempt=attempt,
                            )
                            if attempt < _MAX_RETRIES:
                                await asyncio.sleep(
                                    _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                                )
                                continue
                            raise ElevenLabsServiceError(
                                f"ElevenLabs streaming error: {response.status_code}",
                                status_code=response.status_code,
                            )

                        if response.status_code >= 400:
                            body_text = (await response.aread()).decode(errors="replace")
                            raise ElevenLabsServiceError(
                                f"ElevenLabs streaming error: {response.status_code} - {body_text}",
                                status_code=response.status_code,
                            )

                        logger.debug(
                            "elevenlabs_tts_stream_started",
                            text_length=len(text),
                        )

                        total_bytes = 0
                        async for chunk in response.aiter_bytes(chunk_size=4096):
                            total_bytes += len(chunk)
                            yield chunk

                        logger.debug(
                            "elevenlabs_tts_stream_complete",
                            total_bytes=total_bytes,
                        )
                        return

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("elevenlabs_stream_timeout", attempt=attempt)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue

        raise ElevenLabsServiceError(
            f"ElevenLabs streaming TTS failed after {_MAX_RETRIES} retries: {last_exc}"
        )
