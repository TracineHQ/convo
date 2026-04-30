# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial repository scaffold: license, README skeleton, pyproject, ruff +
  mypy strict configuration, justfile, pre-commit hooks, gitleaks config,
  GitHub Actions CI matrix on Python 3.12 and 3.13.
- Storage layer: `Database` class with `open` / `close` / `migrate` /
  `backup` / `backup_snapshot` / `restore_snapshot`, default DB at
  `~/.claude/convo.db` (overridable via `CONVO_DB`), versioned schema
  migrations under `src/convo/migrations/NNNN_*.sql`, FTS5
  trigram-tokenizer search across tool-call inputs, tool-result outputs,
  and message text.
- CLI subcommands: `convo backup <dest>`, `convo backup --auto`,
  `convo restore <src>`. Exposed via the `convo` console-script entry
  point. Snapshots are append-only; `restore` preserves the snapshot
  source file.
- Wheel-build CI check that asserts `migrations/0001_init.sql` is present
  in the packaged distribution.
- `just snapshots-clean` recipe to mirror `just db-reset` for local resets.

### Notes

- v1.0 is a fresh-install release. There is no upgrade path from any
  pre-OSS internal predecessor; install fresh and ingest your JSONL
  history once intake ships.

### Known limitations

- **Windows `os.replace`** against a DB held open by another process raises
  `PermissionError`. Close other convo processes first. CI matrix is
  ubuntu + macos only.
- **Cross-filesystem restore**: if `$CONVO_BACKUP_DIR` is on a different
  filesystem from `~/.claude/convo.db`, `restore_snapshot()` raises
  `OSError` (atomic-replace requires same FS). Not auto-detected.
- **Silent migrations**: `migrate()` does not log when applying a
  migration. Deferred to a future observability plan.

### Planned for 0.1.0

- JSONL intake pipeline with typed records.
- Read commands: search, format, inspect, export.
- Analytics: tools, commands, sessions, files, skills, model, hooks,
  retries, chains.
- CLI dispatch via typer.
- Period-comparison `diff` command.

### Future work

- Demo asciinema recording in README.
- Optional mkdocs site.
- PyPI publish (deferred until adoption signal).
