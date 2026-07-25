from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_valid_image_size(width: int, height: int, min_side: int) -> bool:
    return min(width, height) >= min_side


def load_and_fix_image(data: bytes) -> Optional[Image.Image]:
    """Load image bytes and fix EXIF orientation."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        return img
    except Exception:
        return None
