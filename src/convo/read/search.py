"""`convo search` — FTS5-backed search across messages, tool_calls, tool_results."""

# Several queries in this module assemble WHERE clauses by joining a fixed
# allow-list of condition fragments. All user-supplied values are bound via
# `?` placeholders. The f-string SQL trips ruff S608 on shape; suppressions
# are per-line on the affected expressions.

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro
from convo.read.filters import since_iso as _filters_since_iso

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import timedelta

    from convo.db import Database


# Sentinels used by `snippet()` so the CLI can locate the highlighted span without
# depending on terminal capabilities. The CLI replaces these with ANSI bold on TTY
# or strips them in plain mode. Chosen to be unlikely to appear in real content.
SNIPPET_PRE: str = "\x02HIT\x02"
SNIPPET_POST: str = "\x03HIT\x03"
SNIPPET_ELLIPSIS: str = "..."
_SNIPPET_TOKENS: int = 12

_KIND_MESSAGE: str = "message"
_KIND_TOOL_CALL: str = "tool_call"
_KIND_TOOL_RESULT: str = "tool_result"

_ERR_INVALID_QUERY = "invalid search query: {reason}"
_ERR_EMPTY_QUERY = "search query must not be empty"
_ERR_SHORT_QUERY_TERM = (
    "search tokens must be at least 3 characters (FTS5 trigram tokenizer minimum)"
)
_MIN_TERM_LEN = 3


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result row from `search()`.

    Fields:
      kind: one of ``"message"``, ``"tool_call"``, ``"tool_result"``.
      id: primary key of the underlying row (``messages.id``, ``tool_calls.id``,
          or ``tool_results.tool_call_id``).
      session_id: the parent session id; never NULL for any indexed row.
      timestamp: ISO timestamp of the row (or its parent message, for
          tool_results which lack their own timestamp). May be ``None`` if the
          source JSONL didn't include one.
      excerpt: FTS5 ``snippet()`` output. Contains :data:`SNIPPET_PRE` /
          :data:`SNIPPET_POST` markers around matched substrings — the CLI
          turns those into ANSI bold or strips them.
      project: ``sessions.project_path`` (may be ``None``).
    """

    kind: str
    id: str
    session_id: str
    timestamp: str | None
    excerpt: str
    project: str | None


def build_fts_query(raw: str) -> str:
    """Convert a user query string into a safe FTS5 MATCH expression.

    Rules:
      - Empty / whitespace-only input → ``ValueError``.
      - If any whitespace-separated token starts with ``+`` or ``-``, treat the
        query as a token list. ``+token`` becomes a required phrase (implicit
        AND) and ``-token`` becomes ``NOT "token"``.
      - Otherwise the whole input is wrapped as a single phrase search (the
        common case). This makes punctuation, FTS5 operators, and quotes inside
        the user's query inert.

    The returned expression is meant to be placed verbatim after ``MATCH ?``
    binding. Any embedded ``"`` is escaped by doubling per FTS5 syntax.
    """
    text = raw.strip()
    if not text:
        raise ValueError(_ERR_EMPTY_QUERY)

    tokens = text.split()
    has_operators = any(t.startswith(("+", "-")) and len(t) > 1 for t in tokens)
    if not has_operators:
        # Whole-phrase mode: the trigram tokenizer needs ≥3 chars to find any
        # match, so a 1- or 2-char query silently returns 0 hits. Reject up-front.
        if len(text) < _MIN_TERM_LEN:
            raise ValueError(_ERR_SHORT_QUERY_TERM)
        return _phrase(text)

    parts: list[str] = []
    for token in tokens:
        term = token[1:] if token.startswith(("+", "-")) and len(token) > 1 else token
        if len(term) < _MIN_TERM_LEN:
            raise ValueError(_ERR_SHORT_QUERY_TERM)
        if token.startswith("-") and len(token) > 1:
            parts.append(f"NOT {_phrase(term)}")
        else:
            parts.append(_phrase(term))
    return " ".join(parts)


def _phrase(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class _Filters:
    """Bundle of WHERE-clause inputs threaded through the SQL builders."""

    fts_match: str
    since_iso: str | None
    project: str | None
    tool: str | None


def search(  # noqa: PLR0913
    db: Database,
    query: str,
    *,
    since: timedelta | None = None,
    project: str | None = None,
    tool: str | None = None,
    limit: int = 50,
) -> Iterator[SearchHit]:
    """Search across messages / tool_calls / tool_results FTS tables.

    Opens a read-only connection on the same DB path as ``db`` (via SQLite's
    ``mode=ro`` URI). The caller's ``Database`` need not be opened — only its
    ``path`` is used. The function materializes results internally because each
    branch uses ``LIMIT`` and the union is small.
    """
    filters = _Filters(
        fts_match=build_fts_query(query),
        since_iso=_filters_since_iso(since),
        project=project,
        tool=tool,
    )
    ro = open_ro(db.path)
    try:
        try:
            rows = _run_search(ro, filters, limit=limit)
        except sqlite3.OperationalError as exc:
            raise ValueError(_ERR_INVALID_QUERY.format(reason=exc)) from exc
    finally:
        ro.close()
    yield from rows


def _run_search(
    conn: sqlite3.Connection,
    filters: _Filters,
    *,
    limit: int,
) -> list[SearchHit]:
    parts: list[tuple[str, list[object]]] = []

    if filters.tool is None:
        parts.append(_message_branch(filters))
    parts.append(_tool_call_branch(filters))
    parts.append(_tool_result_branch(filters))

    union_sql = "\nUNION ALL\n".join(sql for sql, _ in parts)
    params: list[object] = []
    for _, p in parts:
        params.extend(p)
    full_sql = f"SELECT * FROM ({union_sql}) ORDER BY (timestamp IS NULL), timestamp DESC LIMIT ?"  # noqa: S608
    params.append(int(limit))

    return [
        SearchHit(
            kind=str(row["kind"]),
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            timestamp=None if row["timestamp"] is None else str(row["timestamp"]),
            excerpt=str(row["excerpt"]),
            project=None if row["project"] is None else str(row["project"]),
        )
        for row in conn.execute(full_sql, params)
    ]


def _snippet(table: str, col: int) -> str:
    return (
        f"snippet({table}, {col}, "
        f"'{SNIPPET_PRE}', '{SNIPPET_POST}', '{SNIPPET_ELLIPSIS}', {_SNIPPET_TOKENS})"
    )


def _message_branch(filters: _Filters) -> tuple[str, list[object]]:
    where: list[str] = ["messages_fts MATCH ?"]
    params: list[object] = [filters.fts_match]
    if filters.since_iso is not None:
        where.append("m.timestamp IS NOT NULL AND m.timestamp >= ?")
        params.append(filters.since_iso)
    if filters.project is not None:
        where.append("s.project_path = ?")
        params.append(filters.project)
    select_clause = (
        f"SELECT '{_KIND_MESSAGE}' AS kind, m.id AS id, m.session_id AS session_id, "
        f"m.timestamp AS timestamp, {_snippet('messages_fts', 0)} AS excerpt, "
        f"s.project_path AS project"
    )
    sql = (
        f"{select_clause} "
        f"FROM messages_fts "
        f"JOIN messages m ON m.rowid = messages_fts.rowid "
        f"JOIN sessions s ON s.id = m.session_id "
        f"WHERE {' AND '.join(where)}"
    )
    return sql, params


def _tool_call_branch(filters: _Filters) -> tuple[str, list[object]]:
    where: list[str] = ["tool_calls_fts MATCH ?"]
    params: list[object] = [filters.fts_match]
    if filters.since_iso is not None:
        where.append("tc.started_at IS NOT NULL AND tc.started_at >= ?")
        params.append(filters.since_iso)
    if filters.project is not None:
        where.append("s.project_path = ?")
        params.append(filters.project)
    if filters.tool is not None:
        where.append("tc.name = ?")
        params.append(filters.tool)
    select_clause = (
        f"SELECT '{_KIND_TOOL_CALL}' AS kind, tc.id AS id, tc.session_id AS session_id, "
        f"tc.started_at AS timestamp, {_snippet('tool_calls_fts', -1)} AS excerpt, "
        f"s.project_path AS project"
    )
    sql = (
        f"{select_clause} "
        f"FROM tool_calls_fts "
        f"JOIN tool_calls tc ON tc.rowid = tool_calls_fts.rowid "
        f"JOIN sessions s ON s.id = tc.session_id "
        f"WHERE {' AND '.join(where)}"
    )
    return sql, params


def _tool_result_branch(filters: _Filters) -> tuple[str, list[object]]:
    where: list[str] = ["tool_results_fts MATCH ?"]
    params: list[object] = [filters.fts_match]
    if filters.since_iso is not None:
        where.append("m.timestamp IS NOT NULL AND m.timestamp >= ?")
        params.append(filters.since_iso)
    if filters.project is not None:
        where.append("s.project_path = ?")
        params.append(filters.project)
    if filters.tool is not None:
        where.append("tc.name = ?")
        params.append(filters.tool)
    select_clause = (
        f"SELECT '{_KIND_TOOL_RESULT}' AS kind, tr.tool_call_id AS id, "
        f"tc.session_id AS session_id, m.timestamp AS timestamp, "
        f"{_snippet('tool_results_fts', 0)} AS excerpt, s.project_path AS project"
    )
    sql = (
        f"{select_clause} "
        f"FROM tool_results_fts "
        f"JOIN tool_results tr ON tr.rowid = tool_results_fts.rowid "
        f"JOIN tool_calls tc ON tc.id = tr.tool_call_id "
        f"JOIN sessions s ON s.id = tc.session_id "
        f"LEFT JOIN messages m ON m.id = tr.message_id "
        f"WHERE {' AND '.join(where)}"
    )
    return sql, params
