"""A host that keeps its own copy of a project (docs/HOST.md): `--previous-path` brings the sessions recorded where the
folder was before, `--state-dir` keeps the state out of the checkout. Synthetic transcripts only; no runner is started."""
import json
import os
import stat
import subprocess
from pathlib import Path

from contexttrail import contract
from contexttrail.cli import main, parser
from contexttrail.git_context import Scope
from contexttrail.store import Store
from contexttrail.util import dumps


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dumps(r) for r in rows) + "\n", encoding="utf-8")


def sessions(tmp_path: Path, recorded_at: Path) -> list[str]:
    """One Claude Code and one Codex session recorded in `recorded_at`, and the home flags that read them."""
    homes = {"codex": tmp_path / "codex", "claude": tmp_path / "claude", "opencode": tmp_path / "opencode"}
    homes["opencode"].mkdir(exist_ok=True)
    write(homes["claude"] / "projects" / "old" / "c1.jsonl",
          [{"type": "user", "uuid": "c1-0", "sessionId": "c1", "cwd": str(recorded_at),
            "timestamp": "2026-10-06T01:00:00Z", "message": {"content": [{"type": "text", "text": "Use SQLite."}]}}])
    write(homes["codex"] / "sessions" / "2026" / "10" / "06" / "rollout-x1.jsonl",
          [{"type": "session_meta", "timestamp": "2026-10-06T02:00:00Z", "payload": {"id": "x1", "cwd": str(recorded_at)}},
           {"type": "response_item", "timestamp": "2026-10-06T02:00:01Z",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add a test."}]}}])
    return ["--codex-home", str(homes["codex"]), "--claude-home", str(homes["claude"]),
            "--opencode-home", str(homes["opencode"])]


def scanned(capsys, *argv: str) -> dict:
    assert main(["scan", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def git_init(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(folder), "init", "-q"], check=True)


def test_a_previous_path_brings_the_sessions_recorded_where_the_folder_was(tmp_path, capsys):
    folder, original = tmp_path / "copy", tmp_path / "original"  # the original need not exist here
    folder.mkdir()
    flags = sessions(tmp_path, original / "notebooks")
    assert scanned(capsys, str(folder), *flags)["records"] == {"codex": 0, "claude": 0, "opencode": 0, "git": 0}
    result = scanned(capsys, str(folder), *flags, "--previous-path", str(original))
    assert result["records"]["claude"] == 1 and result["records"]["codex"] == 1
    assert result["previous_paths"] == [str(original.resolve())]
    scope = Scope.resolve(folder)
    moved = scope.with_previous([original.resolve()])
    # The folder's own worktree, as if it had never moved; a sibling of the old path is not the project.
    assert moved.worktree_for(str(original / "notebooks")) == scope.worktree_for(str(folder))
    assert moved.worktree_for(str(tmp_path / "original-backup")) is None
    # A scan uses the paths for itself only: nothing is saved.
    assert Store(scope.state_dir, scope.id).previous_paths() == []
    assert scanned(capsys, str(folder), *flags)["records"]["claude"] == 0


def test_analyze_saves_the_previous_paths_and_no_previous_paths_clears_them(tmp_path, capsys):
    folder, original = tmp_path / "copy", tmp_path / "original"
    folder.mkdir()
    flags = sessions(tmp_path, original)
    # No runner: the run is refused, but its checked settings are saved, as the other options are.
    assert main(["analyze", str(folder), *flags, "--previous-path", str(original), "--no-tui", "--json"]) == 77
    capsys.readouterr()
    scope = Scope.resolve(folder)
    assert Store(scope.state_dir, scope.id).previous_paths() == [str(original.resolve())]
    assert scanned(capsys, str(folder), *flags)["records"]["claude"] == 1  # every later command reads the list
    assert main(["status", str(folder), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["previous_paths"] == [str(original.resolve())]
    assert scanned(capsys, str(folder), *flags, "--no-previous-paths")["records"]["claude"] == 0  # this scan only
    assert Store(scope.state_dir, scope.id).previous_paths() == [str(original.resolve())]
    # The sessions no longer belong here, so nothing is pending (a partial run, exit 2) or the run is refused.
    assert main(["analyze", str(folder), *flags, "--no-previous-paths", "--no-tui", "--json"]) in (2, 77)
    capsys.readouterr()
    assert Store(scope.state_dir, scope.id).previous_paths() == []


def test_a_previous_path_that_holds_or_lies_in_the_project_is_refused(tmp_path, capsys):
    folder = tmp_path / "copy"
    (folder / "sub").mkdir(parents=True)
    for value in (tmp_path, folder, folder / "sub", folder / "not-yet"):
        assert main(["scan", str(folder), "--previous-path", str(value)]) == contract.EXIT_USAGE
    assert not (folder / ".contexttrail").exists()  # refused before any state is made


def test_a_previous_path_given_through_a_link_is_kept_as_its_target(tmp_path, capsys):
    folder, real = tmp_path / "copy", tmp_path / "real-original"
    folder.mkdir()
    real.mkdir()
    link = tmp_path / "link-original"
    link.symlink_to(real)
    flags = sessions(tmp_path, link / "work")  # recorded under the link's spelling
    result = scanned(capsys, str(folder), *flags, "--previous-path", str(link))
    assert result["previous_paths"] == [str(real.resolve())] and result["records"]["claude"] == 1
    moved = Scope.resolve(folder).with_previous([real.resolve()])
    assert moved.includes(str(real / "work")) and moved.includes(str(link / "work"))


def test_a_subfolder_scope_takes_the_same_subfolder_of_a_previous_path(tmp_path):
    repo = tmp_path / "repo"
    git_init(repo)
    (repo / "analysis").mkdir()
    scope = Scope.resolve(repo / "analysis")
    old_root = tmp_path / "old-repo"
    moved = scope.with_previous([scope.previous_path(old_root)])
    assert moved.worktree_for(str(old_root / "analysis" / "plots")) == scope.worktree_for(str(repo / "analysis"))
    assert not moved.includes(str(old_root / "other"))
    assert not moved.includes(str(old_root))


def test_nothing_is_read_with_git_at_a_previous_path(tmp_path, capsys, monkeypatch):
    from contexttrail import git_context
    folder, original = tmp_path / "copy", tmp_path / "original"
    git_init(folder)
    git_init(original)  # a repository there must not be read
    flags = sessions(tmp_path, original)
    asked = []
    real = git_context.git
    monkeypatch.setattr(git_context, "git", lambda path, *args, **kw: asked.append(Path(path)) or real(path, *args, **kw))
    scanned(capsys, str(folder), *flags, "--previous-path", str(original))
    assert asked and not any(path.resolve() == original.resolve() for path in asked)


def test_state_dir_keeps_the_state_out_of_the_checkout(tmp_path, capsys):
    folder, state = tmp_path / "copy", tmp_path / "host" / "private" / "contexttrail" / "p1"
    git_init(folder)
    (folder / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(folder), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-qm", "first"], check=True)
    flags = sessions(tmp_path, folder)
    result = scanned(capsys, str(folder), *flags, "--state-dir", str(state))
    assert result["state_dir"] == str(state) and (state / "state.sqlite").is_file()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert not (folder / ".git" / "contexttrail").exists() and not (folder / ".contexttrail").exists()
    # Not saved: the next command finds it only when given it again.
    assert main(["status", str(folder), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "none"
    for argv in (["status", str(folder), "--json"], ["find", "", str(folder), "--json"], ["ops", str(folder)],
                 ["refresh", str(folder), *flags, "--json"], ["graph", str(folder)]):
        assert main([*argv, "--state-dir", str(state)]) == 0, argv
        capsys.readouterr()
    export = tmp_path / "out" / "graph.json"
    export.parent.mkdir()
    assert main(["export", str(folder), "--format", "json", "--output", str(export), "--state-dir", str(state)]) == 0
    assert json.loads(export.read_text())["format"] == "contexttrail.export.v1"
    assert not (folder / ".git" / "contexttrail").exists()


def test_a_state_dir_must_be_absolute_and_outside_the_project(tmp_path, capsys, monkeypatch):
    folder = tmp_path / "copy"
    folder.mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["scan", str(folder), "--state-dir", "relative/state"]) == contract.EXIT_USAGE
    assert main(["scan", str(folder), "--state-dir", str(folder / "state")]) == contract.EXIT_USAGE
    assert not (tmp_path / "relative").exists() and not (folder / "state").exists()


def test_two_projects_cannot_share_one_state_dir(tmp_path, capsys):
    state = tmp_path / "state"
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    flags = sessions(tmp_path, tmp_path / "a")
    assert main(["scan", str(tmp_path / "a"), *flags, "--state-dir", str(state)]) == 0
    capsys.readouterr()
    assert main(["scan", str(tmp_path / "b"), *flags, "--state-dir", str(state)]) != 0


def test_the_hooks_refuse_a_state_dir(tmp_path, capsys):
    folder, state = tmp_path / "copy", tmp_path / "state"
    folder.mkdir()
    for argv in (["note", str(folder), "--enable"], ["auto-update", "--folder", str(folder), "--status"],
                 ["context", str(folder), "--enable"]):
        assert main([*argv, "--state-dir", str(state)]) == contract.EXIT_USAGE, argv
    assert not state.exists()


def test_the_host_finds_both_options_in_help_and_capabilities(capsys):
    root = parser()
    commands = next(a.choices for a in root._actions if hasattr(a, "choices") and isinstance(a.choices, dict))
    analyze, export = commands["analyze"].format_help(), commands["export"].format_help()
    assert "--previous-path" in analyze and "--state-dir" in analyze and "--state-dir" in export
    assert main(["capabilities"]) == 0
    listed = json.loads(capsys.readouterr().out)["commands"]
    flags = lambda name: {flag for arg in listed[name]["arguments"] for flag in arg["flags"]}  # noqa: E731
    for name in ("analyze", "scan", "view", "serve"):
        assert {"--previous-path", "--no-previous-paths", "--state-dir"} <= flags(name)
    for name in ("status", "find", "show", "graph", "export", "refresh", "ops", "context"):
        assert "--state-dir" in flags(name), name
