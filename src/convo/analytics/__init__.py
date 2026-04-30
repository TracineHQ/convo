"""Analytics aggregations over the indexed convo DB.

Read-only, stdlib-only. Each `stats_*` family returns a frozen dataclass.
"""

from __future__ import annotations

from convo.analytics.diff import (
    Delta,
    DeltaReport,
    DiffReport,
    WindowSnapshot,
    compute_diff,
)
from convo.analytics.stats_commands import CommandFreq, CommandsReport, stats_commands
from convo.analytics.stats_files import FileActivity, FilesReport, stats_files
from convo.analytics.stats_model import ModelCount, ModelReport, stats_model
from convo.analytics.stats_sessions import SessionsReport, stats_sessions
from convo.analytics.stats_tools import (
    ToolDurationStat,
    ToolErrorRate,
    ToolFreq,
    ToolsReport,
    stats_tools,
)
from convo.analytics.summary import SummaryReport, gather_summary

__all__ = [
    "CommandFreq",
    "CommandsReport",
    "Delta",
    "DeltaReport",
    "DiffReport",
    "FileActivity",
    "FilesReport",
    "ModelCount",
    "ModelReport",
    "SessionsReport",
    "SummaryReport",
    "ToolDurationStat",
    "ToolErrorRate",
    "ToolFreq",
    "ToolsReport",
    "WindowSnapshot",
    "compute_diff",
    "gather_summary",
    "stats_commands",
    "stats_files",
    "stats_model",
    "stats_sessions",
    "stats_tools",
]
