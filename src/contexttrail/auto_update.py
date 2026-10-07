"""Opt-in automatic analysis from the coding tools' hooks, per project, never blocking the host.

Every host hook (Claude Code `Stop`, Codex `Stop`, opencode `session.idle`) runs one command,
`contexttrail auto-update`, which:

1. finds the project folder (the hook's `cwd` on stdin, or `--folder`), checks that it is a real
   directory and resolves its state directory the way every other command does;
2. does nothing unless the person enabled automatic analysis for that project, in their own
   terminal, with `contexttrail auto-update --enable --runner codex|claude --units N`; enabling
   is the explicit request the analysis invariant asks for, and records the runner consent;
3. still does nothing when an analysis is running, when nothing is pending, inside the cooldown,
   past the daily cap, or when the host is ContextTrail's own runner session;
4. otherwise starts a detached child (new session, stdin closed, output to `auto-update.log` in
   the state directory) and returns at once. The child, `contexttrail auto-update --run`, runs
   `contexttrail analyze … --yes --no-tui --brief --units N --trigger hook` in its own process
   and records how it ended, so after `FAILURE_STOP` failed runs in a row the hook stops starting
   new ones (`failing`) until the person enables automatic analysis again.

The command always exits 0 and never writes transcript text to its log: a hook must not stop the
host, and a log must not become a copy of the conversation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .freshness import FIND_BUDGET_BYTES, check
from .git_context import Scope
from .i18n import tr
from .store import Store
from .util import FlowError, now, private_dir

META_KEY = "auto_update"          # the person's settings for this project
STATE_KEY = "auto_update_state"   # when the hook started runs, and how the last ones ended
LOG_NAME = "auto-update.log"
RUNNERS = ("codex", "claude")
DEFAULT_COOLDOWN = 15 * 60
DEFAULT_MAX_RUNS_PER_DAY = 8
FAILURE_STOP = 3                  # failed runs in a row after which the hook starts nothing more
RUN_OK_CODES = {0, 2}             # analyze: complete/noop, or partial (units left, which `--units N` always leaves)
RUNS_KEPT = 16
SELF_RUN_PREFIXES = ("contexttrail-run-", "projectflow-run-")
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """`15m`, `2h`, `90s`, `1d` or plain seconds → seconds."""
    found = _DURATION.match(text or "")
    if not found:
        raise FlowError(tr(f"기간 형식이 아닙니다: {text!r} (예: 15m, 2h)", f"Not a duration: {text!r} (for example 15m, 2h)"))
    return int(found.group(1)) * _UNITS[found.group(2)]


def hook_folder(argument: str | None, stdin_text: str | None) -> Path | None:
    """The project folder a hook names: `--folder`, else the `cwd` of the JSON on stdin. None when there is none."""
    candidate = argument
    if not candidate and stdin_text:
        try:
            payload = json.loads(stdin_text)
        except ValueError:
            return None
        candidate = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(candidate, str) or not candidate or not Path(candidate).is_absolute():
        return None
    folder = Path(candidate)
    return folder if folder.is_dir() else None


def settings(store: Store) -> dict[str, Any] | None:
    value = store.get_meta(META_KEY)
    return value if isinstance(value, dict) and value.get("runner") in RUNNERS and value.get("units") else None


def enable(store: Store, *, runner: str, units: int, cooldown: int = DEFAULT_COOLDOWN,
           max_runs_per_day: int = DEFAULT_MAX_RUNS_PER_DAY) -> dict[str, Any]:
    if runner not in RUNNERS:
        raise FlowError(tr("Runner는 codex 또는 claude여야 합니다.", "The runner must be codex or claude."))
    if units < 1 or max_runs_per_day < 1 or cooldown < 0:
        raise FlowError(tr("units와 하루 상한은 1 이상, cooldown은 0 이상이어야 합니다.",
                           "units and the daily cap must be at least 1, the cooldown at least 0."))
    value = {"runner": runner, "units": units, "cooldown_seconds": cooldown,
             "max_runs_per_day": max_runs_per_day, "enabled_at": now()}
    store.set_meta(META_KEY, value)
    # Enabling is the person's consent to send this project's records to that runner's model.
    store.set_meta("consent:" + runner, True)
    options = store.get_meta("options", {}) or {}
    options["runner"] = runner
    store.set_meta("options", options)
    # Enabling again is also how the person restarts after the failure stop: the run history is forgotten,
    # the day's start count is not (the cap still holds).
    state = store.get_meta(STATE_KEY, {}) or {}
    store.set_meta(STATE_KEY, {"starts": state.get("starts", []), "runs": []})
    return value


def disable(store: Store) -> None:
    store.set_meta(META_KEY, None)


def decide(scope: Scope, store: Store, *, moment: datetime | None = None) -> tuple[str, dict[str, Any] | None]:
    """Why the hook does nothing, or ("run", settings) when an analysis should start."""
    moment = moment or datetime.now(timezone.utc)
    value = settings(store)
    if not value:
        return "disabled", None
    if scope.folder.name.startswith(SELF_RUN_PREFIXES):
        return "self_run", None
    try:
        with store.analyze_lock():
            pass
    except FlowError:
        return "lock_held", None
    state = store.get_meta(STATE_KEY, {}) or {}
    starts = [s for s in state.get("starts", []) if isinstance(s, str)]
    recent = [s for s in starts if _parse(s) and moment - _parse(s) < timedelta(days=1)]
    if recent and moment - max(_parse(s) for s in recent) < timedelta(seconds=value["cooldown_seconds"]):
        return "cooldown", None
    if len(recent) >= value["max_runs_per_day"]:
        return "daily_cap", None
    if failing(state):
        return "failing", None
    fresh = check(scope, store, budget_bytes=FIND_BUDGET_BYTES, **_homes(store))
    since = fresh.get("since_scan") or {}
    pending = fresh["pending"]["records"] or since.get("records") or (
        not since.get("parsed", True) and (since.get("files_changed") or since.get("databases_changed")))
    if not pending:
        return "nothing_pending", None
    return "run", value


def record_start(store: Store, moment: datetime | None = None) -> None:
    moment = moment or datetime.now(timezone.utc)
    state = store.get_meta(STATE_KEY, {}) or {}
    starts = [s for s in state.get("starts", []) if isinstance(s, str) and _parse(s) and moment - _parse(s) < timedelta(days=1)]
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    starts.append(stamp)
    runs = [r for r in state.get("runs", []) if isinstance(r, dict)]
    runs.append({"started": stamp, "finished": None, "exit": None})
    store.set_meta(STATE_KEY, {"starts": starts[-64:], "runs": runs[-RUNS_KEPT:]})


def record_finish(store: Store, exit_code: int, note: str | None = None, moment: datetime | None = None) -> None:
    """The child's exit code, on the last run that has no end yet (a run killed outright keeps none)."""
    moment = moment or datetime.now(timezone.utc)
    state = store.get_meta(STATE_KEY, {}) or {}
    runs = [r for r in state.get("runs", []) if isinstance(r, dict)]
    target = next((r for r in reversed(runs) if r.get("finished") is None), None)
    if target is None:
        target = {"started": None}
        runs.append(target)
    target.update(finished=moment.strftime("%Y-%m-%dT%H:%M:%SZ"), exit=int(exit_code), ok=int(exit_code) in RUN_OK_CODES)
    if note:
        target["note"] = note[:120]
    store.set_meta(STATE_KEY, {"starts": state.get("starts", []), "runs": runs[-RUNS_KEPT:]})


def failing(state: dict[str, Any]) -> bool:
    """True after `FAILURE_STOP` finished runs in a row that did not end well."""
    finished = [r for r in state.get("runs", []) if isinstance(r, dict) and r.get("finished")]
    tail = finished[-FAILURE_STOP:]
    return len(tail) == FAILURE_STOP and not any(r.get("ok") for r in tail)


def analyze_argv(folder: Path, value: dict[str, Any], python: Path | None = None) -> list[str]:
    return [str(python or sys.executable), "-m", "contexttrail", "analyze", str(folder), "--runner", value["runner"],
            "--yes", "--no-tui", "--brief", "--units", str(value["units"]), "--trigger", "hook"]


def child_argv(folder: Path, python: Path | None = None) -> list[str]:
    """The detached process the hook starts: it runs the analysis and records how it ended."""
    return [str(python or sys.executable), "-m", "contexttrail", "auto-update", "--run", "--folder", str(folder)]


def run_child(folder: Path) -> int:
    """What `contexttrail auto-update --run` does: the analysis, in this process, with its end recorded."""
    from .cli import main  # the CLI imports this module

    scope = Scope.resolve(folder)
    if not (scope.state_dir / "state.sqlite").is_file():
        return 0  # like the hook: no state is created for a project the person did not open
    store = Store(scope.state_dir, scope.id)
    value = settings(store)
    if not value:
        return 0
    code, note = 1, None
    try:
        code = main(analyze_argv(scope.folder, value)[3:])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except BaseException as exc:  # recorded, then re-raised: the log keeps the traceback
        note = type(exc).__name__
        raise
    finally:
        try:
            record_finish(store, code, note)
            _log(scope.state_dir / LOG_NAME, f"run finished: exit {code}" + (f" ({note})" if note else ""))
        except Exception:
            pass
    return code


def spawn(argv: list[str], log_path: Path, cwd: Path) -> int:
    """Start a command that outlives this process: its own session, stdin closed, output appended to a 0600 log."""
    private_dir(log_path.parent)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, f"\n--- {now()} start: {' '.join(argv[1:])}\n".encode("utf-8"))
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, cwd=str(cwd),
                                   start_new_session=True, close_fds=True)
    finally:
        os.close(fd)
    return process.pid


def run_hook(folder: Path | None, *, python: Path | None = None, launcher=None) -> tuple[str, int | None]:
    """What `contexttrail auto-update` does when a hook calls it. Never raises; the caller exits 0."""
    if folder is None:
        return "no_folder", None
    try:
        scope = Scope.resolve(folder)
    except Exception:
        return "no_scope", None
    if not (scope.state_dir / "state.sqlite").is_file():
        return "disabled", None  # never create state for a project the person did not open with ContextTrail
    try:
        store = Store(scope.state_dir, scope.id)
        reason, value = decide(scope, store)
        if reason != "run":
            if reason != "disabled":
                # Where it is enabled, every quiet decision is one line in the log, so "why not?" has an answer.
                _log(scope.state_dir / LOG_NAME, f"hook: {reason}")
            return reason, None
        record_start(store)
        pid = (launcher or spawn)(child_argv(scope.folder, python), scope.state_dir / LOG_NAME, scope.folder)
        return "run", pid
    except Exception as exc:  # a hook reports to its log only
        try:
            _log(scope.state_dir / LOG_NAME, f"hook error: {type(exc).__name__}")
        except OSError:
            pass
        return "error", None


def _log(path: Path, line: str) -> None:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(f"{now()} {line}\n")


def _homes(store: Store) -> dict[str, Path]:
    options = store.get_meta("options", {}) or {}
    return {key: Path(options[key]) for key in ("codex_home", "claude_home", "opencode_home") if options.get(key)}


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def describe(store: Store, log_path: Path | None = None) -> list[str]:
    value = settings(store)
    if not value:
        return [tr("이 프로젝트의 자동 갱신: 꺼짐", "Automatic analysis for this project: off")]
    state = store.get_meta(STATE_KEY, {}) or {}
    starts = state.get("starts", [])
    lines = [tr(f"이 프로젝트의 자동 갱신: 켜짐 · runner {value['runner']} · 한 번에 {value['units']}개 단위 · "
                f"cooldown {value['cooldown_seconds']}초 · 하루 최대 {value['max_runs_per_day']}회 · 켠 시각 {value['enabled_at']}",
                f"Automatic analysis for this project: on · runner {value['runner']} · {value['units']} units per run · "
                f"cooldown {value['cooldown_seconds']} s · at most {value['max_runs_per_day']} runs a day · enabled {value['enabled_at']}"),
             tr(f"훅이 띄운 실행: 지난 24시간 {len(starts)}회" + (f", 마지막 {starts[-1]}" if starts else ""),
                f"Runs started by hooks: {len(starts)} in the last 24 h" + (f", last {starts[-1]}" if starts else ""))]
    finished = [r for r in state.get("runs", []) if isinstance(r, dict) and r.get("finished")]
    if finished:
        last = finished[-1]
        verdict = tr("정상", "ok") if last.get("ok") else tr("실패", "failed")
        lines.append(tr(f"마지막 실행 결과: {verdict} (exit {last.get('exit')}, 종료 {last['finished']})",
                        f"Last run: {verdict} (exit {last.get('exit')}, ended {last['finished']})"))
    if failing(state):
        where = f" · {log_path}" if log_path else ""
        lines.append(tr(f"연속 {FAILURE_STOP}회 실패로 멈춤: 훅은 새 실행을 시작하지 않습니다. 원인은 로그에{where}, "
                        f"고친 뒤 다시 켜려면 contexttrail auto-update --enable --runner {value['runner']} --units {value['units']}",
                        f"Stopped after {FAILURE_STOP} failed runs in a row: the hooks start nothing more. The cause is in the log{where}; "
                        f"once fixed, enable again: contexttrail auto-update --enable --runner {value['runner']} --units {value['units']}"))
    return lines


# ---- hook installation -------------------------------------------------------------------------

_MARKER = "contexttrail auto-update"
_MANAGED_COMMENT = "// ContextTrail managed hook: reinstalled with the program."


def hook_command(python: Path | None = None) -> str:
    import shlex
    return f"{shlex.quote(str(python or sys.executable))} -m contexttrail auto-update"


NOTE_MARKER = "contexttrail note --hook"


def note_hook_command(python: Path | None = None) -> str:
    import shlex
    return f"{shlex.quote(str(python or sys.executable))} -m contexttrail note --hook"


def _merge_hooks(document: dict[str, Any], event: str, entry: dict[str, Any], *, force: bool, where: str,
                 marker: str = _MARKER) -> bool:
    """Put one managed command hook under `document[event]`, replacing an older managed one only with --force."""
    groups = document.setdefault(event, [])
    if not isinstance(groups, list):
        raise FlowError(tr(f"{where}의 {event} 항목이 목록이 아닙니다.", f"The {event} entry of {where} is not a list."))
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            continue
        for index, hook in enumerate(hooks):
            if isinstance(hook, dict) and marker in str(hook.get("command", "")):
                if hook == entry["hooks"][0]:
                    return False
                if not force:
                    raise FlowError(tr(f"{where}에 ContextTrail 훅이 이미 있습니다 (갱신하려면 --force)",
                                       f"{where} already has a ContextTrail hook (--force to update)"))
                hooks[index] = entry["hooks"][0]
                return True
    groups.append(entry)
    return True


def _load_json(path: Path, where: str) -> dict[str, Any]:
    if path.is_symlink():
        raise FlowError(tr(f"{where}가 symlink입니다.", f"{where} is a symlink."))
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise FlowError(tr(f"{where}를 JSON으로 읽지 못했습니다; 손대지 않습니다.", f"{where} is not readable as JSON; left untouched.")) from exc
    if not isinstance(value, dict):
        raise FlowError(tr(f"{where}의 최상위가 객체가 아닙니다.", f"The top level of {where} is not an object."))
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def install_claude_hook(home: Path, python: Path | None = None, *, force: bool = False) -> Path:
    """`Stop` hook in ~/.claude/settings.json, `async` so the turn never waits; other settings are kept."""
    path = home / ".claude" / "settings.json"
    document = _load_json(path, str(path))
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise FlowError(tr(f"{path}의 hooks가 객체가 아닙니다.", f"hooks in {path} is not an object."))
    entry = {"matcher": "", "hooks": [{"type": "command", "command": hook_command(python), "async": True, "timeout": 30}]}
    changed = _merge_hooks(hooks, "Stop", entry, force=force, where=str(path))
    # The note reminder must answer before the turn ends (its `block` sends the agent back), so it is not async.
    note = {"matcher": "", "hooks": [{"type": "command", "command": note_hook_command(python), "timeout": 30}]}
    changed = _merge_hooks(hooks, "Stop", note, force=force, where=str(path), marker=NOTE_MARKER) or changed
    if changed:
        _write_json(path, document)
    return path


def install_codex_hook(home: Path, python: Path | None = None, *, force: bool = False) -> Path:
    """`Stop` hook in ~/.codex/hooks.json: `{"hooks": {"Stop": [...]}}`, the same shape as Claude Code's `hooks` object.

    Codex runs hooks synchronously (the command returns at once) and asks the person to trust a new
    hooks file in its own UI before running it.
    """
    path = home / ".codex" / "hooks.json"
    document = _load_json(path, str(path))
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise FlowError(tr(f"{path}의 hooks가 객체가 아닙니다.", f"hooks in {path} is not an object."))
    entry = {"hooks": [{"type": "command", "command": hook_command(python), "timeout": 30}]}
    changed = _merge_hooks(hooks, "Stop", entry, force=force, where=str(path))
    note = {"hooks": [{"type": "command", "command": note_hook_command(python), "timeout": 30}]}
    changed = _merge_hooks(hooks, "Stop", note, force=force, where=str(path), marker=NOTE_MARKER) or changed
    if changed:
        _write_json(path, document)
    return path


def opencode_plugin(python: Path | None = None) -> str:
    interpreter = str(python or sys.executable)
    return f'''{_MANAGED_COMMENT}
// Runs `contexttrail auto-update` for this directory when a session goes idle. The command returns
// at once and starts an analysis only in projects where automatic analysis was enabled.
// Notes (`contexttrail note`): the session's ID is put into the tool shell so `--session current`
// finds it, and when a session goes idle with edits or checks it did not note, in a project where
// notes are on, the reminder `contexttrail note --hook` answers with is sent to it once.
export const ContextTrailAutoUpdate = async ({{ client, directory, $ }}) => {{
  const python = {json.dumps(interpreter)}
  return {{
    "shell.env": async (input, output) => {{
      if (input && input.sessionID) output.env.OPENCODE_SESSION_ID = input.sessionID
    }},
    event: async ({{ event }}) => {{
      if (event.type !== "session.idle") return
      try {{
        await $`${{python}} -m contexttrail auto-update --folder ${{directory}}`.quiet().nothrow()
      }} catch {{}}
      const sessionID = event.properties && event.properties.sessionID
      if (!sessionID) return
      try {{
        const request = JSON.stringify({{ session_id: sessionID, cwd: directory }})
        const result = await $`${{python}} -m contexttrail note --hook < ${{new Response(request)}}`.quiet().nothrow()
        const text = result.stdout.toString().trim()
        if (!text) return
        const decision = JSON.parse(text)
        if (decision.decision !== "block" || !decision.reason) return
        await client.session.prompt({{ path: {{ id: sessionID }}, body: {{ parts: [{{ type: "text", text: decision.reason }}] }} }})
      }} catch {{}}
    }},
  }}
}}
'''


def install_opencode_plugin(home: Path, python: Path | None = None, *, force: bool = False) -> Path:
    path = home / ".config" / "opencode" / "plugins" / "contexttrail.ts"
    content = opencode_plugin(python)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FlowError(tr(f"플러그인 대상이 일반 파일이 아닙니다: {path}", f"The plugin target is not a regular file: {path}"))
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return path
        if _MANAGED_COMMENT not in existing and not force:
            raise FlowError(tr(f"기존 플러그인을 덮어쓰지 않았습니다: {path} (갱신하려면 --force)",
                               f"An existing plugin was not overwritten: {path} (--force to update)"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    return path


def install_hooks(home: Path, python: Path | None = None, *, claude: bool, codex: bool, opencode: bool,
                  force: bool = False) -> list[Path]:
    home = home.expanduser().resolve()
    python = (python or Path(sys.executable)).expanduser().absolute()
    written: list[Path] = []
    if claude:
        written.append(install_claude_hook(home, python, force=force))
    if codex:
        written.append(install_codex_hook(home, python, force=force))
    if opencode:
        written.append(install_opencode_plugin(home, python, force=force))
    return written
