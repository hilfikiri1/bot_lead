"""Tests for image downloading utilities — no real HTTP required."""
from __future__ import annotations

import hashlib
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.utils.images import compute_sha256, is_valid_image_size, load_and_fix_image


class TestComputeSha256:
    def test_deterministic(self):
        data = b"hello world"
        assert compute_sha256(data) == compute_sha256(data)
        assert compute_sha256(data) == hashlib.sha256(data).hexdigest()

    def test_different_data_different_hash(self):
        assert compute_sha256(b"a") != compute_sha256(b"b")


class TestIsValidImageSize:
    def test_large_enough(self):
        assert is_valid_image_size(500, 500, 300) is True
        assert is_valid_image_size(1920, 1080, 300) is True

    def test_too_small(self):
        assert is_valid_image_size(200, 500, 300) is False
        assert is_valid_image_size(100, 100, 300) is False

    def test_exactly_at_minimum(self):
        assert is_valid_image_size(300, 300, 300) is True


def _create_test_image_bytes(width: int = 500, height: int = 500) -> bytes:
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestLoadAndFixImage:
    def test_valid_jpeg(self):
        data = _create_test_image_bytes(400, 400)
        img = load_and_fix_image(data)
        assert img is not None
        assert img.width == 400

    def test_invalid_bytes_returns_none(self):
        img = load_and_fix_image(b"not an image")
        assert img is None

    def test_empty_bytes_returns_none(self):
        img = load_and_fix_image(b"")
        assert img is None
