"""Service-isolated unit tests for ``Database.restore_snapshot``.

These exercise call sequencing and exact arguments (copy → chmod → replace,
plus partial-failure cleanup) via ``mocker.patch.object`` with
``autospec=True``. End-to-end behavior is covered by
``tests/test_db_snapshots.py``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from convo.db import Database


def test_restore_raises_when_source_missing_before_any_modification(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Missing snapshot → ValueError; no copy/chmod/replace is attempted."""
    # Track that none of the destructive primitives are called.
    copyfile_mock = mocker.patch.object(shutil, "copyfile", autospec=True)
    replace_mock = mocker.patch.object(os, "replace", autospec=True)
    chmod_mock = mocker.patch.object(Path, "chmod", autospec=True)

    seed_source_file(db, path="/keep.jsonl")
    missing = tmp_path / "does-not-exist.db"

    with pytest.raises(ValueError, match="does not exist"):
        db.restore_snapshot(missing)

    copyfile_mock.assert_not_called()
    replace_mock.assert_not_called()
    chmod_mock.assert_not_called()


def test_restore_call_sequence_copy_chmod_replace(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Same-filesystem happy path: copyfile → chmod(0o600) → os.replace, in order.

    The staging file lives next to the live DB, gets its perms tightened
    BEFORE being renamed into place, and then ``os.replace`` performs the
    atomic swap.
    """
    seed_source_file(db, path="/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    live_path = db.path
    staging = live_path.with_name(f"{live_path.name}.restoring")

    # Patch the three primitives. Wire copyfile/os.replace to actually create
    # the file at the destination so the post-restore ``self.open()`` call
    # finds a valid SQLite DB.
    def fake_copyfile(src: str, dst: str) -> str:
        Path(dst).write_bytes(Path(src).read_bytes())
        return dst

    def fake_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        Path(dst).write_bytes(Path(src).read_bytes())
        Path(src).unlink()

    copyfile_mock = mocker.patch.object(
        shutil,
        "copyfile",
        autospec=True,
        side_effect=fake_copyfile,
    )
    replace_mock = mocker.patch.object(
        os,
        "replace",
        autospec=True,
        side_effect=fake_replace,
    )
    chmod_mock = mocker.patch.object(Path, "chmod", autospec=True)

    # Single parent recorder for ordered mock_calls inspection.
    parent = MagicMock()
    parent.attach_mock(copyfile_mock, "copyfile")
    parent.attach_mock(chmod_mock, "chmod")
    parent.attach_mock(replace_mock, "replace")

    db.restore_snapshot(snap)

    # copyfile: src → staging
    copyfile_call = copyfile_mock.call_args_list[0]
    assert Path(copyfile_call.args[0]) == snap
    assert Path(copyfile_call.args[1]) == staging

    # chmod: at least one call against the staging file with mode 0o600,
    # before any os.replace fires.
    staging_chmod_calls = [
        c for c in chmod_mock.call_args_list if Path(c.args[0]) == staging and c.args[1] == 0o600
    ]
    assert staging_chmod_calls, chmod_mock.call_args_list

    # os.replace: staging → live path
    replace_call = replace_mock.call_args_list[0]
    assert Path(replace_call.args[0]) == staging
    assert Path(replace_call.args[1]) == live_path

    # Ordering across the three: filter parent.mock_calls to the events we
    # care about and assert the sequence.
    relevant: list[str] = []
    for name, args, _kwargs in parent.mock_calls:
        if name == "copyfile":
            relevant.append("copyfile")
        elif name == "chmod" and Path(args[0]) == staging and args[1] == 0o600:
            relevant.append("chmod")
        elif name == "replace":
            relevant.append("replace")
    # The first three relevant events must be copyfile → chmod → replace.
    assert relevant[:3] == ["copyfile", "chmod", "replace"], relevant


def test_restore_chmod_lands_before_replace(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Spy ``os.replace`` to assert the staging perms are ALREADY 0o600 when
    the atomic swap happens. If chmod were reordered after replace, the live
    DB would briefly be world-readable.
    """
    seed_source_file(db, path="/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    staging = db.path.with_name(f"{db.path.name}.restoring")

    real_replace = os.replace
    observed: dict[str, int] = {}

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        observed["staging_mode"] = Path(src).stat().st_mode & 0o777 if Path(src).exists() else -1
        observed["staging_path_matches"] = int(Path(src) == staging)
        real_replace(src, dst)

    mocker.patch.object(os, "replace", autospec=True, side_effect=spy_replace)

    db.restore_snapshot(snap)

    assert observed["staging_path_matches"] == 1
    assert observed["staging_mode"] == 0o600, f"got 0o{observed['staging_mode']:o}"


def test_restore_unlinks_staging_when_replace_fails(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """If ``os.replace`` raises, the ``.restoring`` staging file is cleaned up.

    The atomic-replace contract is: nothing about ``self.path`` changes
    until ``os.replace`` succeeds. When it raises, the only side effect
    visible on disk is the staging file — and that must be unlinked.
    """
    seed_source_file(db, path="/keep.jsonl")
    snap = db.backup_snapshot(tmp_path)

    live_path = db.path
    staging = live_path.with_name(f"{live_path.name}.restoring")

    mocker.patch.object(
        os,
        "replace",
        autospec=True,
        side_effect=OSError("EXDEV simulated cross-device link"),
    )

    with pytest.raises(OSError, match="EXDEV"):
        db.restore_snapshot(snap)

    # Staging must have been cleaned up.
    assert not staging.exists(), "staging file leaked after failed restore"
    # Live DB file still exists at its original path (no swap occurred).
    assert live_path.exists()


def test_restore_cleans_up_staging_when_chmod_fails(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Failure during ``staging.chmod`` (between copy and replace) must still
    unlink the staging file via the ``except BaseException`` cleanup arm.
    """
    seed_source_file(db, path="/keep.jsonl")
    snap = db.backup_snapshot(tmp_path)

    live_path = db.path
    staging = live_path.with_name(f"{live_path.name}.restoring")

    real_chmod = Path.chmod
    _chmod_err = "simulated chmod failure"

    def selective_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == staging:
            raise PermissionError(_chmod_err)
        real_chmod(self, mode, follow_symlinks=follow_symlinks)

    mocker.patch.object(Path, "chmod", autospec=True, side_effect=selective_chmod)

    with pytest.raises(PermissionError, match=_chmod_err):
        db.restore_snapshot(snap)

    assert not staging.exists(), "staging file leaked after failed chmod"


def test_restore_cross_filesystem_propagates_replace_error(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """``restore_snapshot`` does not pre-detect cross-filesystem moves; it
    relies on ``os.replace`` (atomic, same-FS only) raising ``OSError`` with
    ``errno=EXDEV``. Verify that error propagates and staging is cleaned.

    SECURITY.md documents same-filesystem-only behavior; this is the
    detection mechanism.
    """
    seed_source_file(db, path="/keep.jsonl")
    snap = db.backup_snapshot(tmp_path)
    staging = db.path.with_name(f"{db.path.name}.restoring")

    exdev = OSError("[Errno 18] Invalid cross-device link")
    exdev.errno = 18  # EXDEV
    mocker.patch.object(os, "replace", autospec=True, side_effect=exdev)

    with pytest.raises(OSError, match="cross-device") as exc_info:
        db.restore_snapshot(snap)

    assert exc_info.value.errno == 18
    assert not staging.exists()


def test_restore_does_not_consume_snapshot_file(
    db: Database,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """The snapshot source must be COPIED (shutil.copyfile), never moved.

    Asserts the call shape: ``shutil.copyfile(snap, staging)`` — guarantees
    the user's snapshot file is preserved on disk.
    """
    copyfile_mock = mocker.patch.object(
        shutil,
        "copyfile",
        autospec=True,
        wraps=shutil.copyfile,
    )

    seed_source_file(db, path="/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    staging = db.path.with_name(f"{db.path.name}.restoring")

    db.restore_snapshot(snap)

    copyfile_mock.assert_called_once()
    src_arg, dst_arg = copyfile_mock.call_args.args
    assert Path(src_arg) == snap
    assert Path(dst_arg) == staging
    # And the snapshot file is still on disk afterwards.
    assert snap.exists()
