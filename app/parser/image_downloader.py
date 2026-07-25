from __future__ import annotations

from pathlib import Path

import httpx
from PIL import Image

from app.config import get_settings
from app.exceptions import ImageDownloadError
from app.utils.images import content_sha256, normalize_image_to_jpeg


async def download_and_prepare_images(
    image_urls: list[str],
    target_dir: Path,
    source_url: str,
) -> list[str]:
    settings = get_settings()
    max_images = settings.max_images
    max_image_bytes = settings.max_image_size_mb * 1024 * 1024
    max_total_bytes = settings.max_total_download_mb * 1024 * 1024
    target_dir.mkdir(parents=True, exist_ok=True)

    prepared: list[str] = []
    seen_hashes: set[str] = set()
    total_downloaded = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": source_url,
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for idx, url in enumerate(image_urls):
            if len(prepared) >= max_images:
                break
            if total_downloaded >= max_total_bytes:
                break
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.content
                if len(data) > max_image_bytes:
                    continue
                total_downloaded += len(data)
                data_hash = content_sha256(data)
                if data_hash in seen_hashes:
                    continue
                seen_hashes.add(data_hash)

                raw_path = target_dir / f"raw_{idx}.img"
                raw_path.write_bytes(data)
                jpeg_path = target_dir / f"img_{idx}.jpg"
                normalize_image_to_jpeg(raw_path, jpeg_path)
                with Image.open(jpeg_path) as img:
                    if min(img.size) < 300:
                        jpeg_path.unlink(missing_ok=True)
                        raw_path.unlink(missing_ok=True)
                        continue
                raw_path.unlink(missing_ok=True)
                prepared.append(str(jpeg_path))
            except Exception:
                continue
    if not prepared:
        raise ImageDownloadError("No images downloaded successfully")
    return prepared
