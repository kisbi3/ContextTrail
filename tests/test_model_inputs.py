import json

import pytest

from projectflow.analysis import IdAliases, tool_steps
from projectflow.demo import CASES, FixtureRunner
from projectflow.model import SourceRecord
from projectflow.schema import EvidenceValidator
from projectflow.util import FlowError, ident


def whole_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from whole_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from whole_strings(item)


def test_runner_sees_short_ids_and_replies_are_mapped_back(laboratory):
    _, store, engine, records, make = laboratory
    source = ident("src_", "decision")
    records.append(make(CASES[0][0], key=source))
    seen = []

    class Watching(FixtureRunner):
        def run(self, task, schema, cancel):
            seen.append(task)
            return super().run(task, schema, cancel)

    result = engine.analyze(Watching)
    assert result["status"] == "complete"
    assert not [text for task in seen for text in whole_strings(task) if text.startswith(("src_", "ev_"))]
    assert seen[0]["data"]["new_records"][0]["source_id"] == "S1"
    # The stored graph and its evidence use the full IDs again.
    [event] = result["graph"]["events"]
    assert store.evidence_many(event["evidence_ids"])[event["evidence_ids"][0]]["source_id"] == source


def test_aliases_replace_whole_ids_only():
    aliases = IdAliases()
    source, event = ident("src_", "a"), ident("ev_", "b")
    task = {"data": {"source_id": source, "ids": [source, event],
                     "lines": [{"line": 1, "text": f"error at {source}:2"}]},
            "repair": {"instruction": f"인용문 불일치: {source}:2-2"}}
    sent = aliases.wire(task)
    assert sent["data"]["source_id"] == "S1" and sent["data"]["ids"] == ["S1", "E1"]
    assert sent["data"]["lines"][0]["text"] == f"error at {source}:2"  # source text is sent as it is
    assert sent["repair"]["instruction"] == "인용문 불일치: S1:2-2"  # host-written text is shortened
    reply = {"evidence": [{"source_id": "S1", "quote": "S1 is not an ID here"}], "to": "E1", "made_up": "S9"}
    assert aliases.expand(reply) == {"evidence": [{"source_id": source, "quote": "S1 is not an ID here"}],
                                     "to": event, "made_up": "S9"}


def call(key, name, arguments, *, cwd="/work/app"):
    body = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SourceRecord(key, "claude", "s", "tool_call", f"Tool: {name}\n{body}", {}, cwd=cwd, tool_call_id=key)


def result(key, text):
    return SourceRecord(key + "-out", "claude", "s", "tool_result", text, {}, tool_call_id=key)


def test_tool_steps_hint_edit_read_and_run():
    records = [
        call("a", "Edit", {"file_path": "/work/app/src/x.py", "old_string": "1", "new_string": "2"}),
        result("a", "The file has been updated."),
        call("b", "Bash", {"command": "sed -n '1,20p' README.md"}),
        call("c", "Bash", {"command": "cat >> tests/test_x.py <<'EOF'\ndef test(): pass\nEOF"}),
        call("d", "exec", 'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: /work/app/install.sh\\n+x\\n'
                          '*** End Patch"))'),
        call("e", "Write", {"content": "print(1)", "file_path": "/work/app/tool.py"}),
        result("e", "<tool_use_error>File has not been read yet</tool_use_error>"),
    ]
    steps = {step["call"]: step for step in tool_steps(records)}
    assert (steps["a"]["hint"], steps["a"]["target"], steps["a"]["result"]) == ("edit", "src/x.py", "a-out")
    assert steps["b"]["hint"] == "read"
    assert steps["c"]["hint"] == "run"  # starts like a read, but writes a file
    assert (steps["d"]["hint"], steps["d"]["target"]) == ("edit", "patch install.sh")
    assert steps["e"]["failed"] and steps["e"]["target"] == "tool.py"


def test_every_file_edit_must_back_an_extracted_event():
    edit = SourceRecord("edit", "claude", "s", "tool_call", 'Tool: Edit\n{"file_path": "docs/NOTES.md"}', {},
                        tool_call_id="edit")
    done = SourceRecord("done", "claude", "s", "tool_result", "The file has been updated.", {}, tool_call_id="edit")
    records = {"edit": edit, "done": done}
    provided = {"edit": [(1, 2)], "done": [(1, 1)]}
    output = {"status": "complete", "read_requests": [], "snapshot_id": "snap", "unit_id": "unit",
              "event_candidates": [], "edge_candidates": [], "existing_event_matches": [], "open_items": [],
              "limitations": [], "unprocessed_record_ids": []}
    required = {"edit": ("Edit docs/NOTES.md", "done")}
    validator = EvidenceValidator(records, provided, required_citations=required)
    with pytest.raises(FlowError, match=r"파일을 바꾼 도구 호출이 어떤 사건의 근거에도 없습니다: edit\(Edit docs/NOTES.md\)"):
        validator.check_extraction(output, "unit", "snap", {"events": []})
    output["event_candidates"].append({
        "id": "tmp:notes", "kind": "action", "title": "노트 갱신", "summary": "", "actor": "assistant",
        "status": "applied", "basis": "tool_record", "session_ids": ["s"], "worktree_ids": [],
        "recorded_at": None, "occurred_at": None,
        "evidence": [{"source_id": "done", "start_line": 1, "end_line": 1, "quote": "The file has been updated."}]})
    EvidenceValidator(records, provided, required_citations=required).check_extraction(
        output, "unit", "snap", {"events": []})  # citing the edit's result is enough


def test_a_long_tool_result_is_shown_as_head_and_tail_and_the_rest_is_readable(laboratory):
    _, _, engine, records, make = laboratory
    output = "\n".join(f"collected test_{n} ... ok" for n in range(400)) + "\n400 passed in 3.1s"
    records.append(make("pytest 돌려줘"))
    records.append(make("Tool: exec\npytest -q", key="call", role="tool_call"))
    records.append(make(output, key="result", role="tool_result"))
    seen = []

    class Watching(FixtureRunner):
        def run(self, task, schema, cancel):
            seen.append(task)
            return super().run(task, schema, cancel)

    engine.analyze(Watching)
    shown = next(r for r in seen[0]["data"]["new_records"] if r["lines"][0]["text"].startswith("collected test_0"))
    numbers = [line["line"] for line in shown["lines"]]
    assert shown["lines"][-1]["text"] == "400 passed in 3.1s"  # the summary at the end is in view
    assert sum(len(line["text"]) for line in shown["lines"]) < 3_200
    gap = shown["omitted"]
    assert numbers == list(range(1, gap["start_line"])) + list(range(gap["end_line"] + 1, 402))
    person = next(r for r in seen[0]["data"]["new_records"] if r["lines"][0]["text"] == "pytest 돌려줘")
    assert "omitted" not in person


def test_one_repair_round_hears_about_a_bad_quote_and_a_bad_relation_together():
    edit = SourceRecord("edit", "codex", "s", "tool_call", "Tool: exec\n./install.sh", {}, tool_call_id="c")
    ran = SourceRecord("ran", "codex", "s", "tool_result", '{"wall_time_seconds":2.257741238,"exit_code":0}', {},
                       tool_call_id="c")
    said = SourceRecord("said", "codex", "s", "assistant", "설치를 마쳤습니다.", {})
    records = {r.source_id: r for r in (edit, ran, said)}
    provided = {"edit": [(1, 2)], "ran": [(1, 1)], "said": [(1, 1)]}
    def event(key, kind, status, source, quote, line=1):
        return {"id": key, "kind": kind, "title": key, "summary": "", "actor": "assistant", "status": status,
                "basis": "tool_record", "session_ids": ["s"], "worktree_ids": [], "recorded_at": None,
                "occurred_at": None, "evidence": [{"source_id": source, "start_line": line, "end_line": line, "quote": quote}]}
    output = {"status": "complete", "read_requests": [], "snapshot_id": "snap", "unit_id": "unit",
              "event_candidates": [event("tmp:install", "action", "applied", "edit", "./install.sh", 2),
                                   event("tmp:run", "outcome", "observed_success", "ran", '"wall_time_seconds":2.257741,'),
                                   event("tmp:report", "outcome", "reported_complete", "said", "설치를 마쳤습니다.")],
              "edge_candidates": [{"id": "tmp:e1", "from_event_id": "tmp:install", "to_event_id": "tmp:report",
                                   "relation": "verifies", "basis": "explicit", "rationale": "", "active": True,
                                   "evidence": [{"source_id": "said", "start_line": 1, "end_line": 1, "quote": "설치를 마쳤습니다."}]}],
              "existing_event_matches": [], "open_items": [], "limitations": [], "unprocessed_record_ids": []}
    with pytest.raises(FlowError) as caught:
        EvidenceValidator(records, provided).check_extraction(output, "unit", "snap", {"events": []})
    message = str(caught.value)
    assert "인용문이 제공된 원문 범위에서 유일하게 일치하지 않습니다: ran:1-1" in message
    assert "verifies 관계는" in message and "tmp:e1" in message


def test_an_output_whose_end_cannot_be_shown_is_sent_whole():
    from projectflow.analysis import _view
    one_line_json = "Chunk ID: 1\nWall time: 2s\nExit code: 0\nOutput:\n" + '{"output":"' + "x" * 5_400 + '"}'
    assert _view(SourceRecord("r", "codex", "s", "tool_result", one_line_json, {})) is None
