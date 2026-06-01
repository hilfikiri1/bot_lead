"""
storage_service.py
Handles saving audio files to local disk or S3-compatible storage.
"""
from __future__ import annotations

import os
import uuid
import logging
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


async def save_audio(audio_bytes: bytes, extension: str = "ogg") -> str:
    """
    Save audio bytes to configured storage backend.
    Returns a URL / path string.
    """
    filename = f"{uuid.uuid4()}.{extension}"

    if settings.storage_backend == "s3":
        return await _save_to_s3(audio_bytes, filename)
    else:
        return await _save_locally(audio_bytes, filename)


async def _save_locally(audio_bytes: bytes, filename: str) -> str:
    storage_dir = Path(settings.local_storage_path)
    storage_dir.mkdir(parents=True, exist_ok=True)
    file_path = storage_dir / filename
    file_path.write_bytes(audio_bytes)
    logger.info("Saved audio locally: %s", file_path)
    return str(file_path)


async def _save_to_s3(audio_bytes: bytes, filename: str) -> str:
    try:
        s3 = _s3_client()
        key = f"audio/{filename}"
        s3.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=audio_bytes,
            ContentType="audio/ogg",
        )
        url = f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{key}"
        logger.info("Saved audio to S3: %s", url)
        return url
    except (BotoCoreError, ClientError) as e:
        logger.error("S3 upload failed: %s", e)
        raise
