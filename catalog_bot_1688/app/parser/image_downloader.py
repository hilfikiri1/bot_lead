"""Download, validate, de-duplicate and normalize product images.

Rules enforced here:
* correct ``User-Agent`` / ``Referer`` + session cookies when fetching;
* per-image size cap and a global total-download cap;
* skip icons/logos/QR codes/avatars/too-small images (Pillow check);
* convert everything to RGB JPEG, fix EXIF orientation, downscale big images;
* de-duplicate by SHA-256 and perceptual hash;
* safe local filenames;
* on a single failed image, keep going with the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import Settings
from app.logging_config import get_logger
from app.utils.filenames import safe_asset_name
from app.utils.images import hamming_distance, process_image

logger = get_logger(__name__)

PHASH_DUPLICATE_THRESHOLD = 5


@dataclass
class DownloadResult:
    """Local image paths grouped by role."""

    gallery: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    @property
    def all_paths(self) -> list[str]:
        return [*self.gallery, *self.detail]


class ImageDownloader:
    """Fetches and processes product images into a job's temporary folder."""

    def __init__(self, settings: Settings, *, cookies: list[dict] | None = None):
        self._settings = settings
        self._cookies = cookies or []
        self._downloaded_bytes = 0
        self._sha_seen: set[str] = set()
        self._phash_seen: list[str] = []

    def _cookie_header(self) -> str:
        parts = [
            f"{c.get('name')}={c.get('value')}"
            for c in self._cookies
            if c.get("name") and c.get("value")
        ]
        return "; ".join(parts)

    def _headers(self, referer: str) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
        }
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def download(
        self,
        gallery_urls: list[str],
        detail_urls: list[str],
        *,
        referer: str,
        destination: Path,
    ) -> DownloadResult:
        destination.mkdir(parents=True, exist_ok=True)
        result = DownloadResult()

        max_bytes_per_image = self._settings.max_image_size_mb * 1024 * 1024
        max_total_bytes = self._settings.max_total_download_mb * 1024 * 1024

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            headers=self._headers(referer),
        ) as client:
            index = 0
            for url in gallery_urls:
                if len(result.gallery) >= self._settings.max_gallery_images:
                    break
                if self._downloaded_bytes >= max_total_bytes:
                    break
                saved = await self._download_one(
                    client, url, destination, "gallery", index, max_bytes_per_image
                )
                if saved:
                    result.gallery.append(saved)
                    index += 1

            for url in detail_urls:
                if len(result.detail) >= self._settings.max_detail_images:
                    break
                total = len(result.gallery) + len(result.detail)
                if total >= self._settings.max_images:
                    break
                if self._downloaded_bytes >= max_total_bytes:
                    break
                saved = await self._download_one(
                    client, url, destination, "detail", index, max_bytes_per_image
                )
                if saved:
                    result.detail.append(saved)
                    index += 1

        logger.info(
            "Downloaded images",
            gallery=len(result.gallery),
            detail=len(result.detail),
            total_mb=round(self._downloaded_bytes / 1024 / 1024, 2),
        )
        return result

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        prefix: str,
        index: int,
        max_bytes_per_image: int,
    ) -> str | None:
        try:
            response = await client.get(url)
            response.raise_for_status()
            raw = response.content
        except Exception as exc:  # noqa: BLE001 - skip individual failures
            logger.debug("Image download failed", url=url[:120], error=str(exc))
            return None

        if not raw or len(raw) > max_bytes_per_image:
            return None

        processed = process_image(raw, min_side=self._settings.min_image_side_px)
        if processed is None:
            return None

        if processed.sha256 in self._sha_seen:
            return None
        for existing in self._phash_seen:
            if hamming_distance(processed.phash, existing) <= PHASH_DUPLICATE_THRESHOLD:
                return None

        self._sha_seen.add(processed.sha256)
        self._phash_seen.append(processed.phash)
        self._downloaded_bytes += len(processed.data)

        filename = safe_asset_name(prefix, index, "jpg")
        path = destination / filename
        try:
            path.write_bytes(processed.data)
        except OSError as exc:
            logger.warning("Failed to write image", error=str(exc))
            return None

        return str(path)
