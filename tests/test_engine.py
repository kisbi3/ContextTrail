import copy
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from projectflow.demo import CASES, FixtureRunner
from projectflow.analysis import _session_unit_chunks
from projectflow.model import Snapshot
from projectflow.schema import EvidenceValidator, EXTRACT_SCHEMA
from projectflow.util import FlowError


def test_no_data_never_constructs_runner(laboratory):
    _, store, engine, _, _ = laboratory
    result = engine.analyze(lambda: pytest.fail("Runner constructed without data"))
    assert result["status"] == "no_data"
    assert result["runner_calls"] == 0


def test_standalone_environment_context_needs_no_model(laboratory):
    _, store, engine, records, make = laboratory
    records.append(replace(make("<environment_context>local date</environment_context>", role="metadata"),
                           lineage={"kind": "environment_context"}))
    result = engine.analyze(lambda: pytest.fail("Environment context must not invoke Runner"))
    assert result["runner_calls"] == 0
    assert store.sources()[records[0].source_id]["processed_hash"] == records[0].content_hash


def test_unit_planner_keeps_interleaved_sessions_together(laboratory):
    _, _, engine, _, make = laboratory
    records = [make("first", key="a1", session="a"),
               make("other", key="b1", session="b"),
               make("second", key="a2", session="a")]
    chunks = _session_unit_chunks(records, engine.config, [])
    assert [[r.source_id for r in chunk] for chunk in chunks] == [["a1", "a2"], ["b1"]]


def test_unit_planner_prefers_compaction_and_long_idle_gap(laboratory):
    _, _, engine, _, make = laboratory
    records = [
        replace(make("earlier", key="a"), recorded_at="2026-09-22T10:00:00Z"),
        replace(make("follow-up", key="b"), recorded_at="2026-09-22T10:20:00Z"),
        replace(make("summary", key="c", role="metadata"),
                recorded_at="2026-09-22T10:21:00Z", lineage={"kind": "compaction"}),
        replace(make("continued", key="d"), recorded_at="2026-09-22T10:22:00Z"),
        replace(make("next visit", key="e"), recorded_at="2026-09-22T14:00:00Z"),
    ]
    chunks = _session_unit_chunks(records, engine.config, [])
    assert [[r.source_id for r in chunk] for chunk in chunks] == [["a", "b"], ["c", "d"], ["e"]]


def test_unit_planner_uses_date_only_after_meaningful_gap(laboratory):
    _, _, engine, _, make = laboratory
    records = [replace(make("late", key="a"), recorded_at="2026-09-22T23:50:00Z"),
               replace(make("still working", key="b"), recorded_at="2026-09-23T00:10:00Z"),
               replace(make("next morning", key="c"), recorded_at="2026-09-23T01:50:00Z")]
    # Midnight alone is not a boundary. The later same-day gap is below three hours.
    assert len(_session_unit_chunks(records, engine.config, [])) == 1
    records[1] = replace(records[1], recorded_at="2026-09-23T00:55:00Z")
    assert [[r.source_id for r in chunk] for chunk in _session_unit_chunks(records, engine.config, [])] == [
        ["a"], ["b", "c"]]


def test_unit_planner_separates_worktrees(laboratory):
    _, _, engine, _, make = laboratory
    records = [make("one", key="a"), replace(make("other worktree", key="b"), worktree_id="another"),
               make("continued", key="c")]
    assert [[r.source_id for r in chunk] for chunk in _session_unit_chunks(records, engine.config, [])] == [
        ["a", "c"], ["b"]]


def test_unit_planner_keeps_tool_pair_and_fragments_at_budget_boundary(laboratory):
    _, _, engine, _, make = laboratory
    engine.config.unit_records = 2
    records = [make("question", key="a"),
               replace(make("call", key="b", role="tool_call"), tool_call_id="call-1"),
               replace(make("result", key="c", role="tool_result"), tool_call_id="call-1"),
               replace(make("part one", key="d"), lineage={"fragment_of": "original"}),
               replace(make("part two", key="e"), lineage={"fragment_of": "original"})]
    chunks = _session_unit_chunks(records, engine.config, [])
    assert [[r.source_id for r in chunk] for chunk in chunks] == [["a"], ["b", "c"], ["d", "e"]]


def test_pipeline_and_noop(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    assert first["status"] == "complete"
    assert first["runner_calls"] == 2
    graph = copy.deepcopy(first["graph"])
    second = engine.analyze(lambda: pytest.fail("No-op must not construct/authenticate a Runner"))
    assert second["status"] == "noop" and second["runner_calls"] == 0
    assert second["graph"] == graph
    assert store.units()[0]["status"] == "integrated"


def test_empty_extraction_is_complete(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make("고맙습니다."))
    result = engine.analyze(FixtureRunner)
    assert result["runner_calls"] == 1  # nothing to integrate, so no second call
    # Every user message is a node, even when the model found nothing in the unit.
    [event] = result["graph"]["events"]
    assert (event["kind"], event["actor"], event["title"]) == ("question", "user", "고맙습니다.")
    assert store.units()[0]["status"] == "integrated"
    assert engine.analyze(lambda: pytest.fail("Empty units must be cached"))["runner_calls"] == 0


def test_append_only_new_records_and_keep_existing_id(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    original_id = first["graph"]["events"][0]["id"]
    records.append(make(CASES[4][0], key="s2"))
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete"
    assert len(result["graph"]["events"]) == 2
    assert result["graph"]["events"][0]["id"] == original_id
    extraction = next(t for t in runner.tasks if t["stage"] == "extract")
    assert extraction["data"]["assigned_source_ids"] == ["s2"]
    assert {r["source_id"] for r in extraction["data"]["new_records"]} == {"s2"}


def test_modified_source_updates_stable_event_and_converges(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    original_id = first["graph"]["events"][0]["id"]
    records[0] = make(CASES[4][0])
    changed = engine.analyze(FixtureRunner)
    assert changed["status"] == "complete", changed
    assert len(changed["graph"]["events"]) == 1
    assert changed["graph"]["events"][0]["id"] == original_id
    assert changed["graph"]["events"][0]["title"] == "SQLite로 전환 결정"
    assert len(changed["graph"]["events"][0]["evidence_ids"]) == 2
    assert engine.analyze(lambda: pytest.fail("Modified source should converge"))["runner_calls"] == 0


def test_non_event_unrelated_unit_is_not_reanalysed(laboratory):
    _, _, engine, records, make = laboratory
    records += [make("안녕하세요", key="unrelated", session="first"), make(CASES[0][0], session="second")]
    engine.analyze(FixtureRunner)
    records[1] = make(CASES[4][0], session="second")
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete"
    assigned = [i for t in runner.tasks if t["stage"] == "extract" for i in t["data"]["assigned_source_ids"]]
    assert assigned == ["s1"]



def test_changed_context_alone_does_not_reanalyse_an_integrated_unit(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.analyze(FixtureRunner)
    records.append(make("추가 내용 없음", key="s2"))
    engine.analyze(FixtureRunner)  # s2's unit was shown s1 as context
    records[0] = make(CASES[4][0])
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assigned = [i for t in runner.tasks if t["stage"] == "extract" for i in t["data"]["assigned_source_ids"]]
    assert assigned == ["s1"]
    assert any("다시 분석합니다" in note and "s1" in note for note in result["limitations"])


def test_a_lost_processed_mark_does_not_resend_an_integrated_unit(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.analyze(FixtureRunner)
    with store.connection() as db, db:
        db.execute("UPDATE source_records SET processed_hash=NULL")
    result = engine.analyze(lambda: pytest.fail("the unit's own records did not change"))
    assert result["status"] == "noop" and result["runner_calls"] == 0
    assert store.sources()["s1"]["processed_hash"] == records[0].content_hash

def test_deleted_source_keeps_graph_and_evidence(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    evidence_id = first["graph"]["events"][0]["evidence_ids"][0]
    records.clear()
    result = engine.analyze(lambda: pytest.fail("Deletion is not a retraction"))
    assert result["status"] == "partial"
    assert result["graph"] == first["graph"]
    assert store.evidence(evidence_id)["quote"] == CASES[0][0]


def test_extraction_survives_integration_failure(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class FailIntegration(FixtureRunner):
        def run(self, task, schema, cancel):
            if task["stage"] == "integrate":
                self.calls += 1
                raise FlowError("intentional integration failure")
            return super().run(task, schema, cancel)
    result = engine.analyze(FailIntegration)
    assert result["status"] == "failed"
    assert store.graph()["version"] == 0
    assert store.units()[0]["status"] == "extracted"
    runner = FixtureRunner()
    recovered = engine.analyze(lambda: runner)
    assert recovered["status"] == "complete", recovered
    assert recovered["reused_extractions"] == 1
    assert runner.calls == 1 and runner.tasks[0]["stage"] == "integrate"
    assert len(store.graph()["events"]) == 1


def test_invalid_quote_is_blocked_after_one_repair(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class BadQuote(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            output["event_candidates"][0]["evidence"][0]["quote"] = "invented quote"
            return output
    runner = BadQuote()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "failed"
    assert runner.calls == 2
    assert store.graph()["version"] == 0
    assert store.sources()["s1"]["processed_hash"] is None


def test_reported_completion_cannot_become_observed_success(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[5][0], role="assistant"))
    class UnsupportedSuccess(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            output["event_candidates"][0]["status"] = "observed_success"
            return output
    result = engine.analyze(UnsupportedSuccess)
    assert result["status"] == "failed"
    assert "tool_result" in result["error"]
    assert store.graph()["events"] == []


def test_runner_change_does_not_reanalyse_integrated_units(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.analyze(FixtureRunner)
    assert engine.analyze(lambda: pytest.fail("Changed runner on unchanged input"))["status"] == "noop"


def test_cancel_before_runner(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    cancel = threading.Event()
    cancel.set()
    result = engine.analyze(lambda: pytest.fail("cancelled"), cancel=cancel)
    assert result["status"] == "cancelled" and result["runner_calls"] == 0
    assert store.graph()["version"] == 0


def test_oversize_record_is_not_processed(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make("x" * 50_000))
    result = engine.analyze(lambda: pytest.fail("oversize excluded"))
    assert result["status"] == "partial"
    assert result["pending_records"] == 1
    assert store.sources()["s1"]["processed_hash"] is None


def test_db_failure_does_not_advance_processed_state(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    with store.connection() as db, db:
        db.execute("CREATE TRIGGER fail_graph BEFORE INSERT ON graph_versions BEGIN SELECT RAISE(FAIL,'test'); END")
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "failed"
    assert store.graph()["version"] == 0
    assert store.sources()["s1"]["processed_hash"] is None
    assert store.units()[0]["status"] == "extracted"
    with store.connection() as db, db:
        db.execute("DROP TRIGGER fail_graph")
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "complete" and result["reused_extractions"] == 1


def test_graph_compare_and_swap(laboratory):
    _, store, _, _, _ = laboratory
    graph = store.graph()
    store.publish(graph, [], {}, {}, expected_version=0)
    with pytest.raises(FlowError, match="version"):
        store.publish(graph, [], {}, {}, expected_version=0)
    assert store.graph()["version"] == 1


def test_single_scope_lock(laboratory):
    _, store, _, _, _ = laboratory
    with store.analyze_lock():
        with pytest.raises(FlowError, match="이미"):
            with store.analyze_lock():
                pytest.fail("second lock acquired")


def test_every_provided_context_is_a_dependency(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.analyze(FixtureRunner)
    records.append(make("추가 내용 없음", key="s2"))
    engine.analyze(FixtureRunner)
    second = next(u for u in store.units() if u["sources"] == ["s2"])
    assert "s1" in second["dependencies"] and "s2" in second["dependencies"]


def test_llm_ops_ledger_records_hashes_attempts_and_no_full_prompt(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "complete"
    calls = store.llm_calls()
    assert [c["stage"] for c in calls] == ["extract", "integrate"]
    for call in calls:
        meta = call["metadata"]
        assert meta["prompt_hash"] and meta["schema_hash"] and meta["input_digest"]
        assert call["status"] == "complete" and call["finished_at"]
        serialized = str(call)
        assert "당신은 프로젝트 이력 분석기다" not in serialized
        assert CASES[0][0] not in serialized


def test_unmatched_partial_quote_repair_receives_exact_source_lines(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class BadQuote(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            output["event_candidates"][0]["evidence"][0]["quote"] = "invented quote"
            return output
    runner = BadQuote()
    engine.analyze(lambda: runner)
    repair = runner.tasks[1]["repair"]
    assert "유일하게 일치하지 않습니다" in repair["instruction"]
    assert repair["exact_source_lines"][0]["lines"][0]["text"] == CASES[0][0].splitlines()[0]


def test_repair_sees_the_quotes_the_model_wrote_not_expanded_lines(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    long_line = "context " * 60 + "distinctive phrase here" + " trailing" * 60
    records.append(make(long_line, key="s2", role="assistant"))
    class Mixed(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "extract" and "repair" not in task:
                first = output["event_candidates"][0]
                partial = {**copy.deepcopy(first), "id": "tmp:partial", "evidence": [
                    {"source_id": "s2", "start_line": 1, "end_line": 1, "quote": "distinctive phrase here"}]}
                first["evidence"][0]["quote"] = "invented quote"
                output["event_candidates"].append(partial)
            return output
    runner = Mixed()
    engine.analyze(lambda: runner)
    previous = runner.tasks[1]["repair"]["previous_output"]
    # The valid partial quote was expanded during validation; the repair still sees the original.
    assert previous["event_candidates"][1]["evidence"][0]["quote"] == "distinctive phrase here"
    assert long_line not in json.dumps(previous, ensure_ascii=False)


def test_a_run_shows_what_it_would_send_before_sending(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    plans = []
    result = engine.analyze(lambda: pytest.fail("declined"), consent=lambda snapshot, plan: plans.append(plan) or False)
    assert result["status"] == "cancelled" and result["runner_calls"] == 0
    [plan] = plans
    assert (plan["units"], plan["units_this_run"], plan["max_calls"]) == (1, 1, 30)
    assert plan["input_tokens_this_run"] > 30_000
