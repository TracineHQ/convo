"""Service-isolated unit tests for ``Database.backup``.

These exercise call sequencing and exact arguments via ``mocker.patch.object``
with ``autospec=True``. End-to-end behavior is covered by
``tests/test_db_backup.py``.
"""

from __future__ import annotations

import sqlite3 as _sql
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call

import pytest

from convo.db import Database

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def _make_db_with_fake_conn(
    mocker: MockerFixture,
    *,
    count: int,
    db_path: Path,
) -> tuple[Database, MagicMock]:
    """Build a ``Database`` with an autospec'd ``sqlite3.Connection``.

    The fake ``execute(...).fetchone()`` returns ``(count,)`` for the
    ``SELECT COUNT(*) FROM source_files`` probe; subsequent execute calls
    (``VACUUM INTO ?``) are recorded but do nothing.
    """
    fake_conn = mocker.create_autospec(_sql.Connection, spec_set=True, instance=True)
    fake_cursor = mocker.create_autospec(_sql.Cursor, spec_set=True, instance=True)
    fake_cursor.fetchone.return_value = (count,)
    fake_conn.execute.return_value = fake_cursor

    db = Database(db_path)
    db.conn = fake_conn
    return db, fake_conn


def test_backup_rejects_empty_db_with_actionable_message(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Empty DB raises with the actionable ``CONVO_DB`` / intake hint."""
    db, fake_conn = _make_db_with_fake_conn(
        mocker,
        count=0,
        db_path=tmp_path / "convo.db",
    )
    dest = tmp_path / "snap.db"

    with pytest.raises(RuntimeError) as exc_info:
        db.backup(dest)

    msg = str(exc_info.value)
    assert "empty convo DB" in msg
    assert "no indexed rows" in msg
    assert "CONVO_DB" in msg
    assert "intake" in msg
    # Only the COUNT(*) probe should have run; no VACUUM INTO attempted.
    assert fake_conn.execute.call_count == 1
    sql = fake_conn.execute.call_args.args[0]
    assert "COUNT(*) FROM source_files" in sql


def test_backup_short_circuits_when_destination_exists(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """When ``dest_path.exists()`` is True, no VACUUM INTO is attempted."""
    db, fake_conn = _make_db_with_fake_conn(
        mocker,
        count=1,
        db_path=tmp_path / "convo.db",
    )
    dest = tmp_path / "already-there.db"

    # Patch Path.exists to always return True (covers both src .path and dest).
    mocker.patch.object(Path, "exists", autospec=True, return_value=True)

    with pytest.raises(FileExistsError, match="Backup destination already exists"):
        db.backup(dest)

    # Only the COUNT(*) probe ran. No "VACUUM INTO" execute was issued.
    sql_calls = [c.args[0] for c in fake_conn.execute.call_args_list]
    assert any("COUNT(*)" in s for s in sql_calls)
    assert not any("VACUUM INTO" in s for s in sql_calls)


def test_backup_invokes_vacuum_into_with_dest_path_param(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Success path: VACUUM INTO is called with the dest path bound as a param."""
    db, fake_conn = _make_db_with_fake_conn(
        mocker,
        count=1,
        db_path=tmp_path / "convo.db",
    )
    dest = tmp_path / "snap.db"

    # Stub Path.chmod so the post-VACUUM permission tighten doesn't blow up
    # on the (non-existent because mocked) dest file.
    chmod_mock = mocker.patch.object(Path, "chmod", autospec=True)

    db.backup(dest)

    # VACUUM INTO must have been issued with the dest path as a bound param.
    vacuum_calls = [c for c in fake_conn.execute.call_args_list if "VACUUM INTO" in c.args[0]]
    assert len(vacuum_calls) == 1
    sql, params = vacuum_calls[0].args
    assert sql == "VACUUM INTO ?"
    assert params == (str(dest),)
    # The 0o600 chmod must have been applied to the dest path post-VACUUM.
    chmod_mock.assert_called_once()
    chmod_self, chmod_mode = chmod_mock.call_args.args
    assert Path(chmod_self) == dest
    assert chmod_mode == 0o600


def test_backup_chmod_fires_after_vacuum_into(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Chmod-to-0o600 must land AFTER the snapshot file is written.

    If the order were reversed there would be a window where the snapshot
    sits at the process umask (often 0o644). Enforce ordering via a single
    parent ``Mock`` whose ``mock_calls`` records both children in sequence.
    """
    db, fake_conn = _make_db_with_fake_conn(
        mocker,
        count=1,
        db_path=tmp_path / "convo.db",
    )
    dest = tmp_path / "snap.db"

    # Single parent recorder; attach both fakes so mock_calls captures order.
    parent = mocker.MagicMock()
    parent.attach_mock(fake_conn.execute, "execute")
    chmod_mock = mocker.patch.object(Path, "chmod", autospec=True)
    parent.attach_mock(chmod_mock, "chmod")

    db.backup(dest)

    # Find the VACUUM INTO call and the chmod call in mock_calls; assert
    # the VACUUM call precedes the chmod call.
    names_in_order = [c[0] for c in parent.mock_calls]
    # Filter to the two events we care about.
    relevant: list[tuple[str, tuple[object, ...]]] = []
    for name, args, _kwargs in parent.mock_calls:
        if (name == "execute" and args and "VACUUM INTO" in args[0]) or name == "chmod":
            relevant.append((name, args))
    assert [n for n, _ in relevant] == ["execute", "chmod"], names_in_order


def test_backup_raises_not_open_when_conn_is_none(tmp_path: Path) -> None:
    """No connection → fail-fast RuntimeError before any filesystem work."""
    db = Database(tmp_path / "convo.db")
    db.conn = None
    with pytest.raises(RuntimeError, match="not open"):
        db.backup(tmp_path / "snap.db")


def test_backup_count_query_uses_fetchone_once(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """The empty-DB guard reads exactly one row from the COUNT cursor."""
    db, fake_conn = _make_db_with_fake_conn(
        mocker,
        count=5,
        db_path=tmp_path / "convo.db",
    )
    mocker.patch.object(Path, "chmod", autospec=True)
    dest = tmp_path / "snap.db"

    db.backup(dest)

    # The cursor returned by execute() is the same fake for every call here,
    # so fetchone is invoked once for the COUNT probe.
    fake_cursor = fake_conn.execute.return_value
    assert fake_cursor.fetchone.call_args_list == [call()]
