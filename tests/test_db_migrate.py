"""Tests for migration discovery and migrate() guard rails."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from convo.db import Database, _discover_migrations
from convo.intake.positions import assign_positions
from convo.read.inspect import _MSG_SELECT

_BASE_TABLES = {
    "messages",
    "schema_migrations",
    "sessions",
    "source_files",
    "tool_calls",
    "tool_results",
    "guard_decisions",
}
_FTS_TABLES = {
    "messages_fts",
    "tool_calls_fts",
    "tool_results_fts",
    "guard_decisions_fts",
}


def _write(p: Path, body: str = "-- noop\n") -> None:
    p.write_text(body, encoding="utf-8")


def test_discover_returns_sorted_contiguous(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "0002_b.sql")
    _write(tmp_path / "0003_c.sql")
    result = _discover_migrations(pkg_root=tmp_path)
    versions = [r[0] for r in result]
    assert versions == [1, 2, 3]
    filenames = [r[1] for r in result]
    assert filenames == ["0001_a.sql", "0002_b.sql", "0003_c.sql"]


def test_discover_raises_on_gap(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "0003_c.sql")
    with pytest.raises(RuntimeError, match="Non-contiguous"):
        _discover_migrations(pkg_root=tmp_path)


def test_discover_raises_on_duplicate(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "0001_b.sql")
    with pytest.raises(RuntimeError, match="Duplicate migration version 1"):
        _discover_migrations(pkg_root=tmp_path)


def test_discover_ignores_non_matching_files(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "README.md", "not a migration")
    _write(tmp_path / "__init__.py", "")
    result = _discover_migrations(pkg_root=tmp_path)
    assert [r[0] for r in result] == [1]


def test_open_refuses_db_from_future_version(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    raw.executescript("PRAGMA user_version = 99;")
    raw.close()

    with pytest.raises(RuntimeError) as excinfo:
        Database(db_path).open()
    msg = str(excinfo.value)
    assert "99" in msg
    assert "refusing to downgrade" in msg

    raw = sqlite3.connect(db_path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 99
    raw.close()


def test_fresh_db_has_current_user_version_and_expected_tables(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6

        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        names = {r[0] for r in rows}
        assert names >= _BASE_TABLES
        assert names >= _FTS_TABLES

        migrations = db.conn.execute(
            "SELECT version, filename, applied_at FROM schema_migrations ORDER BY version",
        ).fetchall()
        assert len(migrations) == 6
        assert migrations[0][0] == 1
        assert migrations[0][1] == "0001_init.sql"
        assert migrations[1][0] == 2
        assert migrations[1][1] == "0002_guard_decisions.sql"
        assert migrations[2][0] == 3
        assert migrations[2][1] == "0003_guard_decisions_repair.sql"
        assert migrations[3][0] == 4
        assert migrations[3][1] == "0004_guard_decisions_lockdown.sql"
        assert migrations[4][0] == 5
        assert migrations[4][1] == "0005_fk_child_indexes.sql"
        assert migrations[5][0] == 6
        assert migrations[5][1] == "0006_message_positions.sql"
        # Parses as ISO-8601:
        for migration in migrations:
            datetime.fromisoformat(migration[2])


def test_upgrade_v1_to_v2_preserves_data(db_path: Path) -> None:
    """A v1-only DB populated with messages should upgrade to v2 cleanly:
    0002 adds guard_decisions tables but must not touch the v1 tables."""
    # Build a v1-only DB by applying just 0001_init.sql.
    raw = sqlite3.connect(db_path)
    init_sql = (
        Path(__file__).resolve().parent.parent / "src/convo/migrations/0001_init.sql"
    ).read_text(
        encoding="utf-8",
    )
    raw.executescript("BEGIN EXCLUSIVE;\n" + init_sql)
    raw.execute(
        "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
        (1, "0001_init.sql", datetime.now(tz=UTC).isoformat()),
    )
    raw.execute("PRAGMA user_version = 1")
    raw.execute("COMMIT")
    # Insert some real data so we can verify it survives the upgrade.
    raw.execute(
        "INSERT INTO source_files"
        "(path, kind, size, mtime_ns, last_indexed_at) "
        "VALUES (?, 'transcript', ?, ?, ?)",
        ("/tmp/keep.jsonl", 100, 0, "2026-05-08T00:00:00Z"),
    )
    raw.commit()
    raw.close()

    # Now open via Database — that triggers migrate() and applies 0002 through 0006.
    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6

        # 0002 added the guard_decisions tables.
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        names = {r[0] for r in rows}
        assert "guard_decisions" in names
        assert "guard_decisions_fts" in names

        # v1 row survived.
        kept = db.conn.execute(
            "SELECT path FROM source_files WHERE path = ?",
            ("/tmp/keep.jsonl",),
        ).fetchone()
        assert kept is not None
        assert kept[0] == "/tmp/keep.jsonl"

        # Both migration rows now in schema_migrations.
        migrations = [
            (r[0], r[1])
            for r in db.conn.execute(
                "SELECT version, filename FROM schema_migrations ORDER BY version",
            )
        ]
        assert migrations == [
            (1, "0001_init.sql"),
            (2, "0002_guard_decisions.sql"),
            (3, "0003_guard_decisions_repair.sql"),
            (4, "0004_guard_decisions_lockdown.sql"),
            (5, "0005_fk_child_indexes.sql"),
            (6, "0006_message_positions.sql"),
        ]


def test_reopen_does_not_rerun_migration(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        first = db.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1",
        ).fetchone()[0]

    with Database(db_path) as db:
        assert db.conn is not None
        rows = db.conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert rows[0] == 6
        second = db.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1",
        ).fetchone()[0]
        assert first == second


def test_repair_heals_v2_db_missing_guard_decisions(db_path: Path) -> None:
    """Repro of the production drift fixed by 0003.

    A DB reached user_version=2 with 0001 + 0002 recorded as applied, but
    guard_decisions never got created (an early widen-only 0002 was applied,
    then the file was edited to add the table). ``migrate()`` skips
    already-recorded versions, so the table stayed missing and
    ``convo index-guard`` failed with "no such table: guard_decisions". The
    0003 forward repair must create the table on next open.
    """
    raw = sqlite3.connect(db_path)
    init_sql = (
        Path(__file__).resolve().parent.parent / "src/convo/migrations/0001_init.sql"
    ).read_text(encoding="utf-8")
    raw.executescript("BEGIN EXCLUSIVE;\n" + init_sql)
    now = datetime.now(tz=UTC).isoformat()
    # Record 0001 and 0002 as applied and bump to v2, but never create the
    # guard_decisions table -- exactly the stuck state on disk.
    raw.execute(
        "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
        (1, "0001_init.sql", now),
    )
    raw.execute(
        "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
        (2, "0002_guard_decisions.sql", now),
    )
    raw.execute("PRAGMA user_version = 2")
    raw.execute("COMMIT")
    stuck_tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "guard_decisions" not in stuck_tables  # precondition: genuinely stuck
    raw.close()

    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
        names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "guard_decisions" in names
        assert "guard_decisions_fts" in names
        versions = [
            r[0] for r in db.conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert versions == [1, 2, 3, 4, 5, 6]


def test_upgrade_v3_to_v4_accepts_lockdown_and_keeps_rows(db_path: Path) -> None:
    """0004 widens guard_decisions.mode for guard's lockdown mode.

    Builds a v3 DB from the real 0001-0003 files with one decision row, then
    opens it so migrate() applies 0004. The old row and its FTS entry survive,
    and a ``mode='lockdown'`` row (rejected by the v2/v3 CHECK) now inserts.
    """
    migrations_dir = Path(__file__).resolve().parent.parent / "src/convo/migrations"
    raw = sqlite3.connect(db_path)
    now = datetime.now(tz=UTC).isoformat()
    for version, name in enumerate(
        ("0001_init.sql", "0002_guard_decisions.sql", "0003_guard_decisions_repair.sql"),
        start=1,
    ):
        sql = (migrations_dir / name).read_text(encoding="utf-8")
        raw.executescript("BEGIN EXCLUSIVE;\n" + sql)
        raw.execute(
            "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
            (version, name, now),
        )
        raw.execute(f"PRAGMA user_version = {version}")
        raw.execute("COMMIT")
    raw.execute(
        "INSERT INTO source_files(id, path, kind, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/tmp/guard.jsonl', 'guard_decisions', 1, 1, ?)",
        (now,),
    )
    decision = (
        "INSERT INTO guard_decisions(source_file_id, line_no, schema_version, mode, timestamp, "
        "hook_id, event, decision, reason, session_id, raw_json) "
        "VALUES (1, ?, 1, ?, ?, 'guard.bash_command_validator', 'PreToolUse', 'deny', ?, "
        "'s1', '{}')"
    )
    raw.execute(decision, (1, "enforce", now, "pre-upgrade decision"))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(decision, (2, "lockdown", now, "rejected before 0004"))
    raw.commit()
    raw.close()

    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
        db.conn.execute(decision, (2, "lockdown", now, "lockdown decision"))
        db.conn.commit()
        modes = [r[0] for r in db.conn.execute("SELECT mode FROM guard_decisions ORDER BY line_no")]
        assert modes == ["enforce", "lockdown"]
        for term in ("upgrade", "lockdown"):
            hits = db.conn.execute(
                "SELECT count(*) FROM guard_decisions_fts WHERE guard_decisions_fts MATCH ?",
                (term,),
            ).fetchone()[0]
            assert hits == 1, term


def test_upgrade_v5_to_v6_backfills_positions_without_touching_messages(db_path: Path) -> None:
    """0006 numbers every existing message per session without writing `messages`.

    Builds a v5 DB from the real 0001-0005 files. A column backfill would
    UPDATE (rewrite) every message row; a logging trigger proves no message
    row is updated, and the FTS index still answers.
    """
    migrations_dir = Path(__file__).resolve().parent.parent / "src/convo/migrations"
    raw = sqlite3.connect(db_path)
    now = datetime.now(tz=UTC).isoformat()
    names = (
        "0001_init.sql",
        "0002_guard_decisions.sql",
        "0003_guard_decisions_repair.sql",
        "0004_guard_decisions_lockdown.sql",
        "0005_fk_child_indexes.sql",
    )
    for version, name in enumerate(names, start=1):
        raw.executescript("BEGIN EXCLUSIVE;\n" + (migrations_dir / name).read_text("utf-8"))
        raw.execute(
            "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
            (version, name, now),
        )
        raw.execute(f"PRAGMA user_version = {version}")
        raw.execute("COMMIT")
    raw.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/tmp/s.jsonl', 1, 1, ?)",
        (now,),
    )
    rows = [
        # (id, session, seq, timestamp): s1 has a NULL timestamp, a seq tie,
        # and a seq tie between a NULL and a non-NULL timestamp. The NULL one
        # (a9) sorts first, so NULLS LAST or an id-only order would misnumber it.
        ("a2", "s1", 0, "2026-10-01T10:00:00Z"),
        ("a1", "s1", 1, None),
        ("a4", "s1", 2, "2026-10-01T10:00:02Z"),
        ("a3", "s1", 2, "2026-10-01T10:00:02Z"),
        ("a5", "s1", 3, "2026-10-01T10:00:03Z"),
        ("a9", "s1", 3, None),
        ("b1", "s2", 5, "2026-10-01T11:00:00Z"),
        ("b2", "s2", 7, "2026-10-01T11:00:01Z"),
    ]
    for sid in ("s1", "s2"):
        raw.execute("INSERT INTO sessions(id, source_file_id) VALUES (?, 1)", (sid,))
    raw.executemany(
        "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
        "VALUES (?, ?, 'user', ?, ?, 'wombat ' || ?, '{}')",
        [(*r, r[0]) for r in rows],
    )
    raw.execute("CREATE TABLE touched(id TEXT)")
    raw.execute(
        "CREATE TRIGGER log_update AFTER UPDATE ON messages "
        "BEGIN INSERT INTO touched VALUES (new.id); END"
    )
    raw.commit()
    raw.close()

    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
        positions = "SELECT message_id, position FROM message_positions"
        stored = dict(db.conn.execute(positions))
        assert stored == {"a2": 1, "a1": 2, "a3": 3, "a4": 4, "a9": 5, "a5": 6, "b1": 1, "b2": 2}
        # The backfill numbers each session in the order inspect lists it...
        for sid in ("s1", "s2"):
            listed = [r[0] for r in db.conn.execute(_MSG_SELECT, (sid,))]
            assert [stored[mid] for mid in listed] == list(range(1, len(listed) + 1)), sid
        # ...and exactly as intake numbers the same rows.
        db.conn.execute("DELETE FROM message_positions")
        assign_positions(db.conn, {"s1": (0, 2), "s2": (5, 2)})
        assert dict(db.conn.execute(positions)) == stored
        assert db.conn.execute("SELECT count(*) FROM touched").fetchone()[0] == 0
        fts = "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'wombat'"
        assert db.conn.execute(fts).fetchone()[0] == len(rows)
        db.conn.execute("DELETE FROM sessions WHERE id = 's2'")
        assert db.conn.execute("SELECT count(*) FROM message_positions").fetchone()[0] == 6
