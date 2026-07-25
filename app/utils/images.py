"""Image deduplication utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dedupe_image_hashes(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        h = file_content_hash(path)
        if h not in seen:
            seen.add(h)
            result.append(path)
    return result
