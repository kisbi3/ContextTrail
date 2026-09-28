import subprocess
from pathlib import Path

from projectflow.git_context import Scope, collect_git
from projectflow.util import FlowError
import projectflow.git_context as git_context


def run(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, check=True, text=True).stdout.strip()


def repo(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    run(folder, "init", "-q")
    run(folder, "config", "user.name", "Fixture")
    run(folder, "config", "user.email", "fixture@example.invalid")
    (folder / "a.txt").write_text("first\n")
    run(folder, "add", "a.txt")
    run(folder, "commit", "-qm", "first")
    return folder


def test_git_commit_and_current_snapshots_are_distinct(tmp_path):
    folder = repo(tmp_path)
    (folder / "a.txt").write_text("second\n")
    result = collect_git(Scope.resolve(folder))
    kinds = {r.locator["kind"] for r in result.records}
    assert {"commit_diff", "staged_diff", "unstaged_diff"} <= kinds
    past = next(r for r in result.records if r.locator["kind"] == "commit_diff")
    current = next(r for r in result.records if r.locator["kind"] == "unstaged_diff")
    assert "+first" in past.content and "+second" in current.content
    assert past.recorded_at is not None and current.recorded_at is None
    assert past.worktree_id == current.worktree_id == Scope.resolve(folder).worktree_for(str(folder))
    assert current.locator["observed_at"]


def test_git_repeated_scan_has_same_digest(tmp_path):
    folder = repo(tmp_path)
    scope = Scope.resolve(folder)
    one, two = collect_git(scope), collect_git(scope)
    assert one.id == two.id


def test_oversized_commit_does_not_hide_later_commits_or_current_diff(tmp_path, monkeypatch):
    folder = repo(tmp_path)
    (folder / "a.txt").write_text("second\n")
    run(folder, "commit", "-qam", "second")
    skipped_oid = run(folder, "rev-parse", "HEAD")
    (folder / "a.txt").write_text("third\n")
    run(folder, "commit", "-qam", "third")
    (folder / "a.txt").write_text("working copy\n")
    original = git_context.git

    def limited(path, *args, **kwargs):
        if args[0] == "diff" and args[-3] == skipped_oid:
            raise FlowError("Git 출력이 안전한 입력 한도를 초과했습니다. 해당 범위는 미처리입니다.")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(git_context, "git", limited)
    result = collect_git(Scope.resolve(folder))
    assert any("diff를 보류" in warning and skipped_oid[:12] in warning
               for warning in result.limitations)
    assert any(r.locator.get("commit") == run(folder, "rev-parse", "HEAD")
               for r in result.records)
    assert any(r.locator["kind"] == "unstaged_diff" and "working copy" in r.content
               for r in result.records)


def test_large_git_diff_is_segmented_for_analysis(tmp_path):
    folder = repo(tmp_path)
    (folder / "large.txt").write_text("change line\n" * 7000)
    run(folder, "add", "large.txt")
    run(folder, "commit", "-qm", "large change")
    oid = run(folder, "rev-parse", "HEAD")
    parts = [r for r in collect_git(Scope.resolve(folder)).records
             if r.locator.get("commit") == oid]
    assert len(parts) > 1
    assert all(len(r.content) <= 32_000 for r in parts)
    assert [r.lineage["fragment_index"] for r in parts] == list(range(1, len(parts) + 1))
    assert all(r.lineage["fragment_count"] == len(parts) for r in parts)
    assert "".join(r.content for r in parts).count("+change line") == 7000


def test_worktrees_share_state_but_not_identity(tmp_path):
    folder = repo(tmp_path)
    other = tmp_path / "worktree"
    run(folder, "worktree", "add", "-qb", "other", str(other), "HEAD")
    one, two = Scope.resolve(folder), Scope.resolve(other)
    assert one.id == two.id and one.state_dir == two.state_dir
    assert one.worktree_for(str(folder)) != one.worktree_for(str(other))
    result = collect_git(one)
    assert len([r for r in result.records if r.locator["kind"] == "commit_diff"]) == 1
    assert len([r for r in result.records if r.locator["kind"] == "unstaged_diff"]) == 2


def test_subfolder_scope_does_not_expand(tmp_path):
    folder = repo(tmp_path)
    (folder / "part").mkdir()
    scope = Scope.resolve(folder / "part")
    assert scope.includes(str(folder / "part"))
    assert not scope.includes(str(folder))
    assert not scope.includes(str(folder / "part-backup"))


def test_git_external_diff_textconv_and_fsmonitor_are_not_executed(tmp_path):
    folder = repo(tmp_path)
    sentinel = tmp_path / "MUST_NOT_EXIST"
    script = tmp_path / "malicious.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    script.chmod(0o700)
    run(folder, "config", "diff.external", str(script))
    run(folder, "config", "diff.evil.textconv", str(script))
    run(folder, "config", "core.fsmonitor", str(script))
    (folder / ".gitattributes").write_text("*.txt diff=evil\n")
    (folder / "a.txt").write_text("changed\n")
    result = collect_git(Scope.resolve(folder))
    assert result.records and not sentinel.exists()


def test_export_excluded_from_current_patch(tmp_path):
    folder = repo(tmp_path)
    (folder / "a.txt").write_text("generated export\n")
    result = collect_git(Scope.resolve(folder), exclude=[str(folder / "a.txt")])
    assert all("generated export" not in r.content for r in result.records)


def test_git_clean_transition_changes_source_hash(tmp_path):
    folder = repo(tmp_path)
    scope = Scope.resolve(folder)
    clean = next(r for r in collect_git(scope).records if r.locator["kind"] == "unstaged_diff")
    (folder / "a.txt").write_text("changed\n")
    changed = next(r for r in collect_git(scope).records if r.locator["kind"] == "unstaged_diff")
    assert clean.source_id == changed.source_id and clean.content_hash != changed.content_hash


def test_unicode_and_space_export_paths_excluded(tmp_path):
    folder = repo(tmp_path)
    for name in ['결과 파일.md', 'quote"file.md', 'tab\tfile.md']:
        (folder/name).write_text('original\n')
        run(folder,'add','--',name)
    run(folder,'commit','-qm','add fixtures')
    outputs=[]
    for name in ['결과 파일.md', 'quote"file.md', 'tab\tfile.md']:
        (folder/name).write_text('SECRET_EXPORT_CONTENT\n'); outputs.append(str(folder/name))
    result=collect_git(Scope.resolve(folder),exclude=outputs)
    assert all('SECRET_EXPORT_CONTENT' not in r.content for r in result.records)
