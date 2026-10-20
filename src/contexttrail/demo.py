"""Deterministic SYNTHETIC fixtures. Not an AI substitute, not a production Runner."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .i18n import language, tr
from .util import FlowError, dumps


# One synthetic story in two languages: a decision, an alternative, a change, a failed test, a
# reversal, a completion report and a passing test. `CASES` is the Korean story the tests use;
# `cases()` picks the story of the screen language, and the fixture runner recognises both.
CASES = [
    ("우선 JSON 파일로 저장하자.", "decision", "JSON 저장 채택", "adopted", "user", "explicit_statement"),
    ("SQLite도 검토할 수 있습니다.", "proposal", "SQLite 대안 제안", "proposed", "assistant", "explicit_statement"),
    ("JSON 저장 코드 patch 적용 완료.", "action", "JSON 저장 구현", "applied", "tool", "tool_record"),
    ("동시 쓰기 테스트: FAILED — JSONDecodeError", "outcome", "동시 쓰기 테스트 실패", "observed_failure", "tool", "tool_record"),
    ("동시 쓰기 때문에 SQLite로 바꾸자.", "revision", "SQLite로 전환 결정", "adopted", "user", "explicit_statement"),
    ("SQLite로 변경했고 테스트도 통과했습니다.", "outcome", "SQLite 변경 완료 보고", "reported_complete", "assistant", "explicit_statement"),
    ("SQLite 동시 쓰기 테스트: 4 passed", "outcome", "SQLite 동시 쓰기 확인", "observed_success", "tool", "tool_record"),
]
CASES_EN = [
    ("Let's store it as a JSON file for now.", "decision", "JSON file storage adopted", "adopted", "user", "explicit_statement"),
    ("SQLite is also worth considering.", "proposal", "SQLite proposed as an alternative", "proposed", "assistant", "explicit_statement"),
    ("Patch applied: JSON storage code.", "action", "JSON storage implemented", "applied", "tool", "tool_record"),
    ("concurrent write test: FAILED — JSONDecodeError", "outcome", "Concurrent write test failed", "observed_failure", "tool", "tool_record"),
    ("Concurrent writes break it, so let's switch to SQLite.", "revision", "Switch to SQLite decided", "adopted", "user", "explicit_statement"),
    ("Switched to SQLite and the tests pass too.", "outcome", "SQLite change reported done", "reported_complete", "assistant", "explicit_statement"),
    ("SQLite concurrent write test: 4 passed", "outcome", "SQLite concurrent writes confirmed", "observed_success", "tool", "tool_record"),
]
def cases() -> list[tuple]:
    """The story of the screen language."""
    return CASES if language() == "ko" else CASES_EN


# The tool calls of the story, as code reads them (edits and checks are found in the calls, not in the text): the
# JSON storage edit and its failing test (Codex, which runs both from its `exec` tool), then the SQLite edit and its
# passing test (Claude Code).
TEST = "pytest tests/test_store.py -q"
PATCH = ('text(await tools.apply_patch("*** Begin Patch\\n*** Update File: store.py\\n@@\\n-pass\\n'
         '+json.dump(data, file)\\n*** End Patch"));')
RUN = f'text(await tools.exec_command({{cmd:"{TEST}"}}));'
FAILED_RUN = "\n1 failed, 3 passed\nExit code: 1"


def story_steps() -> list[dict]:
    """The synthetic story step by step: what each record is, in the order written. `case` points at `cases()`."""
    return [
        {"session": "codex", "role": "user", "case": 0},
        {"session": "codex", "role": "assistant", "case": 1},
        {"session": "codex", "role": "tool_call", "tool": "exec", "input": PATCH, "call": "demo-call-2"},
        {"session": "codex", "role": "tool_result", "case": 2, "call": "demo-call-2"},
        {"session": "codex", "role": "tool_call", "tool": "exec", "input": RUN, "call": "demo-call-3"},
        {"session": "codex", "role": "tool_result", "case": 3, "suffix": FAILED_RUN, "call": "demo-call-3"},
        {"session": "claude", "role": "user", "case": 4},
        {"session": "claude", "role": "tool_call", "tool": "Edit", "call": "demo-call-5",
         "input": {"file_path": "store.py", "old_string": "json.dump(data, file)", "new_string": "db.execute(INSERT, data)"}},
        {"session": "claude", "role": "tool_result", "text": "The file has been updated.", "call": "demo-call-5"},
        {"session": "claude", "role": "tool_call", "tool": "Bash", "input": {"command": TEST}, "call": "demo-call-6"},
        {"session": "claude", "role": "tool_result", "case": 6, "call": "demo-call-6"},
        {"session": "claude", "role": "assistant", "case": 5},
    ]


def step_text(step: dict, story: list[tuple]) -> str:
    if step["role"] == "tool_call":
        return step["input"] if isinstance(step["input"], str) else json.dumps(step["input"], ensure_ascii=False)
    return step.get("text") or story[step["case"]][0] + step.get("suffix", "")


class FixtureRunner:
    """Answers the story call as a model told about the synthetic story would: the work items named back (code's own
    titles), a story event for each assistant message of the story, and each request answered by the first story
    event written after it in its session. In an audit, what a note already records is not written again."""
    name, version, model, is_mock = "synthetic-fixture", "fixture-v1", None, True

    def __init__(self):
        self.calls = 0
        self.tasks = []

    def preflight(self):
        return {"runner": self.name, "live_model_test": False}

    def run(self, task, schema, cancel):
        self.calls += 1
        self.tasks.append(copy.deepcopy(task))
        data = task["data"]
        if task["stage"] != "story":
            raise FlowError(f"the synthetic fixture answers the story call only, not {task['stage']}")
        known = {case[0]: case for case in CASES + CASES_EN}
        written = data.get("written", [])
        story = []
        for block in written:
            case = known.get(block["text"].strip())
            if block["role"] != "assistant" or block.get("noted_by") or not case or case[4] != "assistant":
                continue
            text, kind, title, status, _, _ = case
            story.append({"id": f"N{len(story) + 1}", "kind": kind, "status": status, "title": title, "why": None,
                          "summary": text, "blocks": [block["block"]], "work": []})
        place = {block["block"]: n for n, block in enumerate(written)}
        asked_in = {block["source_id"]: place[block["block"]] for block in written}
        requests = []
        for request in data.get("user_requests", []):
            after = asked_in.get(request["source_id"], len(written))
            reply = next((event for event in story if place[event["blocks"][0]] > after), None)
            requests.append({"id": request["id"],
                             "answered_by": [{"item": reply["id"], "blocks": reply["blocks"]}] if reply else []})
        return {"status": "complete", "snapshot_id": data["snapshot_id"], "unit_id": data["unit_id"],
                # An empty title keeps code's own name for the item, in the output language.
                "work": [{"id": item["id"], "title": "", "summary": "", "checked_by": []} for item in data.get("work", [])],
                "story": story, "requests": requests, "links": [],
                "turn_summary": tr("합성 구간: 저장 방식을 정하고 바꿈", "Synthetic stretch: the storage chosen and changed")
                                if story else tr("합성 구간: 작업 기록만", "Synthetic stretch: the work record only")}


def create_demo(directory: Path) -> tuple[Path, Path, Path]:
    directory = directory.expanduser().resolve()
    if directory.exists() and any(directory.iterdir()):
        raise FlowError(tr("demo는 비어 있거나 새 디렉터리에서만 생성할 수 있습니다.", "The demo can only be created in an empty or new directory."))
    folder, codex_home, claude_home = directory / "sample-project", directory / "fixture-codex", directory / "fixture-claude"
    folder.mkdir(parents=True, exist_ok=True)
    codex = codex_home / "sessions" / "demo.jsonl"
    claude = claude_home / "projects" / "demo" / "demo-claude.jsonl"
    codex.parent.mkdir(parents=True)
    claude.parent.mkdir(parents=True)
    story = cases()
    codex_rows = [{"type": "session_meta", "payload": {"id": "demo-codex", "cwd": str(folder)}}]
    claude_rows = []
    for n, step in enumerate(story_steps()):
        text, stamp = step_text(step, story), f"2026-09-22T10:{n:02d}:00Z"
        if step["session"] == "codex":
            payload = ({"type": "function_call", "name": step["tool"], "arguments": text, "call_id": step["call"]}
                       if step["role"] == "tool_call" else
                       {"type": "function_call_output", "call_id": step["call"], "output": text}
                       if step["role"] == "tool_result" else
                       {"type": "message", "role": step["role"],
                        "content": [{"type": "input_text" if step["role"] == "user" else "output_text", "text": text}]})
            codex_rows.append({"type": "response_item", "timestamp": stamp, "payload": payload})
            continue
        content = ([{"type": "tool_use", "id": step["call"], "name": step["tool"], "input": step["input"]}]
                   if step["role"] == "tool_call" else
                   [{"type": "tool_result", "tool_use_id": step["call"], "content": text}]
                   if step["role"] == "tool_result" else [{"type": "text", "text": text}])
        kind = "assistant" if step["role"] in ("assistant", "tool_call") else "user"
        claude_rows.append({"type": kind, "uuid": f"demo-record-{n}", "sessionId": "demo-claude", "cwd": str(folder),
                            "timestamp": stamp, "message": {"role": kind, "content": content}})
    codex.write_text("\n".join(dumps(r) for r in codex_rows) + "\n", encoding="utf-8")
    claude.write_text("\n".join(dumps(r) for r in claude_rows) + "\n", encoding="utf-8")
    (directory / "DEMO_ONLY.txt").write_text(tr("합성 데이터입니다. 실제 사용자 프로젝트나 AI 분석 결과가 아닙니다.\n",
                                                "Synthetic data. Not a real user project and not an AI analysis result.\n"), encoding="utf-8")
    return folder, codex_home, claude_home
