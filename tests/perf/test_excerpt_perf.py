"""Smoke-test that 600-char excerpts don't blow latency vs 20-char baseline."""

from __future__ import annotations

import statistics
import time

import pytest

from convo.db import Database
from convo.read.search import search


@pytest.mark.perf
def test_excerpt_latency_within_2x_baseline(seeded_db_path: str) -> None:
    """600-char excerpts should not exceed 2x the 20-char baseline latency."""
    with Database(seeded_db_path) as db:

        def time_search(excerpt_chars: int) -> float:
            runs = []
            for _ in range(5):
                t0 = time.perf_counter()
                list(search(db, "the", excerpt_chars=excerpt_chars, limit=20))
                runs.append(time.perf_counter() - t0)
            return statistics.median(runs)

        baseline = time_search(20)
        target = time_search(600)
        assert target < baseline * 2 or target < 0.01, (
            f"600-char excerpts {target:.4f}s > 2x 20-char baseline {baseline:.4f}s"
        )
