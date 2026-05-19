"""`convo inspect` — render a single session: header + message timeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from convo.read._db_access import open_ro
from convo.read.prose import TimelineEvent

if TYPE_CHECKING:
    import sqlite3

    from convo.db import Database


_ERR_NO_MATCH = "no session matches {prefix}"
_ERR_AMBIGUOUS = "session id {prefix} is ambiguous; candidates: {candidates}"
_ERR_NO_SESSIONS = "no sessions in DB"
_MORE_MARKER = "... (and more)"

_RESOLVE_LIMIT: int = 5
_DEFAULT_MESSAGE_CAP: int = 50

# Base SQL fragments — kept as plain string constants so S608 does not fire on
# the dynamic callers that append a LIMIT clause or IN-list placeholders.
_MSG_SELECT = (
    "SELECT id, role, timestamp, content, seq"
    " FROM messages WHERE session_id = ?"
    " ORDER BY seq, timestamp"
)
_TC_SELECT_BASE = (
    "SELECT id, message_id, name, input_json, started_at, seq FROM tool_calls WHERE message_id IN ("
)


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
    """Full session view: header metadata + ordered message timeline.

    ``truncated`` is True when the message list was capped at ``_DEFAULT_MESSAGE_CAP``.
    ``total_messages`` is the total count in the session (may be > len(messages)).
    """

    id: str
    started_at: str | None
    ended_at: str | None
    project_path: str | None
    model: str | None
    git_branch: str | None
    messages: tuple[MessageView, ...]
    truncated: bool = False
    total_messages: int = 0


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


def inspect_session(db: Database, session_id: str, *, full: bool = False) -> SessionView:
    """Build a `SessionView` for `session_id` (which must be an exact id).

    Read-only: opens a fresh `mode=ro` URI connection to `db.path` so it can run
    while a writer holds the main DB. Does not mutate or rely on `db.conn`.

    With ``full=False`` (default), messages are capped at ``_DEFAULT_MESSAGE_CAP``.
    With ``full=True``, all messages are returned.
    """
    ro = open_ro(db.path)
    try:
        header = _fetch_header(ro, session_id)
        messages, total_messages = _fetch_messages(ro, session_id, full=full)
    finally:
        ro.close()

    truncated = not full and total_messages > _DEFAULT_MESSAGE_CAP
    return SessionView(
        id=session_id,
        started_at=header["started_at"],
        ended_at=header["ended_at"],
        project_path=header["project_path"],
        model=header["model"],
        git_branch=header["git_branch"],
        messages=messages,
        truncated=truncated,
        total_messages=total_messages,
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


def _fetch_messages(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    full: bool = False,
) -> tuple[tuple[MessageView, ...], int]:
    """Return ``(messages, total_count)``.

    When ``full=False``, fetches at most ``_DEFAULT_MESSAGE_CAP`` messages.
    ``total_count`` is always the real count in the session.
    """
    total_count: int = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]

    _lim = "" if full else f" LIMIT {_DEFAULT_MESSAGE_CAP}"
    msg_rows = conn.execute(_MSG_SELECT + _lim, (session_id,)).fetchall()

    if not msg_rows:
        return (), total_count

    # Only fetch tool_calls for the messages we actually loaded.
    loaded_ids = [str(m["id"]) for m in msg_rows]
    _ph = ",".join("?" * len(loaded_ids))
    _tc_q = _TC_SELECT_BASE + _ph + ") ORDER BY message_id, seq"
    tc_rows = conn.execute(_tc_q, loaded_ids).fetchall()

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
    return tuple(out), total_count


# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------

_PREVIEW_LEN = 80


def _parse_ts(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp string; return None on failure."""
    if raw is None:
        return None
    s = str(raw)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _ts_offset(raw: object, first_ts: datetime | None) -> int:
    """Return seconds since *first_ts*, or 0 if either timestamp is missing."""
    if first_ts is None:
        return 0
    ts = _parse_ts(raw)
    if ts is None:
        return 0
    return max(0, int((ts - first_ts).total_seconds()))


def _group_by_message(
    raw_events: list[TimelineEvent],
) -> list[list[TimelineEvent]]:
    """Group flat event list into per-message groups (tool_calls follow their parent)."""
    grouped: list[list[TimelineEvent]] = []
    current: list[TimelineEvent] = []
    for ev in raw_events:
        if ev.role == "tool_call":
            current.append(ev)
        else:
            if current:
                grouped.append(current)
            current = [ev]
    if current:
        grouped.append(current)
    return grouped


def _build_events(
    msg_rows: list[object],
    tc_rows: list[object],
    first_ts: datetime | None,
) -> list[TimelineEvent]:
    """Convert DB rows to a flat ordered list of TimelineEvent."""
    by_message: dict[str, list[tuple[object, str, str]]] = {}
    for tc in tc_rows:
        row = tc  # sqlite3.Row
        mid = str(row["message_id"])  # type: ignore[index]
        by_message.setdefault(mid, []).append(
            (row["started_at"], str(row["name"]), str(row["input_json"]))  # type: ignore[index]
        )

    events: list[TimelineEvent] = []
    for m in msg_rows:
        row_m = m  # sqlite3.Row
        mid = str(row_m["id"])  # type: ignore[index]
        content = "" if row_m["content"] is None else str(row_m["content"])  # type: ignore[index]
        preview = content.replace("\n", " ")[:_PREVIEW_LEN]
        events.append(
            TimelineEvent(
                offset_seconds=_ts_offset(row_m["timestamp"], first_ts),  # type: ignore[index]
                role=str(row_m["role"]),  # type: ignore[index]
                tool=None,
                preview=preview,
            )
        )
        for tc_ts, tc_name, tc_input in by_message.get(mid, []):
            tc_preview = tc_input.replace("\n", " ")[:_PREVIEW_LEN]
            fallback_ts = row_m["timestamp"]  # type: ignore[index]
            events.append(
                TimelineEvent(
                    offset_seconds=_ts_offset(
                        tc_ts if tc_ts is not None else fallback_ts, first_ts
                    ),
                    role="tool_call",
                    tool=tc_name,
                    preview=tc_preview,
                )
            )
    return events


def build_timeline(
    db: Database,
    session_id: str,
    *,
    from_message: int | None = None,
    to_message: int | None = None,
) -> tuple[list[TimelineEvent], dict[str, Any]]:
    """Return ``(events, meta)`` for ``convo inspect --timeline``.

    ``meta`` keys: ``project``, ``duration_seconds``, ``message_count``,
    ``tool_call_count``.
    """
    ro = open_ro(db.path)
    try:
        header_row = ro.execute(
            "SELECT project_path, started_at, ended_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if header_row is None:
            raise RuntimeError(_ERR_NO_MATCH.format(prefix=session_id))

        project: str | None = (
            None if header_row["project_path"] is None else str(header_row["project_path"])
        )

        msg_rows = ro.execute(
            "SELECT id, role, timestamp, content, seq "
            "FROM messages WHERE session_id = ? "
            "ORDER BY seq, timestamp",
            (session_id,),
        ).fetchall()

        tc_rows = ro.execute(
            "SELECT message_id, name, input_json, started_at, seq "
            "FROM tool_calls WHERE session_id = ? "
            "ORDER BY message_id, seq",
            (session_id,),
        ).fetchall()
    finally:
        ro.close()

    message_count = len(msg_rows)
    tool_call_count = len(tc_rows)

    first_ts: datetime | None = None
    if msg_rows:
        first_ts = _parse_ts(msg_rows[0]["timestamp"])
    if first_ts is None:
        first_ts = _parse_ts(header_row["started_at"])

    events = _build_events(msg_rows, tc_rows, first_ts)

    if from_message is not None or to_message is not None:
        grouped = _group_by_message(events)
        lo = (from_message - 1) if from_message is not None else 0
        hi = to_message if to_message is not None else len(grouped)
        events = [ev for group in grouped[lo:hi] for ev in group]

    last_ts: datetime | None = None
    if msg_rows:
        last_ts = _parse_ts(msg_rows[-1]["timestamp"])
    duration_seconds = 0
    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        duration_seconds = int((last_ts - first_ts).total_seconds())

    meta: dict[str, Any] = {
        "project": project,
        "duration_seconds": duration_seconds,
        "message_count": message_count,
        "tool_call_count": tool_call_count,
    }
    return events, meta
