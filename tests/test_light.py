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
        tool("test1", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python -m pytest -q"}), "c2"),
        tool("test1-out", "tool_result", "1 failed, 2 passed\nExit code: 1", "c2"),
        tool("edit2", "tool_call", patch.replace("a + b", "a + b  # fixed"), "c3"),
        tool("edit2-out", "tool_result", "The file has been updated.", "c3"),
        tool("test2", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python -m pytest -q"}), "c4"),
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
    assert edges == [{"from": "K1", "to": "K2"}, {"from": "K3", "to": "K4"}]


def test_the_same_check_run_again_with_nothing_changed_between_is_one_result(laboratory):
    _, _, _, _, make = laboratory
    recs = records(make)
    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role), tool_call_id=call_id)
    rerun = [tool("test1b", "tool_call", "Tool: Bash\n" + json.dumps({"command": "python -m pytest -q"}), "c2b"),
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
    assert edges == [{"from": "K1", "to": "K2"}, {"from": "K3", "to": "K4"}, {"from": "K3", "to": "K5"}]


def test_a_light_run_is_one_call_per_unit_and_cites_records_by_code(laboratory):
    scope, store, engine, recs, make = laboratory
    recs.extend(records(make))
    engine.config = replace(engine.config, analysis_mode="light", semantic_review=False, integrate_output="draft_only")
    LightRunner.calls = []
    result = engine.analyze(LightRunner)
    assert result["status"] == "complete", result
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
