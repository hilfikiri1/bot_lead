"""
transcription_service.py
Sends audio to OpenAI Whisper and returns the transcript.
"""
from __future__ import annotations

import io
import logging
from tenacity import retry, stop_after_attempt, wait_exponential

from openai import AsyncOpenAI
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> tuple[str, str]:
    """
    Transcribe audio bytes using OpenAI Whisper.

    Returns:
        (transcript_text, detected_language)
    """
    logger.info("Sending %d bytes to Whisper", len(audio_bytes))
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename

    response = await _client.audio.transcriptions.create(
        model=settings.openai_whisper_model,
        file=audio_file,
        response_format="verbose_json",  # includes language detection
    )

    transcript = response.text.strip()
    language = getattr(response, "language", "unknown") or "unknown"
    logger.info("Transcription complete. Language=%s, chars=%d", language, len(transcript))
    return transcript, language
