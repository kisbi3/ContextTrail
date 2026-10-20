"""The work record and the story: code builds the skeleton from the tool steps, one story call writes the rest."""
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
    # One Codex call ran the tests (exit 1) and then three searches (exit 0): the check failed.
    mixed = '{"exit_code": 1, "output": "2 failed, 184 passed"}\n{"exit_code": 0}\n{"exit_code": 0}\n{"exit_code": 0}'
    assert check_failed(mixed, False)
    # One status covers a whole shell line: a test run piped to tail, or followed by a search, exits 0 after failures.
    piped = "Exit code: 0\nWall time: 3.1 seconds\nOutput:\nFAILED tests/test_x.py::test_a\n=== 2 failed, 184 passed in 3.0s ==="
    assert check_failed(piped, False, test=True) and not check_failed(piped, False)
    assert not check_failed("Exit code: 0\nOutput:\n12 passed in 0.4s", False, test=True)


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
    assert "new_records" not in request  # no tool output is sent but the last lines of a run right after an edit
    assert [w.get("output_tail") for w in request["work"] if w.get("output_tail")] == [["3 passed"]]
    failed = next(w for w in request["work"] if w["status"] == "observed_failure")
    assert failed["failure_tail"][-1] == "Exit code: 1"  # but a failed run's last lines are
    answer = {"status": "complete", "snapshot_id": "snap", "unit_id": "unit_1",
              "work": [{"id": item["id"], "title": "이름", "summary": "", "checked_by": []} for item in items],
              "story": [{"id": "N1", "kind": "outcome", "status": "reported_complete",
                         "title": "부호가 반대였음", "why": None, "summary": "", "blocks": [blocks[1]["id"]], "work": []},
                        {"id": "N2", "kind": "decision", "status": "adopted", "title": "근거 없음",
                         "why": None, "summary": "", "blocks": ["B99"], "work": []}],
              "requests": [], "links": [], "turn_summary": "add() 부호 수정"}
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
    _, store, engine, records_, make = laboratory
    records_.extend(records(make))
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete", {k: result.get(k) for k in ("stop_reason", "error", "limitations", "unit_failures")} | {"stages": [t["stage"] for t in runner.tasks]}
    assert [task["stage"] for task in runner.tasks] == ["story"]
    graph = store.graph()
    titles = {e["title"]: e for e in graph["events"]}
    failed, passed = (titles[f"검사 {word}: python -m pytest tests/test_calc.py -q"] for word in ("실패", "통과"))
    assert (failed["status"], passed["status"]) == ("observed_failure", "observed_success")  # found by code
    assert titles["add()가 틀려요. 고쳐 주세요."]["kind"] == "question"  # the person's message, made by code
    # Each edit is verified by the check after it that names its file.
    edits = [e["id"] for e in graph["events"] if e["kind"] == "action"]
    verifies = {(e["from_event_id"], e["to_event_id"]) for e in graph["edges"] if e["relation"] == "verifies"}
    assert verifies == set(zip(edits, (failed["id"], passed["id"])))
    (summary,) = graph["unit_summaries"].values()
    assert summary["text"] == "합성 구간: 작업 기록만"  # kept with the events the unit added
    assert summary["event_ids"] and set(summary["event_ids"]) <= {e["id"] for e in graph["events"]}


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
              "story": [{"id": "N1", "kind": "decision", "status": "adopted", "title": "분석 실행",
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
              "story": [{"id": "N1", "kind": "decision", "status": "adopted", "title": "고치기로 함",
                         "why": None, "summary": "", "blocks": [blocks[1]["id"]], "work": [work]}],
              "links": [{"from": "N1", "to": work, "relation": "motivates", "blocks": [blocks[1]["id"]]}]}
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, len(r.content.splitlines()))] for r in found}
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, run_items(found), "Korean")
    linked = [e for e in output["edge_candidates"] if (e["from_event_id"], e["to_event_id"], e["relation"]) == ("tmp:N1", f"tmp:{work}", "motivates")]
    assert len(linked) == 1 and linked[0]["basis"] == "explicit"


def test_v2_keeps_a_check_the_story_claims_only_when_the_run_followed_the_edit_and_the_assistant_reported_it(laboratory):
    from contexttrail.light import run_items, skeleton, story_extraction, written_blocks
    from contexttrail.schema import EvidenceValidator
    _, _, _, _, make = laboratory

    def at(record, minute, call=None):
        return replace(record, recorded_at=f"2026-09-22T10:{minute:02d}:00Z", tool_call_id=call)
    patch = "Tool: Edit\n" + json.dumps({"file_path": "/work/app/scripts/build.py", "old_string": "a", "new_string": "b"})
    found = [
        at(make("보고서를 고쳐 주세요.", key="ask"), 0),
        at(make("빌드 스크립트를 고친 뒤 빌드하겠습니다.", key="plan", role="assistant"), 1),
        at(make("Tool: Bash\n" + json.dumps({"command": "python scripts/old.py"}), key="early", role="tool_call"), 2, "c0"),
        at(make("done\nExit code: 0", key="early-out", role="tool_result"), 2, "c0"),
        at(make(patch, key="edit", role="tool_call"), 3, "c1"), at(make("updated", key="edit-out", role="tool_result"), 3, "c1"),
        at(make("Tool: Bash\n" + json.dumps({"command": "python scripts/build.py"}), key="build", role="tool_call"), 4, "c2"),
        at(make("wrote report.pdf (2 pages)", key="build-out", role="tool_result"), 4, "c2"),
        at(make("Tool: Bash\n" + json.dumps({"command": "nohup python scripts/job.py > log 2>&1 &"}), key="job", role="tool_call"), 5, "c3"),
        at(make("", key="job-out", role="tool_result"), 5, "c3"),
        at(make("빌드가 통과해 2쪽 PDF가 나온 것을 확인했습니다.", key="said", role="assistant"), 6),
    ]
    items, edges = skeleton(found)
    runs = run_items(found)
    assert [(r["id"], r["status"]) for r in runs] == [("R1", "passed"), ("R2", "passed"), ("R3", "unknown")]
    blocks = written_blocks(found)
    plan, report = (next(b["id"] for b in blocks if b["source_id"] == key) for key in ("plan", "said"))
    edit = next(i["id"] for i in items if i.get("edit"))
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, max(1, len(r.content.splitlines())))] for r in found}

    def kept(*links):
        answer = {"work": [], "story": [], "turn_summary": None,
                  "links": [{"from": a, "to": b, "relation": "verifies", "blocks": [block]} for a, b, block in links]}
        output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                     EvidenceValidator(pool, provided), blocks, runs, "Korean")
        return output
    output = kept((edit, "R2", report))
    check = [e for e in output["edge_candidates"] if e["relation"] == "verifies"]
    assert len(check) == 1 and check[0]["basis"] == "explicit"
    assert {c["source_id"] for c in check[0]["evidence"]} == {"said", "build-out"}  # the report and the run's result
    EvidenceValidator(pool, provided).check_extraction(output, "u", "snap", {"events": [], "edges": []})
    output = kept((edit, "R2", plan), (edit, "R1", report), (edit, "R3", report))
    assert not [e for e in output["edge_candidates"] if e["relation"] == "verifies"]
    assert not any(c["id"] in ("tmp:R1", "tmp:R3") for c in output["event_candidates"])  # refused claims keep no run
    refused = next(text for text in output["limitations"] if text.startswith("story: link not kept"))
    assert "no block where the assistant reports the run" in refused and "the run came before the edit" in refused
    assert "the run's result is unknown" in refused
    output = kept(("R2", edit, report))  # written from the run to the edit: read the other way, then checked
    assert [(e["from_event_id"], e["to_event_id"]) for e in output["edge_candidates"] if e["relation"] == "verifies"] \
        == [(f"tmp:{edit}", "tmp:R2")]
    assert f"story: link written in reverse, read the other way: R2 → {edit}." in output["limitations"]
    answer = {"work": [], "story": [], "turn_summary": None,
              "links": [{"from": edit, "to": "R2", "relation": "verifies", "blocks": [plan, report]}]}
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, runs, "Korean")
    (check,) = [e for e in output["edge_candidates"] if e["relation"] == "verifies"]
    assert {c["source_id"] for c in check["evidence"]} == {"said", "build-out"}  # the block before the run is not kept


def test_v2_ends_a_run_of_edits_at_a_run_between_them(laboratory):
    _, _, _, _, make = laboratory

    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    patch = "Tool: Edit\n" + json.dumps({"file_path": "/work/app/scripts/build.py", "old_string": "a", "new_string": "b"})
    found = [make("고쳐 주세요.", key="ask"),
             tool("e1", "tool_call", patch, "c1"), tool("e1-out", "tool_result", "updated", "c1"),
             tool("r", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python scripts/build.py"}), "c2"),
             tool("r-out", "tool_result", "ok\nExit code: 0", "c2"),
             tool("e2", "tool_call", patch.replace('"b"', '"c"'), "c3"), tool("e2-out", "tool_result", "updated", "c3")]
    assert len([i for i in skeleton(found)[0] if i.get("edit")]) == 1
    assert [i["sources"][0] for i in skeleton(found, split_at_runs=True)[0] if i.get("edit")] == ["e1", "e2"]


def test_v2_answers_a_request_by_its_id_only_with_a_reply_after_it_in_its_session(laboratory):
    from contexttrail.light import open_requests, request_items, skeleton, story_extraction, story_request, written_blocks
    from contexttrail.schema import EvidenceValidator
    _, _, _, _, make = laboratory
    found = records(make) + [make("문서도 고쳐 줘", key="ask2"), make("문서는 다음에 고치겠습니다.", key="later", role="assistant")]
    items, edges = skeleton(found)
    blocks = written_blocks(found)
    block = {b["source_id"]: b["id"] for b in blocks}
    assert [q["id"] for q in request_items(found)] == ["Q1", "Q2"]
    data = {"user_requests": [{"source_id": r, "recorded_at": None, "text": r} for r in ("ask", "ask2")]}
    request = story_request({"id": "u"}, "snap", data, items, found, blocks)
    assert [(q["id"], q["source_id"]) for q in request["user_requests"]] == [("Q1", "ask"), ("Q2", "ask2")]
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, max(1, len(r.content.splitlines())))] for r in found}
    story = [{"id": "N1", "kind": "outcome", "status": "reported_complete", "title": "부호 오류였음", "why": None,
              "summary": "", "blocks": [block["said"]], "work": []},
             {"id": "Q2", "kind": "decision", "status": "adopted", "title": "ID를 가로챈 사건", "why": None,
              "summary": "", "blocks": [block["said"]], "work": []},
             {"id": "ev_old", "kind": "decision", "status": "adopted", "title": "기존 사건의 ID", "why": None,
              "summary": "", "blocks": [block["said"]], "work": []}]
    links = [{"from": "Q1", "to": "N1", "relation": "answers", "blocks": [block["said"]]},  # kept
             {"from": "Q2", "to": "N1", "relation": "answers", "blocks": [block["said"]]},  # the reply came before Q2
             {"from": "Q1", "to": "Q2", "relation": "answers", "blocks": [block["said"]]},  # request to request
             {"from": "N1", "to": items[0]["id"], "relation": "answers", "blocks": [block["said"]]},  # not a request
             {"from": "Q1", "to": items[0]["id"], "relation": "answers", "blocks": []}]  # no reply cited
    answer = {"work": [], "story": story, "links": links, "turn_summary": None}
    validator = EvidenceValidator(pool, provided)
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 validator, blocks, [], "Korean")
    kept = [(e["from_event_id"], e["to_event_id"]) for e in output["edge_candidates"] if e["relation"] == "answers"]
    assert kept == [("tmp:Q1", "tmp:N1")]
    requests = [c for c in output["event_candidates"] if c["kind"] == "question"]
    assert [(c["id"], c["actor"]) for c in requests] == [("tmp:Q1", "user"), ("tmp:Q2", "user")]
    assert all(c["actor"] == "assistant" for c in output["event_candidates"] if c["kind"] != "question")
    assert not any(c["id"] in ("tmp:Q2", "tmp:ev_old") and c["kind"] == "decision" for c in output["event_candidates"])
    refused = next(t for t in output["limitations"] if t.startswith("story: link not kept"))
    for why in ("no reply after the request in its session", "not to an event of this stretch", "not from a request",
                "no block of the assistant's reply"):
        assert why in refused
    assert "story: event not kept, its ID names a request, work item, block or earlier event: Q2, ev_old." \
        in output["limitations"]
    validator.check_extraction(output, "u", "snap", {"events": [], "edges": []})

    # An earlier request whose turn these records continue can be answered here, by a reply before the session's next
    # request; one of another session cannot, and a reply after the next request belongs to that request's turn.
    earlier = {"ev_old": {"provider": "claude", "session_id": "session-1"}, "ev_far": {"provider": "claude", "session_id": "other"}}
    unit = {"id": "u", "sources": [i for i in pool if i != "ask"]}  # the stretch starts in ev_old's turn
    answer = {"work": [], "story": story[:1], "turn_summary": None,
              "links": [{"from": "ev_old", "to": "N1", "relation": "answers", "blocks": [block["said"]]},
                        {"from": "ev_far", "to": "N1", "relation": "answers", "blocks": [block["said"]]}]}
    output, _ = story_extraction(answer, items, edges, unit, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, [], "Korean", earlier)
    assert [e["from_event_id"] for e in output["edge_candidates"] if e["relation"] == "answers"] == ["ev_old"]
    answer["links"] = [{"from": "ev_old", "to": "N1", "relation": "answers", "blocks": [block["later"]]}]
    output, _ = story_extraction(answer, items, edges, unit, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, [], "Korean", earlier)
    assert not [e for e in output["edge_candidates"] if e["relation"] == "answers"]
    assert any("the reply came after a later request" in t for t in output["limitations"])
    # Offered: the session's last request before these records, whose turn they continue, even when answered in
    # part; not one a later request followed, one of another session, or one after them.
    def asked(key, at, evidence):
        return {"id": key, "kind": "question", "actor": "user", "recorded_at": f"2026-09-22T{at}:00Z", "evidence_ids": [evidence]}
    graph = {"events": [asked("ev_old", "09:00", "a"), asked("ev_last", "09:30", "a"), asked("ev_far", "09:50", "b"),
                        asked("ev_after", "10:30", "a")],
             "edges": [{"from_event_id": "ev_last", "to_event_id": "x", "relation": "answers", "active": True}]}
    source = {"a": {"provider": "claude", "session_id": "session-1"}, "b": {"provider": "claude", "session_id": "other"}}
    assert open_requests(graph, found, source.get) == {"ev_last": {"provider": "claude", "session_id": "session-1"}}


def test_v2_reads_an_answer_written_to_the_request_the_other_way_and_keeps_only_the_reply_after_it(laboratory):
    from contexttrail.light import skeleton, story_extraction, written_blocks
    from contexttrail.schema import EvidenceValidator
    _, _, _, _, make = laboratory
    found = records(make) + [make("문서도 고쳐 줘", key="ask2"), make("문서를 고쳤습니다.", key="later", role="assistant")]
    items, edges = skeleton(found)
    blocks = written_blocks(found)
    block = {b["source_id"]: b["id"] for b in blocks}
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, max(1, len(r.content.splitlines())))] for r in found}
    story = [{"id": "N1", "kind": "outcome", "status": "reported_complete", "title": "문서 고침", "why": None,
              "summary": "", "blocks": [block["later"]], "work": []}]
    # From the event to the request, citing a reply written before the request and one after it.
    links = [{"from": "N1", "to": "Q2", "relation": "answers", "blocks": [block["said"], block["later"]]}]
    output, _ = story_extraction({"work": [], "story": story, "links": links, "turn_summary": None}, items, edges,
                                 {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, [], "Korean")
    (answer,) = [e for e in output["edge_candidates"] if e["relation"] == "answers"]
    assert (answer["from_event_id"], answer["to_event_id"]) == ("tmp:Q2", "tmp:N1")
    assert [c["source_id"] for c in answer["evidence"]] == ["later"]
    assert "story: link written in reverse, read the other way: N1 → Q2." in output["limitations"]


def test_v2_reads_each_requests_answered_by_as_a_claimed_answer_and_merges_one_repeated_in_the_links(laboratory):
    from contexttrail.light import STORY_SCHEMA, skeleton, story_extraction, written_blocks
    from contexttrail.schema import EvidenceValidator, validate_shape
    _, _, _, _, make = laboratory
    found = records(make) + [make("문서도 고쳐 줘", key="ask2"), make("문서를 고쳤습니다.", key="later", role="assistant")]
    items, edges = skeleton(found)
    blocks = written_blocks(found)
    block = {b["source_id"]: b["id"] for b in blocks}
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, max(1, len(r.content.splitlines())))] for r in found}
    story = [{"id": "N1", "kind": "outcome", "status": "reported_complete", "title": "부호 오류였음", "why": None,
              "summary": "", "blocks": [block["said"]], "work": []},
             {"id": "N2", "kind": "outcome", "status": "reported_complete", "title": "문서 고침", "why": None,
              "summary": "", "blocks": [block["later"]], "work": []}]
    answer = {"status": "complete", "snapshot_id": "snap", "unit_id": "u", "work": [], "story": story,
              "requests": [{"id": "Q1", "answered_by": [{"item": "N1", "blocks": [block["said"]]}]},
                           {"id": "Q2", "answered_by": [{"item": "N1", "blocks": [block["said"]]},  # before Q2: refused
                                                        {"item": "N2", "blocks": [block["later"]]}]}],
              "links": [{"from": "Q2", "to": "N2", "relation": "answers", "blocks": [block["later"]]}],
              "turn_summary": None}
    validate_shape(answer, STORY_SCHEMA)
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, [], "Korean")
    kept = [(e["from_event_id"], e["to_event_id"], e["basis"]) for e in output["edge_candidates"] if e["relation"] == "answers"]
    assert kept == [("tmp:Q2", "tmp:N2", "explicit"), ("tmp:Q1", "tmp:N1", "explicit")]  # Q2 → N2 once
    assert any("Q2 → N1 (no reply after the request in its session)" in t for t in output["limitations"])


def test_v2_shows_what_a_run_printed_inside_codexs_wrapper_and_its_command():
    from contexttrail.light import _failure_tail, readable_output
    wrapped = ('Script completed\nWall time 1.3 seconds\nOutput:\n\n'
               + json.dumps({"chunk_id": "c1", "exit_code": 0, "output": "page 1: 2 figures\npage 2: table ok\n"}))
    assert readable_output(wrapped)[-2:] == ["page 1: 2 figures", "page 2: table ok"]
    assert _failure_tail(wrapped)[-1] == "page 2: table ok"  # not the wrapper's head, cut at 300 characters
    assert readable_output('{"not": "a wrapper"}\nplain') == ['{"not": "a wrapper"}', "plain"]


def test_v2_runs_right_after_an_edit_show_their_command(laboratory):
    from contexttrail.light import run_items, skeleton, story_request, written_blocks
    _, _, _, _, make = laboratory

    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    patch = "Tool: Edit\n" + json.dumps({"file_path": "/work/app/scripts/build.py", "old_string": "a", "new_string": "b"})
    script = "python - <<'PY'\nfrom pypdf import PdfReader\nprint(len(PdfReader('out/report.pdf').pages))\nPY"
    found = [make("보고서를 고쳐 주세요.", key="ask"),
             tool("e", "tool_call", patch, "c1"), tool("e-out", "tool_result", "updated", "c1"),
             tool("r", "tool_call", "Tool: Bash\n" + json.dumps({"command": script}), "c2"),
             tool("r-out", "tool_result", "2\nExit code: 0", "c2")]
    items, _ = skeleton(found, split_at_runs=True)
    runs = run_items(found)
    request = story_request({"id": "u"}, "s", {"user_requests": [], "existing_events": []}, items, found,
                            written_blocks(found), runs)
    (run,) = [w for w in request["work"] if w["kind"] == "run"]
    assert "out/report.pdf" in run["command"] and "\n" not in run["command"]


def test_v2_a_codex_call_that_runs_a_command_after_its_patch_shows_that_run(laboratory):
    from contexttrail.light import inline_run, run_items, skeleton, story_extraction, written_blocks
    from contexttrail.schema import EvidenceValidator
    _, _, _, _, make = laboratory

    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    patch = ('text(await tools.apply_patch("*** Begin Patch\\n*** Update File: /work/app/src/calc.py\\n@@\\n-a\\n+b\\n'
             '*** End Patch"));\n')
    both = "Tool: exec\n" + patch + 'text(await tools.exec_command({cmd:"python -m pytest tests -q"}));'
    assert inline_run(both) == "python -m pytest tests -q"
    assert inline_run("Tool: exec\n" + 'text(await tools.exec_command({cmd:"ls"}));\n' + patch) is None  # run first
    found = [make("고쳐 주세요.", key="ask"),
             tool("e1", "tool_call", both, "c1"),
             tool("e1-out", "tool_result", 'Output:\n{"exit_code":0,"output":"3 passed in 0.1s\\n"}', "c1"),
             tool("e2", "tool_call", "Tool: exec\n" + patch.replace("+b", "+c"), "c2"),
             tool("e2-out", "tool_result", "Output:\n{}", "c2"),
             make("테스트 3개가 통과했습니다.", key="said", role="assistant")]
    items, edges = skeleton(found, split_at_runs=True)
    edits = [i for i in items if i.get("edit")]
    assert [i["sources"][0] for i in edits] == ["e1", "e2"]  # the run after the first patch ends that change
    (run,) = run_items(found)
    assert (run["status"], run.get("inline"), run["sources"][0]) == ("passed", True, "e1")
    blocks = written_blocks(found)
    said = next(b["id"] for b in blocks if b["source_id"] == "said")
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, max(1, len(r.content.splitlines())))] for r in found}
    answer = {"work": [], "story": [], "turn_summary": None,
              "links": [{"from": edits[0]["id"], "to": run["id"], "relation": "verifies", "blocks": [said]},
                        {"from": edits[1]["id"], "to": run["id"], "relation": "verifies", "blocks": [said]}]}
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, [run], "Korean")
    kept = [(e["from_event_id"], e["to_event_id"]) for e in output["edge_candidates"] if e["relation"] == "verifies"]
    assert kept == [(f"tmp:{edits[0]['id']}", f"tmp:{run['id']}")]  # the later patch came after that run


def test_v2_reads_each_edits_checked_by_as_a_claimed_check(laboratory):
    from contexttrail.light import STORY_SCHEMA, run_items, skeleton, story_extraction, story_request, written_blocks
    from contexttrail.schema import EvidenceValidator, validate_shape
    _, _, _, _, make = laboratory

    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    patch = "Tool: Edit\n" + json.dumps({"file_path": "/work/app/scripts/build.py", "old_string": "a", "new_string": "b"})
    found = [make("보고서를 고쳐 주세요.", key="ask"),
             tool("e", "tool_call", patch, "c1"), tool("e-out", "tool_result", "updated", "c1"),
             tool("r", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python scripts/build.py"}), "c2"),
             tool("r-out", "tool_result", "wrote report.pdf\nExit code: 0", "c2"),
             make("빌드해서 report.pdf가 나온 것을 확인했습니다.", key="said", role="assistant")]
    items, edges = skeleton(found, split_at_runs=True)
    runs = run_items(found)
    blocks = written_blocks(found)
    said = next(b["id"] for b in blocks if b["source_id"] == "said")
    edit = next(i["id"] for i in items if i.get("edit"))
    # Work items carry no time: each names the first block the assistant wrote after it, where its report starts.
    request = story_request({"id": "u"}, "snap", {"user_requests": []}, items, found, blocks, runs)
    assert {w["id"]: w.get("next_written") for w in request["work"]} == {edit: said, "R1": said}
    assert {w["id"]: w.get("later_runs") for w in request["work"]} == {edit: ["R1"], "R1": None}  # an edit's later runs
    answer = {"status": "complete", "snapshot_id": "snap", "unit_id": "u", "story": [], "requests": [], "links": [], "turn_summary": None,
              "work": [{"id": edit, "title": "빌드 수정", "summary": "", "checked_by": [{"run": "R1", "blocks": [said]}]}]}
    validate_shape(answer, STORY_SCHEMA)
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, max(1, len(r.content.splitlines())))] for r in found}
    output, _ = story_extraction(answer, items, edges, {"id": "u", "sources": list(pool)}, "snap", pool, provided,
                                 EvidenceValidator(pool, provided), blocks, runs, "Korean")
    (check,) = [e for e in output["edge_candidates"] if e["relation"] == "verifies"]
    assert (check["from_event_id"], check["to_event_id"], check["basis"]) == (f"tmp:{edit}", "tmp:R1", "explicit")


def test_v2_a_codex_run_left_running_is_settled_by_its_later_poll(laboratory):
    from contexttrail.light import run_items, story_request, skeleton, written_blocks
    _, _, _, _, make = laboratory

    def tool(key, role, text, call_id):  # Codex writes an exit status for every finished run
        return replace(make(text, key=key, role=role), tool_call_id=call_id, provider="codex")
    started = 'Output:\n{"chunk_id":"a","wall_time_seconds":1.0,"session_id":4500,"output":""}'
    found = [make("빌드해 주세요.", key="ask"),
             tool("r", "tool_call", 'Tool: exec\ntext(await tools.exec_command({cmd:"python scripts/build.py"}));', "c1"),
             tool("r-out", "tool_result", started, "c1"),
             tool("p1", "tool_call", 'Tool: exec\ntext(await tools.write_stdin({session_id:9999,chars:""}));', "c2"),
             tool("p1-out", "tool_result", 'Output:\n{"exit_code":1,"output":"other job failed"}', "c2"),
             tool("p2", "tool_call", 'Tool: exec\ntext(await tools.write_stdin({session_id:4500,chars:""}));', "c3"),
             tool("p2-out", "tool_result", 'Output:\n{"exit_code":0,"output":"5 passed in 0.2s\\n"}', "c3")]
    (run,) = run_items(found)
    assert run["status"] == "passed" and run["sources"] == ["r", "r-out", "p2", "p2-out"]  # not the other session's poll
    items, _ = skeleton(found, split_at_runs=True)
    request = story_request({"id": "u"}, "s", {"user_requests": [], "existing_events": []}, items, found,
                            written_blocks(found), [run])
    assert request["work"][-1]["status"] == "passed"


def test_v2_a_codex_run_is_settled_by_its_poll_inside_a_call_that_does_more(laboratory):
    from contexttrail.light import run_items, story_request, skeleton, written_blocks
    _, _, _, _, make = laboratory

    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id, provider="codex")
    patch = 'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: /work/app/notes.md\\n@@\\n-a\\n+b\\n*** End Patch"));'
    found = [make("빌드해 주세요.", key="ask"),
             tool("r", "tool_call", 'Tool: exec\ntext(await tools.exec_command({cmd:"python scripts/build.py"}));', "c1"),
             tool("r-out", "tool_result", 'Output:\n{"chunk_id":"a","session_id":4500,"output":""}', "c1"),
             # A patch, another command and the poll in one call: the poll's own response is the second command response.
             tool("p", "tool_call", "Tool: exec\n" + patch + '\ntext(await tools.exec_command({cmd:"ls"}));'
                  '\ntext(await tools.write_stdin({session_id:4500,chars:""}));', "c2"),
             tool("p-out", "tool_result", 'Output:\n{}\n{"chunk_id":"b","exit_code":0,"output":"notes.md\\n"}\n'
                  '{"chunk_id":"c","exit_code":1,"output":"Traceback\\nRuntimeError: build failed\\n"}', "c2")]
    run = next(r for r in run_items(found) if r["sources"][0] == "r")
    assert (run["status"], run["sources"]) == ("failed", ["r", "r-out", "p", "p-out"])
    items, _ = skeleton(found, split_at_runs=True)
    request = story_request({"id": "u"}, "s", {"user_requests": [], "existing_events": []}, items, found,
                            written_blocks(found), [run])
    shown = next(w for w in request["work"] if w["id"] == run["id"])
    assert shown["failure_tail"][-1] == "RuntimeError: build failed"  # its own output, not the other command's
    # When the responses do not pair up with the call's commands, the run stays unknown rather than take a guess.
    found[-1] = tool("p-out", "tool_result", 'Output:\n{"chunk_id":"c","exit_code":0,"output":"ok"}', "c2")
    assert next(r for r in run_items(found) if r["sources"][0] == "r")["status"] == "unknown"


def test_v2_reads_a_changed_unit_again_without_adding_its_events_twice(laboratory):
    _, store, engine, records_, make = laboratory
    records_.extend(records(make))
    assert engine.analyze(FixtureRunner)["status"] == "complete"
    before = store.graph()["events"]
    # One record of the integrated unit changes: the unit is read again, and what it finds is what it found before.
    records_[-1] = make("부호가 반대였습니다. 더하기로 바꿨고 테스트 3개가 통과합니다.", key="said", role="assistant")
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "complete" and result["runner_calls"] == 1, result.get("limitations")
    after = store.graph()["events"]
    assert sorted(e["id"] for e in after) == sorted(e["id"] for e in before)
    assert sum(e["kind"] == "question" for e in after) == 1  # the request is not made twice


def test_v2_a_unit_with_nothing_that_passes_is_skipped_not_the_run(laboratory, monkeypatch):
    from contexttrail.util import FlowError
    from contexttrail.schema import EvidenceValidator
    _, store, engine, records_, make = laboratory
    records_.extend(records(make))

    def refuse(*args, **kwargs):
        raise FlowError("refused")
    monkeypatch.setattr(EvidenceValidator, "check_extraction", refuse)
    monkeypatch.setattr(EvidenceValidator, "salvage_extraction", refuse)
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "failed"  # the only unit failed; nothing stopped the run with an exception
    assert any("nothing of this unit passed the checks" in failure.get("message", "")
               for failure in store.unit_failures().values())


def test_v2_a_draft_that_fails_its_checks_fails_the_unit_with_no_integrate_call(laboratory, monkeypatch):
    from contexttrail.schema import EvidenceValidator
    from contexttrail.util import FlowError
    _, store, engine, records_, make = laboratory
    records_.extend(records(make))

    def refuse(*args, **kwargs):
        raise FlowError("refused")
    monkeypatch.setattr(EvidenceValidator, "apply_delta", refuse)
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert [task["stage"] for task in runner.tasks] == ["story"] and result["status"] == "failed"
    assert any("draft failed its checks" in failure.get("message", "") for failure in store.unit_failures().values())
