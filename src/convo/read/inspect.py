"""`convo inspect` — render a single session: header + message timeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro

if TYPE_CHECKING:
    import sqlite3

    from convo.db import Database


_ERR_NO_MATCH = "no session matches {prefix}"
_ERR_AMBIGUOUS = "session id {prefix} is ambiguous; candidates: {candidates}"
_ERR_NO_SESSIONS = "no sessions in DB"
_MORE_MARKER = "... (and more)"

_RESOLVE_LIMIT: int = 5


@dataclass(frozen=True, slots=True)
class ToolCallView:
    """A single tool call attached to its parent message."""

    id: str
    name: str
    input_json: str
    started_at: str | None


@dataclass(frozen=True, slots=True)
class MessageView:
    """One turn in a session (user / assistant / system).

    `content` is the full message text; the CLI applies any truncation.
    `tool_calls` are inline children, ordered by `tool_calls.seq`.
    """

    id: str
    role: str
    timestamp: str | None
    content: str
    tool_calls: tuple[ToolCallView, ...]


@dataclass(frozen=True, slots=True)
class SessionView:
    """Full session view: header metadata + ordered message timeline."""

    id: str
    started_at: str | None
    ended_at: str | None
    project_path: str | None
    model: str | None
    git_branch: str | None
    messages: tuple[MessageView, ...]


def resolve_session_id(db: Database, prefix: str) -> str:
    """Resolve `prefix` to an exact session id.

    - Exact match wins immediately.
    - Otherwise treat `prefix` as a starts-with prefix.
    - Zero matches → `RuntimeError("no session matches <prefix>")`.
    - One match → that id.
    - More than one → `RuntimeError("...ambiguous...candidates: a, b, c")`.
    """
    # Session ids are stored lowercase; normalize so GLOB (case-sensitive) matches.
    needle = prefix.lower()
    ro = open_ro(db.path)
    try:
        # Exact match short-circuit.
        row = ro.execute("SELECT id FROM sessions WHERE id = ?", (needle,)).fetchone()
        if row is not None:
            return str(row[0])

        # GLOB avoids LIKE's `_`/`%` wildcard meaning; only `*` and `?` are special,
        # neither of which can appear in a hex UUID prefix.
        rows = ro.execute(
            "SELECT id FROM sessions WHERE id GLOB ? || '*' LIMIT ?",
            (needle, _RESOLVE_LIMIT + 1),
        ).fetchall()
    finally:
        ro.close()

    if not rows:
        raise RuntimeError(_ERR_NO_MATCH.format(prefix=prefix))
    if len(rows) == 1:
        return str(rows[0][0])

    shown = [str(r[0]) for r in rows[:_RESOLVE_LIMIT]]
    if len(rows) > _RESOLVE_LIMIT:
        shown.append(_MORE_MARKER)
    candidates = ", ".join(shown)
    raise RuntimeError(_ERR_AMBIGUOUS.format(prefix=prefix, candidates=candidates))


def resolve_latest_session(db: Database) -> str:
    """Return the most recent session id by `started_at DESC`, NULLs last.

    Raises ``RuntimeError("no sessions in DB")`` when the table is empty.
    """
    ro = open_ro(db.path)
    try:
        # NULLs LAST: ORDER BY started_at IS NULL puts non-NULLs first, then
        # ORDER BY started_at DESC picks the newest among the timestamped rows.
        row = ro.execute(
            "SELECT id FROM sessions ORDER BY (started_at IS NULL), started_at DESC LIMIT 1",
        ).fetchone()
    finally:
        ro.close()
    if row is None:
        raise RuntimeError(_ERR_NO_SESSIONS)
    return str(row[0])


def inspect_session(db: Database, session_id: str) -> SessionView:
    """Build a `SessionView` for `session_id` (which must be an exact id).

    Read-only: opens a fresh `mode=ro` URI connection to `db.path` so it can run
    while a writer holds the main DB. Does not mutate or rely on `db.conn`.
    """
    ro = open_ro(db.path)
    try:
        header = _fetch_header(ro, session_id)
        messages = _fetch_messages(ro, session_id)
    finally:
        ro.close()

    return SessionView(
        id=session_id,
        started_at=header["started_at"],
        ended_at=header["ended_at"],
        project_path=header["project_path"],
        model=header["model"],
        git_branch=header["git_branch"],
        messages=messages,
    )


def _fetch_header(conn: sqlite3.Connection, session_id: str) -> dict[str, str | None]:
    row = conn.execute(
        "SELECT started_at, ended_at, project_path, model, git_branch FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        # Resolver should have caught this; treat as a clean modeled error.
        raise RuntimeError(_ERR_NO_MATCH.format(prefix=session_id))
    return {
        "started_at": None if row["started_at"] is None else str(row["started_at"]),
        "ended_at": None if row["ended_at"] is None else str(row["ended_at"]),
        "project_path": None if row["project_path"] is None else str(row["project_path"]),
        "model": None if row["model"] is None else str(row["model"]),
        "git_branch": None if row["git_branch"] is None else str(row["git_branch"]),
    }


def _fetch_messages(conn: sqlite3.Connection, session_id: str) -> tuple[MessageView, ...]:
    msg_rows = conn.execute(
        "SELECT id, role, timestamp, content, seq "
        "FROM messages WHERE session_id = ? "
        "ORDER BY seq, timestamp",
        (session_id,),
    ).fetchall()

    if not msg_rows:
        return ()

    tc_rows = conn.execute(
        "SELECT id, message_id, name, input_json, started_at, seq "
        "FROM tool_calls WHERE session_id = ? "
        "ORDER BY message_id, seq",
        (session_id,),
    ).fetchall()

    by_message: dict[str, list[ToolCallView]] = {}
    for tc in tc_rows:
        view = ToolCallView(
            id=str(tc["id"]),
            name=str(tc["name"]),
            input_json=str(tc["input_json"]),
            started_at=None if tc["started_at"] is None else str(tc["started_at"]),
        )
        by_message.setdefault(str(tc["message_id"]), []).append(view)

    out: list[MessageView] = []
    for m in msg_rows:
        mid = str(m["id"])
        out.append(
            MessageView(
                id=mid,
                role=str(m["role"]),
                timestamp=None if m["timestamp"] is None else str(m["timestamp"]),
                content="" if m["content"] is None else str(m["content"]),
                tool_calls=tuple(by_message.get(mid, ())),
            ),
        )
    return tuple(out)
