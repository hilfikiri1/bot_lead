from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_image_to_jpeg(source_path: Path, target_path: Path, max_side: int = 2200) -> Path:
    with Image.open(source_path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(target_path, format="JPEG", quality=90, optimize=True)
    return target_path
