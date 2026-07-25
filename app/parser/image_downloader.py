from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP"}
OUTPUT_FORMAT = "JPEG"
OUTPUT_SUFFIX = ".jpg"
MAX_LONG_SIDE = 2400


def _is_tiny(width: int, height: int) -> bool:
    return min(width, height) < settings.min_image_side


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_filename(url: str, index: int) -> str:
    """Generate a filesystem-safe filename based on URL."""
    parsed = urlparse(url)
    basename = Path(parsed.path).stem
    # Keep only alphanumeric, dash, underscore; truncate
    safe = re.sub(r"[^\w\-]", "_", basename)[:40]
    return f"{index:03d}_{safe}{OUTPUT_SUFFIX}"


def _process_image(data: bytes) -> Optional[bytes]:
    """
    Load, fix EXIF orientation, convert to RGB JPEG, resize if too large.
    Returns processed JPEG bytes or None on failure.
    """
    try:
        img = Image.open(io.BytesIO(data))

        if img.format not in SUPPORTED_FORMATS and img.format is not None:
            logger.debug("unsupported_image_format", fmt=img.format)
            return None

        # Fix EXIF orientation
        img = ImageOps.exif_transpose(img)

        # Convert to RGB (handles RGBA, palette, etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Filter tiny images
        if _is_tiny(img.width, img.height):
            logger.debug("image_too_small", w=img.width, h=img.height)
            return None

        # Downscale if too large
        if max(img.width, img.height) > MAX_LONG_SIDE:
            img.thumbnail((MAX_LONG_SIDE, MAX_LONG_SIDE), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format=OUTPUT_FORMAT, quality=85, optimize=True)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("image_process_error", error=str(exc))
        return None


async def download_images(
    gallery_urls: list[str],
    detail_urls: list[str],
    save_dir: Path,
    cookies: Optional[dict] = None,
    referer: str = "https://detail.1688.com/",
) -> list[str]:
    """
    Download, process, deduplicate, and save product images.
    Returns list of local file paths (relative to save_dir parent or absolute).
    """
    save_dir.mkdir(parents=True, exist_ok=True)

    gallery_urls = gallery_urls[: settings.max_gallery_images]
    detail_urls = detail_urls[: settings.max_detail_images]

    all_urls = [(u, "gallery") for u in gallery_urls] + [
        (u, "detail") for u in detail_urls
    ]

    seen_hashes: set[str] = set()
    saved_paths: list[str] = []
    total_bytes = 0
    index = 0

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    async with httpx.AsyncClient(
        timeout=20.0,
        headers=headers,
        cookies=cookies or {},
        follow_redirects=True,
    ) as client:
        for url, kind in all_urls:
            if len(saved_paths) >= settings.max_images:
                break
            if total_bytes >= settings.max_total_download_bytes:
                logger.warning("total_download_limit_reached")
                break

            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.debug("image_download_failed", url=url[:80], status=resp.status_code)
                    continue

                raw = resp.content
                if len(raw) > settings.max_image_size_bytes:
                    logger.debug("image_too_large", url=url[:80], size=len(raw))
                    continue

                # Deduplicate by SHA-256
                digest = _sha256_of_bytes(raw)
                if digest in seen_hashes:
                    logger.debug("duplicate_image_skipped", url=url[:80])
                    continue

                processed = _process_image(raw)
                if processed is None:
                    continue

                seen_hashes.add(digest)
                total_bytes += len(processed)

                filename = _safe_filename(url, index)
                filepath = save_dir / filename
                filepath.write_bytes(processed)
                saved_paths.append(str(filepath))
                index += 1

                logger.debug("image_saved", path=str(filepath), kind=kind)

            except Exception as exc:
                logger.warning("image_download_error", url=url[:80], error=str(exc))
                continue

    logger.info(
        "images_downloaded",
        count=len(saved_paths),
        total_mb=round(total_bytes / 1024 / 1024, 2),
    )
    return saved_paths
