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
    with pytest.raises(FlowError, match="observed status needs original tool_result evidence"):
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
    with pytest.raises(FlowError, match="verifies must link a change"):
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
