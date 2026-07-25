"""Image processing helpers built on Pillow.

Responsibilities:
* validate that raw bytes are a supported image (JPEG/PNG/WebP);
* reject too-small images (icons, avatars, QR codes are usually small);
* fix EXIF orientation;
* convert to RGB JPEG, downscale large images while keeping aspect ratio;
* compute content hashes for de-duplication (SHA-256 + perceptual hash).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}
MAX_DIMENSION = 1600  # longest side after downscaling
JPEG_QUALITY = 85


@dataclass(slots=True)
class ProcessedImage:
    """Result of processing a raw downloaded image."""

    data: bytes
    width: int
    height: int
    sha256: str
    phash: str


def _phash(image: Image.Image, hash_size: int = 8) -> str:
    """Compute a simple average perceptual hash (aHash) as a hex string."""
    small = image.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if px >= avg else "0" for px in pixels)
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Return the Hamming distance between two equal-length hex hashes."""
    if len(hash_a) != len(hash_b):
        return max(len(hash_a), len(hash_b)) * 4
    xor = int(hash_a, 16) ^ int(hash_b, 16)
    return bin(xor).count("1")


def process_image(
    raw: bytes,
    *,
    min_side: int = 300,
    max_dimension: int = MAX_DIMENSION,
) -> ProcessedImage | None:
    """Validate, normalize and re-encode a raw image.

    Returns ``None`` when the payload is not a valid/large-enough image so the
    caller can simply skip it and continue with the rest.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img_format = (img.format or "").upper()
            if img_format not in SUPPORTED_FORMATS:
                return None

            img = ImageOps.exif_transpose(img)  # fix orientation
            width, height = img.size

            if min(width, height) < min_side:
                return None

            img = img.convert("RGB")

            longest = max(width, height)
            if longest > max_dimension:
                scale = max_dimension / float(longest)
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            phash = _phash(img)

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            data = buffer.getvalue()
            final_width, final_height = img.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    sha256 = hashlib.sha256(data).hexdigest()
    return ProcessedImage(
        data=data,
        width=final_width,
        height=final_height,
        sha256=sha256,
        phash=phash,
    )
