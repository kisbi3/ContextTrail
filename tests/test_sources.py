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


def test_a_transcript_codex_compressed_keeps_its_records_and_ids(tmp_path):
    import zstandard
    folder = tmp_path / "app"
    folder.mkdir()
    home = tmp_path / "codex"
    path = home / "sessions" / "2026" / "10" / "12" / "rollout-2026-10-12T11-24-23-native-session.jsonl"
    rows = codex_rows(folder)
    rows[0]["payload"].pop("id")  # the session is then named after the file, which must not change either
    write(path, rows)
    scope = Scope.resolve(folder)
    plain = collect_logs(scope, codex_home=home, claude_home=tmp_path / "none")
    packed = path.with_name(path.name + ".zst")
    packed.write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
    # While both copies are there the plain one is read, once.
    both = collect_logs(scope, codex_home=home, claude_home=tmp_path / "none")
    assert [r.source_id for r in both.records] == [r.source_id for r in plain.records]
    path.unlink()
    compressed = collect_logs(scope, codex_home=home, claude_home=tmp_path / "none")
    assert [(r.source_id, r.content_hash, r.session_id) for r in compressed.records] == \
        [(r.source_id, r.content_hash, r.session_id) for r in plain.records]
    assert compressed.records[0].locator["path"] == str(packed)


def test_a_compressed_transcript_with_no_decompressor_is_said_and_skipped(tmp_path, monkeypatch):
    import builtins
    import zstandard
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    home = tmp_path / "codex"
    packed = home / "sessions" / "rollout.jsonl.zst"
    packed.parent.mkdir(parents=True)
    packed.write_bytes(zstandard.ZstdCompressor().compress(("\n".join(dumps(r) for r in codex_rows(folder)) + "\n").encode()))
    real_import = builtins.__import__

    def no_zstd(name, *args, **kwargs):
        if name in ("zstandard", "compression"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_zstd)
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    found = collect_logs(Scope.resolve(folder), codex_home=home, claude_home=tmp_path / "none")
    assert found.records == []
    assert any("log file not read: rollout.jsonl.zst" in note and "zstd" in note for note in found.limitations)


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


def test_a_codex_message_sent_through_the_queue_is_marked_and_is_not_the_persons_request(tmp_path):
    from contexttrail.model import is_queued_message, is_user_prompt
    from contexttrail.schema import authoring_of
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "codex.jsonl"
    text = lambda role, words: {"type": "response_item", "payload": {"type": "message", "role": role,
                                "content": [{"type": "input_text" if role == "user" else "output_text", "text": words}]}}
    started = lambda trigger: {"type": "event_msg", "payload": {"type": "task_started", "turn_attribution": {"turn_trigger": trigger}}}
    context = {"type": "turn_context", "payload": {"cwd": str(folder), "model": "gpt-6.1-sol", "effort": "high"}}
    write(path, [
        {"type": "session_meta", "payload": {"id": "native-session", "cwd": str(folder), "model_provider": "openai"}},
        started("composer"), context, text("user", "Start the download and report every hour."), text("assistant", "Started."),
        started("queue"), context, text("user", "Hourly report: 65% verified."), text("assistant", "65% is verified."),
        started("user"), context, text("user", "How far along is it?"), text("assistant", "Almost done.")])
    records = parse_codex(path, Scope.resolve(folder)).records
    assert [r.authoring.get("trigger") for r in records] == [None, None, "queue", None, None, None]
    assert [is_user_prompt(r) for r in records if r.role == "user"] == [True, False, True]
    assert [is_queued_message(r) for r in records] == [False, False, True, False, False, False]
    # The mark says how the turn started; it is not an agent of its own.
    sources = [{"provider": "codex", "authoring": r.authoring} for r in records]
    assert authoring_of(sources) == [{"tool": "codex", "model": "gpt-6.1-sol", "effort": "high", "provider": "openai"}]


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
    write(path, [{"type": "user", "uuid": "u1", "sessionId": "s1", "cwd": str(folder),
                  "timestamp": "2026-09-25T08:40:00Z", "message": {"role": "user", "content": "Start."}}])
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 10)
    # A check on a byte budget does not read a transcript too large to hold, and says so; a scan reads it through.
    snapshot = parse_claude(path, Scope.resolve(folder), stream_large=False)
    assert not snapshot.records
    assert snapshot.limitations[0] == f"log file not read: big.jsonl (over the size limit of 10 bytes: {path.stat().st_size} bytes)"
    streamed = parse_claude(path, Scope.resolve(folder))
    assert [r.content for r in streamed.records] == ["Start."] and streamed.files[0]["streamed"]


def test_a_codex_transcript_too_large_to_hold_is_read_through_with_the_same_records(tmp_path, monkeypatch):
    import zstandard
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    rows = codex_rows(folder) + [{"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": f"step {n}"}]}} for n in range(30)]
    plain, packed = tmp_path / "big.jsonl", tmp_path / "packed" / "big.jsonl.zst"
    write(plain, rows)
    packed.parent.mkdir()
    packed.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    scope = Scope.resolve(folder)
    held, held_packed = parse_codex(plain, scope), parse_codex(packed, scope)
    assert packed.stat().st_size < 1000 < plain.stat().st_size
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 1000)  # both now too large to hold: one on disk, one once decompressed
    monkeypatch.setattr(local, "STREAM_CHUNK", 64)  # lines cross the pieces
    streamed, streamed_packed = parse_codex(plain, scope), parse_codex(packed, scope)
    key = lambda snapshot: [(r.source_id, r.content_hash, r.locator["line"]) for r in snapshot.records]
    assert len(held.records) == 31 and key(streamed) == key(held) and key(streamed_packed) == key(held_packed)
    assert streamed.limitations == held.limitations == []
    (manifest,) = streamed.files
    assert manifest["streamed"] and manifest["digest"] == held.files[0]["digest"]
    assert manifest["complete_bytes"] == held.files[0]["complete_bytes"] == plain.stat().st_size
    assert streamed_packed.files[0]["complete_bytes"] == packed.stat().st_size  # the size on disk, as the index wants
    # Another project's session read through: nothing kept, nothing reported.
    elsewhere = parse_codex(packed, Scope.resolve(tmp_path / "packed"))
    assert not elsewhere.records and not elsewhere.limitations


def test_a_streamed_transcript_reports_its_long_lines_its_unfinished_last_line_and_its_cap(tmp_path, monkeypatch):
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "big.jsonl"
    lines = [dumps(r) for r in codex_rows(folder)]
    huge = dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                                                        "content": [{"type": "input_text", "text": "x" * 500}]}})
    path.write_text("\n".join([lines[0], huge, lines[1]]) + "\n" + '{"type": "response_item"', encoding="utf-8")
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(local, "MAX_LINE_BYTES", 300)
    monkeypatch.setattr(local, "STREAM_CHUNK", 50)
    snapshot = parse_codex(path, Scope.resolve(folder))
    assert [r.locator["line"] for r in snapshot.records] == [3]  # line 2 was over the line limit, line 4 unfinished
    assert "JSONL record over the size limit: big.jsonl:2" in snapshot.limitations
    assert "last JSONL record still being written, skipped: big.jsonl" in snapshot.limitations
    monkeypatch.setattr(local, "STREAM_MAX_BYTES", 120)
    capped = parse_codex(path, Scope.resolve(folder))
    assert any(note.startswith("log file read only in part: big.jsonl") for note in capped.limitations)


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


def test_a_log_rewritten_while_it_is_read_is_not_read(tmp_path, monkeypatch):
    import io
    from contexttrail.sources import local
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    real = local.os.fdopen

    class Rewritten(io.BytesIO):
        def __init__(self, stream):
            super().__init__(stream.read())
            stream.close()
            self.fd = os.open(path, os.O_RDONLY)
        def fileno(self):
            return self.fd
        def seek(self, *args):
            # The second pass reads what a writer put there in between.
            data = self.getvalue().replace(b"2", b"3")
            super().seek(0)
            self.write(data)
            return super().seek(0)
        def close(self):
            os.close(self.fd)
            super().close()

    monkeypatch.setattr(local, "REREAD_CHUNK", 4)
    rows, warnings, _ = local._jsonl(path)
    assert len(rows) == 2 and warnings == []
    monkeypatch.setattr(local.os, "fdopen", lambda fd, mode: Rewritten(real(fd, mode)))
    rows, warnings, _ = local._jsonl(path)
    assert rows == [] and "rewritten or truncated while being read" in warnings[0]


def test_a_compressed_transcript_of_several_frames_is_read_whole():
    import zstandard
    from contexttrail.sources.local import MAX_FILE_BYTES, decompress_zstd
    first, second = b'{"a": 1}\n' * 1000, b'{"b": 2}\n' * 1000
    packed = zstandard.ZstdCompressor().compress(first) + zstandard.ZstdCompressor().compress(second)
    assert decompress_zstd(packed, MAX_FILE_BYTES) == first + second


def test_a_scan_does_not_read_again_an_unchanged_file_that_gave_the_project_nothing(tmp_path, monkeypatch):
    import zstandard
    from contexttrail.sources import local
    from contexttrail.sources.local import ScanCache, scan_cache_key
    folder, other = tmp_path / "app", tmp_path / "other"
    folder.mkdir()
    other.mkdir()
    codex, claude = tmp_path / "codex", tmp_path / "claude"
    ours = codex / "sessions" / "ours.jsonl"
    write(ours, codex_rows(folder))
    theirs = codex / "sessions" / "theirs.jsonl"
    write(theirs, [{**row, "payload": {**row["payload"], "id": "theirs"}} if row["type"] == "session_meta" else row
                   for row in codex_rows(other)])
    packed = codex / "sessions" / "old.jsonl.zst"
    packed.write_bytes(zstandard.ZstdCompressor().compress(theirs.read_bytes()))
    write(claude / "projects" / "p" / "s.jsonl", [{"type": "user", "uuid": "u1", "sessionId": "s", "cwd": str(other),
                                                  "timestamp": "2026-09-25T08:40:00Z", "message": {"role": "user", "content": "Hi."}}])
    scope = Scope.resolve(folder)
    read = []
    real_codex, real_claude = local.parse_codex, local.parse_claude
    monkeypatch.setattr(local, "parse_codex", lambda path, scope: read.append(path.name) or real_codex(path, scope))
    monkeypatch.setattr(local, "parse_claude", lambda path, scope, **kw: read.append(path.name) or real_claude(path, scope, **kw))
    first_cache = ScanCache(scan_cache_key(scope))
    first = collect_logs(scope, codex_home=codex, claude_home=claude, cache=first_cache)
    assert sorted(first_cache.fresh) == sorted(str(p) for p in (packed, theirs, claude / "projects" / "p" / "s.jsonl"))
    read.clear()
    second_cache = ScanCache(first_cache.key, first_cache.fresh)
    second = collect_logs(scope, codex_home=codex, claude_home=claude, cache=second_cache)
    assert read == ["ours.jsonl"] and second_cache.reused == 3  # only the file with this project's records is read
    assert [r.source_id for r in second.records] == [r.source_id for r in first.records]
    assert second.limitations == first.limitations
    assert [{k: v for k, v in f.items() if k != "reused"} for f in second.files] == first.files
    assert sum(bool(f.get("reused")) for f in second.files) == 3
    # A file that changed is read again; one that changes while it is read is not kept.
    with theirs.open("a", encoding="utf-8") as stream:
        stream.write(dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}}) + "\n")
    read.clear()
    third_cache = ScanCache(first_cache.key, second_cache.fresh)
    monkeypatch.setattr(local, "parse_codex", lambda path, scope: (read.append(path.name), path.name == "theirs.jsonl"
                        and path.open("a").write("\n"), real_codex(path, scope))[2])
    collect_logs(scope, codex_home=codex, claude_home=claude, cache=third_cache)
    assert sorted(read) == ["ours.jsonl", "theirs.jsonl"] and str(theirs) not in third_cache.fresh
    # Another set of folders is another cache.
    assert scan_cache_key(scope) != scan_cache_key(Scope.resolve(other))


def test_the_scan_cache_keeps_only_what_the_file_itself_settles(tmp_path, monkeypatch):
    import dataclasses
    import zstandard
    from contexttrail.sources import local
    from contexttrail.sources.local import ScanCache, scan_cache_key
    folder = tmp_path / "app"
    folder.mkdir()
    scope = Scope.resolve(folder)
    codex, claude = tmp_path / "codex", tmp_path / "claude"
    # 1. A Claude Code sub-agent whose parent has not written the launch result yet: no link, nothing kept.
    session = claude / "projects" / "p" / "parent.jsonl"
    call = {"type": "assistant", "uuid": "a1", "sessionId": "parent", "cwd": str(folder), "timestamp": "2026-10-20T01:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Agent", "input": {}}]}}
    write(session, [call])
    sub = claude / "projects" / "p" / "parent" / "subagents" / "agent-abc.jsonl"
    write(sub, [{"type": "assistant", "uuid": "s1", "sessionId": "parent", "isSidechain": True, "cwd": str(folder),
                 "timestamp": "2026-10-20T01:01:00Z", "message": {"role": "assistant", "content": "tests pass"}}])
    (sub.parent / "agent-abc.meta.json").write_text(dumps({"toolUseId": "toolu_1", "agentType": "Explore"}))
    first = ScanCache(scan_cache_key(scope))
    collect_logs(scope, codex_home=codex, claude_home=claude, cache=first)
    assert str(sub) not in first.fresh
    with session.open("a", encoding="utf-8") as stream:  # the parent writes the result: the next scan links it
        stream.write(dumps({"type": "user", "uuid": "r1", "sessionId": "parent", "cwd": str(folder),
                            "timestamp": "2026-10-20T01:02:00Z", "toolUseResult": {"agentId": "abc"},
                            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                                     "content": "done"}]}}) + "\n")
    second = collect_logs(scope, codex_home=codex, claude_home=claude, cache=ScanCache(first.key, first.fresh))
    assert any(r.content == "tests pass" for r in second.records)
    # 2. A file recorded at a path that later becomes a symlink into the project is read again.
    moved = tmp_path / "old-app"
    transcript = codex / "sessions" / "moved.jsonl"
    write(transcript, codex_rows(moved))
    cache = ScanCache(first.key)
    assert not collect_logs(scope, codex_home=codex, claude_home=tmp_path / "none", cache=cache).records
    assert cache.fresh[str(transcript)]["paths"] == [str(moved)]
    moved.symlink_to(folder)
    # Each scan asks a scope of its own (a scope keeps its answers for one scan), as `Engine.scan` does.
    again = collect_logs(dataclasses.replace(scope), codex_home=codex, claude_home=tmp_path / "none",
                         cache=ScanCache(cache.key, cache.fresh))
    assert [r.session_id for r in again.records] == ["native-session"]
    # 3. A read an error cut short is said, even of another project's file, and is not kept.
    other = tmp_path / "other"
    packed = codex / "archived_sessions" / "broken.jsonl.zst"
    packed.parent.mkdir(parents=True)
    whole = zstandard.ZstdCompressor().compress(("\n".join(dumps(r) for r in codex_rows(other)) + "\n").encode() * 50)
    packed.write_bytes(whole[:len(whole) // 2])  # a frame cut in two
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 10)  # too large to hold: streamed
    broken = ScanCache(first.key)
    found = collect_logs(scope, codex_home=codex, claude_home=tmp_path / "none", cache=broken)
    assert any(note.startswith("log file read only in part: broken.jsonl.zst") for note in found.limitations)
    assert str(packed) not in broken.fresh
    # 4. The key is what attribution reads: the folder a worktree was opened from is not part of it.
    assert scan_cache_key(dataclasses.replace(scope, folder=tmp_path)) == scan_cache_key(scope)
    assert scan_cache_key(dataclasses.replace(scope, roots=[tmp_path])) != scan_cache_key(scope)


def test_a_streamed_read_that_fails_part_way_is_a_limitation_not_a_crash(tmp_path, monkeypatch):
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "big.jsonl"
    write(path, codex_rows(folder) * 20)
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(local, "STREAM_CHUNK", 64)
    real = local._file_chunks

    def failing(descriptor, size):
        chunks = real(descriptor, size)
        yield next(chunks)
        chunks.close()
        raise OSError("device gone")
    monkeypatch.setattr(local, "_file_chunks", failing)
    snapshot = parse_codex(path, Scope.resolve(folder))
    assert any(note == "log file read only in part: big.jsonl (OSError)" for note in snapshot.limitations)
    assert snapshot.files[0]["partial"] == "error"


def test_a_compressed_transcript_too_large_on_disk_is_read_through_as_well(tmp_path, monkeypatch):
    import zstandard
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    plain, packed = tmp_path / "s.jsonl", tmp_path / "packed" / "s.jsonl.zst"
    write(plain, codex_rows(folder))
    packed.parent.mkdir()
    packed.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    held = parse_codex(packed, Scope.resolve(folder))
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 10)
    streamed = parse_codex(packed, Scope.resolve(folder))
    assert [r.source_id for r in streamed.records] == [r.source_id for r in held.records] and streamed.files[0]["streamed"]
    refused = parse_codex(packed, Scope.resolve(folder), stream_large=False)  # a check on a byte budget does not
    assert not refused.records and refused.limitations[0].startswith("log file not read: s.jsonl.zst")


def test_a_compressed_transcript_cut_inside_a_frame_is_refused_not_read_as_empty():
    import zstandard
    from contexttrail.sources.local import MAX_FILE_BYTES, decompress_zstd
    from contexttrail.util import FlowError
    whole = zstandard.ZstdCompressor().compress(b'{"a": 1}\n' * 1000)
    try:
        decompress_zstd(whole[:len(whole) // 2], MAX_FILE_BYTES)
    except FlowError as exc:
        assert "ends inside a frame" in str(exc)
    else:
        raise AssertionError("a cut frame was read as if whole")
    assert decompress_zstd(b"", MAX_FILE_BYTES) == b""


def test_a_claude_session_too_large_to_hold_is_read_and_its_sub_agent_still_linked(tmp_path, monkeypatch):
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    session = claude / "projects" / "p" / "parent.jsonl"
    rows = [{"type": "user", "uuid": "u0", "sessionId": "parent", "cwd": str(folder), "timestamp": "2026-10-20T00:59:00Z",
             "message": {"role": "user", "content": "Find the bug."}},
            {"type": "assistant", "uuid": "a1", "sessionId": "parent", "cwd": str(folder), "timestamp": "2026-10-20T01:00:00Z",
             "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Agent", "input": {}}]}},
            {"type": "user", "uuid": "r1", "sessionId": "parent", "cwd": str(folder), "timestamp": "2026-10-20T01:02:00Z",
             "toolUseResult": {"agentId": "abc"},
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "done"}]}}]
    write(session, rows + [{"type": "user", "uuid": f"x{n}", "sessionId": "parent", "cwd": str(folder),
                            "timestamp": "2026-10-20T01:03:00Z", "message": {"role": "user", "content": f"more {n}"}}
                           for n in range(20)])
    sub = claude / "projects" / "p" / "parent" / "subagents" / "agent-abc.jsonl"
    write(sub, [{"type": "assistant", "uuid": "s1", "sessionId": "parent", "isSidechain": True, "cwd": str(folder),
                 "timestamp": "2026-10-20T01:01:00Z", "message": {"role": "assistant", "content": "found it"}}])
    (sub.parent / "agent-abc.meta.json").write_text(dumps({"toolUseId": "toolu_1", "agentType": "Explore"}))
    held = collect_logs(Scope.resolve(folder), codex_home=tmp_path / "codex", claude_home=claude)
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 500)  # the parent is now too large to hold; the sub-agent is not
    monkeypatch.setattr(local, "STREAM_CHUNK", 100)
    streamed = collect_logs(Scope.resolve(folder), codex_home=tmp_path / "codex", claude_home=claude)
    key = lambda snapshot: [(r.source_id, r.content_hash, r.lineage.get("kind")) for r in snapshot.records]
    assert key(streamed) == key(held) and not streamed.limitations
    assert any(r.content == "found it" and r.lineage.get("kind") == "subagent" for r in streamed.records)


def test_a_codex_workdir_through_a_symlink_is_kept_as_written_for_the_cache_check(tmp_path):
    import dataclasses
    from contexttrail.sources.local import ScanCache, scan_cache_key
    folder, elsewhere, outside = tmp_path / "app", tmp_path / "elsewhere", tmp_path / "outside"
    for directory in (folder, elsewhere, outside):
        directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    codex = tmp_path / "codex"
    transcript = codex / "sessions" / "s.jsonl"
    write(transcript, [{"type": "session_meta", "payload": {"id": "s", "cwd": str(outside)}},
                       {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                                                             "arguments": dumps({"cmd": "pytest", "workdir": str(link)})}},
                       {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "3 passed"}}])
    scope = Scope.resolve(folder)
    cache = ScanCache(scan_cache_key(scope))
    assert not collect_logs(scope, codex_home=codex, claude_home=tmp_path / "none", cache=cache).records
    assert str(link) in cache.fresh[str(transcript)]["paths"]  # the path the log wrote, not where the link pointed
    link.unlink()
    link.symlink_to(folder)
    again = collect_logs(dataclasses.replace(scope), codex_home=codex, claude_home=tmp_path / "none",
                         cache=ScanCache(cache.key, cache.fresh))
    assert [r.role for r in again.records] == ["tool_call", "tool_result"]


def test_a_transcript_read_to_the_cap_is_indexed_by_its_size(tmp_path, monkeypatch):
    from contexttrail.sources import local
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "big.jsonl"
    write(path, codex_rows(folder) * 10)
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(local, "STREAM_MAX_BYTES", 300)
    snapshot = parse_codex(path, Scope.resolve(folder))
    (manifest,) = snapshot.files
    assert manifest["partial"] == "cap" and manifest["complete_bytes"] == path.stat().st_size


def test_an_unlinked_sub_agent_of_another_project_is_settled_and_kept(tmp_path):
    from contexttrail.sources.local import ScanCache, scan_cache_key
    folder, other = tmp_path / "app", tmp_path / "other"
    folder.mkdir()
    other.mkdir()
    claude = tmp_path / "claude"
    sub = claude / "projects" / "q" / "parent" / "subagents" / "agent-xyz.jsonl"
    write(sub, [{"type": "assistant", "uuid": "s1", "sessionId": "parent", "isSidechain": True, "cwd": str(other),
                 "timestamp": "2026-10-20T01:01:00Z", "message": {"role": "assistant", "content": "elsewhere"}}])
    with sub.open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")  # a corrupt line of another project's file is not this project's limitation
    scope = Scope.resolve(folder)
    cache = ScanCache(scan_cache_key(scope))
    found = collect_logs(scope, codex_home=tmp_path / "codex", claude_home=claude, cache=cache)
    assert not found.records and not found.limitations and str(sub) in cache.fresh  # a link would change nothing here


def test_a_scan_reports_its_progress_in_counts_only(tmp_path):
    from contexttrail.git_context import Scope
    from contexttrail.sources.local import collect_logs
    folder = tmp_path / "app"; folder.mkdir()
    for n in range(30):
        path = tmp_path / "claude" / "projects" / "p" / f"s{n}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type": "user", "uuid": "u", "sessionId": "s%d", "cwd": "/elsewhere", '
                        '"timestamp": "2026-10-01T00:00:00Z", "message": {"content": "hi"}}\n' % n)
    seen = []
    collect_logs(Scope.resolve(folder), codex_home=tmp_path / "codex", claude_home=tmp_path / "claude",
                 opencode_home=tmp_path / "oc", progress=lambda done, total: seen.append((done, total)))
    assert seen == [(25, 30), (30, 30)]
