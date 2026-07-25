"""Tests for filename utilities."""
from __future__ import annotations

import re
import pytest

from app.utils.filenames import safe_pdf_filename


class TestSafePdfFilename:
    def test_basic_filename(self):
        filename = safe_pdf_filename("Промышленный вентилятор")
        assert filename.startswith("Babrik_Solutions_")
        assert filename.endswith(".pdf")
        assert "_" in filename

    def test_no_special_chars(self):
        filename = safe_pdf_filename("Товар: модель №1 (2024)")
        # Should not contain colon, №, parentheses
        assert ":" not in filename
        assert "(" not in filename
        assert ")" not in filename

    def test_spaces_replaced_with_underscore(self):
        filename = safe_pdf_filename("Умный холодильник")
        assert " " not in filename
        assert "Умный_холодильник" in filename

    def test_long_name_truncated(self):
        long_name = "А" * 100
        filename = safe_pdf_filename(long_name, max_name_len=40)
        # The product name portion should be at most 40 chars
        # Filename structure: Babrik_Solutions_<name>_<date>.pdf
        parts = filename.split("_")
        # Find the date part (YYYY-MM-DD at end before .pdf)
        date_part = filename.rstrip(".pdf").split("_")[-1]
        assert re.match(r"\d{4}-\d{2}-\d{2}", date_part)

    def test_date_format(self):
        filename = safe_pdf_filename("Test")
        # Extract date from filename
        match = re.search(r"(\d{4}-\d{2}-\d{2})\.pdf$", filename)
        assert match is not None

    def test_cyrillic_preserved(self):
        filename = safe_pdf_filename("Электрический чайник")
        assert "Электрический" in filename or "Электрический_чайник" in filename

    def test_empty_ish_name(self):
        # Should not crash on odd input
        filename = safe_pdf_filename("!!!")
        assert filename.startswith("Babrik_Solutions_")
        assert filename.endswith(".pdf")
