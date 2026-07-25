"""Image processing + de-duplication tests."""

from __future__ import annotations

import io

from PIL import Image

from app.utils.images import hamming_distance, process_image


def _make_image(size: tuple[int, int], color: tuple[int, int, int], fmt: str = "PNG") -> bytes:
    img = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    img.save(buffer, format=fmt)
    return buffer.getvalue()


def test_process_valid_image() -> None:
    raw = _make_image((600, 600), (200, 120, 40))
    result = process_image(raw, min_side=300)
    assert result is not None
    assert result.width == 600 and result.height == 600
    # Re-encoded as JPEG.
    assert result.data[:2] == b"\xff\xd8"
    assert len(result.sha256) == 64


def test_rejects_small_image() -> None:
    raw = _make_image((100, 100), (10, 10, 10))
    assert process_image(raw, min_side=300) is None


def test_rejects_non_image() -> None:
    assert process_image(b"not-an-image", min_side=10) is None


def test_downscales_large_image() -> None:
    raw = _make_image((3000, 1500), (50, 90, 130))
    result = process_image(raw, min_side=300, max_dimension=1600)
    assert result is not None
    assert max(result.width, result.height) == 1600


def test_duplicate_detection_by_sha() -> None:
    raw = _make_image((500, 500), (120, 200, 90))
    first = process_image(raw, min_side=300)
    second = process_image(raw, min_side=300)
    assert first is not None and second is not None
    assert first.sha256 == second.sha256


def test_phash_similarity() -> None:
    a = process_image(_make_image((500, 500), (100, 100, 100)), min_side=300)
    b = process_image(_make_image((500, 500), (102, 100, 100)), min_side=300)
    assert a is not None and b is not None
    # Nearly identical flat images -> small Hamming distance.
    assert hamming_distance(a.phash, b.phash) <= 5
