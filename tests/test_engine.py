import copy
import json
import shutil
import sqlite3
import threading
from dataclasses import replace

import pytest

from contexttrail.analysis import (AnalysisConfig, Engine, EVIDENCE_POLICY_REUSE, _session_unit_chunks,
                                   build_task, prompt)
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.model import Snapshot
from contexttrail.schema import (DELTA_ITEM_ARRAYS, DELTA_SCHEMA, PATCH_ARRAYS, REUSABLE_EVIDENCE_SECTIONS,
                                REVIEW_PATCH_SCHEMA, EvidenceValidator, EXTRACT_SCHEMA, merge_review_patch,
                                review_patch_audit)
from contexttrail.store import Store
from contexttrail.util import FlowError, dumps


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


def _step(make, key, hint_text, call_id, kind="Bash"):
    call = replace(make(f"Tool: {kind}\n" + json.dumps({"command": hint_text}), key=f"{key}c", role="tool_call"),
                   tool_call_id=call_id)
    result = replace(make("ok", key=f"{key}r", role="tool_result"), tool_call_id=call_id)
    return [call, result]


def _ids(chunks):
    return [[r.source_id for r in chunk] for chunk in chunks]


def test_unit_planner_cuts_after_a_commit_before_a_person_message(laboratory):
    _, _, engine, _, make = laboratory
    engine.config.unit_records = 7
    records = [make("fix it", key="a"), *_step(make, "e", "pytest -q", "1"),
               *_step(make, "g", "git commit -m x", "2"),
               make("done", key="n", role="assistant"), make("next task", key="u"),
               make("ok", key="v", role="assistant")]
    # The window ends after "next task" (a turn cut), but the commit result is the better cut.
    assert _ids(_session_unit_chunks(records, engine.config, []))[0] == ["a", "ec", "er", "gc", "gr"]


def test_unit_planner_cuts_after_a_run_result_when_nothing_better(laboratory):
    _, _, engine, _, make = laboratory
    engine.config.unit_records = 7
    records = [make("go", key="a", role="assistant"), make("edit", key="b", role="assistant"),
               *_step(make, "t", "pytest -q", "1"),
               *[make(f"talk {i}", key=f"x{i}", role="assistant") for i in range(6)]]
    # Edit, run and result stay together; the piece ends right after the result, not mid-chatter.
    assert _ids(_session_unit_chunks(records, engine.config, []))[0] == ["a", "b", "tc", "tr"]


def test_unit_planner_ignores_read_results_and_failed_commits_as_cut_points(laboratory):
    _, _, engine, _, make = laboratory
    engine.config.unit_records = 6
    read = _step(make, "r", "cat README.md", "1")
    records = [make("go", key="a", role="assistant"), *read,
               *[make(f"talk {i}", key=f"x{i}", role="assistant") for i in range(6)]]
    assert _ids(_session_unit_chunks(records, engine.config, []))[0] == ["a", "rc", "rr", "x0", "x1", "x2"]
    commit = _step(make, "g", "git commit -m x", "2")
    commit[1] = replace(commit[1], content="error: nothing to commit")
    records = [make("go", key="a", role="assistant"), *commit,
               *[make(f"talk {i}", key=f"x{i}", role="assistant") for i in range(6)]]
    # Still a run result (a cut after it is fine) but never ranked as a commit.
    assert _ids(_session_unit_chunks(records, engine.config, []))[0][:3] == ["a", "gc", "gr"]


def test_unit_planner_does_not_leave_a_sliver_for_a_meaningful_cut(laboratory):
    _, _, engine, _, make = laboratory
    engine.config.unit_records = 10
    records = [*_step(make, "g", "git commit -m x", "1"),
               *[make(f"talk {i} " + "y" * 40, key=f"x{i}", role="assistant") for i in range(12)]]
    assert len(_ids(_session_unit_chunks(records, engine.config, []))[0]) == 10


def test_unit_planner_window_follows_the_estimated_payload(laboratory):
    from contexttrail.analysis import estimated_payload, payload_budget
    _, _, engine, _, make = laboratory
    engine.config.task_chars = 100_000
    records = [make("x" * 1000, key=f"a{i}", role="assistant") for i in range(50)]
    chunks = _session_unit_chunks(records, engine.config, [])
    assert len(chunks) > 1
    assert all(estimated_payload(sum(len(r.content) for r in c), len(c)) <= payload_budget(engine.config)
               for c in chunks)
    engine.config.context_mode = "full"
    full = _session_unit_chunks(records, engine.config, [])
    assert len(full) > len(chunks)
    assert all(estimated_payload(sum(len(r.content) for r in c), len(c), "full") <= payload_budget(engine.config)
               for c in full)


def test_unit_planner_ids_do_not_depend_on_the_budget_only_on_the_records(laboratory):
    from contexttrail.util import ident
    _, _, engine, _, make = laboratory
    records = [make(f"m{i}", key=f"a{i}", role="assistant") for i in range(4)]
    first = _ids(_session_unit_chunks(records, engine.config, []))
    assert first == _ids(_session_unit_chunks(list(records), engine.config, []))
    assert ident("unit_", [(r.source_id, r.content_hash) for r in records]) == ident(
        "unit_", [(r.source_id, r.content_hash) for r in list(records)])


def test_stored_unpaid_units_are_regrouped_and_paid_ones_are_kept(laboratory):
    _, store, engine, records, make = laboratory
    records += [make(f"m{i}", key=f"a{i}", role="assistant") for i in range(6)]
    snapshot = engine.scan()
    store.ingest(snapshot.records)
    ids = [r.source_id for r in records]
    store.save_unit("unit_old", ids[:4], {}, "parsed")
    store.save_unit("unit_paid", ids[4:], {}, "draft", {"cached": True})
    engine.config.unit_records = 2
    issues = []
    plans, _, _ = engine._plan_units(snapshot, issues, repair=True)
    by_id = {u["id"]: u for u in plans}
    assert "unit_paid" in by_id and by_id["unit_paid"]["status"] == "draft"
    assert "unit_old" not in by_id
    assert sorted(i for u in plans if u["id"] != "unit_paid" for i in u["sources"]) == ids[:4]
    assert all(len(u["sources"]) <= 2 for u in plans if u["id"] != "unit_paid")
    assert {u["id"]: u["status"] for u in store.units()}["unit_old"] == "superseded"
    assert any("regrouped" in i and "under the current rule" in i for i in issues)
    # A preview never writes.
    store.save_unit("unit_old", ids[:4], {}, "parsed")
    engine._plan_units(snapshot, [])
    assert {u["id"]: u["status"] for u in store.units()}["unit_old"] == "parsed"


def test_session_filter_does_not_supersede_units_of_other_sessions(laboratory):
    _, store, engine, records, make = laboratory
    records += [make(f"a{i}", key=f"a{i}", session="a", role="assistant") for i in range(3)]
    records += [make(f"b{i}", key=f"b{i}", session="b", role="assistant") for i in range(3)]
    snapshot = engine.scan()
    store.ingest(snapshot.records)
    by_session = {s: [r.source_id for r in records if r.session_id == s] for s in ("a", "b")}
    store.save_unit("unit_a", by_session["a"], {}, "parsed")
    store.save_unit("unit_b", by_session["b"], {}, "parsed")
    engine.config.unit_records = 2
    engine.config.session = "a"
    plans, _, _ = engine._plan_units(snapshot, [], repair=True)
    assert all(pool_id in by_session["a"] for u in plans for pool_id in u["sources"])
    statuses = {u["id"]: u["status"] for u in store.units()}
    assert statuses["unit_a"] == "superseded" and statuses["unit_b"] == "parsed"


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
    assert any("re-analyzing an already integrated work unit" in note and "s1" in note
               for note in result["limitations"])


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
    assert "quote not found uniquely in the cited lines" in repair["instruction"]
    assert repair["exact_source_lines"][0]["lines"][0]["text"] == CASES[0][0].splitlines()[0]


def test_repair_receives_the_nearest_provided_lines_to_copy_from(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class BadQuote(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "extract" and "repair" not in task:
                output["event_candidates"][0]["evidence"][0]["quote"] = "우선 JSON 파일로 저장하겠습니다"
            return output
    runner = BadQuote()
    engine.analyze(lambda: runner)
    assert runner.tasks[1]["repair"]["nearest_lines"] == [
        {"source_id": "s1", "line": 1, "text": CASES[0][0], "clipped": False}]


def test_ledger_names_the_failed_quote_category_without_its_text(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class BadQuote(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "extract" and "repair" not in task:
                output["event_candidates"][0]["evidence"][0]["quote"] = "우선 JSON 파일로 저장하겠습니다"
            return output
    engine.analyze(lambda: BadQuote())
    failed, repaired, integrated = store.llm_calls()
    [mismatch] = failed["details"]["quote_mismatch_audit"]
    assert (failed["status"], repaired["status"], integrated["status"]) == (
        "validation_error", "complete", "complete")
    assert mismatch == {"source_id": "s1", "lines": [1, 1], "category": "not_found",
                        "quote_chars": len("우선 JSON 파일로 저장하겠습니다")}
    assert "우선 JSON 파일로 저장하겠습니다" not in json.dumps(failed, ensure_ascii=False)
    assert repaired["details"]["quote_mismatch_audit"] == []  # the audit counts one attempt only


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


def _capturing_analyze(engine):
    """Run the pipeline and keep every request and response schema the runner was given."""
    seen = []

    class Watching(FixtureRunner):
        def run(self, task, schema, cancel):
            seen.append((copy.deepcopy(task), schema))
            return super().run(task, schema, cancel)

    return engine.analyze(Watching), seen


def _rerun(scope, records, config):
    """The same records in the same project, analysed again from an empty state directory."""
    shutil.rmtree(scope.state_dir)
    store = Store(scope.state_dir, scope.id)
    engine = Engine(scope, store, config)
    engine.scan = lambda: Snapshot(list(records))
    return engine, store


def _claims(store):
    """What the graph says, with run-scoped IDs read as titles so two runs can be compared."""
    graph = store.graph()
    titles = {event["id"]: event["title"] for event in graph["events"]}
    return ([(e["title"], e["kind"], e["status"], e["actor"], e["basis"], e["summary"], e["evidence_ids"])
             for e in graph["events"]],
            sorted((titles.get(e["from_event_id"], e["from_event_id"]),
                    titles.get(e["to_event_id"], e["to_event_id"]), e["relation"], e["basis"], e["active"],
                    tuple(e["evidence_ids"])) for e in graph["edges"]),
            sorted(store.evidence_many({i for e in graph["events"] for i in e["evidence_ids"]})))


def _integrate_quote_chars(store):
    return [call["details"]["output_quote_chars"] for call in store.llm_calls() if call["stage"] == "integrate"]


def test_default_evidence_policy_sends_no_evidence_instruction(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    [(integrate, schema)] = [item for item in seen if item[0]["stage"] == "integrate"]
    assert all("evidence_policy" not in task["data"] for task, _ in seen)
    assert (integrate["instructions"], integrate["wire_contract"], integrate["system"]) == (
        prompt("integrate"), build_task("integrate", {})["wire_contract"], prompt("common"))
    assert schema is DELTA_SCHEMA


def test_reuse_policy_adds_only_the_evidence_line_to_the_integrate_request(laboratory):
    scope, _, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant")])
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    full_task, full_schema = next((task, schema) for task, schema in seen if task["stage"] == "integrate")
    full_extract = next(task for task, _ in seen if task["stage"] == "extract")

    reuse_engine, _ = _rerun(scope, records, replace(engine.config, integrate_evidence="reuse"))
    result, seen = _capturing_analyze(reuse_engine)
    assert result["status"] == "complete", result
    reuse_task, reuse_schema = next((task, schema) for task, schema in seen if task["stage"] == "integrate")
    assert next(task for task, _ in seen if task["stage"] == "extract") == full_extract

    # Apart from that one line, the request a runner sees is the same bytes as before.
    assert reuse_task["data"].pop("evidence_policy") == EVIDENCE_POLICY_REUSE
    assert dumps(reuse_task) == dumps(full_task)
    assert reuse_schema is not DELTA_SCHEMA
    assert {name: reuse_schema["properties"][name]["items"]["properties"]["evidence"]["minItems"]
            for name in REUSABLE_EVIDENCE_SECTIONS} == dict.fromkeys(REUSABLE_EVIDENCE_SECTIONS, 0)
    # Nothing else is relaxed: the sections no candidate can back still demand a quote.
    assert reuse_schema["properties"]["open_items_to_upsert"]["items"]["properties"]["evidence"]["minItems"] == 1
    assert reuse_schema["properties"]["edges_to_invalidate"]["items"]["properties"]["evidence"]["minItems"] == 1
    assert reuse_schema["properties"]["review_issues"]["items"]["properties"]["evidence"]["minItems"] == 1


def test_reuse_mode_publishes_the_same_graph_as_full_mode(laboratory):
    scope, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant")])
    full = engine.analyze(FixtureRunner)
    assert full["status"] == "complete", full
    full_claims, full_quotes = _claims(store), _integrate_quote_chars(store)
    assert not [item for call in store.llm_calls() for item in call["details"]["citation_normalization_audit"]
                if item["mode"] == "evidence_reused_from_candidates"]

    reuse_engine, reuse_store = _rerun(scope, records, replace(engine.config, integrate_evidence="reuse"))
    reuse = reuse_engine.analyze(FixtureRunner)
    assert reuse["status"] == "complete", reuse
    assert _claims(reuse_store) == full_claims
    reused = [item for call in reuse_store.llm_calls() if call["stage"] == "integrate"
              for item in call["details"]["citation_normalization_audit"]
              if item["mode"] == "evidence_reused_from_candidates"]
    assert [item["items"] for item in reused] == [7]  # every candidate-linked item, once
    quoted = sum(sum(chars.values()) for chars in _integrate_quote_chars(reuse_store))
    assert 0 < quoted < sum(sum(chars.values()) for chars in full_quotes)


# The default review instruction, spelled out: changing it must be a deliberate change.
FULL_REVIEW_INSTRUCTION = (
    "Compare the proposed GraphDelta with the supplied original evidence and relevant history. Check "
    "duplicate versus genuine retry, state overstatement, and unsupported relation invalidation. Return the "
    "complete corrected GraphDelta and preserve one resolution for every candidate.")


def _revision_review(laboratory):
    """A decision and the revision that supersedes it: the revision's missing revises link sends it to review."""
    scope, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[4][0], key="s2")])
    return scope, store, engine


def _review_calls(store):
    return [call for call in store.llm_calls() if call["metadata"].get("routing_role") == "integrate_review"]


def _review_chars(store):
    return sum(call["details"]["output_chars"] for call in _review_calls(store))


def _review_statuses(store):
    return [(item["status"], [row["status"] for row in item["resolutions"]])
            for item in store.graph()["semantic_review_history"]]


def test_full_review_output_keeps_the_full_delta_request_bytes(laboratory):
    _, store, engine = _revision_review(laboratory)
    engine.config.review_output = "full"
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    (_, extract_schema), (integrate_task, integrate_schema), (review_task, review_schema) = seen
    # The review is the integration request plus the four review keys: the same bytes, byte for byte.
    added = {"review_trigger", "review_issues", "proposed_graph_delta", "review_instruction"}
    assert set(review_task["data"]) - set(integrate_task["data"]) == added
    assert {key: review_task["data"][key] for key in integrate_task["data"]} == integrate_task["data"]
    assert review_task["data"]["review_instruction"] == FULL_REVIEW_INSTRUCTION
    assert review_task["data"]["review_issues"] and review_task["data"]["proposed_graph_delta"]["events_to_add"]
    assert {key: review_task[key] for key in ("system", "instructions", "wire_contract", "stage", "output_language")} == \
        {key: integrate_task[key] for key in ("system", "instructions", "wire_contract", "stage", "output_language")}
    assert review_schema is integrate_schema is DELTA_SCHEMA and extract_schema is EXTRACT_SCHEMA
    assert "patch" not in review_schema["properties"] and "remove" not in review_task["data"]
    assert _review_calls(store)[0]["details"]["output_chars"] > 0


def test_review_output_is_patch_by_default_and_only_full_or_patch(laboratory):
    _, _, engine = _revision_review(laboratory)
    assert engine.config.review_output == "patch"
    # It changes only the review request, so a finished extraction is never resent.
    signature = engine._routing_signature()
    engine.config.review_output = "full"
    assert engine._routing_signature() == signature
    with pytest.raises(FlowError, match="review_output"):
        AnalysisConfig(review_output="diff").validate()


def test_review_patch_publishes_the_same_graph_as_full_mode(laboratory):
    scope, store, engine = _revision_review(laboratory)
    engine.config.review_output = "full"
    full = engine.analyze(FixtureRunner)
    assert full["status"] == "complete", full
    full_claims, full_statuses, full_chars = _claims(store), _review_statuses(store), _review_chars(store)

    patch_engine, patch_store = _rerun(scope, [record for record in engine.scan().records],
                                       replace(engine.config, review_output="patch"))
    patch = patch_engine.analyze(FixtureRunner)
    assert patch["status"] == "complete", patch
    assert _claims(patch_store) == full_claims
    assert _review_statuses(patch_store) == full_statuses == [("reviewed", ["resolved"])]
    # The review answered one added relation instead of the whole delta again.
    assert _review_chars(patch_store) < full_chars
    merged = [item for call in _review_calls(patch_store)
              for item in call["details"]["citation_normalization_audit"]
              if item["mode"] == "review_patch_merged"]
    assert merged == [{"mode": "review_patch_merged", "replaced": 0, "added": 2, "removed": 0}]


def test_review_patch_asks_for_its_own_schema_and_instruction(laboratory):
    _, _, engine = _revision_review(laboratory)
    engine.config.review_output = "patch"
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    review_task, review_schema = seen[2]
    integrate_task, integrate_schema = seen[1]
    assert review_schema is REVIEW_PATCH_SCHEMA
    assert not review_schema is DELTA_SCHEMA and "patch" in review_schema["properties"]
    assert set(review_schema["properties"]["patch"]["properties"]) == set(PATCH_ARRAYS)
    assert review_schema["properties"]["remove"]["items"]["properties"]["operation"]["enum"] == list(DELTA_ITEM_ARRAYS)
    instruction = review_task["data"]["review_instruction"]
    assert instruction != FULL_REVIEW_INSTRUCTION
    for clause in ("Return only what changes", "replaces that item", "in remove",
                   "candidate_resolutions only for candidates whose resolution changes",
                   "change_attributions only for added or replaced items", "never an unchanged one"):
        assert clause in instruction
    # Everything else the review is sent is the integration request.
    assert {key: review_task["data"][key] for key in integrate_task["data"]} == integrate_task["data"]


def test_review_patch_rejects_removing_an_item_the_proposal_never_had(laboratory):
    _, store, engine = _revision_review(laboratory)
    engine.config.review_output = "patch"
    review_tasks, review_schemas = [], []

    class UnknownRemove(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if "patch" in schema["properties"]:
                review_tasks.append(copy.deepcopy(task))
                review_schemas.append(schema)
                output["remove"] = [{"operation": "events_to_add", "item_id": "tmp:invented"}]
            return output

    result = engine.analyze(UnknownRemove)
    assert result["status"] == "failed"
    assert "cannot remove an item that is not in the proposed delta" in result["error"]
    assert [call["status"] for call in _review_calls(store)] == ["validation_error", "validation_error"]
    assert store.graph()["version"] == 0
    # The one repair round asks for the same patch and sees the patch the model wrote, not the merge.
    [first, repair] = review_tasks
    assert "repair" not in first and review_schemas[0] is review_schemas[1] is REVIEW_PATCH_SCHEMA
    assert repair["repair"]["previous_output"]["remove"] == [{"operation": "events_to_add", "item_id": "tmp:invented"}]
    assert "patch" in repair["repair"]["previous_output"] and "events_to_add" in repair["repair"]["previous_output"]["patch"]


def test_review_patch_leaving_a_candidate_unresolved_is_rejected(laboratory):
    _, store, engine = _revision_review(laboratory)
    engine.config.review_output = "patch"

    class UnresolvedCandidate(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if "patch" in schema["properties"] and "repair" not in task:
                output["patch"]["candidate_resolutions"] = [
                    {**copy.deepcopy(item), "disposition": "excluded", "target_ids": []}
                    for item in task["data"]["proposed_graph_delta"]["candidate_resolutions"]]
            return output

    result = engine.analyze(UnresolvedCandidate)
    assert result["status"] == "complete", result  # the repair round put the candidate back
    assert _review_statuses(store) == [("reviewed", ["resolved"])]
    failures = [call["details"]["error"] for call in _review_calls(store) if call["status"] == "validation_error"]
    assert ["change attribution not linked to its GraphDelta change (" in error
            for error in failures] == [True]
    # The merged delta is what got published, so the event the review disowned is not there twice.
    assert [event["title"] for event in store.graph()["events"]] == ["JSON 저장 채택", "SQLite로 전환 결정"]


def test_merge_review_patch_replaces_adds_and_removes_items():
    proposed = {"status": "complete", "read_requests": [], "snapshot_id": "snap", "base_graph_version": 3,
        "events_to_add": [{"id": "tmp:a", "title": "old"}, {"id": "tmp:b", "title": "b"}],
        "events_to_update": [], "edges_to_add": [{"id": "tmp:e"}], "edges_to_invalidate": [],
        "open_items_to_upsert": [], "open_items_to_resolve": [],
        "candidate_resolutions": [{"candidate_id": "tmp:a", "candidate_kind": "event"},
                                  {"candidate_id": "tmp:b", "candidate_kind": "event"}],
        "change_attributions": [{"operation": "events_to_add", "item_id": "tmp:a"},
                                {"operation": "events_to_add", "item_id": "tmp:b"},
                                {"operation": "edges_to_add", "item_id": "tmp:e"}],
        "review_issues": [], "review_resolutions": [], "limitations": ["kept"]}
    answer = {"status": "complete", "read_requests": [], "snapshot_id": "snap", "base_graph_version": 3,
        "patch": {"events_to_add": [{"id": "tmp:b", "title": "new"}, {"id": "tmp:c"}],
                  "events_to_update": [], "edges_to_add": [], "edges_to_invalidate": [],
                  "open_items_to_upsert": [], "open_items_to_resolve": [],
                  "candidate_resolutions": [{"candidate_id": "tmp:b", "candidate_kind": "event", "reason": "고침"}],
                  "change_attributions": [], "review_issues": []},
        "remove": [{"operation": "events_to_add", "item_id": "tmp:a"}],
        "review_resolutions": [{"issue_id": "r1", "status": "modified"}], "limitations": ["added"]}

    merged = merge_review_patch(proposed, answer)
    assert [item["id"] for item in merged["events_to_add"]] == ["tmp:b", "tmp:c"]
    assert merged["events_to_add"][0]["title"] == "new"      # the replaced item keeps its place
    assert merged["edges_to_add"] == proposed["edges_to_add"]  # untouched sections stay the proposal's
    assert [item["item_id"] for item in merged["change_attributions"]] == ["tmp:b", "tmp:e"]
    assert [item["reason"] for item in merged["candidate_resolutions"] if item["candidate_id"] == "tmp:b"] == ["고침"]
    assert merged["review_resolutions"] == answer["review_resolutions"]
    assert merged["limitations"] == ["kept", "added"]
    assert proposed["events_to_add"][0]["title"] == "old"  # the proposal itself is never edited
    assert review_patch_audit(proposed, answer) == {"replaced": 2, "added": 1, "removed": 1}
    with pytest.raises(FlowError, match="cannot remove an item that is not in the proposed delta"):
        merge_review_patch(proposed, {**answer, "remove": [{"operation": "edges_to_add", "item_id": "tmp:none"}]})
    with pytest.raises(FlowError, match="names a proposed item of another kind"):
        merge_review_patch(proposed, {**answer, "patch": {**answer["patch"],
                                                           "edges_to_add": [{"id": "tmp:a"}]}})


def test_review_patch_bookkeeping_with_one_reading_is_settled_in_code():
    from contexttrail.schema import reconcile_review_patch
    empty = {"events_to_update": [], "edges_to_invalidate": [], "open_items_to_upsert": [], "open_items_to_resolve": []}
    cite = {"source_id": "s1", "start_line": 1, "end_line": 1, "quote": "the run passed"}
    proposed = {**empty, "events_to_add": [{"id": "tmp:a", "evidence": [cite]}], "edges_to_add": [{"id": "tmp:e"}]}
    merged = {**empty,
        "events_to_add": [{"id": "tmp:c", "evidence": []}],          # the review removed tmp:a and added tmp:c
        "edges_to_add": [{"id": "tmp:e"}],
        "candidate_resolutions": [
            {"candidate_id": "c1", "candidate_kind": "event", "disposition": "added", "target_ids": ["tmp:a"], "reason": "r"},
            {"candidate_id": "c2", "candidate_kind": "event", "disposition": "added", "target_ids": ["tmp:c"], "reason": "r"},
            {"candidate_id": "c3", "candidate_kind": "edge", "disposition": "added", "target_ids": ["tmp:e"], "reason": "r"}],
        "change_attributions": [{"operation": "events_to_add", "item_id": "tmp:a", "candidate_ids": ["c1"]},
                                {"operation": "edges_to_add", "item_id": "tmp:e", "candidate_ids": ["c3"]}]}
    candidates = {"event_candidates": [{"id": "c2", "evidence": [cite]}], "edge_candidates": [], "open_items": []}
    counts = reconcile_review_patch(merged, proposed, candidates)
    assert counts == {"stale_attributions": 1, "excluded_resolutions": 1, "added_attributions": 1}
    assert [(a["operation"], a["item_id"], a["candidate_ids"]) for a in merged["change_attributions"]] == [
        ("edges_to_add", "tmp:e", ["c3"]), ("events_to_add", "tmp:c", ["c2"])]
    assert merged["change_attributions"][1]["evidence"] == [cite]
    assert merged["candidate_resolutions"][0]["disposition"] == "excluded"
    assert merged["candidate_resolutions"][0]["target_ids"] == []
    orphan = {**empty, "events_to_add": [{"id": "tmp:x", "evidence": [cite]}], "edges_to_add": [],
              "candidate_resolutions": [], "change_attributions": []}
    assert reconcile_review_patch(orphan, {**empty, "events_to_add": [], "edges_to_add": []}, None)["added_attributions"] == 0
    assert orphan["change_attributions"] == []  # no candidate points at it: left for the checks to reject


def _integrate_calls(store):
    return [call for call in store.llm_calls() if call["metadata"].get("routing_role") == "integrate"]


def test_integrate_output_is_draft_by_default_and_full_sends_no_draft(laboratory):
    import dataclasses
    assert {f.name: f.default for f in dataclasses.fields(AnalysisConfig)}["integrate_output"] == "draft"
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    assert engine.config.integrate_output == "full"  # pinned for these tests by conftest
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    [(integrate, schema)] = [item for item in seen if item[0]["stage"] == "integrate"]
    assert "draft_graph_delta" not in integrate["data"] and "integrate_instruction" not in integrate["data"]
    assert schema is DELTA_SCHEMA
    with pytest.raises(FlowError, match="integrate_output"):
        AnalysisConfig(integrate_output="skip").validate()


def test_integrate_patch_publishes_the_same_graph_as_full_mode(laboratory):
    scope, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant")])
    full = engine.analyze(FixtureRunner)
    assert full["status"] == "complete", full
    full_claims = _claims(store)
    full_chars = sum(call["details"]["output_chars"] for call in _integrate_calls(store))

    patch_engine, patch_store = _rerun(scope, records, replace(engine.config, integrate_output="patch"))
    result, seen = _capturing_analyze(patch_engine)
    assert result["status"] == "complete", result
    assert _claims(patch_store) == full_claims
    [(task, schema)] = [item for item in seen if item[0]["stage"] == "integrate"]
    assert schema is REVIEW_PATCH_SCHEMA
    assert "return only what changes" in task["data"]["integrate_instruction"]
    assert task["data"]["draft_graph_delta"]["events_to_add"]
    # The integrator wrote what the draft lacked, not the whole delta again.
    assert sum(call["details"]["output_chars"] for call in _integrate_calls(patch_store)) < full_chars
    assert [item["mode"] for call in _integrate_calls(patch_store)
            for item in call["details"]["citation_normalization_audit"]
            if item["mode"].endswith("_merged")] == ["integrate_patch_merged"]


def test_integrate_draft_needs_no_call_while_the_graph_is_empty(laboratory):
    scope, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant")])
    draft_engine, draft_store = _rerun(scope, records, replace(engine.config, integrate_output="draft"))
    result, seen = _capturing_analyze(draft_engine)
    assert result["status"] == "complete", result
    assert not [task for task, _ in seen if task["stage"] == "integrate" and "review_issues" not in task["data"]]
    assert not _integrate_calls(draft_store)
    graph = draft_store.graph()
    extracted = [task for task, _ in seen if task["stage"] == "extract"]
    assert graph["version"] == 1 and graph["events"] and extracted


def test_draft_delta_follows_one_existing_match_and_drops_a_link_it_folds_onto_itself():
    from contexttrail.schema import draft_delta
    quote = [{"source_id": "s", "start_line": 1, "end_line": 1, "quote": "q"}]
    event = lambda i: {"id": i, "kind": "action", "title": i, "summary": "", "actor": "assistant",
                       "status": "applied", "basis": "tool_record", "session_ids": [], "worktree_ids": [],
                       "recorded_at": None, "occurred_at": None, "evidence": quote}
    edge = lambda i, a, b: {"id": i, "from_event_id": a, "to_event_id": b, "relation": "motivates",
                            "basis": "explicit", "evidence": quote, "rationale": "", "active": True}
    candidates = {"event_candidates": [event("tmp:a"), event("tmp:b"), event("tmp:c")],
                  "edge_candidates": [edge("tmp:e1", "tmp:a", "tmp:b"), edge("tmp:e2", "tmp:a", "ev_old")],
                  "existing_event_matches": [{"candidate_id": "tmp:a", "existing_event_id": "ev_old",
                                              "reason": "", "evidence": quote},
                                             {"candidate_id": "tmp:c", "existing_event_id": "ev_x", "reason": "", "evidence": quote},
                                             {"candidate_id": "tmp:c", "existing_event_id": "ev_y", "reason": "", "evidence": quote}],
                  "open_items": [{"id": "tmp:o", "text": "t", "status": "open", "related_event_ids": ["tmp:a"],
                                  "evidence": quote}], "limitations": ["L"]}
    delta = draft_delta(candidates, 3, "snap")
    assert [e["id"] for e in delta["events_to_add"]] == ["tmp:b", "tmp:c"]  # two matches: no single reading
    assert [(e["id"], e["from_event_id"], e["to_event_id"]) for e in delta["edges_to_add"]] == [
        ("tmp:e1", "ev_old", "tmp:b")]
    assert delta["open_items_to_upsert"][0]["related_event_ids"] == ["ev_old"]
    assert {(r["candidate_id"], r["disposition"], tuple(r["target_ids"])) for r in delta["candidate_resolutions"]} == {
        ("tmp:a", "duplicate", ("ev_old",)), ("tmp:b", "added", ("tmp:b",)), ("tmp:c", "added", ("tmp:c",)),
        ("tmp:e1", "added", ("tmp:e1",)), ("tmp:e2", "excluded", ()), ("tmp:o", "added", ("tmp:o",))}
    assert sorted((a["operation"], a["item_id"]) for a in delta["change_attributions"]) == [
        ("edges_to_add", "tmp:e1"), ("events_to_add", "tmp:b"), ("events_to_add", "tmp:c"),
        ("open_items_to_upsert", "tmp:o")]
    assert (delta["base_graph_version"], delta["limitations"]) == (3, ["L"])


def test_a_duplicate_of_an_item_the_same_delta_adds_is_folded_into_it():
    validator = EvidenceValidator({}, {})
    delta = {"events_to_add": [{"id": "tmp:a"}], "edges_to_add": [], "open_items_to_upsert": [],
             "candidate_resolutions": [
                 {"candidate_id": "tmp:x", "candidate_kind": "event", "disposition": "duplicate", "target_ids": ["tmp:a"]},
                 {"candidate_id": "tmp:y", "candidate_kind": "event", "disposition": "duplicate", "target_ids": ["ev_old"]},
                 {"candidate_id": "tmp:z", "candidate_kind": "event", "disposition": "updated",
                  "target_ids": ["tmp:a", "ev_old"]}]}
    assert validator.settle_in_delta_duplicates(delta) == 1
    assert [row["disposition"] for row in delta["candidate_resolutions"]] == ["added", "duplicate", "updated"]
    assert validator.normalizations == [{"mode": "in_delta_duplicate_as_added", "items": 1}]



def test_a_relation_no_candidate_names_is_attributed_to_the_event_candidates_at_its_ends(laboratory):
    _, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant"),
                    make(CASES[5][0], key="s3", role="assistant")])
    strays = []

    class StrayAttribution(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "integrate" and output["edges_to_add"]:
                edge = output["edges_to_add"][0]
                ends = {edge["from_event_id"], edge["to_event_id"]}
                stray = next((r["candidate_id"] for r in output["candidate_resolutions"]
                              if r["candidate_kind"] == "event" and not set(r["target_ids"]) & ends), None)
                assert stray, "the fixture needs an event away from the relation"
                for item in output["change_attributions"]:
                    if item["item_id"] == edge["id"]:
                        item["candidate_ids"] = [stray]
                        strays.append(stray)
            return output

    result = engine.analyze(StrayAttribution)
    assert result["status"] == "complete", result.get("error")
    assert strays
    audit = [item for call in store.llm_calls() if call["stage"] == "integrate"
             for item in call["details"]["citation_normalization_audit"]]
    assert [item["operation"] for item in audit if item["mode"] == "attribution_candidates_from_endpoints"] == [
        "edges_to_add"]


def test_a_new_event_may_close_an_old_open_item_it_was_not_opened_on(laboratory):
    _, store, _, _, make = laboratory
    record = make("The flaky upload test now passes after the retry fix.", role="assistant")
    cite = [{"source_id": record.source_id, "start_line": 1, "end_line": 1,
             "quote": "The flaky upload test now passes after the retry fix."}]
    graph = store.graph()
    graph["events"] = [dict(id="ev_old", title="old", summary="", kind="question", status="asked", actor="user",
                            basis="explicit_statement", session_ids=[], worktree_ids=[], evidence_ids=[],
                            recorded_at=None, occurred_at=None)]
    graph["open_items"] = [dict(id="open_old", text="flaky upload", status="open", related_event_ids=["ev_old"],
                                evidence_ids=[])]
    event = {"id": "tmp:e", "kind": "outcome", "title": "업로드 테스트 통과 보고", "summary": "", "actor": "assistant",
             "status": "reported_complete", "basis": "explicit_statement", "session_ids": [], "worktree_ids": [],
             "recorded_at": None, "occurred_at": None, "evidence": cite}
    candidates = {"event_candidates": [event], "edge_candidates": [], "existing_event_matches": [],
                  "open_items": [], "limitations": []}
    delta = {"status": "complete", "read_requests": [], "snapshot_id": "snap", "base_graph_version": graph["version"],
             "events_to_add": [copy.deepcopy(event)], "events_to_update": [], "edges_to_add": [],
             "edges_to_invalidate": [], "open_items_to_upsert": [],
             "open_items_to_resolve": [{"id": "open_old", "reason": "통과 보고", "evidence": cite}],
             "candidate_resolutions": [{"candidate_id": "tmp:e", "candidate_kind": "event", "disposition": "added",
                                        "target_ids": ["tmp:e"], "reason": "r", "evidence": cite}],
             "change_attributions": [
                 {"operation": "events_to_add", "item_id": "tmp:e", "candidate_ids": ["tmp:e"], "reason": "r",
                  "evidence": cite},
                 {"operation": "open_items_to_resolve", "item_id": "open_old", "candidate_ids": ["tmp:e"],
                  "reason": "r", "evidence": cite}],
             "review_issues": [], "review_resolutions": [], "limitations": []}
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 1)]},
                                  assigned_source_ids={record.source_id})
    result = validator.apply_delta(delta, graph, "snap", "run", candidates)
    assert [(item["id"], item["status"]) for item in result["open_items"]] == [("open_old", "resolved")]
