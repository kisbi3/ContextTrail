"""The contract a program driving ContextTrail (a host) relies on (docs/HOST.md): schema names on every JSON output,
`capabilities`, distinct exit codes, `analyze --json`, `--sources` with scan's `sources` block, and
`export --since-version` / `--graph-version`. Synthetic transcripts only; no runner is ever started."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from contexttrail import auto_update, cli, contract, studio_graph
from contexttrail.cli import main, parser
from contexttrail.git_context import Scope
from contexttrail.render import graph_changes
from contexttrail.sources.local import zstd_backend
from contexttrail.store import Store
from contexttrail.util import dumps


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dumps(r) for r in rows) + "\n", encoding="utf-8")


def project(tmp_path: Path) -> tuple[Path, list[str], dict[str, Path]]:
    """A folder with one Claude Code and one Codex session recorded in it, and the home flags that read them."""
    folder = tmp_path / "app"
    folder.mkdir()
    homes = {"codex": tmp_path / "codex", "claude": tmp_path / "claude", "opencode": tmp_path / "opencode"}
    homes["opencode"].mkdir()
    write(homes["claude"] / "projects" / "app" / "c1.jsonl",
          [{"type": "user", "uuid": "c1-0", "sessionId": "c1", "cwd": str(folder), "timestamp": "2026-10-06T01:00:00Z",
            "message": {"content": [{"type": "text", "text": "Use SQLite for the cache."}]}}])
    write(homes["codex"] / "sessions" / "2026" / "10" / "06" / "rollout-x1.jsonl",
          [{"type": "session_meta", "timestamp": "2026-10-06T02:00:00Z", "payload": {"id": "x1", "cwd": str(folder)}},
           {"type": "response_item", "timestamp": "2026-10-06T02:00:01Z",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add a test."}]}}])
    flags = ["--codex-home", str(homes["codex"]), "--claude-home", str(homes["claude"]),
             "--opencode-home", str(homes["opencode"])]
    return folder, flags, homes


def output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def demo(tmp_path: Path, capsys) -> Path:
    assert main(["demo", "--path", str(tmp_path / "demo"), "--no-tui"]) == 0
    capsys.readouterr()
    return tmp_path / "demo" / "sample-project"


def test_every_json_output_names_its_schema(tmp_path, capsys, monkeypatch):
    folder = demo(tmp_path, capsys)
    assert main(["scan", str(folder)]) == 0
    assert output(capsys)["schema"] == "contexttrail.scan.v1"
    assert main(["status", str(folder), "--json"]) == 0
    assert output(capsys)["schema"] == "contexttrail.status.v1"
    assert main(["find", "", str(folder), "--json"]) == 0
    found = output(capsys)
    assert found["schema"] == "contexttrail.find.v1" and "events" in found  # the fields are as they were
    scope = Scope.resolve(folder)
    event = Store(scope.state_dir, scope.id).graph()["events"][0]["id"]
    assert main(["show", event, str(folder), "--json"]) == 0
    assert output(capsys)["schema"] == "contexttrail.show.v1"
    assert main(["note", str(folder), "--list", "--session", "s1", "--json"]) == 0
    assert output(capsys)["schema"] == "contexttrail.note-list.v1"
    assert main(["ops", str(folder)]) == 0
    assert output(capsys)["schema"] == "contexttrail.ops.v1"
    assert main(["ops", str(folder), "--details"]) == 0
    details = output(capsys)
    assert details["schema"] == "contexttrail.ops.v1" and "summary" in details and "calls" in details
    assert main(["analyze", str(folder), "--json"]) == 0
    assert output(capsys)["schema"] == "contexttrail.analyze.v1"

    class Stub:
        def __init__(self, name, model=None):
            self.name = name

        def preflight(self):
            return {"sandbox": "stub"}
    monkeypatch.setattr(cli, "CLIRunner", Stub)  # doctor never reaches a real CLI here
    assert main(["doctor", "--runner", "codex"]) == 0
    assert [entry["schema"] for entry in output(capsys)] == ["contexttrail.doctor.v1"]
    # A folder never opened answers in the same shape, with its own schema.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["status", str(empty), "--json"]) == 0
    assert output(capsys) | {"events": []} == {"schema": "contexttrail.status.v1", "state": "none", "scope": str(empty.resolve()),
                                              "events": [], "ai_calls": 0}


def test_capabilities_reads_commands_and_options_off_the_parser(capsys):
    assert main(["capabilities"]) == 0
    found = output(capsys)
    assert found["schema"] == "contexttrail.capabilities.v1"
    from contexttrail import __version__
    assert found["version"] == __version__
    commands = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
    assert set(found["commands"]) == set(commands)
    analyze = {a["name"]: a for a in found["commands"]["analyze"]["arguments"]}
    assert {"--sources", "--json", "--runner", "--units", "--yes", "folder"} <= set(analyze)
    assert analyze["--runner"]["choices"] == ["codex", "claude"] and analyze["--units"]["type"] == "integer"
    assert analyze["--sources"]["type"] == "list" and analyze["--yes"]["type"] == "flag"
    assert "--trigger" not in analyze and "--langsmith" not in analyze  # hidden from --help, hidden here
    export = {a["name"] for a in found["commands"]["export"]["arguments"]}
    assert {"--since-version", "--graph-version", "--format", "--output"} <= export
    assert found["exit_codes"].keys() == {"0", "1", "2", "64", "75", "77", "130"}
    assert found["outputs"]["scan"] == "contexttrail.scan.v1" and found["outputs"]["export"] == "contexttrail.export.v1"
    codex = found["sources"]["codex"]
    assert codex["zstd_backend"] == zstd_backend() and codex["jsonl_zst"] is (zstd_backend() is not None)
    assert ".jsonl.zst" in codex["files"] and found["sources"]["names"] == ["codex", "claude", "opencode"]


def test_usage_errors_exit_64_not_2_which_is_partial(tmp_path, capsys):
    folder, flags, _ = project(tmp_path)
    for argv in (["analyze", str(folder), "--no-review"], ["scan", str(folder), "--sources", "codex,vim"],
                 ["scan", str(folder), "--units"], ["export", str(folder), "--format", "pdf", "--output", "x"]):
        with pytest.raises(SystemExit) as stopped:
            main(argv)
        assert stopped.value.code == contract.EXIT_USAGE
    # A value the parser takes but the checks refuse is a usage error too.
    assert main(["scan", str(folder), *flags, "--workers", "99"]) == 64
    assert main(["scan", str(folder), "--codex-home", str(tmp_path / "typo")]) == 64
    assert main(["scan", str(folder), *flags, "--recent", "2", "--session", "x"]) == 64
    assert main(["install-hooks"]) == 64  # nothing named to install into; nothing is touched
    capsys.readouterr()


def test_a_busy_project_exits_75_and_analyze_json_still_prints_one_object(tmp_path, capsys, monkeypatch):
    folder, flags, _ = project(tmp_path)
    assert main(["scan", str(folder), *flags]) == 0
    capsys.readouterr()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    with store.analyze_lock():
        assert main(["analyze", str(folder), *flags, "--runner", "codex", "--yes", "--json"]) == 75
        result = output(capsys)
        assert result["schema"] == "contexttrail.analyze.v1" and result["status"] == "not_started"
        assert result["error_kind"] == "busy" and result["exit_code"] == 75 and result["runner_calls"] == 0
        assert main(["analyze", str(folder), *flags, "--runner", "codex", "--yes", "--no-tui"]) == 75
        capsys.readouterr()
        # scan reads the transcripts, but cannot plan while the project is held: it says so and exits busy.
        assert main(["scan", str(folder), *flags]) == 75
        scanned = output(capsys)
        assert scanned["plan"] is None and scanned["plan_error"]["kind"] == "busy"
        assert main(["status", str(folder), "--json"]) == 0  # a read is not held up
        capsys.readouterr()
    # The state database held by a long write past the wait: busy, not a traceback.
    def locked(args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(cli, "_status", locked)
    assert main(["status", str(folder), "--json"]) == 75
    assert "상태 데이터베이스" in capsys.readouterr().err


def test_refusals_exit_77(tmp_path, capsys, monkeypatch):
    folder, flags, _ = project(tmp_path)
    # No runner chosen and no terminal to ask at.
    assert main(["analyze", str(folder), *flags, "--no-tui", "--json"]) == 77
    result = output(capsys)
    assert result["status"] == "failed" and result["error_kind"] == "refused" and result["runner_calls"] == 0
    # A runner, but no consent given for it.
    assert main(["analyze", str(folder), *flags, "--runner", "codex", "--no-tui"]) == 77
    capsys.readouterr()
    # Notes are off for this project.
    assert main(["note", str(folder), "--kind", "decision", "--title", "x", "--quote", "SQLite", "--session", "c1"]) == 77
    assert "notes are off" in capsys.readouterr().err
    # A prompt whose input ends (a host's pty, a closed stdin) is a refusal, not an internal error.
    class Terminal:
        def isatty(self):
            return True

        def read(self, *a):
            return ""
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    assert main(["analyze", str(folder), *flags, "--runner", "codex", "--no-tui", "--json"]) == 77
    assert output(capsys)["error_kind"] == "refused"


def test_analyze_json_is_the_only_thing_on_stdout(tmp_path, capsys):
    folder = demo(tmp_path, capsys)
    assert main(["analyze", str(folder), "--json", "--brief"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)  # nothing else on stdout: no flow, no brief lines
    assert result["status"] == "noop" and result["exit_code"] == 0 and result["graph_version"] >= 1
    assert set(result) >= {"schema", "status", "completed_units", "skipped_units", "runner_calls", "stop_reason",
                           "error_kind", "error", "graph_version", "limitations_count", "pending_records"}


def test_run_exit_follows_status_and_refusal():
    assert contract.run_exit({"status": "complete"}) == 0 and contract.run_exit({"status": "no_data"}) == 0
    assert contract.run_exit({"status": "partial"}) == 2 and contract.run_exit({"status": "call_limit"}) == 2
    assert contract.run_exit({"status": "failed"}) == 1
    assert contract.run_exit({"status": "cancelled"}) == 130
    # A declined consent is recorded as a cancelled run but reported as a refusal.
    assert contract.run_exit({"status": "cancelled", "error_kind": "refused"}) == 77
    assert contract.run_exit({"status": "failed", "error_kind": "refused"}) == 77


def test_sources_reads_only_the_named_tools_and_scan_says_what_it_read(tmp_path, capsys, monkeypatch):
    folder, flags, homes = project(tmp_path)
    # A transcript file that is a link is skipped, and counted.
    target = tmp_path / "elsewhere.jsonl"
    target.write_text((homes["claude"] / "projects" / "app" / "c1.jsonl").read_text())
    (homes["claude"] / "projects" / "app" / "linked.jsonl").symlink_to(target)
    assert main(["scan", str(folder), *flags]) == 0
    every = output(capsys)
    assert every["records"]["claude"] == 1 and every["records"]["codex"] == 1
    by = {entry["provider"]: entry for entry in every["sources"]}
    assert by["claude"] == {"provider": "claude", "home": str(homes["claude"].resolve()), "origin": "flag", "exists": True,
                            "read": True, "files_examined": 1, "files_read": 1, "files_with_records": 1,
                            "files_skipped_symlink": 1, "dirs_skipped_symlink": 0, "records": 1}
    assert by["codex"]["records"] == 1 and by["opencode"]["databases"] == []
    assert every["source_selection"] == {"sources": ["codex", "claude", "opencode"], "origin": "default"}

    assert main(["scan", str(folder), *flags, "--sources", "claude"]) == 0
    only = output(capsys)
    assert only["records"]["codex"] == 0 and only["records"]["claude"] == 1
    by = {entry["provider"]: entry for entry in only["sources"]}
    assert not by["codex"]["read"] and by["codex"]["files_examined"] == 0 and by["claude"]["read"]
    assert only["source_selection"] == {"sources": ["claude"], "origin": "flag"}
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    assert "sources" not in (store.get_meta("options") or {})  # a scan saves nothing

    # analyze saves it, as it saves the homes (here it is refused for want of a runner, after the options).
    assert main(["analyze", str(folder), *flags, "--sources", "codex,claude", "--no-tui"]) == 77
    capsys.readouterr()
    assert store.get_meta("options")["sources"] == ["codex", "claude"]
    monkeypatch.setenv("CODEX_HOME", str(homes["codex"]))
    assert main(["scan", str(folder)]) == 0
    saved = output(capsys)
    assert saved["source_selection"] == {"sources": ["codex", "claude"], "origin": "saved"}
    by = {entry["provider"]: entry for entry in saved["sources"]}
    assert by["codex"]["origin"] == "saved" and not by["opencode"]["read"]
    # status reads what the project reads: a new Claude session counts, and nothing of opencode is opened.
    write(homes["claude"] / "projects" / "app" / "c2.jsonl",
          [{"type": "user", "uuid": "c2-0", "sessionId": "c2", "cwd": str(folder), "timestamp": "2026-10-07T01:00:00Z",
            "message": {"content": [{"type": "text", "text": "Next."}]}}])
    assert main(["status", str(folder), "--json"]) == 0
    assert output(capsys)["since_scan"]["records"] == 1
    assert main(["status", str(folder), "--json", "--sources", "codex"]) == 0
    assert output(capsys)["since_scan"]["records"] == 0


def test_where_a_home_comes_from(tmp_path, monkeypatch, capsys):
    folder, flags, homes = project(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(homes["codex"]))
    assert main(["scan", str(folder), "--claude-home", str(homes["claude"])]) == 0
    by = {entry["provider"]: entry for entry in output(capsys)["sources"]}
    assert by["codex"]["origin"] == "environment" and by["codex"]["home"] == str(homes["codex"])
    assert by["claude"]["origin"] == "flag"
    assert by["opencode"]["origin"] == "environment"  # the suite points XDG_DATA_HOME at a temp directory
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert main(["scan", str(folder), "--claude-home", str(homes["claude"])]) == 0
    by = {entry["provider"]: entry for entry in output(capsys)["sources"]}
    assert by["opencode"]["origin"] == "default" and not by["opencode"]["exists"]


def test_export_since_version_and_an_older_graph_version(tmp_path, capsys):
    folder = demo(tmp_path, capsys)
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    latest = store.graph()
    assert latest["version"] >= 2
    first = store.graph(1)
    out = tmp_path / "delta.json"
    assert main(["export", str(folder), "--format", "json", "--output", str(out), "--since-version", "1"]) == 0
    delta = json.loads(out.read_text())
    assert delta["format"] == "contexttrail.export-delta.v1" and delta["sensitive"] is True
    assert (delta["from_version"], delta["to_version"]) == (1, latest["version"])
    before = {event["id"] for event in first["events"]}
    assert [e["id"] for e in delta["events_added"]] == [e["id"] for e in latest["events"] if e["id"] not in before]
    cited = {i for e in delta["events_added"] + delta["edges_added"] for i in e["evidence_ids"]}
    assert cited and cited <= set(delta["evidence"])
    # From version 0 every event is new; from the latest nothing changed.
    out0 = tmp_path / "all.json"
    assert main(["export", str(folder), "--format", "json", "--output", str(out0), "--since-version", "0"]) == 0
    assert len(json.loads(out0.read_text())["events_added"]) == len(latest["events"])
    same = tmp_path / "same.json"
    assert main(["export", str(folder), "--format", "json", "--output", str(same), "--since-version", str(latest["version"])]) == 0
    assert not any(json.loads(same.read_text())[key] for key in ("events_added", "events_updated", "edges_added"))
    # An older version whole, in any format.
    old = tmp_path / "v1.json"
    assert main(["export", str(folder), "--format", "json", "--output", str(old), "--graph-version", "1"]) == 0
    exported = json.loads(old.read_text())
    assert exported["format"] == "contexttrail.export.v1" and exported["graph"]["version"] == 1
    assert [e["id"] for e in exported["graph"]["events"]] == [e["id"] for e in first["events"]]
    assert main(["export", str(folder), "--format", "md", "--output", str(tmp_path / "v1.md"), "--graph-version", "1"]) == 0
    # Refused: a delta in md, a version that does not exist, a delta that runs backwards.
    assert main(["export", str(folder), "--format", "md", "--output", str(tmp_path / "x.md"), "--since-version", "1"]) == 64
    assert main(["export", str(folder), "--format", "json", "--output", str(tmp_path / "x.json"), "--since-version", "99"]) == 64
    assert main(["export", str(folder), "--format", "json", "--output", str(tmp_path / "y.json"),
                 "--since-version", "2", "--graph-version", "1"]) == 64


def test_graph_changes_sorts_updates_invalidations_and_removals():
    old = {"version": 1, "events": [{"id": "a", "status": "applied"}, {"id": "b", "status": "proposed"},
                                    {"id": "c", "status": "adopted"}, {"id": "gone", "status": "adopted"}],
           "edges": [{"id": "e1", "active": True}, {"id": "e2", "active": True, "relation": "follows"}],
           "open_items": [{"id": "o1", "status": "open"}]}
    new = {"version": 3, "events": [{"id": "a", "status": "applied"}, {"id": "b", "status": "withdrawn"},
                                    {"id": "c", "status": "adopted", "title": "new"}, {"id": "d", "status": "applied"}],
           "edges": [{"id": "e1", "active": False}, {"id": "e2", "active": True, "relation": "motivates"},
                     {"id": "e3", "active": True}],
           "open_items": [{"id": "o1", "status": "resolved"}]}
    changes = graph_changes(old, new)
    assert [e["id"] for e in changes["events_added"]] == ["d"]
    assert [e["id"] for e in changes["events_updated"]] == ["c"]
    assert [e["id"] for e in changes["events_invalidated"]] == ["b"]
    assert changes["events_removed"] == ["gone"]
    assert [e["id"] for e in changes["edges_invalidated"]] == ["e1"]
    assert [e["id"] for e in changes["edges_updated"]] == ["e2"] and [e["id"] for e in changes["edges_added"]] == ["e3"]
    assert [i["id"] for i in changes["open_items_resolved"]] == ["o1"]


def test_a_busy_hook_run_is_not_a_failure_toward_the_stop(tmp_path):
    store = Store(tmp_path / "state", "scope")
    for code in (1, 75, 1, 75):
        auto_update.record_start(store)
        auto_update.record_finish(store, code)
    state = store.get_meta(auto_update.STATE_KEY)
    assert not auto_update.failing(state)  # two failures; the busy runs neither help nor hurt
    assert state["runs"][-1]["busy"] is True and state["runs"][-1]["ok"] is False
    auto_update.record_start(store)
    auto_update.record_finish(store, 1)
    assert auto_update.failing(store.get_meta(auto_update.STATE_KEY))
    auto_update.record_start(store)
    auto_update.record_finish(store, 2)  # partial is a fine run, as before
    assert not auto_update.failing(store.get_meta(auto_update.STATE_KEY))


def test_the_hook_child_that_finds_the_project_busy_exits_75_and_is_not_counted(tmp_path, capsys):
    folder, flags, _ = project(tmp_path)
    assert main(["scan", str(folder), *flags]) == 0
    capsys.readouterr()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    auto_update.enable(store, runner="claude", units=1, cooldown=0)
    with store.analyze_lock():  # a person's analysis started between the hook's check and the child's start
        assert auto_update.run_child(folder) == 75
    state = store.get_meta(auto_update.STATE_KEY)
    assert state["runs"][-1]["exit"] == 75 and state["runs"][-1]["busy"] and not auto_update.failing(state)
    assert any("skipped" in line or "건너뜀" in line for line in auto_update.describe(store))


def test_a_one_off_sources_scan_is_not_remembered_by_status_or_the_hook(tmp_path, capsys):
    folder = demo(tmp_path, capsys)
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    auto_update.enable(store, runner="codex", units=1, cooldown=0)
    assert main(["scan", str(folder)]) == 0
    capsys.readouterr()
    assert auto_update.decide(scope, store)[0] == "nothing_pending"
    assert main(["scan", str(folder), "--sources", "codex"]) == 0  # once, to look at Codex alone
    capsys.readouterr()
    # The index keeps what the full scan saw of the other tools, so their unchanged files are not new.
    assert main(["status", str(folder), "--json"]) == 0
    since = output(capsys)["since_scan"]
    assert since["files_new"] == 0 and since["files_changed"] == 0
    claude_home = Path(store.get_meta("options")["claude_home"])
    write(claude_home / "projects" / "new" / "n1.jsonl",
          [{"type": "user", "uuid": "n-0", "sessionId": "n1", "cwd": str(folder.resolve()),
            "timestamp": "2026-10-20T01:00:00Z", "message": {"content": [{"type": "text", "text": "New Claude work."}]}}])
    # New work of a tool the one-off scan left out is new work for every check that reads all tools.
    assert auto_update.decide(scope, store)[0] == "run"
    assert main(["status", str(folder), "--json"]) == 0
    since = output(capsys)["since_scan"]
    assert since["records"] == 1 and since["files_new"] == 1 and since["by_source"] == {"claude": 1}


def test_linked_subdirectories_are_followed_within_the_home_and_counted_when_they_lead_out(tmp_path, capsys):
    folder, flags, homes = project(tmp_path)
    # A Claude Code project directory moved within the home and linked back: followed.
    real = homes["claude"] / "archive" / "app"
    real.parent.mkdir()
    (homes["claude"] / "projects" / "app").rename(real)
    (homes["claude"] / "projects" / "app").symlink_to(real, target_is_directory=True)
    # A Codex year directory linked to a place outside the Codex home: not entered, counted and named.
    outside = tmp_path / "elsewhere" / "2026"
    outside.parent.mkdir()
    (homes["codex"] / "sessions" / "2026").rename(outside)
    (homes["codex"] / "sessions" / "2026").symlink_to(outside, target_is_directory=True)
    # A link back up the tree is entered once, not forever.
    (homes["claude"] / "projects" / "loop").symlink_to(homes["claude"] / "projects", target_is_directory=True)
    assert main(["scan", str(folder), *flags]) == 0
    found = output(capsys)
    by = {entry["provider"]: entry for entry in found["sources"]}
    assert found["records"]["claude"] == 1 and by["claude"]["files_examined"] == 1
    assert by["claude"]["dirs_skipped_symlink"] == 0
    assert found["records"]["codex"] == 0 and by["codex"]["files_examined"] == 0
    assert by["codex"]["dirs_skipped_symlink"] == 1
    assert any(line.startswith("linked directory not followed") and "2026" in line for line in found["limitations"])
    # The same directory linked from inside the Codex home is followed.
    inside = homes["codex"] / "kept"
    outside.rename(inside)
    (homes["codex"] / "sessions" / "2026").unlink()
    (homes["codex"] / "sessions" / "2026").symlink_to(inside, target_is_directory=True)
    assert main(["scan", str(folder), *flags]) == 0
    found = output(capsys)
    by = {entry["provider"]: entry for entry in found["sources"]}
    assert found["records"]["codex"] == 1 and by["codex"]["dirs_skipped_symlink"] == 0
    assert not any(line.startswith("linked directory not followed") for line in found["limitations"])


def test_analyze_json_interrupted_mid_run_reports_the_recorded_run(tmp_path, capsys, monkeypatch):
    folder = demo(tmp_path, capsys)

    class Interrupted:
        def invoke(self, state, config=None):
            studio_graph._cli_invocations[state["run_id"]].completed = 2  # two units published, then SIGTERM
            raise KeyboardInterrupt("SIGTERM")
    monkeypatch.setattr(studio_graph, "graph", Interrupted())
    assert main(["analyze", str(folder), "--json"]) == 130
    result = output(capsys)
    assert result["status"] == "cancelled" and result["exit_code"] == 130 and result["error_kind"] == "cancelled"
    assert result["run_id"] and result["run_id"].startswith("run_") and result["completed_units"] == 2
    assert result["graph_version"] >= 1
    # An interrupt before any run began is still reported as one that never started.
    def early(*a, **k):
        raise KeyboardInterrupt("SIGINT")
    monkeypatch.setattr(cli.Engine, "analyze", early)
    assert main(["analyze", str(folder), "--json"]) == 130
    result = output(capsys)
    assert result["status"] == "cancelled" and result["run_id"] is None and result["completed_units"] == 0


def test_a_usage_error_of_analyze_json_still_prints_one_object(tmp_path, capsys):
    folder, flags, _ = project(tmp_path)
    for argv in (["analyze", str(folder), "--json", "--bogus"], ["analyze", str(folder), "--json", "--workers", "x"]):
        with pytest.raises(SystemExit) as stopped:
            main(argv)
        assert stopped.value.code == 64
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["schema"] == "contexttrail.analyze.v1" and result["status"] == "not_started"
        assert result["error_kind"] == "usage" and result["exit_code"] == 64 and result["error"]
        assert "error:" in captured.err
    with pytest.raises(SystemExit):
        main(["analyze", str(folder), "--bogus"])  # without --json, stdout stays empty
    assert capsys.readouterr().out == ""


def test_a_full_scan_after_a_sources_scan_still_reuses_the_other_tools_files(tmp_path, capsys):
    folder, flags, homes = project(tmp_path)
    write(homes["codex"] / "sessions" / "2026" / "10" / "06" / "rollout-other.jsonl",
          [{"type": "session_meta", "timestamp": "2026-10-06T02:00:00Z", "payload": {"id": "o1", "cwd": "/elsewhere"}},
           {"type": "response_item", "timestamp": "2026-10-06T02:00:01Z",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "other"}]}}])
    assert main(["scan", str(folder), *flags]) == 0
    codex = {entry["provider"]: entry for entry in output(capsys)["sources"]}["codex"]
    assert (codex["files_examined"], codex["files_read"]) == (2, 2)
    assert main(["scan", str(folder), *flags, "--sources", "claude"]) == 0
    capsys.readouterr()
    assert main(["scan", str(folder), *flags]) == 0
    codex = {entry["provider"]: entry for entry in output(capsys)["sources"]}["codex"]
    assert (codex["files_examined"], codex["files_read"]) == (2, 1)  # the other project's file is not read again
