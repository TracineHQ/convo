"""Tests for `stats_model` analytics family."""

from __future__ import annotations

from typing import TYPE_CHECKING

from convo.analytics import stats_model
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def _insert_session(db: Database, sfid: int, *, sid: str, model: str | None) -> None:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, model) VALUES (?, ?, ?)",
        (sid, sfid, model),
    )
    db.conn.commit()


def test_stats_model_histogram_and_null_count(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/m.jsonl")
    _insert_session(db, sfid, sid="s1", model="opus-4")
    _insert_session(db, sfid, sid="s2", model="opus-4")
    _insert_session(db, sfid, sid="s3", model="opus-4")
    _insert_session(db, sfid, sid="s4", model="sonnet-4")
    _insert_session(db, sfid, sid="s5", model=None)

    report = stats_model(db)
    assert report.total_sessions == 5
    assert report.null_count == 1
    counts = {m.model: m.session_count for m in report.by_model}
    assert counts == {"opus-4": 3, "sonnet-4": 1}
    # Sorted descending by count
    assert report.by_model[0].model == "opus-4"


def test_stats_model_empty_string_treated_as_null(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/e.jsonl")
    _insert_session(db, sfid, sid="s1", model="")
    _insert_session(db, sfid, sid="s2", model="opus-4")
    report = stats_model(db)
    assert report.null_count == 1
    counts = {m.model: m.session_count for m in report.by_model}
    assert counts == {"opus-4": 1}


def test_stats_model_empty_db(db: Database) -> None:
    report = stats_model(db)
    assert report.total_sessions == 0
    assert report.null_count == 0
    assert report.by_model == ()
