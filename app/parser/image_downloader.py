from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog
from PIL import Image, ImageOps

from app.config import Settings
from app.parser.models import ParsedProduct
from app.utils.filenames import safe_filename

logger = structlog.get_logger(__name__)
SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP"}


class ImageDownloader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.max_image_bytes = settings.max_image_size_mb * 1024 * 1024
        self.max_total_bytes = settings.max_total_download_mb * 1024 * 1024

    async def download_product_images(self, product: ParsedProduct, job_dir: Path) -> list[str]:
        image_dir = job_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        urls = (product.gallery_image_urls[: self.settings.max_gallery_images] + product.detail_image_urls[: self.settings.max_detail_images])[: self.settings.max_images]
        paths: list[str] = []
        seen_hashes: set[str] = set()
        total_bytes = 0
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Referer": product.source_url, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            for index, url in enumerate(urls, start=1):
                if total_bytes >= self.max_total_bytes:
                    break
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    content = response.content
                    if len(content) > self.max_image_bytes:
                        logger.warning("image_too_large", url=url, size=len(content))
                        continue
                    total_bytes += len(content)
                    digest = hashlib.sha256(content).hexdigest()
                    if digest in seen_hashes:
                        continue
                    seen_hashes.add(digest)
                    path = self._process_image(content, image_dir, index, url)
                    if path:
                        paths.append(str(path))
                except Exception as exc:
                    logger.warning("image_download_failed", url=url, error=str(exc))
                    continue
        product.local_image_paths = paths
        return paths

    def _process_image(self, content: bytes, image_dir: Path, index: int, url: str) -> Path | None:
        source_name = Path(urlparse(url).path).stem or f"image_{index}"
        output = image_dir / f"{index:02d}_{safe_filename(source_name, max_length=36)}.jpg"
        temp = image_dir / f"raw_{index}"
        temp.write_bytes(content)
        try:
            with Image.open(temp) as image:
                if image.format not in SUPPORTED_FORMATS:
                    return None
                image = ImageOps.exif_transpose(image)
                if min(image.size) < 300:
                    return None
                image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
                rgb = image.convert("RGB")
                rgb.save(output, format="JPEG", quality=88, optimize=True)
                return output
        finally:
            temp.unlink(missing_ok=True)
