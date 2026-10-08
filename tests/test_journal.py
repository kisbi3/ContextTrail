"""`contexttrail note`: an agent writes an event of its own session, checked against that session's records."""
import json
from pathlib import Path

import pytest

from contexttrail import journal
from contexttrail.cli import main
from contexttrail.git_context import Scope
from contexttrail.store import Store
from contexttrail.util import FlowError, dumps

SESSION = "sess-note-1"


def row(folder: Path, n: int, kind: str, content: list[dict]) -> dict:
    return {"type": kind, "uuid": f"u{n}", "parentUuid": f"u{n - 1}" if n else None, "sessionId": SESSION,
            "cwd": str(folder), "timestamp": f"2026-10-07T10:{n:02d}:00Z", "message": {"content": content}}


def transcript(folder: Path, *, with_note_call: bool = True) -> list[dict]:
    rows = [
        row(folder, 0, "user", [{"type": "text", "text": "Make the store safe under two concurrent writers."}]),
        row(folder, 1, "assistant", [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {
            "file_path": str(folder / "store.py"), "old_string": "open(path, 'w')",
            "new_string": "with file_lock(path): write_atomically(path)"}}]),
        row(folder, 2, "user", [{"type": "tool_result", "tool_use_id": "t1", "content": "The file store.py has been updated."}]),
        row(folder, 3, "assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q tests/test_store.py"}}]),
        row(folder, 4, "user", [{"type": "tool_result", "tool_use_id": "t2",
                                  "content": "FAILED tests/test_store.py::test_two_writers - JSONDecodeError\n1 failed, 12 passed"}]),
        row(folder, 5, "assistant", [{"type": "text", "text": "All concurrency tests pass now, the fix is complete."}]),
    ]
    if with_note_call:
        rows.append(row(folder, 6, "assistant", [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {
            "command": "contexttrail note --kind outcome --quote 'quote that only the note call holds'"}}]))
    return rows


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    path = claude / "projects" / "app" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(dumps(r) for r in transcript(folder)) + "\n", encoding="utf-8")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    store.set_meta("options", {"codex_home": str(tmp_path / "codex"), "claude_home": str(claude),
                               "opencode_home": str(tmp_path / "opencode")})
    journal.enable(store)
    return folder, scope, store


def note(scope, store, **kwargs):
    return journal.write(scope, store, SESSION, **kwargs)


def test_a_note_quoting_its_own_edit_is_published_with_the_request_it_answered(project):
    _, scope, store = project
    result = note(scope, store, kind="action", title="Write the store under a file lock",
                  quotes=["with file_lock(path): write_atomically(path)"])
    graph = store.graph()
    assert result["version"] == graph["version"] == 1
    assert result["ref"] == f"contexttrail:{result['event']['id']}@v1"
    event = result["event"]
    assert (event["kind"], event["status"], event["basis"], event["origin"]) == ("action", "applied", "tool_record", "note")
    assert event["session_ids"] == [SESSION]
    evidence = store.evidence(event["evidence_ids"][0])
    assert "file_lock" in evidence["quote"] and evidence["source"]["role"] == "tool_call"
    # The person's message became a request by code, and the note hangs from it as dialog order.
    request = next(e for e in graph["events"] if e["actor"] == "user")
    assert (request["kind"], request["status"]) == ("question", "asked")
    assert any(edge["from_event_id"] == request["id"] and edge["to_event_id"] == event["id"]
               and edge.get("origin") == "dialog_turn" for edge in graph["edges"])
    assert journal.noted(graph, SESSION) == [event]


def test_an_observed_failure_verifies_the_change_it_ran(project):
    _, scope, store = project
    change = note(scope, store, kind="action", title="Lock the store", quotes=["write_atomically(path)"])
    result = note(scope, store, kind="outcome", status="observed_failure", title="Two-writer test failed",
                  quotes=["test_two_writers - JSONDecodeError"], relations={"verifies": [change["ref"]]})
    assert result["version"] == 2
    [edge] = result["edges"]
    assert (edge["from_event_id"], edge["to_event_id"], edge["relation"], edge["basis"]) == (
        change["event"]["id"], result["event"]["id"], "verifies", "explicit")


def test_a_claimed_success_without_a_tool_result_is_refused_and_nothing_is_stored(project):
    _, scope, store = project
    with pytest.raises(FlowError, match="needs a quote of a tool's output.*found in: assistant"):
        note(scope, store, kind="outcome", status="observed_success", title="Concurrency fixed",
             quotes=["All concurrency tests pass now"])
    assert store.graph()["version"] == 0
    # Reported, it is kept as what the assistant said.
    result = note(scope, store, kind="outcome", status="reported_complete", title="Reported fixed",
                  quotes=["All concurrency tests pass now"])
    assert result["event"]["basis"] == "explicit_statement"


def test_a_quote_not_in_the_session_is_refused_with_the_closest_lines(project):
    _, scope, store = project
    with pytest.raises(FlowError, match="quote not found.*Closest lines: .*test_two_writers - JSONDecodeError"):
        note(scope, store, kind="outcome", status="observed_failure", title="x", quotes=["test_two_writer - JSONDecodeErr0r"])
    # The note's own command line holds every quote; it is never a source.
    with pytest.raises(FlowError, match="quote not found"):
        note(scope, store, kind="decision", title="x", quotes=["quote that only the note call holds"])
    with pytest.raises(FlowError, match="too short"):
        note(scope, store, kind="decision", title="x", quotes=["FAILED"[:5]])
    assert store.graph()["version"] == 0


def test_an_unknown_target_or_a_wrong_relation_is_refused(project):
    _, scope, store = project
    with pytest.raises(FlowError, match="no single event"):
        note(scope, store, kind="revision", title="x", quotes=["write_atomically(path)"], relations={"revises": ["ev_missing"]})
    change = note(scope, store, kind="action", title="Lock the store", quotes=["write_atomically(path)"])
    with pytest.raises(FlowError, match="--verifies links a change to the observed result"):
        note(scope, store, kind="decision", title="x", quotes=["Make the store safe under two concurrent writers."],
             relations={"verifies": [change["event"]["id"]]})
    assert store.graph()["version"] == 1


def test_a_note_waits_for_no_analysis_and_leaves_other_records_alone(project):
    _, scope, store = project
    from contexttrail.model import SourceRecord
    other = SourceRecord("src_other", "codex", "other", "user", "elsewhere", {"kind": "jsonl", "path": "x", "line": 1})
    store.ingest([other])
    with store.analyze_lock():
        with pytest.raises(FlowError, match="analysis is running"):
            note(scope, store, kind="action", title="x", quotes=["write_atomically(path)"])
    note(scope, store, kind="action", title="x", quotes=["write_atomically(path)"])
    assert store.sources()["src_other"]["available"] == 1


def test_the_cli_writes_and_lists_notes_of_the_current_session(project, monkeypatch, capsys):
    folder, _, store = project
    for name in ("CODEX_THREAD_ID", "OPENCODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert main(["note", str(folder), "--kind", "action", "--title", "Lock the store",
                 "--quote", "write_atomically(path)"]) == 0
    reference = capsys.readouterr().out.strip()
    assert reference.startswith("contexttrail:ev_") and reference.endswith("@v1")
    assert main(["note", str(folder), "--kind", "outcome", "--status", "observed_failure", "--title", "Failed",
                 "--quote", "1 failed, 12 passed", "--verifies", reference, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["version"] == 2 and result["edges"][0]["relation"] == "verifies"
    assert main(["note", str(folder), "--list"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 2 and "action/applied" in listed[0] and "outcome/observed_failure" in listed[1]
    assert main(["note", str(folder), "--kind", "decision", "--title", "x", "--quote", "not anywhere at all"]) == 1


def test_analysis_leaves_a_noted_session_to_the_agent(project, tmp_path):
    from contexttrail.analysis import AnalysisConfig, Engine
    from contexttrail.demo import FixtureRunner
    from contexttrail.freshness import check
    folder, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    other = homes["claude_home"] / "projects" / "app" / "other-session.jsonl"
    other.write_text(dumps({"type": "user", "uuid": "o1", "sessionId": "other-session", "cwd": str(folder),
                            "timestamp": "2026-10-07T09:00:00Z",
                            "message": {"content": [{"type": "text", "text": "Use SQLite for the store."}]}}) + "\n")
    engine = Engine(scope, store, AnalysisConfig(**homes))
    before = engine.preview_plan(engine.scan())
    assert before["units"] == 2  # both sessions wait
    note(scope, store, kind="action", title="Lock the store", quotes=["write_atomically(path)"])
    assert SESSION in store.journaled_sessions()
    # The session goes on after the note; what it writes is the agent's too.
    path = homes["claude_home"] / "projects" / "app" / f"{SESSION}.jsonl"
    with path.open("a") as handle:
        handle.write(dumps(row(folder, 7, "user", [{"type": "text", "text": "Now add a retry."}])) + "\n")
    # Not counted as waiting for analysis either.
    assert check(scope, store, **homes)["since_scan"]["records"] == 0
    plan = engine.preview_plan(engine.scan())
    assert plan["units"] == 1
    result = engine.analyze(FixtureRunner)
    assert result["status"] in {"complete", "noop"}
    sources = store.sources()
    assert all(row["processed_hash"] == row["content_hash"] for row in sources.values())
    notes = journal.noted(store.graph(), SESSION)
    assert len(notes) == 1 and len([e for e in store.graph()["events"] if e["actor"] == "user"]) >= 2
    fresh = check(scope, store, **homes)
    assert fresh["pending"]["records"] == 0


def test_a_planned_unit_of_a_noted_session_is_superseded_not_sent(project, tmp_path):
    from contexttrail.analysis import AnalysisConfig, Engine
    _, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    engine = Engine(scope, store, AnalysisConfig(**homes))
    snapshot = engine.scan()
    store.ingest(snapshot.records)
    ids = [r.source_id for r in snapshot.records]
    store.save_unit("unit_planned", ids, {}, "parsed")
    note(scope, store, kind="action", title="Lock the store", quotes=["write_atomically(path)"])
    store.acknowledge_journaled(snapshot.records)
    units, _, _ = engine._plan_units(snapshot, [], repair=True)
    assert units == []
    assert {u["id"]: u["status"] for u in store.units()}["unit_planned"] == "superseded"


def hook_input(folder, **extra):
    return json.dumps({"session_id": SESSION, "cwd": str(folder), "hook_event_name": "Stop", **extra})


def test_the_end_of_turn_hook_sends_the_agent_back_once_for_unnoted_work(project, tmp_path):
    folder, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    store.set_meta(journal.SETTINGS_KEY, {"enabled_at": "2026-10-07T09:00:00+00:00"})
    decision = journal.hook(hook_input(folder), **homes)
    assert decision["decision"] == "block" and "contexttrail note" in decision["reason"]
    # Claude Code says the turn was already sent back; Codex does not, and the reminder time answers.
    assert journal.hook(hook_input(folder, stop_hook_active=True), **homes) is None
    assert journal.hook(hook_input(folder), **homes) is None
    # Off for the project, or never opened with ContextTrail: silent, and no state is made.
    journal.disable(store)
    store.set_meta(journal.ACTIVITY_KEY, {})
    assert journal.hook(hook_input(folder), **homes) is None
    bare = tmp_path / "bare"
    bare.mkdir()
    assert journal.hook(hook_input(bare), **homes) is None
    assert not (Scope.resolve(bare).state_dir / "state.sqlite").exists()
    assert journal.hook("not json", **homes) is None


def test_two_hooks_at_one_turns_end_remind_once(project, tmp_path):
    # `install-hooks --claude` and the plugin each add the hook, and the host runs both at once.
    from concurrent.futures import ThreadPoolExecutor
    folder, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    store.set_meta(journal.SETTINGS_KEY, {"enabled_at": "2026-10-07T09:00:00+00:00"})
    with ThreadPoolExecutor(4) as pool:
        answers = list(pool.map(lambda _: journal.hook(hook_input(folder), **homes), range(4)))
    assert [answer["decision"] for answer in answers if answer] == ["block"]


def test_work_noted_before_the_turn_ends_needs_no_reminder(project, tmp_path):
    folder, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    store.set_meta(journal.SETTINGS_KEY, {"enabled_at": "2026-10-07T09:00:00+00:00"})
    note(scope, store, kind="action", title="Lock the store", quotes=["write_atomically(path)"])
    assert journal.hook(hook_input(folder), **homes) is None
    # Work before notes were turned on is not asked about either.
    store.set_meta(journal.ACTIVITY_KEY, {})
    store.set_meta(journal.SETTINGS_KEY, {"enabled_at": "2026-10-07T11:00:00+00:00"})
    assert journal.hook(hook_input(folder), **homes) is None


def test_notes_are_refused_while_off_and_the_cli_switches_them(project, capsys):
    folder, scope, store = project
    assert main(["note", str(folder), "--disable"]) == 0
    with pytest.raises(FlowError, match="notes are off"):
        note(scope, store, kind="action", title="x", quotes=["write_atomically(path)"])
    assert main(["note", str(folder), "--enable"]) == 0
    assert journal.enabled(store)


def test_a_file_changed_through_the_shell_counts_as_work_to_note():
    from contexttrail.model import SourceRecord
    def call(command):
        return SourceRecord("c", "claude", SESSION, "tool_call", "Tool: Bash\n" + json.dumps({"command": command}), {},
                            recorded_at="2026-10-07T10:00:00Z")
    changes = ["printf 'def mul(a, b):\\n    return a * b\\n' >> calc.py && python3 -c 'print(1)'",
               "cat > notes.txt <<'EOF'\nx\nEOF", "sed -i '' 's/a/b/' calc.py", "make 2> build.log", "rm -f old.py"]
    for command in changes:
        assert journal.unnoted_work([call(command)], 0.0), command
    # Running or reading without writing a file is not, nor is output thrown away.
    for command in ["python3 -c 'print(6 * 7)'", "cat calc.py; ls", "python3 run.py >/dev/null 2>&1",
                    "python3 -c 'print(2 > 1)'"]:
        assert not journal.unnoted_work([call(command)], 0.0), command


def test_a_fetch_that_only_updates_remote_tracking_is_not_work_to_note():
    from contexttrail.model import SourceRecord
    def call(command):
        return SourceRecord("c", "claude", SESSION, "tool_call", "Tool: Bash\n" + json.dumps({"command": command}), {},
                            recorded_at="2026-10-07T10:00:00Z")
    # A status check: the working tree and the branches stay as they were.
    for command in ["git fetch", "git fetch origin", "git fetch --all --prune", "git -C app fetch origin main",
                    "git fetch --tags https://example.com/r.git", "git fetch origin && git status -sb",
                    "git fetch origin; git log --oneline -3", "git remote update"]:
        assert not journal.unnoted_work([call(command)], 0.0), command
    # A fetch that writes a local branch, or any change next to it, still is.
    for command in ["git fetch origin main:main", "git fetch --update-head-ok origin main",
                    "git fetch origin && git merge origin/main", "git fetch && git pull", "git pull", "git push origin main",
                    "git checkout -b topic", "git add -A"]:
        assert journal.unnoted_work([call(command)], 0.0), command


def test_the_reminder_is_never_taken_for_a_persons_request(project):
    from dataclasses import replace
    from contexttrail.model import SourceRecord, is_user_prompt
    record = SourceRecord("r", "opencode", SESSION, "user", journal.REMINDER, {})
    assert not is_user_prompt(record)
    assert not is_user_prompt(replace(record, content="Stop hook feedback:\n" + journal.REMINDER))


def test_the_opencode_plugin_passes_the_session_and_sends_the_reminder():
    from contexttrail.auto_update import opencode_plugin
    plugin = opencode_plugin(Path("/venv/bin/python"))
    assert '"shell.env"' in plugin and "OPENCODE_SESSION_ID = input.sessionID" in plugin
    assert "note --hook < ${new Response(request)}" in plugin and "client.session.prompt" in plugin


def test_only_a_call_that_runs_note_is_kept_out_of_the_quotes():
    from contexttrail.model import SourceRecord
    def call(command):
        return SourceRecord("c", "claude", SESSION, "tool_call", "Tool: Bash\n" + json.dumps({"command": command}), {})
    assert journal.runs_note(call("contexttrail note --kind action --quote 'x y z w v'"))
    assert journal.runs_note(call("cd app && A=$(contexttrail note --kind action --quote 'x')"))
    assert journal.runs_note(call('CT=~/.local/bin/contexttrail; $CT note --list'))
    assert journal.runs_note(call("/venv/bin/python -m contexttrail note --hook"))
    # Writing code or docs that mention the command is ordinary work, and quotable.
    assert not journal.runs_note(call("python3 - <<'EOF'\ntext = 'run contexttrail note --kind action'\nEOF"))
    assert not journal.runs_note(call("grep -n 'contexttrail note' README.md"))


def test_a_short_output_is_quotable_as_its_whole_line():
    from contexttrail.model import SourceRecord
    def record(key, role, content):
        return SourceRecord(key, "opencode", SESSION, role, content, {})
    records = [record("old", "tool_result", "5"), record("say", "assistant", "5"),
               record("new", "tool_result", "checking\n 5 \ndone")]
    assert journal.locate(records, "5") == {"source_id": "new", "start_line": 2, "end_line": 2, "quote": " 5 "}
    # Only a whole line of a tool's output: a short fragment, or a line someone said, is not enough.
    with pytest.raises(FlowError, match="too short"):
        journal.locate(records, "chec")
    with pytest.raises(FlowError, match="too short"):
        journal.locate(records[1:2], "5")


def test_an_inherited_session_variable_from_another_tool_is_passed_over(project):
    _, scope, store = project
    # Codex started from a Claude Code shell: both variables and Claude's marker are visible.
    environ = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "claude-elsewhere", "CODEX_THREAD_ID": SESSION}
    assert journal.current_session(environ) == "claude-elsewhere"
    assert journal.current_in_project(scope, store, environ) == SESSION
    assert journal.current_in_project(scope, store, {"CODEX_THREAD_ID": "nowhere"}) == "nowhere"


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def test_a_sandboxed_note_is_queued_and_the_hook_stores_it_with_its_link(project, tmp_path, private_tmp):
    folder, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    store.set_meta(journal.SETTINGS_KEY, {"enabled_at": "2026-10-07T09:00:00+00:00"})
    change = journal.queue(scope, SESSION, kind="action", title="Lock the store", summary="", quotes=["write_atomically(path)"],
                           status=None, actor="assistant", relations={}, **homes)
    journal.queue(scope, SESSION, kind="outcome", title="Failed", summary="", quotes=["1 failed, 12 passed"],
                  status="observed_failure", actor="assistant", relations={"verifies": [change["queued"]]}, **homes)
    # Quotes are checked when queued.
    with pytest.raises(FlowError, match="quote not found"):
        journal.queue(scope, SESSION, kind="decision", title="x", summary="", quotes=["nowhere in the session"],
                      status=None, actor="assistant", relations={}, **homes)
    assert store.graph()["version"] == 0 and len(journal.queued(scope, SESSION)) == 2
    assert journal.hook(hook_input(folder), **homes) is None  # stored, and the turn's work is now noted
    graph = store.graph()
    action, outcome = journal.noted(graph, SESSION)
    assert any(e["from_event_id"] == action["id"] and e["to_event_id"] == outcome["id"] and e["relation"] == "verifies"
               for e in graph["edges"])
    assert journal.queued(scope) == []


def test_a_queued_note_that_fails_its_checks_is_reported_back_once(project, tmp_path, private_tmp):
    folder, scope, store = project
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    item = journal.queue(scope, SESSION, kind="revision", title="Fix", summary="", quotes=["write_atomically(path)"],
                         status=None, actor="assistant", relations={"revises": ["ev_missing"]}, **homes)
    decision = journal.hook(hook_input(folder), **homes)
    assert decision["decision"] == "block" and item["queued"] in decision["reason"] and "no single event" in decision["reason"]
    assert journal.queued(scope) == []
    # While an analysis holds the lock the queue waits for the next hook.
    journal.queue(scope, SESSION, kind="action", title="Lock", summary="", quotes=["write_atomically(path)"],
                  status=None, actor="assistant", relations={}, **homes)
    with store.analyze_lock():
        journal.hook(hook_input(folder, stop_hook_active=True), **homes)
    assert len(journal.queued(scope)) == 1


def test_the_cli_queues_when_the_state_cannot_be_written(project, monkeypatch, capsys, private_tmp):
    folder, _, store = project
    import contexttrail.cli as cli
    for name in ("CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    def read_only(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(cli, "Store", read_only)
    assert main(["note", str(folder), "--kind", "action", "--title", "Lock", "--quote", "write_atomically(path)"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("q_") and "queued" in out
    assert main(["note", str(folder), "--list"]) == 0
    assert "(queued, stored when the turn ends)" in capsys.readouterr().out


def test_an_observed_result_is_found_in_the_tools_output_before_a_message_repeating_it(project, tmp_path):
    folder, scope, store = project
    path = tmp_path / "claude" / "projects" / "app" / f"{SESSION}.jsonl"
    with path.open("a") as handle:
        handle.write(dumps(row(folder, 8, "assistant", [{"type": "text", "text": "Result: 1 failed, 12 passed"}])) + "\n")
    result = note(scope, store, kind="outcome", status="observed_failure", title="Failed", quotes=["1 failed, 12 passed"])
    assert store.evidence(result["event"]["evidence_ids"][0])["source"]["role"] == "tool_result"
    # A reported result cannot verify a change: refused before anything is checked against the graph.
    with pytest.raises(FlowError, match="--verifies links a change to the observed result"):
        note(scope, store, kind="outcome", status="reported_complete", title="x", quotes=["1 failed, 12 passed"],
             relations={"verifies": ["ev_whatever"]})


def test_a_change_made_through_the_shell_cites_the_call_before_a_message_repeating_its_code(project, tmp_path):
    folder, scope, store = project
    path = tmp_path / "claude" / "projects" / "app" / f"{SESSION}.jsonl"
    with path.open("a") as handle:
        handle.write(dumps(row(folder, 8, "assistant", [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {
            "command": "cat >> calc.py <<'EOF'\n\ndef neg(a):\n    return -a\nEOF"}}])) + "\n")
        handle.write(dumps(row(folder, 9, "user", [{"type": "tool_result", "tool_use_id": "t9", "content": ""}])) + "\n")
        handle.write(dumps(row(folder, 10, "assistant", [{"type": "text", "text": "Added:\n\ndef neg(a):\n    return -a"}])) + "\n")
    result = note(scope, store, kind="action", title="Add neg", quotes=["def neg(a):"])
    assert result["event"]["status"] == "applied"
    assert store.evidence(result["event"]["evidence_ids"][0])["source"]["role"] == "tool_call"
