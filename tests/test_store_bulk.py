"""Batched evidence reads.

`evidence_many` used to call `evidence` per id, and `evidence` opens a
connection each time. A Harness is built twice per work unit and collects every
evidence id in the graph, so a 4,000-item graph spent 3.7s per unit on
connection setup alone, growing with the graph. These tests pin the batched
behaviour: same results, one query per chunk, and no per-id connections.
"""

import json
import time
from contextlib import contextmanager

import pytest

from projectflow.store import _SQL_VARIABLES, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state", "scope")


def seed(store, count):
    evidence = {f"ev_{i:05d}": {"id": f"ev_{i:05d}", "text": "t" * 120, "kind": "tool"}
                for i in range(count)}
    store.publish({"events": [], "edges": [], "version": 0, "analysis_status": "partial"},
                  [], {}, evidence, expected_version=0)
    return evidence


def test_returns_every_requested_item(store):
    seed(store, 50)
    out = store.evidence_many([f"ev_{i:05d}" for i in range(50)])
    assert len(out) == 50
    assert out["ev_00000"]["text"] == "t" * 120


def test_deduplicates_and_ignores_unknown_and_empty(store):
    seed(store, 10)
    ids = [f"ev_{i:05d}" for i in range(10)]
    assert len(store.evidence_many(ids + ids)) == 10
    assert store.evidence_many([]) == {}
    assert store.evidence_many(["does-not-exist"]) == {}
    assert set(store.evidence_many(ids + ["does-not-exist"])) == set(ids)


def test_batches_beyond_the_sql_variable_limit(store, monkeypatch):
    """A chunk boundary is where a batched query silently loses rows.

    Asserting the statement count is what makes this discriminate: the old
    per-id implementation also returned every row, so a result-only assertion
    would have passed against the code being fixed. One connection is opened
    outside the loop; the chunking shows up as several statements on it.
    """
    chunk = _SQL_VARIABLES - 10
    count = chunk * 2 + 100          # at least three batches
    seed(store, count)
    ids = [f"ev_{i:05d}" for i in range(count)]
    seen: list[str] = []
    real = store.connection

    @contextmanager
    def traced():
        with real() as db:
            db.set_trace_callback(seen.append)
            yield db

    monkeypatch.setattr(store, "connection", traced)
    out = store.evidence_many(ids)
    assert len(out) == count
    selects = [s for s in seen if "evidence_items" in s and s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 3, f"expected 3 batches for {count} ids at {chunk}/batch, got {len(selects)}"
    assert count > 2 * chunk


def test_a_batch_boundary_does_not_lose_or_duplicate_rows(store):
    """Compare against the per-id lookup at exactly the boundary."""
    chunk = _SQL_VARIABLES - 10
    seed(store, chunk + 1)
    ids = [f"ev_{i:05d}" for i in range(chunk + 1)]
    out = store.evidence_many(ids)
    assert len(out) == len(ids)
    assert set(out) == set(ids)


def test_falsey_stored_rows_are_not_reported(store):
    """The per-id read this replaced skipped falsey values; keep that contract.

    A caller that reads a missing key treats it as "no evidence", so reporting a
    `null` or `{}` row would change behaviour rather than match the old code.
    """
    with store.connection() as db, db:
        for key, value in (("ev_null", None), ("ev_empty", {}), ("ev_zero", 0)):
            db.execute("INSERT OR REPLACE INTO evidence_items VALUES (?,?)", (key, json.dumps(value)))
        db.execute("INSERT OR REPLACE INTO evidence_items VALUES (?,?)",
                   ("ev_real", json.dumps({"id": "ev_real", "text": "t"})))
    out = store.evidence_many(["ev_null", "ev_empty", "ev_zero", "ev_real", "ev_absent"])
    assert set(out) == {"ev_real"}, out
    assert store.evidence("ev_null") is None       # the single lookup agrees


def test_uses_one_connection_not_one_per_id(store, monkeypatch):
    """The defect being fixed, asserted directly rather than by timing."""
    seed(store, 300)
    opened = []
    real = store.connection

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(store, "connection", counting)
    out = store.evidence_many([f"ev_{i:05d}" for i in range(300)])
    assert len(out) == 300
    assert len(opened) == 1, f"expected a single connection, opened {len(opened)}"


def test_large_graph_read_is_not_quadratic_in_ids(store):
    """A wall-clock bound, generous enough not to flake on a slow machine.

    The old implementation opened ~4,000 connections for 4,000 ids and took
    seconds. A single query cannot be order-of-magnitude slower, so this
    catches a regression to per-id reads without asserting a precise time.
    """
    seed(store, 4000)
    ids = [f"ev_{i:05d}" for i in range(4000)]
    start = time.perf_counter()
    out = store.evidence_many(ids)
    elapsed = time.perf_counter() - start
    assert len(out) == 4000
    assert elapsed < 2.0, f"4,000 ids took {elapsed:.2f}s; expected a batched read"


def test_order_of_results_does_not_depend_on_input_order(store):
    seed(store, 20)
    ids = [f"ev_{i:05d}" for i in range(20)]
    assert list(store.evidence_many(ids)) == list(store.evidence_many(list(reversed(ids))))


def test_matches_evidence_for_every_id(store):
    """Chunking must not change what a single lookup returns."""
    seed(store, 1200)
    ids = [f"ev_{i:05d}" for i in range(0, 1200, 37)]
    batched = store.evidence_many(ids)
    for evidence_id in ids:
        assert batched[evidence_id] == store.evidence(evidence_id)
