"""SQLite-backed storage primitives for convo."""

from __future__ import annotations

import os
import re
import sqlite3 as _sql
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from types import TracebackType

DEFAULT_DB_PATH: Path = Path.home() / ".claude" / "convo.db"
DEFAULT_SNAPSHOT_DIR: Path = Path.home() / ".claude" / "convo-backups"
SCHEMA_VERSION: int = 1
_MIN_SQLITE: tuple[int, int, int] = (3, 37, 0)
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_ERR_SQLITE_TOO_OLD = (
    "convo requires SQLite >= 3.37.0 (for STRICT tables); this interpreter bundles {found}"
)
_ERR_DB_FROM_FUTURE = (
    "DB at {path} is at user_version {found}, "
    "but this convo only knows up to version {known}; refusing to downgrade"
)
_ERR_MIGRATION_GAP = "Non-contiguous migration versions discovered: {versions}"
_ERR_MIGRATION_DUP = "Duplicate migration version {version} ({first}, {second})"
_ERR_NOT_OPEN = "Database is not open"
_ERR_LEGACY_DB = (
    "Detected a legacy convo DB at {path}. Run `convo migrate-legacy` "
    "to convert it, or pass `--db <other-path>` for a fresh DB."
)


def resolve_db_path(explicit: Path | str | None = None) -> Path:
    """Resolve DB path with precedence: explicit arg > $CONVO_DB > DEFAULT_DB_PATH."""
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("CONVO_DB")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB_PATH


def _discover_migrations(
    pkg_root: Path | None = None,
) -> list[tuple[int, str, str]]:
    """Return [(version, filename, sql_text)] sorted by version, contiguous from 1."""
    if pkg_root is None:
        entries = list(files("convo.migrations").iterdir())
    else:
        entries = list(pkg_root.iterdir())
    found: dict[int, tuple[str, str]] = {}
    for entry in entries:
        match = _MIGRATION_RE.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise RuntimeError(
                _ERR_MIGRATION_DUP.format(
                    version=version,
                    first=found[version][0],
                    second=entry.name,
                ),
            )
        found[version] = (entry.name, entry.read_text(encoding="utf-8"))
    versions = sorted(found)
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(_ERR_MIGRATION_GAP.format(versions=versions))
    return [(v, found[v][0], found[v][1]) for v in versions]


class Database:
    """Owned SQLite connection with migration + backup helpers."""

    path: Path
    conn: _sql.Connection | None

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = resolve_db_path(path)
        self.conn = None

    def open(self) -> Self:
        """Open the connection, apply pragmas, run pending migrations."""
        if _sql.sqlite_version_info < _MIN_SQLITE:
            raise RuntimeError(
                _ERR_SQLITE_TOO_OLD.format(found=_sql.sqlite_version),
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _sql.connect(self.path)
        self.conn.executescript(
            "PRAGMA journal_mode = WAL;"
            "PRAGMA foreign_keys = ON;"
            "PRAGMA synchronous = NORMAL;"
            "PRAGMA temp_store = MEMORY;"
            "PRAGMA busy_timeout = 5000;"
            "PRAGMA mmap_size = 268435456;"
            "PRAGMA cache_size = -64000;",
        )
        self.conn.row_factory = _sql.Row
        self._check_legacy_db()
        self.migrate()
        return self

    def _check_legacy_db(self) -> None:
        if self.conn is None:
            raise RuntimeError(_ERR_NOT_OPEN)
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('conversations', 'schema_migrations')",
        )
        names = {row[0] for row in cur.fetchall()}
        if "conversations" in names and "schema_migrations" not in names:
            raise RuntimeError(_ERR_LEGACY_DB.format(path=self.path))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def migrate(self) -> int:
        if self.conn is None:
            raise RuntimeError(_ERR_NOT_OPEN)
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                _ERR_DB_FROM_FUTURE.format(
                    path=self.path,
                    found=current,
                    known=SCHEMA_VERSION,
                ),
            )
        for version, filename, sql in _discover_migrations():
            if version <= current:
                continue
            applied_at = datetime.now(UTC).isoformat()
            script = (
                "BEGIN EXCLUSIVE;\n"
                f"{sql}\n"
                f"INSERT INTO schema_migrations(version, filename, applied_at) "
                f"VALUES ({int(version)}, '{filename}', '{applied_at}');\n"
                f"PRAGMA user_version = {int(version)};\n"
                "COMMIT;"
            )
            self.conn.executescript(script)
        result: int = self.conn.execute("PRAGMA user_version").fetchone()[0]
        return result

    def backup(self, dest: Path | str) -> None:
        raise NotImplementedError

    def backup_snapshot(self, snapshot_dir: Path | str | None = None) -> Path:
        raise NotImplementedError

    def prune_snapshots(
        self,
        snapshot_dir: Path | str | None = None,
        keep_n: int = 7,
    ) -> list[Path]:
        raise NotImplementedError

    def auto_snapshot(
        self,
        snapshot_dir: Path | str | None = None,
        keep_n: int = 7,
    ) -> Path:
        raise NotImplementedError

    def restore_snapshot(self, src: Path | str) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
