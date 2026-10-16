"""Maintain `message_positions`: each message's 1-indexed place in its session.

Position follows MESSAGE_ORDER_BY (seq, timestamp, id; NULL timestamps first),
the order `inspect` numbers messages in. Intake calls `assign_positions` after
inserting a batch, inside the batch's transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# Highest stored position among the session's rows at the largest seq below
# the batch, plus how many of those rows have no stored position.
_LAST_BEFORE = (
    "SELECT MAX(p.position), COUNT(*) - COUNT(p.position) "
    "FROM messages m LEFT JOIN message_positions p ON p.message_id = m.id "
    "WHERE m.session_id = ?1 AND m.seq = "
    "(SELECT MAX(seq) FROM messages WHERE session_id = ?1 AND seq < ?2)"
)
_NUMBERED_FROM = (
    "SELECT 1 FROM messages m JOIN message_positions p ON p.message_id = m.id "
    "WHERE m.session_id = ? AND m.seq >= ? LIMIT 1"
)
_APPEND = (
    "INSERT INTO message_positions(message_id, position) "
    "SELECT id, ?3 + ROW_NUMBER() OVER (ORDER BY seq, timestamp, id) "
    "FROM messages WHERE session_id = ?1 AND seq >= ?2"
)
_RECOMPUTE = (
    "INSERT INTO message_positions(message_id, position) "
    "SELECT id, ROW_NUMBER() OVER (ORDER BY seq, timestamp, id) "
    "FROM messages WHERE session_id = ? "
    "ON CONFLICT(message_id) DO UPDATE SET position = excluded.position "
    "WHERE message_positions.position <> excluded.position"
)


def _append_base(conn: Any, session_id: str, min_new_seq: int) -> int | None:
    """Return the position the batch continues from, or None if it can't append.

    Appending is valid when the rows just before the batch are numbered (so
    the last position is known) and no numbered row sits at or after
    `min_new_seq` (so no new row sorts between or ties with an existing one).
    Both checks are index seeks plus the batch's own rows, not a session scan.
    """
    last, unnumbered = conn.execute(_LAST_BEFORE, (session_id, min_new_seq)).fetchone()
    if unnumbered:
        return None
    if conn.execute(_NUMBERED_FROM, (session_id, min_new_seq)).fetchone() is not None:
        return None
    return int(last or 0)


def assign_positions(conn: Any, batch: Mapping[str, tuple[int, int]]) -> None:
    """Number the rows a batch inserted, per session.

    `batch` maps session_id to `(min_new_seq, source_count)`: the smallest seq
    the batch inserted and how many source files feed the session. A
    single-source session whose new rows all sort after its existing rows
    takes the append path, O(batch): new rows get the last position plus their
    rank in the batch. Anything else (a second source file, rows that land
    between or tie with existing ones, unnumbered rows) renumbers the session.
    """
    for session_id, (min_new_seq, source_count) in batch.items():
        base = _append_base(conn, session_id, min_new_seq) if source_count == 1 else None
        if base is None:
            conn.execute(_RECOMPUTE, (session_id,))
        else:
            conn.execute(_APPEND, (session_id, min_new_seq, base))
