"""Regression guard: re-indexing one grown file in a ~100k-message DB stays fast.

When a transcript grows, `convo index` deletes the file's rows and re-inserts
them. `messages.parent_id` and `tool_results.message_id` are `ON DELETE SET
NULL` foreign keys; without an index on those child columns SQLite scans both
tables once per deleted message, so the delete is quadratic. On this shape the
re-index took about 12 minutes before migration 0005 and a few seconds after.
"""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from tests.perf._synth import write_transcript

if TYPE_CHECKING:
    from pathlib import Path

_FILES = 5
_RECORDS_PER_FILE = 20_000
# The quadratic delete this guards against takes about 12 minutes on this
# shape; with the indexes it takes a few seconds. Windows CI runners' disk I/O
# is several times slower and noisier, so they get more headroom.
_BUDGET_SECONDS = 60.0 if sys.platform == "win32" else 15.0


def _sid(i: int) -> str:
    return f"5e551011-0000-4000-8000-{i:012d}"


def _index(db_path: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    rc = main(["--db", str(db_path), "index", "--projects-dir", str(root), "--json"])
    assert rc == 0
    payload: dict[str, object] = json.loads(capsys.readouterr().out)["index"]
    return payload


@pytest.mark.perf
def test_grown_file_reindex_under_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "projects"
    db_path = tmp_path / "convo.db"
    paths = [root / f"slug-{i}" / f"{_sid(i)}.jsonl" for i in range(_FILES)]
    for i, path in enumerate(paths):
        write_transcript(path, _sid(i), start=0, count=_RECORDS_PER_FILE)

    first = _index(db_path, root, capsys)
    assert first["files_indexed"] == _FILES
    rows = first["rows_inserted"]
    assert isinstance(rows, dict)
    assert rows["messages"] == _FILES * _RECORDS_PER_FILE

    # A live session appends two records; only that file changes.
    write_transcript(paths[0], _sid(0), start=_RECORDS_PER_FILE, count=2)

    start = time.perf_counter()
    second = _index(db_path, root, capsys)
    elapsed = time.perf_counter() - start
    print(f"grown-file re-index: {elapsed:.2f}s")

    assert second["files_indexed"] == 1
    assert second["files_skipped"] == _FILES - 1
    assert elapsed < _BUDGET_SECONDS, (
        f"re-indexing one grown {_RECORDS_PER_FILE}-record file took {elapsed:.1f}s "
        f"(budget {_BUDGET_SECONDS:.0f}s); are the FK child-key indexes missing?"
    )
