"""SQLite-backed storage primitives for convo."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3 as _sql
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from types import TracebackType

DEFAULT_DB_PATH: Path = Path.home() / ".claude" / "convo.db"
SNAPSHOT_DIR_NAME: str = "convo-backups"
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
_ERR_DB_UNREADABLE = "DB at {path} is not a usable SQLite file: {reason}"
_ERR_BACKUP_DEST_EXISTS = "Backup destination already exists: {dest}"
_ERR_BACKUP_EMPTY_DB = (
    "Refusing to back up an empty convo DB at {path} (no indexed rows). "
    "Point CONVO_DB / --db at a populated convo database, "
    "or run intake first."
)
_ERR_RESTORE_SRC_MISSING = "Snapshot source does not exist: {src}"
_ERR_RESTORE_BAD_DB = "Snapshot source is not a usable convo DB: {src} ({reason})"
_ERR_RESTORE_FROM_FUTURE = (
    "Snapshot at {src} is from a newer schema version "
    "(snapshot {snapshot_v} > current {current_v}); refusing to restore"
)


def resolve_snapshot_dir(explicit: Path | str | None, db_path: Path) -> Path:
    """Resolve snapshot dir.

    Precedence: explicit arg > $CONVO_BACKUP_DIR > sibling of resolved DB
    (`<db>.parent / convo-backups`).
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("CONVO_BACKUP_DIR")
    if env:
        return Path(env).expanduser()
    return db_path.parent / SNAPSHOT_DIR_NAME


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
        try:
            self.conn.executescript(
                "PRAGMA journal_mode = WAL;"
                "PRAGMA foreign_keys = ON;"
                "PRAGMA synchronous = NORMAL;"
                "PRAGMA temp_store = MEMORY;"
                "PRAGMA busy_timeout = 5000;"
                "PRAGMA mmap_size = 268435456;"
                "PRAGMA cache_size = -64000;",
            )
        except _sql.DatabaseError as exc:
            self.close()
            raise RuntimeError(
                _ERR_DB_UNREADABLE.format(path=self.path, reason=exc),
            ) from exc
        self.conn.row_factory = _sql.Row
        self.migrate()
        return self

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
        if self.conn is None:
            raise RuntimeError(_ERR_NOT_OPEN)
        if self.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0:
            raise RuntimeError(_ERR_BACKUP_EMPTY_DB.format(path=self.path))
        dest_path = Path(dest).expanduser()
        if dest_path.exists():
            raise FileExistsError(_ERR_BACKUP_DEST_EXISTS.format(dest=dest_path))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn.execute("VACUUM INTO ?", (str(dest_path),))
        # VACUUM INTO honors the process umask (often 0o644). Snapshot may
        # contain prompt/response text — tighten to owner-only by default.
        dest_path.chmod(0o600)

    def backup_snapshot(self, snapshot_dir: Path | str | None = None) -> Path:
        target_dir = resolve_snapshot_dir(snapshot_dir, self.path)
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        dest = target_dir / f"convo-{timestamp}.db"
        self.backup(dest)
        return dest

    def restore_snapshot(self, src: Path | str) -> None:
        src_path = Path(src).expanduser()
        if not src_path.exists():
            raise ValueError(_ERR_RESTORE_SRC_MISSING.format(src=src_path))
        try:
            probe = _sql.connect(str(src_path))
            try:
                snapshot_v = probe.execute("PRAGMA user_version").fetchone()[0]
                probe.execute("SELECT version FROM schema_migrations LIMIT 1").fetchall()
            finally:
                probe.close()
        except _sql.DatabaseError as exc:
            raise ValueError(
                _ERR_RESTORE_BAD_DB.format(src=src_path, reason=exc),
            ) from exc
        if snapshot_v > SCHEMA_VERSION:
            raise ValueError(
                _ERR_RESTORE_FROM_FUTURE.format(
                    src=src_path,
                    snapshot_v=snapshot_v,
                    current_v=SCHEMA_VERSION,
                ),
            )
        self.close()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        # Copy snapshot to a sibling tempfile of the live DB, then atomic-replace.
        # Same-FS guarantees the os.replace is atomic and the user's snapshot file
        # is preserved (never moved). Staging file is unlinked on partial failure
        # so a crashed restore never leaves orphan `.restoring` files behind.
        staging = self.path.with_name(f"{self.path.name}.restoring")
        try:
            shutil.copyfile(src_path, staging)
            os.replace(staging, self.path)  # noqa: PTH105 — atomic-replace; tests patch os.replace
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        self.open()

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
