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
- JSONL intake pipeline: `convo index [--full] [--projects-dir PATH]
  [--dry-run] [--json]`. Walks `~/.claude/projects/<slug>/*.jsonl`,
  parses Claude Code session records, populates `source_files`,
  `sessions`, `messages`, `tool_calls`, `tool_results`. Idempotent
  (sha256-keyed); `--full` re-indexes. Cross-file `parent_id` and
  `tool_call_id` references are NULLed/dropped; PK collisions across
  resumed sessions are deduped via `INSERT OR IGNORE`; FK validation
  is deferred to commit-time so out-of-order parent records work.
  Per-file errors are contained — one bad file does not abort the
  tree run.
- `intake/` package: typed `IntakeRecord` dataclass union, `parse_line`
  / `parse_file` lazy generator, pure `map_record`, `index_file` /
  `index_tree` orchestrators, `compute_file_signature` (chunked sha256).
- `convo info [--json]`: at-a-glance database overview — schema version
  (`PRAGMA user_version`), row counts per indexed table (`source_files`,
  `sessions`, `messages`, `tool_calls`, `tool_results`), last index
  timestamp, top 5 projects by session count, live DB size, and snapshot
  directory path / count / total bytes. JSON mode emits a versioned
  envelope (`schema_version: 1`).
- `convo search "<query>" [--since SPAN] [--project P] [--tool T]
  [--limit N] [--json]`: FTS5-backed search across messages, tool calls,
  and tool results, ranked by timestamp DESC. `--since` accepts a
  shorthand span (`7d`, `24h`, `90m`, `30s`). Queries flow through to
  FTS5 MATCH syntax, so `+required` / `-excluded` prefixes work; invalid
  FTS5 input is rejected with a clean error envelope. Prose mode prints
  one hit per line with `[kind] timestamp | excerpt | session_id` and
  highlights matched substrings on a TTY.
- `convo inspect <session-id> [--json] [--full]`: session timeline with
  header (started_at, ended_at, project, model, git_branch) and a
  numbered message list with inline tool calls. Accepts a partial UUID
  prefix — exact match resolves directly, ambiguous prefixes print the
  candidate list, no match emits `convo: no session matches <prefix>`.
  `--full` dumps message content verbatim; default truncates to 200
  chars per message.
- `convo snapshots [--json]`: list snapshot files in `CONVO_BACKUP_DIR`
  with aligned `name | size | age` columns, newest first. JSON mode
  emits structured `SnapshotInfo` records (path, timestamp_utc,
  size_bytes, age_human).
- `convo restore --latest`: shorthand that resolves `CONVO_BACKUP_DIR`,
  picks the newest snapshot file by mtime, and restores it via the
  existing atomic-swap path. Mutually exclusive with positional `<src>`;
  empty snapshot directory exits 1 with a clean error.
- `convo stats <family> [--since SPAN] [--project P] [--json]`: per-family
  SQL aggregations over the indexed corpus. `tools` ranks tool-call usage
  by name (top-20 frequency, top-10 by median duration, per-tool error
  rate via `tool_results.is_error`); `commands` groups the first user
  message of each session (whitespace-collapsed, truncated to 80 chars)
  and returns the top-20 frequency histogram; `sessions` reports count,
  median and p95 duration (computed in Python via `statistics`), and a
  24-bucket hour-of-day distribution of session start times (UTC);
  `files` reports `source_files` count, total size, message-count sum,
  and the top-10 indexed JSONL files ranked by `message_count`; `model`
  reports a sessions-per-model histogram with a separate null/unknown
  bucket. Each family emits a versioned JSON envelope
  (`schema_version: 1`) under `--json`.
- `convo summary [--since SPAN] [--project P] [--json]`: composes all
  five `stats` families into one dashboard run. Re-uses the Phase B
  `parse_span` and `--project` filter so the window/scope is consistent
  across sub-reports. JSON mode emits a single envelope keyed by family.
- `convo diff [--since SPAN] [--project P] [--json]`: runs the same
  aggregations over two consecutive windows of equal length (current vs
  previous) and reports per-row deltas. Default span is 7d. Prose mode
  highlights deltas with ANSI green/red on a TTY; JSON mode emits raw
  current/previous/delta triples.
- Wheel-build CI check that asserts `migrations/0001_init.sql` is present
  in the packaged distribution.
- `just snapshots-clean` recipe to mirror `just db-reset` for local resets.
- `convo backup [--json]` and `convo restore [--json]`: snapshot/restore
  commands now emit a versioned envelope on stdout under `--json`
  (`{"schema_version": 1, "backup": {"snapshot_path", "size_bytes"}}` and
  `{"schema_version": 1, "restore": {"source"}}`), making them scriptable
  alongside the other read commands.
- `convo index --json`: response is now wrapped in the standard envelope
  shape `{"schema_version": 1, "index": {...}}` (was a flat object), matching
  every other JSON command.
- JSON error envelope contract: when `--json` is set and a modeled error is
  raised (`RuntimeError`, `ValueError`, `OSError`, `sqlite3.DatabaseError`,
  `FileExistsError`), the CLI now emits
  `{"schema_version": 1, "error": {"message": "..."}}` on stdout and leaves
  stderr empty, so JSON consumers can `jq` the result instead of getting
  empty stdout. Argparse-level errors (e.g. `--since potato`) keep their
  native argparse behaviour and still exit 2.

### Changed

- Stats family total field names normalized to uniform `total`. Previously
  each family used a distinct name (`total_calls`, `total_sessions_with_command`,
  `total_sessions`, `total_files`, `total_sessions`). Scripts can now loop
  every family with a single accessor (`body["total"]`). Family-specific
  extras on `stats files` (`total_size_bytes`, `total_message_count`) are
  unchanged. Affects both `--json` payload and prose output.

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

- `convo stats hooks` and `convo stats skills` — deferred to v1.1;
  require a `0002_live_hooks.sql` schema addition to capture pre/post
  tool hook events and skill invocations from the JSONL.

### Future work

- Demo asciinema recording in README.
- Optional mkdocs site.
- Claude Code plugin/extension packaging (primary distribution target).
