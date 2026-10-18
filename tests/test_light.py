"""Light analysis (`--light`): code builds the skeleton from the tool steps, one model call writes the rest."""
import json
from dataclasses import replace

from contexttrail.demo import FixtureRunner
from contexttrail.light import CITED_LINES, cite, skeleton
from contexttrail.model import SourceRecord


def records(make):
    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    patch = "Tool: Edit\n" + json.dumps({"file_path": "/work/app/src/calc.py", "old_string": "a - b", "new_string": "a + b"})
    return [
        make("add()가 틀려요. 고쳐 주세요.", key="ask"),
        tool("edit1", "tool_call", patch, "c1"), tool("edit1-out", "tool_result", "The file has been updated.", "c1"),
        tool("test1", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python -m pytest tests/test_calc.py -q"}), "c2"),
        tool("test1-out", "tool_result", "1 failed, 2 passed\nExit code: 1", "c2"),
        tool("edit2", "tool_call", patch.replace("a + b", "a + b  # fixed"), "c3"),
        tool("edit2-out", "tool_result", "The file has been updated.", "c3"),
        tool("test2", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python -m pytest tests/test_calc.py -q"}), "c4"),
        tool("test2-out", "tool_result", "3 passed", "c4"),
        make("부호가 반대였습니다. 빼기 대신 더하기로 바꿨고, 테스트가 통과합니다.", key="said", role="assistant"),
    ]


class LightRunner(FixtureRunner):
    """Answers the light request like a model would; any other stage is a failure of the test."""
    calls: list = []

    def run(self, task, schema, cancel):
        assert task["stage"] == "light", task["stage"]
        LightRunner.calls.append(task)
        data = task["data"]
        said = next(r for r in data["new_records"] if r["role"] == "assistant")
        return {"status": "complete", "snapshot_id": data["snapshot_id"], "unit_id": data["unit_id"],
                "skeleton": [{"id": item["id"], "title": f"T {item['id']}", "summary": "why"} for item in data["skeleton"]],
                "events": [{"kind": "decision", "status": "adopted", "actor": "assistant", "title": "Flip the sign",
                            "summary": "The bug was a sign error.",
                            "sources": [{"source_id": said["source_id"], "start_line": 1, "end_line": 1},
                                        {"source_id": "not-in-this-unit", "start_line": None, "end_line": None}]}]}


def test_the_skeleton_follows_the_tool_steps(laboratory):
    _, _, _, _, make = laboratory
    items, edges = skeleton(records(make))
    assert [(i["kind"], i["status"]) for i in items] == [
        ("action", "applied"), ("outcome", "observed_failure"), ("action", "applied"), ("outcome", "observed_success")]
    assert items[0]["title"] == "Edit calc.py" and items[0]["sources"] == ["edit1", "edit1-out"]
    assert items[1]["sources"][0] == "test1-out"  # the result first: an observed status rests on it
    assert [(e["from"], e["to"]) for e in edges] == [("K1", "K2"), ("K3", "K4")]


def test_the_same_check_run_again_with_nothing_changed_between_is_one_result(laboratory):
    _, _, _, _, make = laboratory
    recs = records(make)
    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    rerun = [tool("test1b", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python -m pytest tests/test_calc.py -q"}), "c2b"),
             tool("test1b-out", "tool_result", "1 failed, 2 passed\nExit code: 1", "c2b")]
    other = [tool("lint", "tool_call", "Tool: Bash\n" + json.dumps({"command": "ruff check ."}), "c5"),
             tool("lint-out", "tool_result", "All checks passed!", "c5")]
    items, edges = skeleton(recs[:5] + rerun + recs[5:9] + other + recs[9:])
    # The repeated failing run folds into one failed result that keeps both runs' records; a different check
    # after the passing run is its own result, and the edit → test links are unchanged.
    assert [(i["kind"], i["status"]) for i in items] == [
        ("action", "applied"), ("outcome", "observed_failure"), ("action", "applied"), ("outcome", "observed_success"),
        ("outcome", "observed_success")]
    assert items[1]["sources"] == ["test1b-out", "test1b", "test1-out", "test1"]
    assert items[4]["title"] == "Check passed: ruff check ."
    assert [(e["from"], e["to"]) for e in edges] == [("K1", "K2"), ("K3", "K4")]  # a lint of the whole tree names no file


def test_a_light_run_is_one_call_per_unit_and_cites_records_by_code(laboratory):
    scope, store, engine, recs, make = laboratory
    recs.extend(records(make))
    engine.config = replace(engine.config, analysis_mode="light", semantic_review=False, integrate_output="draft_only")
    LightRunner.calls = []
    result = engine.analyze(LightRunner)
    assert result["status"] == "complete", {k: result.get(k) for k in ("stop_reason", "error", "limitations", "unit_failures")} | {"stages": [t["stage"] for t in runner.tasks]}
    [task] = LightRunner.calls
    assert task["system"].startswith("You write the project history") and "quote" not in json.dumps(task["data"]["skeleton"])
    graph = store.graph()
    titles = {e["title"]: e for e in graph["events"]}
    assert {"T K1", "T K2", "T K3", "T K4", "Flip the sign"} <= set(titles)
    assert titles["T K2"]["status"] == "observed_failure" and titles["T K4"]["status"] == "observed_success"
    assert any(e["kind"] == "question" for e in graph["events"])  # the person's message, made by code
    verifies = [(e["from_event_id"], e["to_event_id"]) for e in graph["edges"] if e["relation"] == "verifies"]
    assert (titles["T K3"]["id"], titles["T K4"]["id"]) in verifies
    # The decision cites the assistant's line; the source outside the unit was dropped, not asked about again.
    assert len(titles["Flip the sign"]["evidence_ids"]) == 1


def test_code_cuts_the_cited_lines_within_what_was_shown(laboratory):
    _, _, _, _, make = laboratory
    record = make("\n".join(f"line {n}" for n in range(1, 60)), key="long", role="tool_result")
    assert cite(record, [(1, 10), (50, 59)], 52, 55) == {"source_id": "long", "start_line": 52, "end_line": 55,
                                                       "quote": "line 52\nline 53\nline 54\nline 55"}
    assert cite(record, [(1, 10)], 30, 35)["start_line"] == 1  # a range outside the shown lines falls back
    whole = cite(record, [(1, 59)])
    assert (whole["start_line"], whole["end_line"]) == (1, CITED_LINES)


def test_a_check_fails_by_its_exit_status_else_by_a_failure_count():
    from contexttrail.light import check_failed
    assert check_failed("Chunk ID: 1\nWall time: 2s\nExit code: 0\nOutput:\n1 failed", False) is False  # the status wins
    assert check_failed("Process exited with code 2", False) and check_failed('{"exit_code":1}', False)
    assert check_failed("Error: Exit code 1\nboom", True)
    assert check_failed("=== 1 failed, 2 passed in 0.1s ===", False) and not check_failed("3 passed", False)
    assert not check_failed("0 failed", False)


def test_a_scan_marks_what_each_tool_call_did_and_an_event_reads_it_off_its_citations(laboratory):
    from contexttrail.analysis import mark_steps
    from contexttrail.schema import step_of
    _, _, _, _, make = laboratory
    found = records(make)
    doc = replace(make("Tool: Edit\n" + json.dumps({"file_path": "/work/app/README.md", "old_string": "a", "new_string": "b"}),
                       key="readme", role="tool_call"), tool_call_id="c5")
    found.append(doc)
    mark_steps(found)
    step = {record.source_id: record.step for record in found}
    assert step["test1"] == step["test1-out"] == {"hint": "test"}
    assert step["edit1"] == {"hint": "edit"} and step["readme"] == {"hint": "edit", "doc": True}
    assert step["ask"] == {} and step["said"] == {}  # only tool records are marked
    meta = {record.source_id: record.metadata() for record in found}
    outcome = {"kind": "outcome", "status": "observed_success"}
    change = {"kind": "action", "status": "applied"}
    assert step_of(outcome, [meta["test2-out"], meta["said"]]) == "check"  # the words around a run do not count
    assert step_of(outcome, [meta["test2-out"], meta["edit2"]]) is None
    assert step_of(change, [meta["readme"]]) == "doc" and step_of(change, [meta["edit1"], meta["readme"]]) is None
    assert step_of(outcome, [{**meta["test2-out"], "step": {}}]) is None  # a record read before `step` says nothing
    assert "step" not in __import__("contexttrail.analysis", fromlist=["record_for_model"]).record_for_model(found[3])


def test_an_edit_is_linked_to_a_test_only_when_the_run_names_every_edited_file(laboratory):
    from contexttrail.light import covers
    _, _, _, _, make = laboratory
    assert "calc.py" in covers("pytest tests/test_calc.py -q", ["/work/app/src/calc.py"])
    assert covers("python -m pytest -q", ["src/calc.py"]) is None  # the whole suite: what it covered is unknown
    assert covers("pytest -k smoke", ["payment.py"]) is None  # a selection is not the whole suite either
    assert covers("pytest tests/test_database.py", ["base.py"]) is None  # names compared whole, never a substring
    assert covers("pytest tests/test_parser.py", ["parser.py", "writer.py"]) is None  # only some of the files
    assert covers("pytest tests/test_parser.py tests/test_writer.py", ["parser.py", "writer.py"])
    assert covers("ls -la", ["a.py"]) is None
    found = records(make)
    other = "Tool: Bash\n" + json.dumps({"command": "python -m pytest tests/test_docs.py -q"})
    found[7] = replace(found[7], content=other)
    items, edges = skeleton(found)
    checks = {item["id"] for item in items if item["kind"] == "outcome"}
    linked = {edge["to"] for edge in edges}
    assert len(checks) == 2 and len(linked) == 1  # the run naming calc.py links; the docs test after edit2 does not
    assert all(edge["why"] for edge in edges)



def test_a_unit_summary_is_asked_only_when_chosen_and_kept_with_the_units_events(laboratory):
    from contexttrail.analysis import UNIT_SUMMARY_REQUEST
    from contexttrail.demo import CASES
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    runner = FixtureRunner()
    assert engine.analyze(lambda: runner)["status"] == "complete"
    assert all("unit_summary_request" not in task["data"] for task in runner.tasks)  # the default input is unchanged
    assert "unit_summaries" not in store.graph()
    records.append(make(CASES[1][0], key="s2"))
    engine.config.unit_summary = True
    runner = FixtureRunner()
    assert engine.analyze(lambda: runner)["status"] == "complete"
    assert runner.tasks[0]["data"]["unit_summary_request"] == UNIT_SUMMARY_REQUEST
    (summary,) = store.graph()["unit_summaries"].values()
    assert summary["text"] == "합성 구간: 저장 방식을 정하고 바꿈"
    assert summary["event_ids"] and set(summary["event_ids"]) <= {e["id"] for e in store.graph()["events"]}


def test_pipeline_v2_keeps_the_work_code_found_and_the_story_cites_written_blocks(laboratory):
    from contexttrail.light import STORY_SCHEMA, skeleton, story_extraction, story_request, written_blocks
    from contexttrail.schema import EvidenceValidator, validate_shape
    _, _, _, _, make = laboratory
    found = records(make)
    items, edges = skeleton(found)
    blocks = written_blocks(found)
    assert [b["source_id"] for b in blocks] == ["ask", "said"]  # only what the person and the assistant wrote
    unit = {"id": "unit_1", "sources": [r.source_id for r in found]}
    request = story_request(unit, "snap", {"user_requests": [], "existing_events": []}, items, found, blocks)
    assert "new_records" not in request and "3 passed" not in json.dumps(request)  # no tool output is sent
    failed = next(w for w in request["work"] if w["status"] == "observed_failure")
    assert failed["failure_tail"][-1] == "Exit code: 1"  # but a failed run's last lines are
    answer = {"status": "complete", "snapshot_id": "snap", "unit_id": "unit_1",
              "work": [{"id": item["id"], "title": "이름", "summary": ""} for item in items],
              "story": [{"id": "N1", "kind": "outcome", "status": "reported_complete", "actor": "assistant",
                         "title": "부호가 반대였음", "why": None, "summary": "", "blocks": [blocks[1]["id"]], "work": []},
                        {"id": "N2", "kind": "decision", "status": "adopted", "actor": "assistant", "title": "근거 없음",
                         "why": None, "summary": "", "blocks": ["B99"], "work": []}],
              "links": [], "turn_summary": "add() 부호 수정"}
    validate_shape(answer, STORY_SCHEMA)
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, len(r.content.splitlines()))] for r in found}
    output, learned = story_extraction(answer, items, edges, unit, "snap", pool, provided,
                                       EvidenceValidator(pool, provided), blocks)
    titles = {c["title"] for c in output["event_candidates"]}
    assert "부호가 반대였음" in titles and "근거 없음" not in titles  # a story event with no block left is dropped
    assert "story: no evidence left for N2." in output["limitations"]
    assert any(e["relation"] == "verifies" for e in output["edge_candidates"])  # the code's check link stays
    assert learned["turn_summary"] == "add() 부호 수정" and learned["unclaimed_blocks"] == []


def test_a_v2_run_publishes_the_code_found_work_and_the_turn_summary(laboratory):
    from contexttrail.demo import CASES
    _, store, engine, records_, make = laboratory
    records_.extend(records(make))
    import dataclasses
    engine.config = dataclasses.replace(engine.config, pipeline="v2")
    engine.config.validate()  # what Engine() does with the config it is given
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete", {k: result.get(k) for k in ("stop_reason", "error", "limitations", "unit_failures")} | {"stages": [t["stage"] for t in runner.tasks]}
    assert [task["stage"] for task in runner.tasks] == ["story"]
    graph = store.graph()
    assert any(event["status"] == "observed_failure" for event in graph["events"])  # the failed check, found by code
    assert list(graph["unit_summaries"].values())[0]["text"] == "합성 구간: 작업 기록만"
    assert engine.config.integrate_output == "draft_only" and not engine.config.semantic_review  # no integrate, no review


def test_v2_keeps_a_run_only_when_the_story_points_at_it_and_names_unnamed_work_in_korean(laboratory):
    from contexttrail.light import run_items, skeleton, story_extraction, written_blocks
    from contexttrail.schema import EvidenceValidator
    _, _, _, _, make = laboratory
    found = records(make)
    start = replace(make("Tool: Bash\n" + json.dumps({"command": "python scripts/run_pipeline.py start"}), key="job", role="tool_call"), tool_call_id="c9")
    out = replace(make("started pid 41965\nExit code: 0", key="job-out", role="tool_result"), tool_call_id="c9")
    other = replace(make("Tool: Bash\n" + json.dumps({"command": "ps -o pid -p 41965"}), key="ps", role="tool_call"), tool_call_id="c10")
    found += [start, out, other]
    runs = run_items(found)
    assert [(r["id"], r["status"]) for r in runs] == [("R1", "passed"), ("R2", "unknown")]
    items, edges = skeleton(found)
    blocks = written_blocks(found)
    answer = {"work": [], "links": [], "turn_summary": None,
              "story": [{"id": "N1", "kind": "decision", "status": "adopted", "actor": "assistant", "title": "분석 실행",
                         "why": None, "summary": "", "blocks": [blocks[1]["id"]], "work": ["R1"]}]}
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, len(r.content.splitlines()))] for r in found}
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, runs, "Korean")
    titles = [c["title"] for c in output["event_candidates"]]
    assert "실행: python scripts/run_pipeline.py start" in titles and not any("ps -o" in t for t in titles)
    assert any(t.startswith("수정: calc.py") for t in titles) and any(t.startswith("검사 실패: ") for t in titles)
    assert any(e["relation"] == "motivates" and e["to_event_id"] == "tmp:R1" for e in output["edge_candidates"])


def test_v2_draws_one_relation_when_the_story_names_the_work_and_links_it_too(laboratory):
    from contexttrail.light import run_items, skeleton, story_extraction, written_blocks
    from contexttrail.schema import EvidenceValidator
    _, _, _, _, make = laboratory
    found = records(make)
    items, edges = skeleton(found)
    blocks = written_blocks(found)
    work = items[0]["id"]
    answer = {"work": [], "turn_summary": None,
              "story": [{"id": "N1", "kind": "decision", "status": "adopted", "actor": "assistant", "title": "고치기로 함",
                         "why": None, "summary": "", "blocks": [blocks[1]["id"]], "work": [work]}],
              "links": [{"from": "N1", "to": work, "relation": "motivates", "blocks": [blocks[1]["id"]]}]}
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, len(r.content.splitlines()))] for r in found}
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, run_items(found), "Korean")
    linked = [e for e in output["edge_candidates"] if (e["from_event_id"], e["to_event_id"], e["relation"]) == ("tmp:N1", f"tmp:{work}", "motivates")]
    assert len(linked) == 1 and linked[0]["basis"] == "explicit"
