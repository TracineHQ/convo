# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `convo migrate [--backup] [--json]` applies pending schema migrations,
  reporting each one as it runs. `--backup` first writes a snapshot to the
  snapshot directory, and says so plainly (`backup_status` under `--json`)
  when there was nothing pending to protect. `convo index` and the hook's
  background run apply migrations themselves and log them on stderr (the
  hook log, for the background run). `convo info` reports
  `pending_migrations`.

  **Upgrading from 2.1.0:** before upgrading, take a snapshot with the 2.1.0
  CLI (`convo backup --auto`). The first `convo index` or session end after
  the upgrade migrates the DB to schema 5, usually before you get to
  `convo migrate --backup`, so that snapshot is your way back. To go back to
  2.1.0, which refuses a v5 DB (its `restore` included): make sure no convo
  process is running (including a hook's background run), downgrade, then
  copy the snapshot over `<CONVO_DB>` by hand and delete the `-wal` and
  `-shm` files beside it.
- `convo hook session-end` reads Claude Code's hook JSON on stdin, starts a
  detached `convo index --session <id>` for that one transcript, and returns
  in milliseconds. If another convo writer is running, one background run
  waits for it (up to an hour) and then indexes every transcript, newest
  first; runs for sessions that end meanwhile leave theirs to it and exit.
  The background run applies pending migrations like any `convo index` and
  appends its output to `<CONVO_DB>.hook.log`, each line stamped with the
  time and the session id.
- `convo index --session ID` indexes one session's transcript; `--no-wait`
  exits (status `busy`) instead of waiting for another convo writer;
  `--no-migrate` refuses to run while migrations are pending.

### Changed

- The plugin's SessionEnd hook now runs `convo hook session-end`. Claude Code
  gives SessionEnd hooks 1.5 s by default (a plugin hook's `timeout` does not
  raise it), so the old in-hook `convo index` was cut off before it could
  catch up on a grown session. The hook no longer runs `convo index-guard`;
  run it yourself when you want guard decisions refreshed. Upgrade the CLI
  (`uv tool upgrade tracine-convo` or `pipx upgrade tracine-convo`) along
  with the plugin: the plugin updates from the marketplace on its own, and
  with an older CLI the hook falls back to the old in-hook `convo index`.
- Read commands (`info`, `search`, `inspect`, `stats`, `summary`, `diff`) and
  `backup` no longer apply migrations. `convo index`, the hook's background
  run and `convo migrate` do. A DB too old to read (schema 3 or earlier)
  now gets an error telling you to run `convo migrate`, in the JSON envelope
  under `--json` / `--format json`.
- Writers (`index`, `index-guard`, `migrate`, `restore`, and the hook's
  background run) share an advisory lock, `<CONVO_DB>.ingest.lock`, so they
  no longer collide on the DB. `convo index` waits for the holder by default.
- `convo index` processes transcripts most recently modified first, so fresh
  sessions are searchable before a backlog, and walks the projects tree once
  per run instead of twice.
- Schema v6 (migration `0006_message_positions.sql`) stores each message's
  `position` in a new `message_positions` table. `convo index` fills it as it
  writes messages. Existing messages are numbered when the DB migrates
  (`convo migrate`, `convo index` or the hook's background run), without
  rewriting `messages` or its FTS index: about 3 s for 665k messages on a
  3.7 GB test DB with a warm page cache, up to about 15 s cold. Until then,
  `convo search` on a schema 4 or 5 DB counts each hit's position at query
  time, as before.

  **Upgrading from 2.1.1:** take a snapshot first (`convo backup --auto`).
  The first `convo index` or session end after the upgrade migrates the DB
  from schema 5 to 6, and 2.1.1 refuses a v6 DB (its `restore` included). To
  go back, make sure no convo process is running (including a hook's
  background run), downgrade, then copy the snapshot over `<CONVO_DB>` by
  hand and delete the `-wal` and `-shm` files beside it.

### Fixed

- Re-indexing a grown transcript was quadratic in the size of the DB:
  `messages.parent_id` and `tool_results.message_id` are `ON DELETE SET NULL`
  foreign keys with no index, so deleting each old row scanned both tables.
  One 20k-record file in a 120k-message DB took about 17 minutes; schema v5
  (migration 0005) indexes both columns, which brings it to seconds.
- The FTS update triggers fired on any column change, so the `SET NULL`
  updates above (and any non-text update) rewrote search-index entries.
  Schema v5 limits them to the indexed text columns.
- A snapshot that failed partway (disk full, interrupted) left a partial file
  that `convo restore --latest` would then pick. It is now removed. `backup`
  and `migrate --backup` also check for free space first and say on stderr
  when they start writing.
- `convo search` reads each hit's `position` from `message_positions` instead
  of numbering every message in each hit's session on every query. A search
  with hits in a 32k-message session drops from about 120 ms to 7 to 10 ms
  (`--limit` 10 to 500). `tool_result` hits now carry the position of the
  message that made the call instead of `null`.
- A relative `--db` or `CONVO_DB` path is resolved against the current
  directory. Before, every read command failed with "relative path can't be
  expressed as a file URI".
- `--project <name>` picks the project whose last path component is `<name>`
  even when a nested folder also contains it (`eval-kit` vs
  `eval-kit/python`). The basename match never fired, so these fell through to
  the substring match and errored as ambiguous.
- `--project` ignores a trailing `/` or `\`, which tab completion adds. Before,
  `--project /work/eval-kit/` skipped `/work/eval-kit` and picked
  `/work/eval-kit/python`, the only path containing the slash.
- `convo search` JSON `total` is the number of matches before `--limit`, not
  the size of the returned page. Prose output reports both
  (`-- 120 hits, showing 10`) and numbers hits `hit N of <total>`.
- Search prose says "1 hit", "1 session" and "1 project" instead of "1 hits",
  "1 sessions" and "1 projects".
- `convo search --fields` rejects unknown field names (exit 2) and lists the
  valid ones in the error and in `--help`. Before, an unknown name printed an
  empty column.
- `convo index --dry-run` ends with "Would index N files ... Dry run: nothing
  was written." instead of "Indexed N files" and an all-zero "Inserted" line.
- The README, plugin descriptions and the plugin shim's "not installed" hint
  say the plugin needs the CLI and give `pipx install tracine-convo` (the shim
  pointed at a git URL). The README lists all nine slash commands, documents
  `index-guard`, `--db`, `--session`, `--tool-exact` and `--fields`, and shows
  `--format json` instead of the deprecated `search --json`.
- Slash-command docs for `diff`, `info`, `inspect`, `stats` and `summary` said
  `--format=json`, which those commands reject; they take `--json`.
  `/convo:projects` and `/convo:tools` no longer hint a `--limit` they don't
  accept.
- `convo search --help` says `--since` also takes an ISO date, and its
  example uses `--format json`.
- `LICENSE` is the official Apache 2.0 text again. Sections 6 and 9 had been
  reworded.

### Removed

- The "top by median duration" section of `convo stats tools` and its help
  text. Intake never records tool-call durations, so it always printed
  "(none)". The JSON key `top_by_median_duration` stays, always `[]`, until the
  next envelope `schema_version` bump.

## [2.1.0] - 2026-10-15

### Added

- `convo inspect --max-chars N` sets the per-message content and tool-call
  input limit (defaults unchanged: 200 content / 80 tool input, 80 for both
  with `--timeline`); `--max-chars 0` returns full text.
- `convo inspect --json` echoes `from_message`/`to_message` and adds
  `tool_calls[].truncated`.
- Message `position` (1-indexed, the number `inspect` prints and
  `--from-message/--to-message` take) on `inspect` messages and timeline
  events, and on `search` hits (message hits; the calling message for
  tool_call hits; null for tool_result hits). Quote any hit in full with
  `convo inspect <session_id> --from-message P --to-message P --max-chars 0`.
  Messages that tie on both `seq` and `timestamp` (only possible across two
  source files for one session) are now ordered by id instead of insertion
  order, so numbering is deterministic.

### Changed

- The plugin's SessionEnd hook also runs `convo index-guard`, and writes hook
  errors to `~/.claude/convo-hook-errors.log` instead of discarding them.

### Fixed

- `convo index-guard` indexes decisions logged under `guard mode lockdown`.
  They were quarantined as `unknown_mode`; schema v4 adds `lockdown` to
  `guard_decisions.mode`.
- Databases that reached schema v2 without a `guard_decisions` table (from an
  early form of migration 0002) are repaired on open (schema v3). Before,
  `convo index-guard` failed with "no such table: guard_decisions".
- `convo inspect --from-message/--to-message` now apply without `--timeline`
  and reject 0, negative, and reversed ranges, or a start past the last
  message, with an error; an end past the last message is clamped (JSON echoes
  the requested values). Timeline `message_count`/`duration_seconds` still
  describe the whole session.
- `convo inspect --timeline --json` emits a JSON envelope (with the full
  session header) instead of prose.
- `convo search --excerpt-chars` help claimed a 600-character window, but
  FTS5 caps snippets at 64 tokens and the flag was mapped as if a token were
  six characters. It is now mapped one token per character (the trigram
  tokenizer), defaults to the 64 maximum (the same ~66-character excerpts as
  before), says so in `--help`, and rejects negative values.

## [2.0.1] - 2026-05-24

- Expand skill tool surface, harden hook timeout, document sqlite requirement (#22)
- Wire claude plugin validate into CI + pre-commit (#21)

## [2.0.0] - 2026-05-20

### Breaking changes

- Default query semantics: phrase -> AND across tokens. Quoted strings remain
  literal phrases.
- Default `--limit`: 50 -> 10.
- JSON envelope: every command now returns `{schema_version: 2, <command>: {...}}`.
  v1 `schema_version: 1` envelopes are gone.
- `convo inspect --full` semantics changed: was "no excerpt truncation," now
  "no message cap."
- Excerpt format: `\x02HIT\x02` sentinels replaced with `[match]` brackets in
  prose; `indices: [[start, end]]` char offsets in JSON.

### Added

- Default output is structured prose (`--format=prose`). JSON via
  `--format=json` or `--json`.
- `--excerpt-chars N` configures snippet window (default 600).
- `--fields ts,session,command,excerpt` projects fields tab-separated, one hit
  per line.
- `--session <prefix>` filter restricts search to one session.
- `--tool` is now prefix-match by default. `--tool-exact` for exact matching.
- `--since` accepts ISO dates (`2026-04-01`) in addition to shorthand
  (`7d`/`24h`).
- `--project` accepts basename/substring shorthand; ambiguous matches return a
  helpful error.
- `convo inspect --timeline` shows per-message structured view with relative
  timestamps.
- New commands: `convo projects`, `convo tools`, `convo sessions` for
  discovery.
- 0-hit responses include deterministic suggestions (hyphen-split, time-widen,
  filter-drop).
- Grep-style flags (`-A`, `-B`, `-C`, `-E`, `-i`) and invented flags
  (`--quiet`, `--by-caller`, etc.) rejected with helpful hints.

### Removed

- `+token` no longer changes behavior (was AND-required; now AND is default).
- Top-level envelope keys outside the `<command>` wrapper.

## [1.1.0] - 2026-05-11

### Added

- `convo info` `--help` now has an epilog describing the output sections.
- `convo stats` `--help` epilog now lists the `hooks` family alongside the
  legacy five.
- `index`, `search`, `inspect`, and `stats` now show concrete `Examples:`
  blocks in their `--help` output.
- `convo --version` now prints a `gh`-style three-line block: name+version,
  install path, repo URL.
- `tests/test_stats_hooks.py` covers the previously-untested `stats hooks`
  analytics module (counts by hook/decision, since/project filters).
- `tests/test_db_migrate.py::test_upgrade_v1_to_v2_preserves_data` exercises
  the v1→v2 migration on a populated DB.
- `seed_guard_decision()` helper in `tests/_seed.py` for `guard_decisions`
  rows.
- `.gitattributes` enforces LF line endings on text files for Windows
  parity.

### Changed

- `convo index-guard --path /missing.jsonl` now exits 1 with
  `convo: path not found: ...` to stderr. Auto-discovery with no `--path`
  argument and no log present is unchanged (clean exit 0).
- `.github/workflows/ci.yml` test+smoke jobs now use `setup-uv`'s
  `python-version` input instead of a separate `uv python pin` step,
  saving roughly 28 s per Windows run.
- `__main__.py` now also catches `OSError` errno 22/232 on Windows for
  broken-pipe handling.
- Three Windows skips in `tests/test_db_snapshots.py` converted from
  inline `pytest.skip()` to `@pytest.mark.skipif`.

### Fixed

- `.github/workflows/codeql.yml` gains a top-level `permissions: contents: read`
  default. Closes Scorecard `TokenPermissionsID` alert.
- `.github/workflows/ci.yml` actionlint installer is now SHA-pinned instead
  of tag-pinned. Closes Scorecard `PinnedDependenciesID` alert.
- `.claude/hooks/pre-bash-block-py-writes.sh` strips shell line comments
  before regex-matching (was triggering false positives on path tokens
  inside `#` comments) and now also matches absolute-path redirects under
  `src/convo/` and `tests/`.
- `.claude/hooks/post-edit-semgrep.sh` now branches on semgrep's exit code
  instead of grepping its stdout for emoji/keywords (the prior approach
  silently passed if output formatting changed).
- All four `.claude/hooks/*.sh` scripts now use `set -euo pipefail`.

### Documented

- `SECURITY.md` notes that POSIX mode bits (`0o600`) are silently ignored
  on Windows; the DB inherits the parent directory's NTFS ACL.

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
