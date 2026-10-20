import copy
import json
import threading
from dataclasses import replace

import pytest

from contexttrail.analysis import _FIXED_TOKENS, _TYPICAL_RATIO, _session_unit_chunks, build_task, prompt
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.light import STORY_SCHEMA
from contexttrail.schema import EvidenceValidator
from contexttrail.util import FlowError


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


def _sent(task):
    """The records a story call was asked about: those whose writing it was shown, as blocks."""
    return sorted({block["source_id"] for block in task["data"]["written"]})


def test_pipeline_and_noop(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    assert first["status"] == "complete"
    assert first["runner_calls"] == 1  # the story call is the unit's only call
    graph = copy.deepcopy(first["graph"])
    second = engine.analyze(lambda: pytest.fail("No-op must not construct/authenticate a Runner"))
    assert second["status"] == "noop" and second["runner_calls"] == 0
    assert second["graph"] == graph
    assert store.units()[0]["status"] == "integrated"


def test_empty_extraction_is_complete(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make("고맙습니다."))
    result = engine.analyze(FixtureRunner)
    assert result["runner_calls"] == 1
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
    [story] = runner.tasks
    assert [r["source_id"] for r in story["data"]["user_requests"]] == ["s2"]
    assert _sent(story) == ["s2"]


def test_modified_source_updates_the_stable_event_and_converges(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    original_id = first["graph"]["events"][0]["id"]
    records[0] = make(CASES[4][0])
    runner = FixtureRunner()
    changed = engine.analyze(lambda: runner)
    assert changed["status"] == "complete", changed
    assert runner.calls == 1 and _sent(runner.tasks[0]) == ["s1"]  # the changed record is read again
    # The request read again is the same kind citing the same record: the existing event, not a second one, and it
    # now says what the record says (the old evidence stays for audit).
    assert len(changed["graph"]["events"]) == 1
    event = changed["graph"]["events"][0]
    assert event["id"] == original_id and event["title"] == CASES[4][0]
    assert len(event["evidence_ids"]) == 2
    assert engine.analyze(lambda: pytest.fail("Modified source should converge"))["runner_calls"] == 0


def test_non_event_unrelated_unit_is_not_reanalysed(laboratory):
    _, _, engine, records, make = laboratory
    records += [make("안녕하세요", key="unrelated", session="first"), make(CASES[0][0], session="second")]
    engine.analyze(FixtureRunner)
    records[1] = make(CASES[4][0], session="second")
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete"
    assert [i for t in runner.tasks for i in _sent(t)] == ["s1"]


def test_changed_context_alone_does_not_reanalyse_an_integrated_unit(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.analyze(FixtureRunner)
    records.append(make("추가 내용 없음", key="s2"))
    engine.analyze(FixtureRunner)  # s2's unit was shown s1 as context
    records[0] = make(CASES[4][0])
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert [i for t in runner.tasks for i in _sent(t)] == ["s1"]
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
    # Integration is code (the draft of the story's candidates); a failure there comes after the story call.
    integrate = engine._integrate_unit
    def fail(*args, **kwargs):
        raise FlowError("intentional integration failure")
    engine._integrate_unit = fail
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "failed"
    assert store.graph()["version"] == 0
    assert store.units()[0]["status"] == "extracted"
    engine._integrate_unit = integrate
    runner = FixtureRunner()
    recovered = engine.analyze(lambda: runner)
    assert recovered["status"] == "complete", recovered
    assert recovered["reused_extractions"] == 1
    assert runner.calls == 0  # the saved reading is published, not asked for again
    assert len(store.graph()["events"]) == 1


def test_invalid_story_is_blocked_after_one_repair_and_only_the_code_made_events_are_kept(laboratory):
    from contexttrail.light import STORY_FALLBACK_LIMITATION
    _, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant")])
    class BadStory(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            output["story"][0]["kind"] = "action"  # not a story kind: the answer fails its checks every time
            return output
    runner = BadStory()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete", result
    assert runner.calls == 2  # the story call and its one repair
    assert [call["status"] for call in store.llm_calls()] == ["validation_error", "validation_error"]
    # Nothing the refused answer said is published: the person's request (made by code) stays, the proposal does not.
    assert [(e["kind"], e["title"]) for e in store.graph()["events"]] == [("question", CASES[0][0])]
    assert STORY_FALLBACK_LIMITATION in store.graph()["limitations"]


def test_reported_completion_cannot_become_observed_success(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[5][0], role="assistant"))
    class UnsupportedSuccess(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            output["story"][0]["status"] = "observed_success"
            return output
    # Without a tool result the claim is read as what its citation backs: a reported completion.
    result = engine.analyze(UnsupportedSuccess)
    assert result["status"] == "complete", result
    assert [event["status"] for event in store.graph()["events"]] == ["reported_complete"]


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
    assert [c["stage"] for c in calls] == ["story"]
    for call in calls:
        meta = call["metadata"]
        assert meta["prompt_hash"] and meta["schema_hash"] and meta["input_digest"]
        assert call["status"] == "complete" and call["finished_at"]
        serialized = str(call)
        assert prompt("story")[:200] not in serialized
        assert CASES[0][0] not in serialized


def test_a_run_shows_what_it_would_send_before_sending(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    plans = []
    result = engine.analyze(lambda: pytest.fail("declined"), consent=lambda snapshot, plan: plans.append(plan) or False)
    assert result["status"] == "cancelled" and result["runner_calls"] == 0
    [plan] = plans
    assert (plan["units"], plan["units_this_run"], plan["max_calls"]) == (1, 1, 30)
    assert plan["input_tokens_this_run"] >= _FIXED_TOKENS * _TYPICAL_RATIO  # a whole request, however small the unit


def _capturing_analyze(engine):
    """Run the pipeline and keep every request and response schema the runner was given."""
    seen = []

    class Watching(FixtureRunner):
        def run(self, task, schema, cancel):
            seen.append((copy.deepcopy(task), schema))
            return super().run(task, schema, cancel)

    return engine.analyze(Watching), seen


def test_a_story_request_carries_the_story_prompt_and_schema(laboratory):
    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    [(story, schema)] = seen
    assert (story["stage"], story["system"], story["instructions"], story["wire_contract"]) == (
        "story", prompt("story"), "", build_task("story", {})["wire_contract"])
    assert schema is STORY_SCHEMA
    assert "journal_audit" not in story["data"]  # the audit's rules are added only in an audit


def test_every_unit_is_published_from_its_story_call_alone(laboratory):
    """The second session's unit meets a graph that already has events, and is still one story call, no other."""
    _, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant"),
                    make(CASES[0][0], key="t1", session="session-2"), make(CASES[1][0], key="t2", role="assistant", session="session-2")])
    result, seen = _capturing_analyze(engine)
    assert result["status"] == "complete", result
    assert [task["stage"] for task, _ in seen] == ["story", "story"]
    assert store.graph()["version"] == 2


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


def test_an_answer_that_fails_its_checks_gets_the_repair_round_with_what_it_wrote(laboratory):
    _, store, engine, records, make = laboratory
    records.extend([make(CASES[0][0]), make(CASES[1][0], key="s2", role="assistant")])
    class OffSchema(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if "repair" not in task:
                output["story"][0]["kind"] = "action"  # not a story kind
            return output
    runner = OffSchema()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete", result
    calls = store.llm_calls()
    assert [call["status"] for call in calls] == ["validation_error", "complete"]
    assert "action" in calls[0]["details"]["error"]
    repair = runner.tasks[1]["repair"]
    assert set(repair) == {"instruction", "previous_output"}
    assert repair["previous_output"]["story"][0]["kind"] == "action"
    assert "SQLite 대안 제안" in [event["title"] for event in store.graph()["events"]]


def test_a_check_that_breaks_on_an_answer_is_a_refusal_recorded_with_the_call(laboratory, monkeypatch):
    from contexttrail import light
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    original = light.story_check
    broken = []
    def story_check(answer):
        if not broken:
            broken.append(True)
            raise TypeError("object of type 'NoneType' has no len()")
        return original(answer)
    monkeypatch.setattr(light, "story_check", story_check)
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "complete", result
    calls = store.llm_calls()
    assert calls[0]["status"] == "validation_error"  # recorded, not left running
    assert calls[0]["details"]["error"].startswith("answer check failed in code (TypeError)")
