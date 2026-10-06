"""The opt-in automatic analysis: what the hook command does and does not do. No model, no real hook host."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contexttrail import auto_update
from contexttrail.analysis import AnalysisConfig, Engine
from contexttrail.auto_update import (analyze_argv, child_argv, decide, enable, failing, hook_folder, install_hooks,
                                      parse_duration, run_child, run_hook, spawn)
from contexttrail import cli
from contexttrail.cli import main
from contexttrail.demo import FixtureRunner
from contexttrail.git_context import Scope
from contexttrail.store import Store
from contexttrail.util import FlowError

from test_freshness import claude_rows, write


def project(tmp_path, *, records=True):
    folder = tmp_path / "app"
    folder.mkdir()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = {"codex_home": str(tmp_path / "codex"), "claude_home": str(tmp_path / "claude"), "opencode_home": str(tmp_path / "opencode")}
    store.set_meta("options", homes)
    if records:
        write(Path(homes["claude_home"]) / "projects" / "p" / "s1.jsonl", claude_rows(folder, "s1", ["Use SQLite.", "Done."]))
    engine = Engine(scope, store, AnalysisConfig(**{k: Path(v) for k, v in homes.items()}))
    engine.preview_plan(engine.scan())  # ingests, so the records are pending
    return folder, scope, store, engine


def launcher(calls):
    def fake(argv, log_path, cwd):
        calls.append((argv, log_path, cwd))
        return 4242
    return fake


def test_hook_does_nothing_where_automatic_analysis_is_not_enabled(tmp_path):
    folder, scope, store, _ = project(tmp_path)
    calls = []
    assert run_hook(folder, launcher=launcher(calls)) == ("disabled", None)
    assert calls == []
    # a folder ContextTrail never opened gets no state directory either
    other = tmp_path / "other"
    other.mkdir()
    assert run_hook(other, launcher=launcher(calls)) == ("disabled", None)
    assert not (other / ".contexttrail").exists()
    assert run_hook(None, launcher=launcher(calls)) == ("no_folder", None)
    assert hook_folder(None, json.dumps({"cwd": str(tmp_path / "missing")})) is None
    assert hook_folder(None, json.dumps({"cwd": "relative/path"})) is None
    assert hook_folder(None, "not json") is None
    assert hook_folder(str(folder), None) == folder
    assert hook_folder(None, json.dumps({"cwd": str(folder), "session_id": "x"})) == folder


def test_enabling_records_consent_and_the_runner_and_the_hook_then_starts_a_run(tmp_path):
    folder, scope, store, _ = project(tmp_path)
    value = enable(store, runner="claude", units=3, cooldown=0)
    assert store.get_meta("consent:claude") is True
    assert store.get_meta("options")["runner"] == "claude"
    calls = []
    reason, pid = run_hook(folder, launcher=launcher(calls))
    assert (reason, pid) == ("run", 4242)
    argv, log_path, cwd = calls[0]
    assert argv == child_argv(scope.folder) == [sys.executable, "-m", "contexttrail", "auto-update", "--run", "--folder", str(scope.folder)]
    assert log_path == scope.state_dir / "auto-update.log" and cwd == scope.folder
    state = store.get_meta("auto_update_state")
    assert state["starts"] and state["runs"][-1] == {"started": state["starts"][-1], "finished": None, "exit": None}
    assert analyze_argv(scope.folder, value) == [sys.executable, "-m", "contexttrail", "analyze", str(scope.folder), "--runner", "claude",
                                                 "--yes", "--no-tui", "--brief", "--units", "3", "--trigger", "hook"]


def test_the_child_records_how_the_analysis_ended_and_three_failures_stop_the_hook(tmp_path, monkeypatch, capsys):
    folder, scope, store, _ = project(tmp_path)
    enable(store, runner="claude", units=2, cooldown=0)
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 2)  # partial: units left, which --units always leaves
    assert run_hook(folder, launcher=launcher([]))[0] == "run"
    assert run_child(folder) == 2
    assert seen == [analyze_argv(scope.folder, {"runner": "claude", "units": 2})[3:]]
    runs = store.get_meta("auto_update_state")["runs"]
    assert runs[-1]["exit"] == 2 and runs[-1]["ok"] is True and runs[-1]["finished"]
    assert "run finished: exit 2" in (scope.state_dir / "auto-update.log").read_text()
    monkeypatch.setattr(cli, "main", lambda argv: 1)
    for _ in range(3):
        assert run_hook(folder, launcher=launcher([]))[0] == "run"
        assert run_child(folder) == 1
    state = store.get_meta("auto_update_state")
    assert failing(state) and [r["ok"] for r in state["runs"]] == [True, False, False, False]
    assert run_hook(folder, launcher=launcher([])) == ("failing", None)
    assert "hook: failing" in (scope.state_dir / "auto-update.log").read_text()
    assert main(["auto-update", "--status", "--folder", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "마지막 실행 결과: 실패 (exit 1" in out and "연속 3회 실패로 멈춤" in out and "auto-update.log" in out
    # a child that dies with an exception records that too; enabling again forgets the history, not the day's starts
    def boom(argv):
        raise RuntimeError("x")
    monkeypatch.setattr(cli, "main", boom)
    enable(store, runner="claude", units=2, cooldown=0)
    assert store.get_meta("auto_update_state")["runs"] == [] and len(store.get_meta("auto_update_state")["starts"]) == 4
    assert run_hook(folder, launcher=launcher([]))[0] == "run"
    with pytest.raises(RuntimeError):
        run_child(folder)
    assert store.get_meta("auto_update_state")["runs"][-1]["note"] == "RuntimeError"
    # the child, like the hook, creates no state for a folder ContextTrail never opened
    other = tmp_path / "other"
    other.mkdir()
    assert main(["auto-update", "--run", "--folder", str(other)]) == 0
    assert not (other / ".contexttrail").exists()


def test_hook_stays_quiet_during_a_run_when_nothing_is_pending_in_cooldown_and_past_the_cap(tmp_path):
    folder, scope, store, engine = project(tmp_path)
    enable(store, runner="codex", units=2, cooldown=600, max_runs_per_day=2)
    with store.analyze_lock():
        assert decide(scope, store)[0] == "lock_held"
    moment = datetime.now(timezone.utc) - timedelta(hours=30)  # in the past, so the real clock below sees no recent run
    assert decide(scope, store, moment=moment)[0] == "run"
    auto_update.record_start(store, moment)
    assert decide(scope, store, moment=moment + timedelta(minutes=5))[0] == "cooldown"
    assert decide(scope, store, moment=moment + timedelta(minutes=11))[0] == "run"
    auto_update.record_start(store, moment + timedelta(minutes=11))
    assert decide(scope, store, moment=moment + timedelta(hours=2))[0] == "daily_cap"
    assert decide(scope, store, moment=moment + timedelta(hours=25))[0] == "run"
    engine.analyze(FixtureRunner)  # nothing pending any more
    assert decide(scope, store, moment=moment + timedelta(hours=25))[0] == "nothing_pending"
    assert run_hook(folder, launcher=launcher([])) == ("nothing_pending", None)
    assert "hook: nothing_pending" in (scope.state_dir / "auto-update.log").read_text()
    auto_update.disable(store)
    assert decide(scope, store)[0] == "disabled"


def test_a_self_run_folder_and_a_bad_folder_end_quietly(tmp_path):
    folder = tmp_path / "contexttrail-run-abc"
    folder.mkdir()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    enable(store, runner="codex", units=1)
    assert decide(scope, store)[0] == "self_run"
    assert run_hook(tmp_path / "nowhere")[0] in {"no_folder", "no_scope", "disabled"}


def test_the_hook_command_exits_zero_and_prints_nothing_to_stdout(tmp_path, capsys, monkeypatch):
    folder, scope, store, _ = project(tmp_path)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"cwd": str(folder), "hook_event_name": "Stop"})))
    assert main(["auto-update"]) == 0
    assert capsys.readouterr().out == ""
    calls = []
    monkeypatch.setattr(auto_update, "spawn", launcher(calls))
    assert main(["auto-update", "--enable", "--folder", str(folder), "--runner", "codex", "--units", "2", "--cooldown", "1h"]) == 0
    out = capsys.readouterr().out
    assert "켰습니다" in out and "cooldown 3600초" in out
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"cwd": str(folder)})))
    assert main(["auto-update"]) == 0
    assert calls and calls[0][0][-3:] == ["--run", "--folder", str(folder)]
    assert main(["auto-update", "--status", "--folder", str(folder)]) == 0
    assert "지난 24시간 1회" in capsys.readouterr().out
    # with --folder, stdin is not touched (a caller holding it open must not stall the hook), and a relative folder works
    monkeypatch.setattr("sys.stdin", _NeverRead())
    monkeypatch.chdir(folder.parent)
    assert main(["auto-update", "--folder", folder.name]) == 0
    assert "hook: cooldown" in (scope.state_dir / "auto-update.log").read_text()
    assert main(["auto-update", "--disable", "--folder", str(folder)]) == 0
    assert "꺼짐" in capsys.readouterr().out


class _NeverRead:
    def isatty(self):
        return False

    def read(self, n=-1):
        raise AssertionError("stdin was read although --folder was given")


class _Stdin:
    def __init__(self, text):
        self.text = text

    def isatty(self):
        return False

    def read(self, n=-1):
        return self.text


def test_spawned_analysis_outlives_the_hook_process(tmp_path):
    marker = tmp_path / "alive.txt"
    log = tmp_path / "state" / "auto-update.log"
    argv = [sys.executable, "-c", f"import time; time.sleep(0.5); open({str(marker)!r}, 'w').write('alive')"]
    pid = spawn(argv, log, tmp_path)
    assert os.getsid(pid) != os.getsid(0)  # its own session: a host killing its process group does not reach it
    assert (log.parent.stat().st_mode & 0o777) == 0o700 and (log.stat().st_mode & 0o777) == 0o600
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.read_text() == "alive"
    assert "start: -c" in log.read_text() and "alive" not in log.read_text().split("start:")[0]


def test_parse_duration():
    assert [parse_duration(x) for x in ("15m", "2h", "90s", "1d", "30")] == [900, 7200, 90, 86400, 30]
    with pytest.raises(FlowError):
        parse_duration("soon")


def test_hooks_are_merged_into_the_hosts_files_and_user_entries_survive(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}}), encoding="utf-8")
    python = Path("/venv/bin/python")
    paths = install_hooks(home, python, claude=True, codex=True, opencode=True)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["model"] == "opus"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "say done"
    ours = settings["hooks"]["Stop"][1]["hooks"][0]
    assert ours == {"type": "command", "command": "/venv/bin/python -m contexttrail auto-update", "async": True, "timeout": 30}
    codex = json.loads((home / ".codex" / "hooks.json").read_text())
    assert codex["hooks"]["Stop"][0]["hooks"][0]["command"] == "/venv/bin/python -m contexttrail auto-update"
    assert "async" not in codex["hooks"]["Stop"][0]["hooks"][0]
    plugin = (home / ".config" / "opencode" / "plugins" / "contexttrail.ts").read_text()
    assert 'event.type !== "session.idle"' in plugin and "auto-update --folder ${directory}" in plugin
    # files ContextTrail creates are private; the user's existing settings.json keeps its own mode
    assert all(p.stat().st_mode & 0o077 == 0 for p in paths[1:])
    # idempotent, then a changed interpreter needs --force, then --force updates and keeps the user's hook
    assert install_hooks(home, python, claude=True, codex=True, opencode=True) == paths
    assert json.loads((home / ".claude" / "settings.json").read_text()) == settings
    with pytest.raises(FlowError, match="--force"):
        install_hooks(home, Path("/other/python"), claude=True, codex=False, opencode=False)
    install_hooks(home, Path("/other/python"), claude=True, codex=True, opencode=True, force=True)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 2 and settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "say done"
    assert "/other/python" in settings["hooks"]["Stop"][1]["hooks"][0]["command"]
    # a user-authored plugin is never replaced without --force; an unreadable settings file is left alone
    (home / ".config" / "opencode" / "plugins" / "contexttrail.ts").write_text("my plugin", encoding="utf-8")
    with pytest.raises(FlowError, match="--force"):
        install_hooks(home, python, claude=False, codex=False, opencode=True)
    (home / ".codex" / "hooks.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(FlowError, match="JSON"):
        install_hooks(home, python, claude=False, codex=True, opencode=False)
    assert (home / ".codex" / "hooks.json").read_text() == "{not json"


def test_the_run_ledger_names_the_trigger(tmp_path):
    folder, scope, store, engine = project(tmp_path)
    engine.config.trigger = "hook"
    engine.analyze(FixtureRunner)
    with store.connection() as db:
        manifests = [json.loads(row["manifest"]) for row in db.execute("SELECT manifest FROM analysis_runs")]
    assert manifests and manifests[-1]["trigger"] == "hook"
