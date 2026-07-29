"""
storage_service.py
Handles saving audio files to local disk or S3-compatible storage.
"""

from __future__ import annotations

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


async def save_project_file(
    content: bytes,
    filename: str,
    mime_type: str = "application/octet-stream",
) -> str:
    """Persist a Telegram document/photo until Drive upload is confirmed."""
    safe_name = Path(filename).name or f"{uuid.uuid4()}.bin"
    if settings.storage_backend == "s3":
        key = f"project_files/{uuid.uuid4()}_{safe_name}"
        try:
            s3 = _s3_client()
            s3.put_object(
                Bucket=settings.s3_bucket_name,
                Key=key,
                Body=content,
                ContentType=mime_type or "application/octet-stream",
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("S3 project file upload failed: %s", exc)
            raise
        return f"s3://{settings.s3_bucket_name}/{key}"
    storage_dir = Path(settings.local_storage_path) / "project_files"
    storage_dir.mkdir(parents=True, exist_ok=True)
    file_path = storage_dir / f"{uuid.uuid4()}_{safe_name}"
    file_path.write_bytes(content)
    logger.info("Saved project file locally: %s", file_path)
    return str(file_path)


def read_project_file_bytes(storage_path: str) -> bytes:
    if storage_path.startswith("s3://"):
        bucket, separator, key = storage_path[5:].partition("/")
        if not separator or not bucket or not key:
            raise ValueError("Некорректный S3 путь проектного файла.")
        try:
            response = _s3_client().get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            logger.error("S3 project file read failed: %s", exc)
            raise
    path = Path(storage_path)
    if not path.is_file():
        raise FileNotFoundError(f"Файл не найден: {storage_path}")
    return path.read_bytes()


def delete_project_file(storage_path: str) -> None:
    """Remove the temporary project file after confirmation or cancellation."""
    if not storage_path:
        return
    if storage_path.startswith("s3://"):
        bucket, separator, key = storage_path[5:].partition("/")
        if not separator or not bucket or not key:
            raise ValueError("Некорректный S3 путь проектного файла.")
        try:
            _s3_client().delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.error("S3 project file cleanup failed: %s", exc)
            raise
        return
    path = Path(storage_path)
    if path.is_file():
        path.unlink()


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
