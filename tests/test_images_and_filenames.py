from pathlib import Path

from PIL import Image

from app.utils.filenames import catalog_pdf_filename, safe_filename
from app.utils.images import deduplicate_files


def test_safe_filename_normalizes_title():
    assert safe_filename("Babrik / товар: 500 мл !!!") == "Babrik_товар_500_мл"
    assert catalog_pdf_filename("Тестовый товар").startswith("Babrik_Solutions_Тестовый_товар_")


def test_deduplicate_images_removes_duplicates(tmp_path: Path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    Image.new("RGB", (400, 400), "white").save(first)
    second.write_bytes(first.read_bytes())
    result = deduplicate_files([first, second])
    assert result == [first]
    assert not second.exists()
