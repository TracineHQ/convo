"""File signature primitives for idempotent intake."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_CHUNK_SIZE = 8 * 1024


def compute_file_signature(path: Path) -> tuple[bytes, int, int]:
    """Return `(sha256_bytes, size, mtime_ns)` for `path`.

    The hash is computed by streaming the file in 8 KiB chunks so multi-MB
    JSONL transcripts do not need to fit in memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    stat = path.stat()
    return (digest.digest(), stat.st_size, stat.st_mtime_ns)
