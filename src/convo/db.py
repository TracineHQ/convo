"""SQLite-backed storage primitives for convo."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3 as _sql
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

DEFAULT_DB_PATH: Path = Path.home() / ".claude" / "convo.db"
SNAPSHOT_DIR_NAME: str = "convo-backups"
SCHEMA_VERSION: int = 5
# Oldest schema the read commands can query. Migrations past it (0005 only
# adds indexes and narrows triggers) are left to writers and `convo migrate`,
# so a read never sits through an index build.
READ_MIN_SCHEMA_VERSION: int = 4
_MIN_SQLITE: tuple[int, int, int] = (3, 37, 0)
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_ERR_SQLITE_TOO_OLD = (
    "convo requires SQLite >= 3.37.0 (for STRICT tables); this interpreter bundles {found}"
)
_ERR_DB_FROM_FUTURE = (
    "DB at {path} is at user_version {found}, "
    "but this convo only knows up to version {known}; refusing to downgrade"
)
_ERR_MIGRATION_REQUIRED = (
    "DB at {path} is at schema version {found}; this needs version {needed} or newer. "
    "Run: convo migrate (add --backup to snapshot it first)"
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
_ERR_BACKUP_NO_SPACE = (
    "Not enough free space for a snapshot in {dir}: it needs up to {needed:.1f} MiB "
    "(the DB plus its WAL) and {free:.1f} MiB is free"
)
_BYTES_PER_MIB = 1024 * 1024
_ERR_RESTORE_SRC_MISSING = "Snapshot source does not exist: {src}"
_ERR_RESTORE_BAD_DB = "Snapshot source is not a usable convo DB: {src} ({reason})"
_ERR_RESTORE_FROM_FUTURE = (
    "Snapshot at {src} is from a newer schema version "
    "(snapshot {snapshot_v} > current {current_v}); refusing to restore"
)


class MigrationRequiredError(RuntimeError):
    """The DB's schema is older than the operation needs; `convo migrate` fixes it."""


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """One migration script applied by `Database.apply_migrations`."""

    version: int
    filename: str
    duration_ms: int


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


def _chmod_sidecars(db_path: Path) -> None:
    """Tighten WAL/SHM sidecars to 0o600 if present."""
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.chmod(0o600)


class Database:
    """Owned SQLite connection with migration + backup helpers."""

    path: Path
    conn: _sql.Connection | None

    def __init__(self, path: Path | str | None = None, *, migrate: bool = True) -> None:
        """Bind to `path` (see `resolve_db_path`); nothing is opened yet.

        `migrate=False` is for readers and the hook: `open()` then leaves
        pending migrations alone (a brand-new, empty DB is still initialized,
        since there is nothing to migrate) and callers check the version with
        `require_schema` or `pending_migrations`.
        """
        self.path = resolve_db_path(path)
        self.conn = None
        self._migrate_on_open = migrate

    def open(self) -> Self:
        """Open the connection, apply pragmas, run pending migrations (see `__init__`)."""
        if _sql.sqlite_version_info < _MIN_SQLITE:
            raise RuntimeError(
                _ERR_SQLITE_TOO_OLD.format(found=_sql.sqlite_version),
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # Pre-create with 0o600 to avoid the TOCTOU window where sqlite
            # would otherwise create the file at the process umask (often 0o644)
            # before we could chmod it. Atomic perms-on-create.
            # Windows: mode arg silently ignored; file inherits parent ACL.
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
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
        # WAL mode creates -wal / -shm sidecars at process umask; tighten them
        # to 0o600 since they hold uncommitted prompt/response data.
        _chmod_sidecars(self.path)
        self.conn.row_factory = _sql.Row
        if self._migrate_on_open or self.schema_version() == 0:
            self.migrate()
        else:
            self._refuse_future_version(self.schema_version())
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def schema_version(self) -> int:
        """Return the DB's `PRAGMA user_version` (the last applied migration)."""
        if self.conn is None:
            raise RuntimeError(_ERR_NOT_OPEN)
        version: int = self.conn.execute("PRAGMA user_version").fetchone()[0]
        return version

    def pending_migrations(self) -> list[tuple[int, str]]:
        """Return `(version, filename)` for each migration not yet applied."""
        current = self.schema_version()
        return [(v, name) for v, name, _ in _discover_migrations() if v > current]

    def require_schema(self, minimum: int) -> None:
        """Raise `MigrationRequiredError` if the DB is older than `minimum`."""
        current = self.schema_version()
        if current < minimum:
            raise MigrationRequiredError(
                _ERR_MIGRATION_REQUIRED.format(path=self.path, found=current, needed=minimum),
            )

    def _refuse_future_version(self, current: int) -> None:
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                _ERR_DB_FROM_FUTURE.format(
                    path=self.path,
                    found=current,
                    known=SCHEMA_VERSION,
                ),
            )

    def apply_migrations(
        self,
        on_start: Callable[[int, str], None] | None = None,
    ) -> list[AppliedMigration]:
        """Apply each pending migration in its own exclusive transaction.

        `on_start(version, filename)` is called before each script runs, so a
        caller can report progress on slow ones.
        """
        current = self.schema_version()
        self._refuse_future_version(current)
        assert self.conn is not None
        applied: list[AppliedMigration] = []
        for version, filename, sql in _discover_migrations():
            if version <= current:
                continue
            if on_start is not None:
                on_start(version, filename)
            started = time.monotonic()
            applied_at = datetime.now(UTC).isoformat()
            self.conn.executescript("BEGIN EXCLUSIVE;\n" + sql)
            self.conn.execute(
                "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
                (int(version), filename, applied_at),
            )
            self.conn.execute(f"PRAGMA user_version = {int(version)}")
            self.conn.execute("COMMIT")
            duration_ms = int((time.monotonic() - started) * 1000)
            applied.append(AppliedMigration(version, filename, duration_ms))
        return applied

    def migrate(self) -> int:
        """Apply pending migrations and return the resulting schema version."""
        self.apply_migrations()
        return self.schema_version()

    def backup(self, dest: Path | str) -> None:
        if self.conn is None:
            raise RuntimeError(_ERR_NOT_OPEN)
        if self.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0:
            raise RuntimeError(_ERR_BACKUP_EMPTY_DB.format(path=self.path))
        dest_path = Path(dest).expanduser()
        if dest_path.exists():
            raise FileExistsError(_ERR_BACKUP_DEST_EXISTS.format(dest=dest_path))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        self._check_snapshot_space(dest_path.parent)
        try:
            self.conn.execute("VACUUM INTO ?", (str(dest_path),))
            # VACUUM INTO honors the process umask (often 0o644). Snapshot may
            # contain prompt/response text — tighten to owner-only by default.
            dest_path.chmod(0o600)
        except BaseException:
            # A partial snapshot would sort newest and be what `restore
            # --latest` picks; never leave one behind.
            dest_path.unlink(missing_ok=True)
            raise

    def _check_snapshot_space(self, target_dir: Path) -> None:
        """Refuse up front if `target_dir` can't hold a copy of the DB and its WAL."""
        wal = Path(str(self.path) + "-wal")
        needed = self.path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
        free = shutil.disk_usage(target_dir).free
        if free < needed:
            raise RuntimeError(
                _ERR_BACKUP_NO_SPACE.format(
                    dir=target_dir,
                    needed=needed / _BYTES_PER_MIB,
                    free=free / _BYTES_PER_MIB,
                ),
            )

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
            staging.chmod(0o600)
            os.replace(staging, self.path)  # noqa: PTH105 — atomic-replace; tests patch os.replace
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        self.open()
        _chmod_sidecars(self.path)

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
