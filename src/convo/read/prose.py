"""Structured-prose renderer for search and discovery output.

The format is RFC-822-ish: each record is ``--- hit N of M ---`` followed by
one ``label: value`` line per field, then a multi-line body. Designed for
agent consumption -- graceful truncation, no JSON parsing, field labels in
user vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from convo.read.search import SearchHit

_INDENT = " " * 9  # aligns continuation lines under the ``match`` body
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


@dataclass(frozen=True, slots=True)
class SearchRenderConfig:
    """Knobs for prose rendering. All optional."""

    fields: list[str] = field(default_factory=list)
    """When set, render projection-mode: one TSV line per hit with these
    fields, no record dividers. Empty list = full prose format."""

    show_total: bool = True
    """Append ``N hits across K sessions, J projects.`` footer line."""

    next_step_hint: bool = True
    """Append ``Inspect a session: convo inspect <session> --timeline`` footer."""


def render_search_hits(
    *,
    query: str,
    hits: list[SearchHit],
    total: int,
    config: SearchRenderConfig,
    suggestions: list[tuple[str, str]] | None = None,
) -> str:
    """Render a search result list as structured prose.

    ``suggestions`` is a list of ``(kind, description)`` tuples emitted on
    zero-hit results. Ignored when hits is non-empty. Returns the full
    rendered string (no trailing newline).
    """
    if config.fields:
        return _render_projection(hits, config.fields)

    if not hits:
        return _render_zero(query, suggestions or [])

    parts: list[str] = []
    parts.append(f"convo search {query!r} -- {total} hits")
    parts.append("")

    for i, hit in enumerate(hits, start=1):
        parts.append(f"--- hit {i} of {total} ---")
        parts.append(f"session: {hit.session_id[:8]}")
        parts.append(f"when:    {_fmt_when(hit.timestamp)}")
        if hit.project:
            parts.append(f"project: {_fmt_project(hit.project)}")
        parts.append(f"kind:    {_fmt_kind(hit)}")
        body_label = _body_label(hit.kind)
        body = _indent_continuations(hit.excerpt)
        parts.append(f"{body_label}: {body}")
        parts.append("")

    if config.show_total or config.next_step_hint:
        if parts[-1] == "":
            parts.pop()
        parts.append("")
        footer_bits: list[str] = []
        if config.show_total:
            unique_sessions = len({h.session_id for h in hits})
            unique_projects = len({h.project for h in hits if h.project})
            footer_bits.append(
                f"{len(hits)} hits across {unique_sessions} sessions, {unique_projects} projects."
            )
        if config.next_step_hint:
            footer_bits.append("Inspect a session: convo inspect <session> --timeline")
        parts.append(" ".join(footer_bits))

    return "\n".join(parts)


def _render_zero(query: str, suggestions: list[tuple[str, str]]) -> str:
    out = [f"convo search {query!r} -- 0 hits."]
    if suggestions:
        out.append("")
        out.append("Suggestions:")
        for _kind, description in suggestions:
            out.append(f"- {description}")
    return "\n".join(out)


def _render_projection(hits: list[SearchHit], fields: list[str]) -> str:
    rows = []
    for hit in hits:
        row = [_project_field(hit, f) for f in fields]
        rows.append("\t".join(row))
    return "\n".join(rows)


def _project_field(hit: SearchHit, name: str) -> str:
    fn = _PROJECTION_FIELD_FNS.get(name)
    if fn is None:
        return ""
    return fn(hit)


def _fmt_when(ts: str | None) -> str:
    if ts is None:
        return "unknown"
    # Trim fractional seconds: "2026-05-12T14:23:45.123Z" -> "2026-05-12 14:23:45Z"
    if "." in ts:
        ts = ts.split(".")[0] + "Z" if ts.endswith("Z") else ts.split(".")[0]
    return ts.replace("T", " ")


def _fmt_project(path: str) -> str:
    if "/" in path:
        return path.rsplit("/", 1)[-1] or path
    return path


def _fmt_kind(hit: SearchHit) -> str:
    role = hit.role
    tool_origin = hit.tool_origin
    if hit.kind == "message" and role:
        return f"message ({role})"
    if hit.kind == "tool_call" and tool_origin:
        return f"tool_call ({tool_origin})"
    if hit.kind == "tool_result" and tool_origin:
        return f"tool_result (from {tool_origin})"
    return hit.kind


def _body_label(kind: str) -> str:
    return {
        "message": "content",
        "tool_call": "command",
        "tool_result": "output",
    }.get(kind, "match")


def _indent_continuations(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    head, *rest = lines
    if not rest:
        return head
    return head + "\n" + "\n".join(_INDENT + line for line in rest)


_PROJECTION_FIELD_FNS: dict[str, Callable[[SearchHit], str]] = {
    "session": lambda h: h.session_id[:8],
    "ts": lambda h: _fmt_when(h.timestamp),
    "when": lambda h: _fmt_when(h.timestamp),
    "project": lambda h: _fmt_project(h.project) if h.project else "",
    "kind": lambda h: h.kind,
    "excerpt": lambda h: h.excerpt or "",
    "command": lambda h: h.excerpt or "",  # alias for tool_call kind
    "content": lambda h: h.excerpt or "",  # alias for message kind
    "output": lambda h: h.excerpt or "",  # alias for tool_result kind
    "tool": lambda h: h.tool_origin or "",
}


# ---------------------------------------------------------------------------
# Timeline renderer (convo inspect --timeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One event in an inspect --timeline view."""

    offset_seconds: int
    role: str
    tool: str | None
    preview: str


def render_timeline(  # noqa: PLR0913
    *,
    session_id: str,
    project: str | None,
    duration_seconds: int,
    message_count: int,
    tool_call_count: int,
    events: list[TimelineEvent],
    from_message: int | None = None,
    to_message: int | None = None,
) -> str:
    parts: list[str] = []
    parts.append(f"convo inspect {session_id[:8]} --timeline (session: {session_id})")
    parts.append("")
    parts.append(
        f"duration:   {_fmt_duration(duration_seconds)}, "
        f"{message_count} msg, {tool_call_count} tool calls"
    )
    if project:
        parts.append(f"project:    {project}")
    parts.append("")

    for ev in events:
        offset = _fmt_offset(ev.offset_seconds)
        role = ev.role.ljust(9)
        tool = (ev.tool or "").ljust(7)
        parts.append(f"{offset}  {role} {tool} {ev.preview}")

    if from_message is not None or to_message is not None:
        parts.append("")
        parts.append(f"Showing messages from {from_message or 1} of {message_count}.")
    return "\n".join(parts)


def _fmt_offset(seconds: int) -> str:
    hh, rem = divmod(seconds, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _fmt_duration(seconds: int) -> str:
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE} min"
    return (
        f"{seconds // _SECONDS_PER_HOUR}h {(seconds % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE}m"
    )
