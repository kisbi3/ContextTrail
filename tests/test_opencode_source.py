"""The opencode source: a synthetic opencode.db in a temp directory, never a real one.

The table columns and the JSON shapes follow opencode 1.18 (packages/schema/src/v1/session.ts,
packages/core/src/session/sql.ts); the fixture carries only the columns the parser reads plus
the ones a row needs to exist.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from contexttrail import i18n
from contexttrail.analysis import AnalysisConfig, Engine, classify_steps, session_family, step_hint
from contexttrail.git_context import Scope
from contexttrail.model import is_user_prompt
from contexttrail.schema import edited_files
from contexttrail.sources import opencode as opencode_module
from contexttrail.sources.local import collect_logs
from contexttrail.sources.opencode import (IGNORED_NO_ANALYSIS_VALUE, KNOWN_UNPARSED, opencode_data_dir,
                                          opencode_databases, parse_opencode)
from contexttrail.store import Store

SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, slug TEXT NOT NULL,
    directory TEXT NOT NULL, path TEXT, title TEXT NOT NULL, version TEXT NOT NULL, agent TEXT, model TEXT,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, time_compacting INTEGER, time_archived INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE credential (id TEXT PRIMARY KEY, secret TEXT NOT NULL);
"""

T0 = 1_790_000_000_000  # 2026-09-21T14:13:20Z, in milliseconds like opencode


class Builder:
    """Writes rows the way opencode does: ascending ids, millisecond times, JSON `data`."""

    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO credential VALUES ('cred_1', 'NEVER-READ')")
        self.n = 0

    def tick(self) -> int:
        self.n += 1
        return self.n

    def session(self, directory, *, parent=None, agent=None, ident=None):
        n = self.tick()
        ident = ident or f"ses_{n:04d}"
        self.db.execute("INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, agent, time_created, time_updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)", (ident, "prj_1", parent, f"slug-{n}", str(directory), "title", "1.18.33",
                                                         agent, T0 + n * 1000, T0 + n * 1000))
        return ident

    def message(self, session, role, **extra):
        n = self.tick()
        ident = f"msg_{n:04d}"
        if role == "user":
            data = {"role": "user", "time": {"created": T0 + n * 1000}, "agent": "build",
                    "model": {"providerID": "openrouter", "modelID": "stealth/space-bunny-alpha", "variant": "max"}}
        else:
            data = {"role": "assistant", "time": {"created": T0 + n * 1000}, "parentID": extra.pop("parent", "msg_0000"),
                    "modelID": "stealth/space-bunny-alpha", "providerID": "openrouter", "mode": "build", "agent": "build",
                    "path": {"cwd": extra.pop("cwd"), "root": extra.pop("root", None) or ""}, "cost": 0,
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}, "variant": "max"}
        data.update(extra)
        self.db.execute("INSERT INTO message VALUES (?,?,?,?,?)", (ident, session, T0 + n * 1000, T0 + n * 1000, json.dumps(data)))
        return ident

    def part(self, session, message, **data):
        n = self.tick()
        ident = f"prt_{n:04d}"
        self.db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (ident, message, session, T0 + n * 1000, T0 + n * 1000, json.dumps(data)))
        return ident

    def tool(self, session, message, name, params, output=None, *, error=None, metadata=None, call=None):
        n = self.n + 1
        call = call or f"call_{n:04d}"
        if error is not None:
            state = {"status": "error", "input": params, "error": error, "time": {"start": T0, "end": T0 + 1}}
        elif output is None:
            state = {"status": "running", "input": params, "time": {"start": T0}}
        else:
            state = {"status": "completed", "input": params, "output": output, "title": name,
                     "metadata": metadata or {}, "time": {"start": T0, "end": T0 + 1}}
        return self.part(session, message, type="tool", callID=call, tool=name, state=state)

    def done(self):
        self.db.commit()
        self.db.close()
        return self.path


def project(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    return folder, Scope.resolve(folder)


def conversation(builder, session, folder, *, parent_message=None):
    """One turn: the person asks, the assistant edits a file, runs the tests and answers."""
    user = builder.message(session, "user")
    builder.part(session, user, type="text", text="Switch the store to SQLite.")
    assistant = builder.message(session, "assistant", parent=user, cwd=str(folder))
    builder.part(session, assistant, type="step-start", snapshot="a" * 40)
    builder.part(session, assistant, type="reasoning", text="PRIVATE THOUGHTS", time={"start": T0})
    builder.tool(session, assistant, "edit", {"filePath": str(folder / "store.py"), "oldString": "json", "newString": "sqlite3"},
                 "Edit applied successfully.", metadata={"diff": "-json\n+sqlite3"})
    builder.tool(session, assistant, "bash", {"command": "pytest -q", "workdir": "."}, "2 passed", metadata={"exit": 0})
    builder.part(session, assistant, type="patch", hash="b" * 40, files=[str(folder / "store.py")])
    builder.part(session, assistant, type="text", text="Done: the store now uses SQLite and the tests pass.", time={"start": T0})
    builder.part(session, assistant, type="step-finish", reason="stop", cost=0,
                 tokens={"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}})
    return user, assistant


def test_in_scope_session_becomes_records_in_message_and_part_order(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    conversation(builder, session, folder)
    snapshot = parse_opencode(builder.done(), scope)
    roles = [r.role for r in snapshot.records]
    assert roles == ["user", "tool_call", "tool_result", "tool_call", "tool_result", "metadata", "assistant"]
    assert all(r.provider == "opencode" and r.session_id == session for r in snapshot.records)
    assert is_user_prompt(snapshot.records[0])
    assert not any("PRIVATE" in r.content for r in snapshot.records)
    assert snapshot.records[1].content.startswith("Tool: edit\n")
    assert snapshot.records[1].tool_call_id == snapshot.records[2].tool_call_id == "call_0007"
    assert snapshot.records[3].content == 'Tool: bash\n{"command": "pytest -q", "workdir": "."}'
    assert snapshot.records[4].content == "2 passed"
    assert snapshot.records[5].content.startswith("Files changed in this step (opencode snapshot bbbbbbbbbbbb):\n")
    assert snapshot.records[5].lineage == {"kind": "patch"}
    assert snapshot.records[6].parent_record_id == "msg_0002"  # the assistant turn answers that user message
    assert snapshot.records[0].recorded_at == "2026-09-21T14:13:23.000Z"
    assert [r.locator["line"] for r in snapshot.records] == [1, 4, 4, 5, 5, 6, 7]
    assert snapshot.records[2].locator == {"kind": "sqlite", "path": str(builder.path), "table": "part", "row_id": "prt_0007",
                                           "message_id": "msg_0004", "line": 4,
                                           "raw_hash": snapshot.records[1].locator["raw_hash"]}
    assert not snapshot.limitations
    assert snapshot.files[0]["selection"]["sessions"] == {"examined": 1, "selected": 1, "outside_scope": 0, "unattributed": 0, "self_generated": 0}


def test_tool_hints_and_edited_files_read_opencode_tool_names(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    user = builder.message(session, "user")
    builder.part(session, user, type="text", text="Go.")
    assistant = builder.message(session, "assistant", parent=user, cwd=str(folder))
    builder.tool(session, assistant, "read", {"filePath": str(folder / "a.py")}, "content")
    builder.tool(session, assistant, "write", {"filePath": str(folder / "b.py"), "content": "x = 1"}, "File written")
    builder.tool(session, assistant, "bash", {"command": "git commit -m 'b'"}, "[main 1234] b")
    builder.tool(session, assistant, "task", {"description": "review", "prompt": "look", "subagent_type": "explore"}, "ok")
    records = parse_opencode(builder.done(), scope).records
    hints = [step_hint(r)[2] for r in records if r.role == "tool_call"]
    assert hints == ["read", "edit", "commit", "delegate"]
    assert edited_files([r for r in records if "Tool: write" in r.content][0]) == [str(folder / "b.py")]
    assert step_hint([r for r in records if "Tool: read" in r.content][0])[1] == "a.py"
    assert classify_steps(records)["hints"] == {"read": 1, "edit": 1, "commit": 1, "delegate": 1}


def test_sessions_are_attributed_by_directory_only(tmp_path):
    folder, scope = project(tmp_path)
    other = tmp_path / "app-backup"
    other.mkdir()
    builder = Builder(tmp_path / "opencode.db")
    inside = builder.session(folder)
    prefix = builder.session(other)  # a string prefix of the project path is not the project
    relative = builder.session("relative/path")
    self_run = builder.session(tmp_path / "contexttrail-run-x")
    for session in (inside, prefix, relative, self_run):
        user = builder.message(session, "user")
        builder.part(session, user, type="text", text=f"Message in {session} mentioning {folder}")
    snapshot = parse_opencode(builder.done(), scope)
    assert [r.session_id for r in snapshot.records] == [inside]
    assert snapshot.files[0]["selection"]["sessions"] == {"examined": 4, "selected": 1, "outside_scope": 1, "unattributed": 1, "self_generated": 1}
    assert any("path attribution unclear, 1 sessions excluded" in note for note in snapshot.limitations)


def test_bash_workdir_moves_a_call_and_its_result_out_of_scope(tmp_path):
    folder, scope = project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    user = builder.message(session, "user")
    builder.part(session, user, type="text", text="Go.")
    assistant = builder.message(session, "assistant", parent=user, cwd=str(folder))
    builder.tool(session, assistant, "bash", {"command": "ls", "workdir": str(elsewhere)}, "secret listing")
    builder.tool(session, assistant, "bash", {"command": "ls", "workdir": "sub"}, "inside listing")
    records = parse_opencode(builder.done(), scope).records
    assert not any("secret listing" in r.content for r in records)
    inside = [r for r in records if r.content == "inside listing"]
    assert inside and inside[0].cwd == str(folder / "sub")


def test_subagent_session_carries_its_parent_and_the_task_call(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    parent = builder.session(folder)
    child = builder.session(folder, parent=parent, agent="explore")
    user = builder.message(parent, "user")
    builder.part(parent, user, type="text", text="Find the bug.")
    assistant = builder.message(parent, "assistant", parent=user, cwd=str(folder))
    builder.tool(parent, assistant, "task", {"description": "hunt", "prompt": "find it", "subagent_type": "explore"},
                 "<task id=\"" + child + "\" state=\"completed\">found</task>", call="call_task",
                 metadata={"parentSessionId": parent, "sessionId": child, "model": {"providerID": "p", "modelID": "m"}})
    sub_user = builder.message(child, "user")
    builder.part(child, sub_user, type="text", text="find it")
    sub_assistant = builder.message(child, "assistant", parent=sub_user, cwd=str(folder))
    builder.part(child, sub_assistant, type="text", text="The bug is in store.py.", time={"start": T0})
    records = parse_opencode(builder.done(), scope).records
    sub = [r for r in records if r.session_id == child]
    assert sub and all(r.lineage == {"kind": "subagent", "parent_session_id": parent, "agent_type": "explore",
                                     "parent_tool_call_id": "call_task"} for r in sub)
    assert not is_user_prompt(sub[0])  # the parent's prompt, not the person's
    assert session_family(records, parent) == {parent, child}


def test_compaction_marker_then_summary_are_metadata_and_a_unit_boundary(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    conversation(builder, session, folder)
    request = builder.message(session, "user")
    builder.part(session, request, type="compaction", auto=True)
    summary = builder.message(session, "assistant", parent=request, cwd=str(folder), summary=True, mode="compaction", agent="compaction")
    builder.part(session, summary, type="text", text="Summary: the store moved to SQLite.", time={"start": T0})
    follow = builder.message(session, "user")
    builder.part(session, follow, type="text", text="Continue from here.", metadata={"compaction_continue": True})
    records = parse_opencode(builder.done(), scope).records
    marker, text, continued = records[-3], records[-2], records[-1]
    assert marker.role == "metadata" and marker.derivation == "summary" and marker.lineage == {"kind": "compaction"}
    assert marker.content == "[opencode compaction boundary: automatic; the summary follows]"
    assert text.role == "metadata" and text.derivation == "summary" and text.lineage == {"kind": "compaction_summary"}
    assert text.content == "Summary: the store moved to SQLite."
    assert continued.role == "metadata" and continued.lineage == {"kind": "compaction_continue"}
    # The planner cuts before the marker, so the summary opens the next unit.
    store = Store(scope.state_dir, scope.id)
    engine = Engine(scope, store, AnalysisConfig(opencode_home=tmp_path, codex_home=tmp_path / "none", claude_home=tmp_path / "none"))
    snapshot = engine.scan()
    with store.analyze_lock():
        store.ingest(snapshot.records)
        plan_units, _, _ = engine._plan_units(snapshot, [])
    assert len(plan_units) == 2
    assert plan_units[1]["sources"][0] == marker.source_id


def test_synthetic_and_ignored_user_text_are_not_the_persons_words(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    user = builder.message(session, "user")
    builder.part(session, user, type="text", text="Attached media from tool result:", synthetic=True)
    builder.part(session, user, type="text", text="/undo expanded text", ignored=True)
    builder.part(session, user, type="file", mime="image/png", filename="shot.png", url="data:image/png;base64," + "A" * 400)
    builder.part(session, user, type="agent", name="explore")
    builder.part(session, user, type="text", text="Look at the screenshot.")
    records = parse_opencode(builder.done(), scope).records
    assert [(r.role, r.lineage.get("kind")) for r in records] == [
        ("metadata", "synthetic"), ("metadata", "ignored"), ("metadata", "attachment"), ("user", None)]
    assert "base64" not in records[2].content and "sha256=" in records[2].content
    assert [is_user_prompt(r) for r in records] == [False, False, False, True]


def test_subtask_part_is_a_request_the_person_made(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    user = builder.message(session, "user")
    builder.part(session, user, type="subtask", prompt="Review the diff.", description="review", agent="reviewer")
    records = parse_opencode(builder.done(), scope).records
    assert records[0].role == "user" and records[0].content == "Subtask for agent reviewer: review\nReview the diff."
    assert is_user_prompt(records[0])


def test_tool_error_running_call_truncated_output_and_turn_error(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    user = builder.message(session, "user")
    builder.part(session, user, type="text", text="Go.")
    assistant = builder.message(session, "assistant", parent=user, cwd=str(folder),
                                error={"name": "MessageAbortedError", "data": {"message": "aborted by the person"}})
    builder.tool(session, assistant, "bash", {"command": "false"}, error="exit 1")
    builder.tool(session, assistant, "bash", {"command": "sleep 100"})
    builder.tool(session, assistant, "bash", {"command": "cat big"}, "head of output",
                 metadata={"truncated": True, "outputPath": str(tmp_path / "tool-output" / "x")})
    records = parse_opencode(builder.done(), scope).records
    roles = [r.role for r in records]
    assert roles == ["user", "tool_call", "tool_result", "tool_call", "tool_call", "tool_result", "metadata"]
    assert records[2].content == "[tool error]\nexit 1"
    assert records[5].content == "head of output\n[opencode truncated this output; the full text in its tool-output directory was not read]"
    assert records[6].content == "Assistant turn ended with error: MessageAbortedError: aborted by the person"
    assert records[6].locator["table"] == "message" and records[6].lineage == {"kind": "turn_error"}


def test_unknown_part_types_are_reported_and_registries_are_deliberate(tmp_path):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    user = builder.message(session, "user")
    builder.part(session, user, type="text", text="Go.")
    builder.part(session, user, type="hologram", text="??")
    assistant = builder.message(session, "assistant", parent=user, cwd=str(folder))
    builder.part(session, assistant, type="retry", attempt=1, error={"name": "APIError"}, time={"created": T0})
    builder.part(session, assistant, type="snapshot", snapshot="c" * 40)
    snapshot = parse_opencode(builder.done(), scope)
    assert len(snapshot.records) == 1
    assert any(note.startswith("opencode unsupported record types") and "part:hologram" in note for note in snapshot.limitations)
    assert any("unparsed record type part:retry, 1 records" in note for note in snapshot.limitations)
    assert not any("snapshot" in note for note in snapshot.limitations)
    assert not set(IGNORED_NO_ANALYSIS_VALUE) & set(KNOWN_UNPARSED)
    assert all(reason.strip() for reason in list(IGNORED_NO_ANALYSIS_VALUE.values()) + list(KNOWN_UNPARSED.values()))


def file_state(path: Path) -> tuple:
    info = os.stat(path)
    return info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def test_database_is_read_only_even_in_wal_mode_and_never_names_other_tables(tmp_path, monkeypatch):
    folder, scope = project(tmp_path)
    home = tmp_path / "data" / "opencode"
    home.mkdir(parents=True)
    builder = Builder(home / "opencode.db")
    builder.db.execute("PRAGMA journal_mode=WAL")
    session = builder.session(folder)
    conversation(builder, session, folder)
    builder.done()
    before = {p.name: file_state(p) for p in home.iterdir()}
    statements = []
    original = opencode_module._connect
    def spy(path):
        connection = original(path)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(opencode_module, "_connect", spy)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    snapshot = collect_logs(scope, codex_home=tmp_path / "none", claude_home=tmp_path / "none")
    assert len(snapshot.records) == 7
    assert {p.name: file_state(p) for p in home.iterdir()} == before
    assert statements and not any("credential" in sql or "account" in sql for sql in statements)
    assert opencode_data_dir() == home


def test_data_dir_and_database_discovery(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert opencode_data_dir() == tmp_path / ".local" / "share" / "opencode"
    assert opencode_data_dir(Path("~/custom")) == tmp_path / "custom"
    home = tmp_path / "home"
    home.mkdir()
    (home / "opencode.db").write_bytes(b"")
    (home / "opencode-dev.db").write_bytes(b"")
    (home / "other.db").write_bytes(b"")
    (home / "opencode-link.db").symlink_to(home / "other.db")
    assert opencode_databases(home) == [home / "opencode-dev.db", home / "opencode.db"]
    named = tmp_path / "named.db"
    named.write_bytes(b"")
    monkeypatch.setenv("OPENCODE_DB", str(named))
    assert opencode_databases(home)[0] == named
    assert opencode_databases(tmp_path / "missing") == [named]


def test_unreadable_or_foreign_database_is_a_limitation_not_a_crash(tmp_path):
    folder, scope = project(tmp_path)
    foreign = tmp_path / "opencode.db"
    db = sqlite3.connect(foreign)
    db.execute("CREATE TABLE storage (k TEXT)")
    db.commit()
    db.close()
    snapshot = parse_opencode(foreign, scope)
    assert not snapshot.records
    assert snapshot.limitations == [f"opencode database without session/message/part tables, not read: opencode.db"]
    garbage = tmp_path / "opencode-x.db"
    garbage.write_bytes(b"not a database at all" * 100)
    snapshot = parse_opencode(garbage, scope)
    assert not snapshot.records and "not read" in snapshot.limitations[0]


def test_scan_counts_opencode_records_and_the_screen_names_the_tool(tmp_path, capsys):
    folder, scope = project(tmp_path)
    builder = Builder(tmp_path / "opencode.db")
    session = builder.session(folder)
    conversation(builder, session, folder)
    builder.done()
    from contexttrail.cli import main
    i18n.set_language("en")
    assert main(["scan", str(folder), "--opencode-home", str(tmp_path), "--codex-home", str(tmp_path / "none"),
                 "--claude-home", str(tmp_path / "none")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["records"]["opencode"] == 7 and output["records"]["codex"] == 0
    assert output["runner_calls"] == 0
    assert Store(scope.state_dir, scope.id).get_meta("options")["opencode_home"] == str(tmp_path.resolve())
