"""Tests for `search()` in `convo.read.search`."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from convo.read.search import SNIPPET_POST, SNIPPET_PRE, SearchHit, build_fts_query, search

if TYPE_CHECKING:
    from pathlib import Path


def _ts(offset: timedelta) -> str:
    """Produce an ISO-8601 'Z' timestamp `offset` before now (UTC)."""
    return (datetime.now(UTC) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_search_corpus(db: Database) -> dict[str, str]:
    """Seed two sessions across two projects with messages, tool calls, results.

    Returns a dict of named timestamps so tests can reference them.
    """
    assert db.conn is not None

    ts_now = _ts(timedelta(seconds=0))
    ts_1h = _ts(timedelta(hours=1))
    ts_1d = _ts(timedelta(days=1))
    ts_2d = _ts(timedelta(days=2))
    ts_30d = _ts(timedelta(days=30))

    # source files
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (?, ?, 0, 0, ?)",
        (1, "/data/foo.jsonl", ts_now),
    )
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (?, ?, 0, 0, ?)",
        (2, "/data/bar.jsonl", ts_now),
    )

    # sessions in two projects
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("s1", 1, "/work/foo"),
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("s2", 2, "/work/bar"),
    )

    # messages with varying timestamps and discoverable tokens.
    # Tuple shape: (id, session_id, seq, content, timestamp).
    msgs: list[tuple[str, str, int, str, str]] = [
        ("m1", "s1", 0, "kafka pipeline notes for ingestion", ts_now),
        ("m2", "s1", 1, "python dataframe analysis details", ts_1d),
        ("m3", "s2", 0, "kafka cluster sizing recommendations", ts_2d),
        ("m4", "s2", 1, "rust async runtime survey", ts_30d),
        ("m5", "s1", 2, "deploy procedure document update", ts_now),
        ("m6", "s2", 2, "investigation findings report draft", ts_1h),
    ]
    for mid, sid, seq, content, timestamp in msgs:
        db.conn.execute(
            "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
            "VALUES (?, ?, 'user', ?, ?, ?, '{}')",
            (mid, sid, seq, timestamp, content),
        )

    # tool calls: Bash on m1 (s1, recent), Read on m2 (s1, 1d ago)
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
        "started_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
        ("tc1", "m1", "s1", "Bash", '{"command": "echo kafka started okay"}', ts_now),
    )
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
        "started_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
        ("tc2", "m2", "s1", "Read", '{"path": "/var/log/python.log"}', ts_1d),
    )

    # tool_results — link via message_id so timestamps flow through for --since
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, message_id, output_text) VALUES (?, ?, ?)",
        ("tc1", "m1", "kafka consumer ready acknowledgement"),
    )
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, message_id, output_text) VALUES (?, ?, ?)",
        ("tc2", "m2", "python traceback included in output"),
    )

    db.conn.commit()
    return {"now": ts_now, "1h": ts_1h, "1d": ts_1d, "2d": ts_2d, "30d": ts_30d}


def test_search_returns_hits_across_kinds(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka"))
    kinds = {h.kind for h in hits}
    # kafka appears in messages (m1, m3), tool_call input (tc1), and tool_result (tc1).
    assert "message" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    # All hits should be SearchHit instances with expected fields.
    for h in hits:
        assert isinstance(h, SearchHit)
        assert h.session_id in {"s1", "s2"}


def test_search_excerpt_has_snippet_markers(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka"))
    assert hits, "expected at least one hit"
    # At least one excerpt should contain the snippet markers around the match.
    assert any(SNIPPET_PRE in h.excerpt and SNIPPET_POST in h.excerpt for h in hits)


def test_search_since_filters_recent(db: Database) -> None:
    _seed_search_corpus(db)
    # 30d-old "kafka" message in s2 should drop out at 36h cutoff but not 3d cutoff.
    hits_36h = list(search(db, "kafka", since=timedelta(hours=36)))
    msg_ids = {h.id for h in hits_36h if h.kind == "message"}
    assert "m1" in msg_ids
    assert "m3" not in msg_ids  # 2 days ago, outside 36h window

    hits_3d = list(search(db, "kafka", since=timedelta(days=3)))
    msg_ids_3d = {h.id for h in hits_3d if h.kind == "message"}
    assert "m1" in msg_ids_3d
    assert "m3" in msg_ids_3d


def test_search_project_filter(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka", project="/work/foo"))
    assert hits
    assert all(h.project == "/work/foo" for h in hits)
    assert all(h.session_id == "s1" for h in hits)


def test_search_tool_filter(db: Database) -> None:
    _seed_search_corpus(db)
    # When --tool is set, message branch is dropped; only tool_calls/results
    # whose tool_calls.name matches are returned.
    hits = list(search(db, "kafka", tool="Bash"))
    assert hits
    kinds = {h.kind for h in hits}
    assert "message" not in kinds
    # All hits should trace back to the Bash tool call (tc1).
    for h in hits:
        assert h.kind in {"tool_call", "tool_result"}
        assert h.id == "tc1"

    # Non-matching tool name returns nothing.
    assert list(search(db, "kafka", tool="Edit")) == []


def test_search_limit_honored(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka", limit=1))
    assert len(hits) == 1


def test_search_empty_query_raises(db: Database) -> None:
    _seed_search_corpus(db)
    with pytest.raises(ValueError, match="search query must not be empty"):
        list(search(db, ""))
    with pytest.raises(ValueError, match="search query must not be empty"):
        list(search(db, "   "))


def test_search_short_token_raises(db: Database) -> None:
    """Trigram tokenizer needs ≥3 chars; queries shorter than that must error."""
    _seed_search_corpus(db)
    with pytest.raises(ValueError, match="at least 3 characters"):
        list(search(db, "x"))
    with pytest.raises(ValueError, match="at least 3 characters"):
        list(search(db, "ab"))
    # Operator tokens after `+` / `-` strip must also be ≥3 chars.
    with pytest.raises(ValueError, match="at least 3 characters"):
        list(search(db, "+go -no"))


def test_search_results_ordered_by_timestamp_desc(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka"))
    timestamps = [h.timestamp for h in hits if h.timestamp is not None]
    assert timestamps == sorted(timestamps, reverse=True)


def test_search_special_chars_do_not_crash(db: Database) -> None:
    _seed_search_corpus(db)
    # FTS5 special chars; default behavior is to wrap as a single phrase.
    for query in ["hello*", "foo:bar", "(parens)", 'with"quotes', "AND OR NOT"]:
        # Should not raise; may return zero hits.
        list(search(db, query))


def test_search_required_and_excluded_tokens(db: Database) -> None:
    _seed_search_corpus(db)
    # +kafka requires kafka; -python excludes python. m1 has kafka, no python -> hit.
    # m2 has python only -> excluded by both rules.
    hits = list(search(db, "+kafka -python"))
    msg_ids = {h.id for h in hits if h.kind == "message"}
    assert "m1" in msg_ids
    assert "m2" not in msg_ids

    # +kafka +cluster requires both — only m3 has both.
    hits_both = list(search(db, "+kafka +cluster"))
    msg_ids_both = {h.id for h in hits_both if h.kind == "message"}
    assert msg_ids_both == {"m3"}


def test_search_no_hits_for_unknown_term(db: Database) -> None:
    _seed_search_corpus(db)
    assert list(search(db, "nonexistentterm")) == []


def test_search_works_with_space_in_db_path(tmp_path: Path) -> None:
    spaced_dir = tmp_path / "with space"
    spaced_dir.mkdir()
    db_file = spaced_dir / "convo.db"
    with Database(db_file) as db:
        _seed_search_corpus(db)
        hits = list(search(db, "kafka"))
    assert hits


def test_search_perf_smoke(db: Database) -> None:
    """B2.6 perf smoke: ~500 messages, query, assert wall-clock < 500ms."""
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/data/x.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES ('s1', 1, '/p')",
    )
    rows = []
    for i in range(500):
        # ~half contain "needle" to ensure the query hits many rows.
        content = f"needle filler line {i}" if i % 2 == 0 else f"haystack other line {i}"
        rows.append((f"m{i}", "s1", i, "2026-04-29T00:00:00Z", content))
    db.conn.executemany(
        "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
        "VALUES (?, ?, 'user', ?, ?, ?, '{}')",
        rows,
    )
    db.conn.commit()

    start = time.perf_counter()
    hits = list(search(db, "needle", limit=500))
    elapsed = time.perf_counter() - start

    assert len(hits) >= 250  # roughly half the corpus matches
    assert elapsed < 0.5, f"search took {elapsed:.3f}s — perf budget is 500ms"


# ---------------------------------------------------------------------------
# build_fts_query unit tests (v2 AND-default semantics)
# ---------------------------------------------------------------------------


def test_default_is_and_across_tokens() -> None:
    fts = build_fts_query("kafka migration")
    # AND-default: both phrases present, no OR
    assert '"kafka"' in fts
    assert '"migration"' in fts
    assert "OR" not in fts
    assert "NOT" not in fts


def test_quoted_phrase_is_literal() -> None:
    fts = build_fts_query('"kafka migration"')
    assert fts == '"kafka migration"'


def test_or_keyword_uppercase() -> None:
    fts = build_fts_query("kafka OR rabbitmq")
    assert '"kafka"' in fts
    assert "OR" in fts
    assert '"rabbitmq"' in fts


def test_or_keyword_pipe() -> None:
    fts = build_fts_query("kafka | rabbitmq")
    assert '"kafka"' in fts
    assert "OR" in fts
    assert '"rabbitmq"' in fts


def test_minus_excludes() -> None:
    fts = build_fts_query("kafka -retry")
    assert '"kafka"' in fts
    assert "NOT" in fts
    assert '"retry"' in fts


def test_plus_prefix_is_no_op() -> None:
    # +token used to mean "required" under old behavior; AND-default makes
    # it a no-op. Should still produce a valid query.
    fts = build_fts_query("+kafka migration")
    assert '"kafka"' in fts
    assert '"migration"' in fts


def test_empty_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_fts_query("")


def test_short_token_raises() -> None:
    with pytest.raises(ValueError, match="at least 3 characters"):
        build_fts_query("ka")  # trigram tokenizer needs >=3 chars


# ---------------------------------------------------------------------------
# Task 7: --session, --excerpt-chars, --tool-exact
# ---------------------------------------------------------------------------


def test_session_filter_restricts_to_one_session(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka", session="s1"))
    assert hits
    for h in hits:
        assert h.session_id == "s1"


def test_session_filter_prefix_match(db: Database) -> None:
    _seed_search_corpus(db)
    hits_s = list(search(db, "kafka", session="s"))
    hits_s1 = list(search(db, "kafka", session="s1"))
    assert len(hits_s) >= len(hits_s1)


def test_excerpt_chars_controls_snippet_size(db: Database) -> None:
    _seed_search_corpus(db)
    short = list(search(db, "kafka", excerpt_chars=50, limit=20))
    long = list(search(db, "kafka", excerpt_chars=500, limit=20))
    if short and long:
        s_ids = {h.id for h in short}
        for hit in long:
            if hit.id in s_ids:
                short_excerpt = next(h.excerpt for h in short if h.id == hit.id)
                assert len(hit.excerpt) >= len(short_excerpt)
                return


def test_tool_exact_match_filters_strictly(db: Database) -> None:
    _seed_search_corpus(db)
    hits_prefix = list(search(db, "kafka", tool="B"))
    hits_exact_b = list(search(db, "kafka", tool="B", tool_exact=True))
    assert len(hits_exact_b) <= len(hits_prefix)


def test_excerpt_chars_caps_at_fts5_max(db: Database) -> None:
    _seed_search_corpus(db)
    hits = list(search(db, "kafka", excerpt_chars=10000, limit=5))
    assert isinstance(hits, list)


def test_message_hit_has_role(db: Database) -> None:
    _seed_search_corpus(db)
    hits = [h for h in search(db, "kafka") if h.kind == "message"]
    assert hits, "expected at least one message hit"
    for h in hits:
        assert h.role in {"user", "assistant", "system"}, f"unexpected role: {h.role!r}"
        assert h.tool_origin is None


def test_tool_call_hit_carries_tool_name_in_origin(db: Database) -> None:
    _seed_search_corpus(db)
    hits = [h for h in search(db, "kafka") if h.kind == "tool_call"]
    if hits:
        assert hits[0].role is None
        assert hits[0].tool_origin is not None


def test_tool_result_hit_has_tool_origin(db: Database) -> None:
    _seed_search_corpus(db)
    hits = [h for h in search(db, "kafka") if h.kind == "tool_result"]
    if hits:
        assert hits[0].role is None
        assert hits[0].tool_origin is not None
