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
    """One result row from `search()`."""

    kind: str
    id: str
    session_id: str
    timestamp: str | None
    excerpt: str
    project: str | None
    role: str | None = None
    """For ``kind == "message"`` hits: the role (user/assistant/system).
    None for tool_call and tool_result hits."""

    tool_origin: str | None = None
    """For tool_call hits: the tool name. For tool_result hits: the name of
    the tool whose result this is. None for message hits."""


def build_fts_query(raw: str) -> str:
    """Convert a user query into a safe FTS5 MATCH expression.

    Default semantics (v2):
      - Whitespace-separated tokens are AND'd together.
      - Quoted strings are literal phrases.
      - The literal token ``OR`` (any case) between two terms is a
        disjunction. The character ``|`` is treated the same.
      - ``-token`` excludes (FTS5 NOT).
      - ``+token`` is accepted as a no-op (legacy syntax).

    The returned expression is meant to be placed verbatim after ``MATCH ?``.
    All embedded double-quotes inside phrases are escaped by doubling.
    """
    text = raw.strip()
    if not text:
        msg = _ERR_EMPTY_QUERY
        raise ValueError(msg)

    tokens = _tokenize_query(text)
    if not tokens:
        msg = _ERR_EMPTY_QUERY
        raise ValueError(msg)

    parts: list[str] = []
    for kind, value in tokens:
        if kind == "OR":
            parts.append("OR")
            continue
        if kind == "NOT":
            if len(value) < _MIN_TERM_LEN:
                msg = _ERR_SHORT_QUERY_TERM
                raise ValueError(msg)
            parts.append(f"NOT {_phrase(value)}")
            continue
        # PHRASE
        if len(value) < _MIN_TERM_LEN:
            msg = _ERR_SHORT_QUERY_TERM
            raise ValueError(msg)
        parts.append(_phrase(value))

    return " ".join(parts)


def _tokenize_query(text: str) -> list[tuple[str, str]]:
    """Lex the query into ``(kind, value)`` pairs.

    Kinds: ``PHRASE`` (raw text or quoted), ``OR``, ``NOT``.
    Hand-rolled lexer (no shlex) so quote escaping is fully under our control.
    """
    out: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            # Quoted phrase; read until next unescaped quote.
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            value = text[i + 1 : j]
            out.append(("PHRASE", value))
            i = j + 1
            continue
        if c == "|":
            out.append(("OR", "|"))
            i += 1
            continue
        # Read until whitespace.
        j = i
        while j < n and not text[j].isspace():
            j += 1
        raw = text[i:j]
        i = j

        if raw.upper() == "OR":
            out.append(("OR", raw))
            continue
        if raw.startswith("-") and len(raw) > 1:
            out.append(("NOT", raw[1:]))
            continue
        if raw.startswith("+") and len(raw) > 1:
            out.append(("PHRASE", raw[1:]))
            continue
        out.append(("PHRASE", raw))

    return out


def _phrase(s: str) -> str:
    """Wrap a value as an FTS5 quoted phrase, escaping inner quotes."""
    escaped = s.replace('"', '""')
    return f'"{escaped}"'


@dataclass(frozen=True, slots=True)
class _Filters:
    """Bundle of WHERE-clause inputs threaded through the SQL builders."""

    fts_match: str
    since_iso: str | None
    project: str | None
    tool: str | None
    session: str | None
    tool_exact: bool
    snippet_tokens: int


def search(  # noqa: PLR0913
    db: Database,
    query: str,
    *,
    since: timedelta | None = None,
    project: str | None = None,
    tool: str | None = None,
    session: str | None = None,
    tool_exact: bool = False,
    excerpt_chars: int = 600,
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
        session=session,
        tool_exact=tool_exact,
        snippet_tokens=max(1, min(64, excerpt_chars // 6)),
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
            role=None if row["role"] is None else str(row["role"]),
            tool_origin=None if row["tool_origin"] is None else str(row["tool_origin"]),
        )
        for row in conn.execute(full_sql, params)
    ]


def _snippet(table: str, col: int, tokens: int) -> str:
    return (
        f"snippet({table}, {col}, "
        f"'{SNIPPET_PRE}', '{SNIPPET_POST}', '{SNIPPET_ELLIPSIS}', {tokens})"
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
    if filters.session is not None:
        where.append("s.id LIKE ?")
        params.append(filters.session + "%")
    snip = _snippet("messages_fts", 0, filters.snippet_tokens)
    select_clause = (
        f"SELECT '{_KIND_MESSAGE}' AS kind, m.id AS id, m.session_id AS session_id, "
        f"m.timestamp AS timestamp, {snip} AS excerpt, "
        f"s.project_path AS project, m.role AS role, NULL AS tool_origin"
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
    if filters.session is not None:
        where.append("s.id LIKE ?")
        params.append(filters.session + "%")
    if filters.tool is not None:
        if filters.tool_exact:
            where.append("tc.name = ?")
            params.append(filters.tool)
        else:
            where.append("tc.name LIKE ?")
            params.append(filters.tool + "%")
    snip = _snippet("tool_calls_fts", 1, filters.snippet_tokens)
    select_clause = (
        f"SELECT '{_KIND_TOOL_CALL}' AS kind, tc.id AS id, tc.session_id AS session_id, "
        f"tc.started_at AS timestamp, {snip} AS excerpt, "
        f"s.project_path AS project, NULL AS role, tc.name AS tool_origin"
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
    if filters.session is not None:
        where.append("s.id LIKE ?")
        params.append(filters.session + "%")
    if filters.tool is not None:
        if filters.tool_exact:
            where.append("tc.name = ?")
            params.append(filters.tool)
        else:
            where.append("tc.name LIKE ?")
            params.append(filters.tool + "%")
    snip = _snippet("tool_results_fts", 0, filters.snippet_tokens)
    select_clause = (
        f"SELECT '{_KIND_TOOL_RESULT}' AS kind, tr.tool_call_id AS id, "
        f"tc.session_id AS session_id, m.timestamp AS timestamp, "
        f"{snip} AS excerpt, s.project_path AS project, NULL AS role, tc.name AS tool_origin"
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


def extract_indices_and_clean(raw: str) -> tuple[str, list[list[int]]]:
    """Strip SNIPPET_PRE/SNIPPET_POST sentinels and emit char-offset indices.

    Returns the cleaned string (with ``[match]`` brackets in place of the
    sentinels) and a list of ``[start, end]`` pairs pointing at the match
    content inside the cleaned string.
    """
    parts: list[str] = []
    indices: list[list[int]] = []
    out_pos = 0
    i = 0
    n = len(raw)
    while i < n:
        if raw.startswith(SNIPPET_PRE, i):
            i += len(SNIPPET_PRE)
            parts.append("[")
            out_pos += 1
            start = out_pos
            end_marker = raw.find(SNIPPET_POST, i)
            if end_marker == -1:
                # Unterminated; treat the rest as match content
                content = raw[i:]
                i = n
            else:
                content = raw[i:end_marker]
                i = end_marker + len(SNIPPET_POST)
            parts.append(content)
            out_pos += len(content)
            end = out_pos
            parts.append("]")
            out_pos += 1
            indices.append([start, end])
        else:
            parts.append(raw[i])
            i += 1
            out_pos += 1
    return "".join(parts), indices
