# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Fixed

## [1.0.0] - 2026-05-06

First stable release. Marketplace submission. Public CLI surface, JSON
envelope schemas, and on-disk DB layout are now subject to semantic
versioning.

### Added

- `convo stats hooks` — analytics over guard's PreToolUse decision log.
  Reports per-hook decision counts and top denied commands. Path discovery
  follows the contract in `guard/docs/JSONL_FORMAT.md` §3.2: explicit path
  → `$GUARD_DECISIONS_PATH` → `~/.claude/guard-decisions.jsonl`.
- `guard_decisions` table plus its FTS5 mirror via migration
  `0002_guard_decisions.sql`. One row per JSONL line; indexed on
  `session_id`, `hook_id`, `decision`, `timestamp`, and `tool_name`.
- `intake/guard.py` ingestion module — sha256-idempotent reader for
  guard's decision JSONL, mirroring the transcript ingest path's
  per-file error containment.
- `convo summary` now composes six analytics families (`tools`,
  `commands`, `sessions`, `files`, `model`, `hooks`).
- Regression invariants in `tests/test_version_consistency.py`:
  release-workflow PyPI URL must match `pyproject.toml` `[project].name`,
  and every registered stats family must appear in `README.md`.

### Changed

- `convo summary` output covers six families instead of five.

### Fixed

- Release workflow `environment.url` now references the `tracine-convo`
  PyPI distribution (was still pointing at the legacy `convo` URL after
  the rename in v0.1.0).
- README roadmap no longer lists `convo stats hooks` as deferred — the
  feature has shipped.

## [0.1.0] - 2026-04-30

Initial public release.

### Added

- `convo index` walks `~/.claude/projects/<slug>/*.jsonl` and populates a local
  SQLite database (`~/.claude/convo.db` by default; `CONVO_DB` overrides).
  Idempotent via sha256; `--full` forces re-index. Per-file errors are contained.
- `convo info` reports schema version, row counts, last index time, top projects,
  and snapshot directory size.
- `convo search "<query>" [--since SPAN] [--project P] [--tool T] [--limit N]`
  runs FTS5 trigram search across messages, tool calls, and tool results.
  Supports `+required` / `-excluded` prefixes.
- `convo inspect <session-id>` renders a session timeline with inline tool calls.
  Accepts a UUID prefix; `--full` dumps untruncated content; `--latest` resolves
  the most recent session.
- `convo snapshots` lists snapshot files with `name | size | age` columns.
- `convo backup <dest>` / `convo backup --auto` writes a `VACUUM INTO` snapshot.
  Snapshot files are written `0600`.
- `convo restore <src>` / `convo restore --latest` performs an atomic-swap
  restore from a snapshot; the source file is preserved.
- `convo stats <family>` aggregates over the corpus. Families: `tools`,
  `commands`, `sessions`, `files`, `model`.
- `convo summary` composes all five stats families into one report.
- `convo diff` compares the current window against the previous window of
  equal length and reports per-bucket deltas.
- `--json` on every read and write command emits
  `{"schema_version": 1, "<command>": {...}}`. Modeled errors emit
  `{"schema_version": 1, "error": {"message": "..."}}` on stdout.
- `--version` prints the installed CLI version.
- Claude Code plugin packaging under `.claude-plugin/` with `convo-search` and
  `convo-summary` skills and a thin `bin/convo` wrapper that defers to the
  `convo` binary on `PATH`.

### Known limitations

- **Windows**: `os.replace` against an open DB raises `PermissionError`. CI
  runs on Ubuntu and macOS only.
- **Cross-filesystem restore**: `$CONVO_BACKUP_DIR` and `~/.claude/convo.db`
  must share a filesystem; the atomic-replace requires it.
