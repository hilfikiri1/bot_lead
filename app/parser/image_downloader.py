"""Image download and processing utilities."""

from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.utils.filenames import safe_filename

logger = get_logger(__name__)

MIN_IMAGE_SIDE = 300
MAX_GALLERY_IMAGES = 8
MAX_DETAIL_IMAGES = 4

SKIP_URL_PATTERNS = re.compile(
    r"(icon|logo|avatar|qr|sprite|blank|placeholder|loading|spacer|1x1|emoji)",
    re.I,
)


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_valid_image_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if SKIP_URL_PATTERNS.search(url):
        return False
    return True


def _normalize_image_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def process_image(data: bytes, output_path: Path, *, max_dimension: int = 1600) -> bool:
    try:
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            width, height = img.size
            if min(width, height) < MIN_IMAGE_SIDE:
                return False
            if max(width, height) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(output_path, format="JPEG", quality=88, optimize=True)
            return True
    except Exception as exc:
        logger.warning("image_process_failed", error=str(exc))
        return False


class ImageDownloader:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._seen_hashes: set[str] = set()
        self._total_bytes = 0

    async def download_images(
        self,
        gallery_urls: list[str],
        detail_urls: list[str],
        output_dir: Path,
        *,
        referer: str,
        cookies: dict[str, str] | None = None,
    ) -> list[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        local_paths: list[str] = []

        gallery_selected = [_normalize_image_url(u) for u in gallery_urls if _is_valid_image_url(u)]
        detail_selected = [_normalize_image_url(u) for u in detail_urls if _is_valid_image_url(u)]

        gallery_selected = gallery_selected[:MAX_GALLERY_IMAGES]
        detail_selected = detail_selected[:MAX_DETAIL_IMAGES]

        remaining = self.settings.max_images - len(gallery_selected)
        if remaining > 0:
            detail_selected = detail_selected[:remaining]
        else:
            detail_selected = []

        all_urls = gallery_selected + detail_selected

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
        }

        cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
        if cookie_header:
            headers["Cookie"] = cookie_header

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for idx, url in enumerate(all_urls):
                if len(local_paths) >= self.settings.max_images:
                    break
                if self._total_bytes >= self.settings.max_total_download_bytes:
                    logger.warning("download_limit_reached")
                    break

                try:
                    path = await self._download_one(client, url, output_dir, idx, headers)
                    if path:
                        local_paths.append(str(path))
                except Exception as exc:
                    logger.warning("image_download_failed", url=url, error=str(exc))
                    continue

        return local_paths

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        output_dir: Path,
        idx: int,
        headers: dict[str, str],
    ) -> Path | None:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "image" not in content_type and not any(
            ext in urlparse(url).path.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")
        ):
            return None

        data = response.content
        if len(data) > self.settings.max_image_size_bytes:
            logger.warning("image_too_large", url=url, size=len(data))
            return None

        content_hash = _content_hash(data)
        if content_hash in self._seen_hashes:
            return None
        self._seen_hashes.add(content_hash)
        self._total_bytes += len(data)

        filename = safe_filename(f"img_{idx:02d}.jpg")
        output_path = output_dir / filename

        if process_image(data, output_path):
            return output_path
        return None
