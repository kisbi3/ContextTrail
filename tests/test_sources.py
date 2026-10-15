import json
import os
from pathlib import Path

from contexttrail.git_context import Scope
from contexttrail.sources.local import collect_logs, parse_claude, parse_codex
from contexttrail.util import dumps


def write(path, rows, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dumps(r) for r in rows) + "\n" + tail, encoding="utf-8")


def codex_rows(folder):
    return [{"type": "session_meta", "payload": {"id": "native-session", "cwd": str(folder)}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "우선 JSON 파일로 저장하자."}]}}]


def test_codex_scope_not_string_prefix(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    unrelated = tmp_path / "app-backup"
    unrelated.mkdir()
    scope = Scope.resolve(folder)
    good, bad = tmp_path / "good.jsonl", tmp_path / "bad.jsonl"
    write(good, codex_rows(folder))
    write(bad, codex_rows(unrelated))
    assert len(parse_codex(good, scope).records) == 1
    assert not parse_codex(bad, scope).records


def test_codex_ui_duplicate_is_not_double_counted(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    write(path, codex_rows(folder) + [{"type": "event_msg", "payload": {"type": "user_message", "message": "우선 JSON 파일로 저장하자."}}])
    assert len(parse_codex(path, Scope.resolve(folder)).records) == 1


def test_codex_environment_context_is_metadata_only_when_standalone(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    environment = "<environment_context>\n<cwd>" + str(folder) + "</cwd>\n</environment_context>"
    write(path, [{"type": "session_meta", "payload": {"id": "session", "cwd": str(folder)}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                       "content": environment}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                       "content": environment + "\n실제 질문"}}])
    records = parse_codex(path, Scope.resolve(folder)).records
    assert records[0].role == "metadata"
    assert records[0].lineage["kind"] == "environment_context"
    assert records[1].role == "user"
    assert "실제 질문" in records[1].content


def test_append_and_incomplete_json_boundary(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    rows = codex_rows(folder)
    write(path, rows, '{"type":"response_item"')
    first = parse_codex(path, Scope.resolve(folder))
    assert len(first.records) == 1 and any("last JSONL record still being written" in x for x in first.limitations)
    with path.open("a") as stream:
        stream.write(',"payload":{"type":"function_call_output","call_id":"c","output":"OK"}}\n')
    second = parse_codex(path, Scope.resolve(folder))
    assert len(second.records) == 2
    assert second.records[0].source_id == first.records[0].source_id


def test_archiving_same_content_keeps_ids(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    home = tmp_path / "codex"
    path = home / "sessions" / "original.jsonl"
    write(path, codex_rows(folder))
    scope = Scope.resolve(folder)
    first = collect_logs(scope, codex_home=home, claude_home=tmp_path / "none")
    dest = home / "archived_sessions" / "moved.jsonl"
    dest.parent.mkdir()
    path.rename(dest)
    second = collect_logs(scope, codex_home=home, claude_home=tmp_path / "none")
    assert first.records[0].source_id == second.records[0].source_id
    assert first.records[0].content_hash == second.records[0].content_hash
    assert first.records[0].locator != second.records[0].locator


def test_tool_call_result_link_and_cwd(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    rows = codex_rows(folder) + [
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call-42", "arguments": '{"cmd":"false"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-42", "output": "exit 1"}}]
    write(path, rows)
    records = parse_codex(path, Scope.resolve(folder)).records
    assert [r.role for r in records[-2:]] == ["tool_call", "tool_result"]
    assert records[-1].tool_call_id == records[-2].tool_call_id == "call-42"


def test_unknown_record_is_reported(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    write(path, codex_rows(folder) + [{"type": "future_v99", "payload": {}}])
    assert any("unsupported record" in x for x in parse_codex(path, Scope.resolve(folder)).limitations)


def test_private_reasoning_not_collected(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    write(path, codex_rows(folder) + [{"type": "response_item", "payload": {"type": "reasoning", "text": "PRIVATE"}}])
    assert all("PRIVATE" not in r.content for r in parse_codex(path, Scope.resolve(folder)).records)


def test_claude_preserves_roles_and_tool_ids(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "claude.jsonl"
    write(path, [
        {"type": "assistant", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
         "message": {"content": [{"type": "thinking", "thinking": "PRIVATE"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "u1", "sessionId": "session", "cwd": str(folder),
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert [r.role for r in records] == ["tool_call", "tool_result"]
    assert records[0].tool_call_id == records[1].tool_call_id == "t1"
    assert records[1].parent_record_id == "u1"


def test_claude_sidecar_outside_session_is_denied(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("NEVER_READ_THIS", encoding="utf-8")
    path = tmp_path / "projects" / "project" / "session.jsonl"
    write(path, [{"type": "user", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": f"Full output saved to: {secret}"}]}}])
    result = parse_claude(path, Scope.resolve(folder))
    assert all("NEVER_READ_THIS" not in r.content for r in result.records)
    assert any("outside the allowed directory" in x for x in result.limitations)


def test_claude_allowed_sidecar(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "projects" / "project" / "session.jsonl"
    sidecar = path.parent / "session" / "tool-results" / "r.txt"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("4 passed", encoding="utf-8")
    write(path, [{"type": "user", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": f"Full output saved to: {sidecar}"}]}}])
    result = parse_claude(path, Scope.resolve(folder))
    assert len(result.records) == 2 and result.records[-1].content == "4 passed"


def test_unlinked_subagent_not_guessed(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "agent.jsonl"
    write(path, [{"type": "assistant", "uuid": "u1", "sessionId": "session", "cwd": str(folder), "isSidechain": True,
                  "message": {"content": "claim"}}])
    result = parse_claude(path, Scope.resolve(folder))
    assert not result.records and any("subagent" in x for x in result.limitations)


def test_log_symlink_is_not_followed(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    actual, link = tmp_path / "real.jsonl", tmp_path / "link.jsonl"
    write(actual, codex_rows(folder))
    link.symlink_to(actual)
    assert not parse_codex(link, Scope.resolve(folder)).records


def test_session_summary_is_derived(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "input.jsonl"
    write(path, codex_rows(folder) + [{"type": "compacted", "payload": {"summary": "완료했음"}}])
    records = parse_codex(path, Scope.resolve(folder)).records
    assert records[-1].derivation == "summary"


def test_claude_compact_user_record_is_summary_metadata(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "claude.jsonl"
    write(path, [{"type": "user", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
                  "isCompactSummary": True,
                  "message": {"role": "user", "content": "Summary: earlier work"}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert len(records) == 1
    assert records[0].role == "metadata" and records[0].derivation == "summary"
    assert records[0].lineage["kind"] == "compaction"


def compacted(message=""):
    return {"type": "compacted", "payload": {"message": message, "replacement_history": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old question"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "old answer"}]},
    ]}}


def test_codex_replacement_history_is_derived_context(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "codex.jsonl"
    # At the head of a file nothing above holds that history, so it is kept.
    write(path, codex_rows(folder)[:1] + [compacted()] + codex_rows(folder)[1:])
    records = parse_codex(path, Scope.resolve(folder)).records
    compact = [r for r in records if r.derivation == "summary"]
    assert len(compact) == 1 and compact[0].role == "metadata"
    assert "old question" in compact[0].content and "old answer" in compact[0].content
    assert compact[0].lineage["replacement_items"] == 2


def test_mid_file_codex_compaction_keeps_its_summary_not_a_second_copy_of_the_history(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "codex.jsonl"
    write(path, codex_rows(folder) + [compacted("Summary: JSON 저장으로 정함")])
    compact = [r for r in parse_codex(path, Scope.resolve(folder)).records if r.derivation == "summary"]
    assert [r.content for r in compact] == ["Summary: JSON 저장으로 정함"]


def test_codex_pasted_image_is_a_marker_not_base64(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "codex.jsonl"
    image = "data:image/png;base64," + "iVBORw0KGgo" * 100
    write(path, codex_rows(folder)[:1] + [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "이 화면 봐줘"}, {"type": "input_image", "image_url": image, "detail": "high"}]}}])
    [record] = parse_codex(path, Scope.resolve(folder)).records
    assert "iVBORw0KGgo" not in record.content
    assert record.content.startswith("이 화면 봐줘\n[input_image payload omitted") and "media_type=image/png" in record.content


def test_codex_tool_search_is_call_result_pair(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "codex.jsonl"
    rows = codex_rows(folder) + [
        {"type": "response_item", "payload": {"type": "tool_search_call", "id": "ts1", "call_id": "c1",
                                                  "status": "completed", "arguments": {"query": "pytest"}}},
        {"type": "response_item", "payload": {"type": "tool_search_output", "call_id": "c1",
                                                  "status": "completed", "tools": [{"name": "pytest"}]}}]
    write(path, rows)
    records = parse_codex(path, Scope.resolve(folder)).records
    pair = [r for r in records if r.tool_call_id == "c1"]
    assert [r.role for r in pair] == ["tool_call", "tool_result"]


def test_claude_file_attachment_is_metadata_snapshot(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "claude.jsonl"
    write(path, [{"type": "attachment", "uuid": "a1", "sessionId": "session", "cwd": str(folder),
                  "attachment": {"type": "edited_text_file", "filename": str(folder / "a.py"),
                                 "snippet": "1\tprint('x')"}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert len(records) == 1 and records[0].role == "metadata"
    assert records[0].lineage["kind"] == "attachment"
    assert "print('x')" in records[0].content


def test_inline_image_base64_is_not_copied_to_prompt_record(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "claude.jsonl"
    payload = "A" * 100_000
    write(path, [{"type": "user", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
                  "message": {"content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}}]}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert len(records) == 1 and records[0].role == "metadata"
    assert payload[:1000] not in records[0].content
    assert "payload omitted from text analysis" in records[0].content


def test_collect_includes_only_verified_claude_subagent_and_lineage(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    home = tmp_path / "claude"
    project = home / "projects" / "project"
    main = project / "session.jsonl"
    write(main, [
        {"type": "assistant", "uuid": "m1", "sessionId": "session", "cwd": str(folder),
         "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Agent", "input": {}}]}},
        {"type": "user", "uuid": "m2", "sessionId": "session", "cwd": str(folder),
         "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "launched"}]},
         "toolUseResult": {"agentId": "abc"}},
    ])
    subdir = project / "session" / "subagents"; subdir.mkdir(parents=True)
    write(subdir / "agent-abc.jsonl", [{"type": "assistant", "uuid": "s1", "sessionId": "session", "cwd": str(folder),
                                         "isSidechain": True, "agentId": "abc", "message": {"content": "subagent result"}}])
    (subdir / "agent-abc.meta.json").write_text(dumps({"toolUseId": "tool-1", "spawnDepth": 1, "model": "sonnet"}), encoding="utf-8")
    snapshot = collect_logs(Scope.resolve(folder), codex_home=tmp_path / "none", claude_home=home)
    sub = [r for r in snapshot.records if "subagent result" in r.content]
    assert len(sub) == 1
    assert sub[0].lineage["kind"] == "subagent"
    assert sub[0].lineage["parent_tool_call_id"] == "tool-1"


def test_relocated_sidecar_reconnects_only_same_session_directory(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "claude" / "projects" / "project" / "session.jsonl"
    local = path.parent / "session" / "tool-results" / "r.txt"
    local.parent.mkdir(parents=True); local.write_text("real full output", encoding="utf-8")
    original = "/home/u/.claude/projects/project/session/tool-results/r.txt"
    write(path, [{"type": "user", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": f"Full output saved to: {original}"}]}}])
    result = parse_claude(path, Scope.resolve(folder))
    assert any(r.locator.get("kind") == "sidecar" and r.content == "real full output" for r in result.records)


def test_codex_subagent_parent_session_is_preserved(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "codex.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "child", "cwd": str(folder), "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "parent", "depth": 1, "agent_nickname": "Hooke", "agent_role": "worker"}}}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}}]
    write(path, rows)
    records = parse_codex(path, Scope.resolve(folder)).records
    assert records and all(r.lineage["parent_session_id"] == "parent" for r in records)



def test_forked_codex_subagent_skips_the_history_it_copied_from_its_parent(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": "parent", "depth": 1}}}
    def message(role, text):
        kind = "input_text" if role == "user" else "output_text"
        return {"type": "response_item", "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}}
    rows = [{"type": "session_meta", "payload": {"id": "child", "cwd": str(folder), "source": spawn,
                                                 "forked_from_id": "parent", "subagent_history_start_ordinal": 3}},
            message("user", "parent asked this"), message("assistant", "parent answered this"),
            message("user", "child task"), message("assistant", "child did this")]
    write(tmp_path / "child.jsonl", rows)
    snapshot = parse_codex(tmp_path / "child.jsonl", Scope.resolve(folder))
    assert [r.content for r in snapshot.records] == ["child task", "child did this"]
    assert snapshot.files[0]["selection"]["inherited_rows_skipped"] == 2
    assert snapshot.files[0]["selection"]["forked_from"] == "parent"


def test_a_fork_the_person_made_keeps_its_own_messages(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    rows = [{"type": "session_meta", "payload": {"id": "fork", "cwd": str(folder), "forked_from_id": "parent",
                                                 "history_base": {"thread_id": "parent", "end_ordinal_exclusive": 40}}},
            *codex_rows(folder)[1:]]
    write(tmp_path / "fork.jsonl", rows)
    records = parse_codex(tmp_path / "fork.jsonl", Scope.resolve(folder)).records
    assert [r.content for r in records] == ["우선 JSON 파일로 저장하자."]
    assert not records[0].lineage  # still a message the person typed

def test_long_text_record_is_split_with_shared_lineage(tmp_path):
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "claude.jsonl"
    text = "line\n" * 20_000
    write(path, [{"type": "assistant", "uuid": "u1", "sessionId": "session", "cwd": str(folder),
                  "message": {"content": text}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert len(records) > 1 and max(len(r.content) for r in records) <= 32_000
    assert len({r.lineage["fragment_of"] for r in records}) == 1


def test_codex_records_carry_the_model_and_effort_of_their_turn(tmp_path):
    from dataclasses import replace
    from contexttrail.analysis import record_for_model
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "codex.jsonl"
    text = lambda role, words: {"type": "response_item", "payload": {"type": "message", "role": role,
                                "content": [{"type": "input_text" if role == "user" else "output_text", "text": words}]}}
    write(path, [
        {"type": "session_meta", "payload": {"id": "native-session", "cwd": str(folder), "model_provider": "openai"}},
        text("user", "Words before any turn."),  # what Codex injects before the first turn names no model
        {"type": "turn_context", "payload": {"cwd": str(folder), "model": "gpt-6.1-sol", "effort": "high", "summary": "none"}},
        text("user", "Fix the tests."),  # the turn's context comes first, then the person's prompt and the work
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": '{"cmd":"pytest"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "1 passed"}},
        text("assistant", "Done."),
        {"type": "turn_context", "payload": {"cwd": str(folder), "model": "gpt-6-luna", "effort": "medium"}},
        text("user", "Now with Luna."),
        text("assistant", "Sure.")])
    records = parse_codex(path, Scope.resolve(folder)).records
    assert [r.role for r in records] == ["user", "user", "tool_call", "tool_result", "assistant", "user", "assistant"]
    sol = {"model": "gpt-6.1-sol", "effort": "high", "provider": "openai"}
    assert records[0].authoring == {}
    assert [r.authoring for r in records[1:5]] == [sol] * 4
    assert [r.authoring for r in records[5:]] == [{"model": "gpt-6-luna", "effort": "medium", "provider": "openai"}] * 2
    # Outside the content hash (a stored record filled in later is not re-analysed) and hidden from the model.
    assert replace(records[1], authoring={}).content_hash == records[1].content_hash
    assert "authoring" not in record_for_model(records[1])


def test_claude_records_carry_the_assistant_model_and_a_tool_result_follows_its_call(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "claude.jsonl"
    write(path, [
        {"type": "user", "uuid": "u0", "sessionId": "session", "cwd": str(folder),
         "message": {"role": "user", "content": "Fix it."}},
        {"type": "assistant", "uuid": "u1", "parentUuid": "u0", "sessionId": "session", "cwd": str(folder),
         "effort": "high", "perTurnEffort": "xhigh",
         "message": {"model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "u1", "sessionId": "session", "cwd": str(folder),
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}},
        {"type": "assistant", "uuid": "u3", "parentUuid": "u2", "sessionId": "session", "cwd": str(folder), "effort": "high",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API error"}]}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert [r.role for r in records] == ["user", "tool_call", "tool_result", "assistant"]
    assert records[0].authoring == {}  # the person's message names no model
    assert records[1].authoring == {"model": "claude-opus-5-5", "effort": "xhigh"}  # the turn's effort over the session's
    assert records[2].authoring == records[1].authoring  # the tool answered the call that model made
    assert records[3].authoring == {}  # a message Claude Code wrote itself has no model behind it


def test_a_codex_exec_run_is_the_sub_agent_of_the_call_that_started_it_and_has_no_person(tmp_path):
    from contexttrail.model import is_user_prompt
    folder = tmp_path / "app"; folder.mkdir()
    claude_home, codex_home = tmp_path / "claude", tmp_path / "codex"
    thread = "01a13d65-195c-7040-a7e1-7a940f6dc20f"
    write(claude_home / "projects" / "project" / "session.jsonl", [
        {"type": "assistant", "uuid": "m1", "sessionId": "session", "cwd": str(folder),
         "message": {"model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                                "input": {"command": "codex exec --json 'review the diff'"}}]}},
        {"type": "user", "uuid": "m2", "parentUuid": "m1", "sessionId": "session", "cwd": str(folder),
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": '{"type":"thread.started","thread_id":"%s"}\n{"type":"item.completed"}' % thread}]}}])

    def rollout(name, session_id, words):
        write(codex_home / "sessions" / "2026" / "10" / "15" / f"rollout-2026-10-15T11-30-04-{name}.jsonl", [
            {"type": "session_meta", "payload": {"id": session_id, "cwd": str(folder), "originator": "codex_exec", "source": "exec"}},
            {"type": "turn_context", "payload": {"cwd": str(folder), "model": "gpt-6.1-sol", "effort": "high"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": words}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One issue: missing except."}]}}])
    rollout(thread, thread, "review the diff")
    rollout("ffffffff-0000-0000-0000-000000000001", "ffffffff-0000-0000-0000-000000000001", "evaluate the test strategy")
    records = collect_logs(Scope.resolve(folder), codex_home=codex_home, claude_home=claude_home).records
    called = [r for r in records if r.session_id == thread]
    assert [r.role for r in called] == ["user", "assistant"]
    # Started by the Claude Code call whose result printed its thread ID: that session's sub-agent, named by the tool.
    assert all(r.lineage == {"kind": "subagent", "agent_type": "codex exec", "parent_session_id": "session",
                             "parent_tool_call_id": "t1", "parent_result_record_id": next(
                                 r.source_id for r in records if r.role == "tool_result")} for r in called)
    assert not is_user_prompt(called[0])  # the prompt was Claude's, not the person's
    assert called[1].authoring == {"model": "gpt-6.1-sol", "effort": "high"}
    # A run whose caller is not in the records stays a run with no person.
    alone = [r for r in records if r.session_id == "ffffffff-0000-0000-0000-000000000001"]
    assert alone and all(r.lineage == {"kind": "unattended", "agent_type": "codex exec"} for r in alone)
    assert not is_user_prompt(alone[0])


def test_a_claude_print_run_has_no_person(tmp_path):
    from contexttrail.model import is_user_prompt
    folder = tmp_path / "app"; folder.mkdir()
    path = tmp_path / "claude.jsonl"
    write(path, [
        {"type": "user", "uuid": "u0", "sessionId": "session", "cwd": str(folder), "entrypoint": "sdk-cli",
         "message": {"role": "user", "content": "Summarise the failing tests."}},
        {"type": "assistant", "uuid": "u1", "parentUuid": "u0", "sessionId": "session", "cwd": str(folder), "entrypoint": "sdk-cli",
         "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Two fail on Windows paths."}]}}])
    records = parse_claude(path, Scope.resolve(folder)).records
    assert [r.lineage for r in records] == [{"kind": "unattended", "agent_type": "claude -p"}] * 2
    assert not is_user_prompt(records[0])


def test_a_transcript_over_the_size_limit_is_named_with_the_limit_in_the_limitations(tmp_path, monkeypatch):
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "big.jsonl"
    write(path, codex_rows(folder))
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 10)
    snapshot = parse_codex(path, Scope.resolve(folder))
    assert not snapshot.records
    assert snapshot.limitations[0] == f"log file not read: big.jsonl (over the size limit of 10 bytes: {path.stat().st_size} bytes)"


def test_a_forked_claude_session_drops_the_copied_rows_and_keeps_where_it_came_from(tmp_path):
    from dataclasses import replace
    from contexttrail.analysis import record_for_model
    folder = tmp_path / "app"; folder.mkdir()
    home = tmp_path / "claude"; project = home / "projects" / "project"

    def row(uuid, parent, session, at, text, role="user"):
        return {"type": role, "uuid": uuid, "parentUuid": parent, "sessionId": session, "cwd": str(folder),
                "timestamp": at, "message": {"role": role, "content": text}}
    parent = [row("u1", None, "parent", "2026-09-25T08:40:00Z", "Start the work."),
              row("u2", "u1", "parent", "2026-09-25T08:41:00Z", "Started.", "assistant"),
              row("u3", "u2", "parent", "2026-09-26T15:00:00Z", "Now the docs."),
              row("u4", "u3", "parent", "2026-09-26T15:01:00Z", "Docs done.", "assistant"),
              row("u5", "u4", "parent", "2026-09-26T15:40:00Z", "And the parent goes on.")]
    write(project / "parent.jsonl", parent)
    # Two forks of the same point: each file copies the recent rows under its own session ID, then its own rows.
    for name, at in (("fork-a", "2026-09-26T15:50:00Z"), ("fork-b", "2026-09-27T02:38:00Z")):
        copies = [{**r, "sessionId": name} for r in parent[2:4]]
        write(project / f"{name}.jsonl", copies + [row(f"{name}-1", "u4", name, at, "Continue with the tests.")])
    # A fork whose parent is gone keeps its copies: they are the only text of that history.
    write(project / "orphan.jsonl", [row("x1", None, "orphan", "2026-09-20T10:00:00Z", "Old words."),
                                     row("orphan-1", "x1", "orphan", "2026-09-28T10:00:00Z", "New words.")])
    records = collect_logs(Scope.resolve(folder), codex_home=tmp_path / "none", claude_home=home).records
    by_session = {}
    for r in records:
        by_session.setdefault(r.session_id, []).append(r)
    assert [r.native_record_id for r in by_session["parent"]] == ["u1", "u2", "u3", "u4", "u5"]
    assert [r.native_record_id for r in by_session["fork-a"]] == ["fork-a-1"]
    assert [r.native_record_id for r in by_session["fork-b"]] == ["fork-b-1"]
    assert by_session["fork-a"][0].fork == {"from_session_id": "parent", "point_at": "2026-09-26T15:01:00Z"}
    assert by_session["fork-b"][0].fork == by_session["fork-a"][0].fork
    assert all(r.fork == {} for r in by_session["parent"])
    assert [r.native_record_id for r in by_session["orphan"]] == ["x1", "orphan-1"]
    own = by_session["fork-a"][0]
    assert replace(own, fork={}).content_hash == own.content_hash  # outside the hash: no unit is sent again
    assert "fork" not in record_for_model(own)
