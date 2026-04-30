"""`convo summary` — composes the five `stats_*` families into a single dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.analytics.stats_commands import CommandsReport, stats_commands
from convo.analytics.stats_files import FilesReport, stats_files
from convo.analytics.stats_model import ModelReport, stats_model
from convo.analytics.stats_sessions import SessionsReport, stats_sessions
from convo.analytics.stats_tools import ToolsReport, stats_tools

if TYPE_CHECKING:
    from datetime import timedelta

    from convo.db import Database


@dataclass(frozen=True, slots=True)
class SummaryReport:
    """Aggregate of all five stats families over the same (since, project) window."""

    since: timedelta | None
    project: str | None
    tools: ToolsReport
    commands: CommandsReport
    sessions: SessionsReport
    files: FilesReport
    model: ModelReport


def gather_summary(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> SummaryReport:
    """Compose the five `stats_*` families into a single SummaryReport."""
    tools = stats_tools(db, since=since, project=project)
    commands = stats_commands(db, since=since, project=project)
    sessions = stats_sessions(db, since=since, project=project)
    files = stats_files(db, since=since, project=project)
    model = stats_model(db, since=since, project=project)
    return SummaryReport(
        since=since,
        project=project,
        tools=tools,
        commands=commands,
        sessions=sessions,
        files=files,
        model=model,
    )
