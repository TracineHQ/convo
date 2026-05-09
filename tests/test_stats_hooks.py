"""Tests for `stats_hooks` analytics family."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from convo.analytics.stats_hooks import stats_hooks
from tests._seed import seed_guard_decision, seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def test_stats_hooks_empty_db(db: Database) -> None:
    report = stats_hooks(db)
    assert report.total == 0
    assert report.top_by_hook == ()
    assert report.by_decision == ()


def test_stats_hooks_counts_by_hook_and_decision(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/g.jsonl")
    seed_guard_decision(db, sfid, line_no=1, hook_id="guard.bash", decision="deny")
    seed_guard_decision(db, sfid, line_no=2, hook_id="guard.bash", decision="allow")
    seed_guard_decision(db, sfid, line_no=3, hook_id="guard.bash", decision="deny")
    seed_guard_decision(db, sfid, line_no=4, hook_id="guard.write", decision="allow")

    report = stats_hooks(db)

    assert report.total == 4

    by_hook = {f.hook_id: f.count for f in report.top_by_hook}
    assert by_hook == {"guard.bash": 3, "guard.write": 1}
    # Most-frequent first.
    assert report.top_by_hook[0].hook_id == "guard.bash"

    by_decision = {f.decision: f.count for f in report.by_decision}
    assert by_decision == {"deny": 2, "allow": 2}


def test_stats_hooks_since_filter(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/g.jsonl")
    seed_guard_decision(db, sfid, line_no=1, timestamp="2020-01-01T00:00:00Z")
    seed_guard_decision(db, sfid, line_no=2, timestamp="2099-01-01T00:00:00Z")

    report = stats_hooks(db, since=timedelta(days=1))

    # Only the future-dated row falls inside the trailing-1d window.
    assert report.total == 1


def test_stats_hooks_project_filter(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/g.jsonl")
    seed_guard_decision(db, sfid, line_no=1, cwd="/proj/A")
    seed_guard_decision(db, sfid, line_no=2, cwd="/proj/A")
    seed_guard_decision(db, sfid, line_no=3, cwd="/proj/B")

    report = stats_hooks(db, project="/proj/A")

    assert report.total == 2
