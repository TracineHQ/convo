"""`convo tag-sdk`: fill `sessions.entrypoint` for rows indexed before it existed.

Two passes, planned read-only and then applied in one transaction:

1. From disk. A session with no entrypoint (or only the heuristic tag) whose
   transcript is still on disk takes the first `entrypoint` in that file.
2. By heuristic, within one project. Claude Code's cleanup deletes old
   transcripts, so for many rows the DB is the only copy, and rows indexed
   from a Claude Code that predates the field have nothing to read back. A
   session is tagged `sdk-inferred` only when all of these hold:

   * its entrypoint is unknown and its transcript is gone,
   * it has at most `max_messages` messages,
   * its first user prompt, whitespace-collapsed, is longer than
     `prefix_chars` and its first `prefix_chars` characters (the template)
     are shared by at least `min_shared` such sessions in the project,
   * no session recorded as `cli` in the project starts with that template,
   * the prompt is not a slash-command wrapper (`<command-...`), which only
     interactive use writes.

Nothing is deleted: tagging only changes which rows the read commands count
by default, and `--include-sdk` still shows them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from convo.intake.entrypoint import INFERRED_SDK_ENTRYPOINT, file_entrypoint

if TYPE_CHECKING:
    import sqlite3

DEFAULT_MAX_MESSAGES: int = 10
DEFAULT_MIN_SHARED: int = 20
DEFAULT_PREFIX_CHARS: int = 12
SAMPLE_SIZE: int = 5
# First prompts Claude Code writes for a typed slash command; never a template.
_INTERACTIVE_PROMPT_PREFIXES: tuple[str, ...] = ("<command-", "<local-command-")

# Sessions pass 1 may fill: unknown, or tagged by an earlier heuristic run.
_UNRESOLVED_SQL = (
    "SELECT s.id AS id, sf.path AS path FROM sessions s "
    "JOIN source_files sf ON sf.id = s.source_file_id "
    "WHERE (s.entrypoint IS NULL OR s.entrypoint = ?)"
)
# A session's first user prompt with text (tool-result-only turns have none).
_FIRST_PROMPT_SQL = (
    "(SELECT m.content FROM messages m WHERE m.session_id = s.id AND m.role = 'user' "
    "AND trim(coalesce(m.content, '')) <> '' ORDER BY m.seq, m.timestamp, m.id LIMIT 1)"
)
# The only interpolated value below is the constant `_FIRST_PROMPT_SQL`.
_CANDIDATES_SQL = (
    "SELECT s.id AS id, sf.path AS path, "  # noqa: S608
    "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n_messages, "
    f"{_FIRST_PROMPT_SQL} AS first_prompt "
    "FROM sessions s JOIN source_files sf ON sf.id = s.source_file_id "
    "WHERE s.project_path = ? AND s.entrypoint IS NULL"
)
_CLI_PROMPTS_SQL = (
    f"SELECT {_FIRST_PROMPT_SQL} AS first_prompt FROM sessions s "  # noqa: S608
    "WHERE s.project_path = ? AND s.entrypoint = 'cli'"
)
_SET_ENTRYPOINT = "UPDATE sessions SET entrypoint = ? WHERE id = ?"
_SET_INFERRED = "UPDATE sessions SET entrypoint = ? WHERE id = ? AND entrypoint IS NULL"


@dataclass(frozen=True, slots=True)
class Criteria:
    """Heuristic thresholds (see the module docstring)."""

    max_messages: int = DEFAULT_MAX_MESSAGES
    min_shared: int = DEFAULT_MIN_SHARED
    prefix_chars: int = DEFAULT_PREFIX_CHARS


@dataclass(frozen=True, slots=True)
class TemplateGroup:
    """One first-prompt template and the sessions that start with it."""

    prefix: str
    session_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TagPlan:
    """What `apply_plan` would write. Built read-only by `plan_tags`."""

    project: str | None
    criteria: Criteria
    from_files: dict[str, str] = field(default_factory=dict)
    """session id -> entrypoint read from its transcript on disk."""
    files_checked: int = 0
    """Sessions with an unresolved entrypoint whose transcript is on disk."""
    templates: tuple[TemplateGroup, ...] = ()
    """Templates that qualify; their sessions get `sdk-inferred`."""
    skipped_cli_templates: tuple[TemplateGroup, ...] = ()
    """Templates that met the count but also start a `cli` session."""
    candidates: int = 0
    """Unknown-entrypoint sessions with no transcript and few enough messages."""

    @property
    def inferred_ids(self) -> list[str]:
        return [sid for group in self.templates for sid in group.session_ids]

    @property
    def changes(self) -> int:
        return len(self.from_files) + len(self.inferred_ids)


def template_key(prompt: str | None, prefix_chars: int) -> str | None:
    """Return the prompt's template prefix, or None if it can't be a template."""
    if prompt is None:
        return None
    collapsed = " ".join(prompt.split())
    if len(collapsed) <= prefix_chars or collapsed.startswith(_INTERACTIVE_PROMPT_PREFIXES):
        return None
    return collapsed[:prefix_chars]


def _from_files(
    conn: sqlite3.Connection, project: str | None
) -> tuple[dict[str, str], int, set[str]]:
    """Pass 1: (session -> entrypoint read from disk, files checked, ids whose file exists)."""
    sql, params = _UNRESOLVED_SQL, [INFERRED_SDK_ENTRYPOINT]
    if project is not None:
        sql += " AND s.project_path = ?"
        params.append(project)
    found: dict[str, str] = {}
    on_disk: set[str] = set()
    for row in conn.execute(sql, params):
        path = Path(str(row["path"]))
        if not path.is_file():
            continue
        on_disk.add(str(row["id"]))
        try:
            entrypoint = file_entrypoint(path)
        except OSError:
            continue
        if entrypoint is not None:
            found[str(row["id"])] = entrypoint
    return found, len(on_disk), on_disk


def _templates(
    conn: sqlite3.Connection, project: str, criteria: Criteria, on_disk: set[str]
) -> tuple[tuple[TemplateGroup, ...], tuple[TemplateGroup, ...], int]:
    """Pass 2: (qualifying templates, templates skipped for a cli match, candidate count)."""
    groups: dict[str, list[str]] = {}
    candidates = 0
    for row in conn.execute(_CANDIDATES_SQL, (project,)):
        sid = str(row["id"])
        if sid in on_disk or Path(str(row["path"])).is_file():
            continue
        if int(row["n_messages"]) > criteria.max_messages:
            continue
        candidates += 1
        key = template_key(row["first_prompt"], criteria.prefix_chars)
        if key is not None:
            groups.setdefault(key, []).append(sid)
    cli_keys = {
        template_key(row["first_prompt"], criteria.prefix_chars)
        for row in conn.execute(_CLI_PROMPTS_SQL, (project,))
    }
    keep: list[TemplateGroup] = []
    skipped: list[TemplateGroup] = []
    for key, ids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(ids) < criteria.min_shared:
            continue
        group = TemplateGroup(prefix=key, session_ids=tuple(sorted(ids)))
        (skipped if key in cli_keys else keep).append(group)
    return tuple(keep), tuple(skipped), candidates


def plan_tags(
    conn: sqlite3.Connection,
    *,
    project: str | None,
    criteria: Criteria | None = None,
) -> TagPlan:
    """Work out what to tag, reading only. The heuristic pass needs `project`."""
    criteria = criteria or Criteria()
    from_files, files_checked, on_disk = _from_files(conn, project)
    if project is None:
        return TagPlan(
            project=None, criteria=criteria, from_files=from_files, files_checked=files_checked
        )
    templates, skipped, candidates = _templates(conn, project, criteria, on_disk)
    return TagPlan(
        project=project,
        criteria=criteria,
        from_files=from_files,
        files_checked=files_checked,
        templates=templates,
        skipped_cli_templates=skipped,
        candidates=candidates,
    )


def apply_plan(conn: sqlite3.Connection, plan: TagPlan) -> int:
    """Write `plan` in one transaction; return the number of sessions updated."""
    updated = 0
    with conn:
        for sid, entrypoint in sorted(plan.from_files.items()):
            updated += conn.execute(_SET_ENTRYPOINT, (entrypoint, sid)).rowcount
        for sid in plan.inferred_ids:
            updated += conn.execute(_SET_INFERRED, (INFERRED_SDK_ENTRYPOINT, sid)).rowcount
    return updated
