from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import tr
from .model import SourceRecord, Snapshot, segment_record
from .util import FlowError, digest, ident, now, within


SAFE_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_PAGER": "cat", "LC_ALL": "C"}


def git(path: Path, *args: str, ok: bool = False, cap: int = 4_000_000) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(SAFE_ENV)
    command = ["git", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "core.untrackedCache=false", "-c", "core.pager=cat", "-c", "diff.external=",
               "-c", "submodule.recurse=false", "-C", str(path), *args]
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FlowError("Git 읽기 실행 실패 또는 timeout") from exc
    if result.returncode and not ok:
        raise FlowError("Git 읽기 실패: " + " ".join(args[:2]))
    if result.returncode:
        return ""
    if len(result.stdout) > cap:
        raise FlowError("Git 출력이 안전한 입력 한도를 초과했습니다. 해당 범위는 미처리입니다.")
    return result.stdout.decode("utf-8", errors="replace").rstrip("\n")


@dataclass
class Scope:
    folder: Path
    root: Path | None
    common_dir: Path | None
    relative: str
    roots: list[Path]
    state_dir: Path
    id: str
    # One scan asks about the same few folders hundreds of thousands of times; each answer resolves
    # symlinks on disk. A scan builds its own Scope (dataclasses.replace), so answers last one scan.
    _answers: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def worktree_for(self, cwd: str | None) -> str | None:
        if cwd in self._answers:
            return self._answers[cwd]
        self._answers[cwd] = answer = self._worktree_for(cwd)
        return answer

    def _worktree_for(self, cwd: str | None) -> str | None:
        if not cwd:
            return None
        path = Path(cwd)
        if not path.is_absolute():
            return None
        # Resolve existing symlinks; never use string-prefix matching.
        for root in sorted(self.roots, key=lambda p: len(p.parts), reverse=True):
            allowed = root / self.relative if self.root else root
            if within(path, allowed):
                return ident("wt_", str(root))
        return None

    def includes(self, cwd: str | None) -> bool:
        return self.worktree_for(cwd) is not None

    @classmethod
    def resolve(cls, folder: str | Path) -> "Scope":
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise FlowError(tr(f"프로젝트 디렉터리가 없습니다: {folder}", f"Project directory does not exist: {folder}"))
        root_text = git(folder, "rev-parse", "--show-toplevel", ok=True)
        if not root_text:
            return cls(folder, None, None, "", [folder], folder / ".projectflow",
                       ident("scope_", str(folder)))
        root = Path(root_text).resolve()
        relative = str(folder.relative_to(root))
        relative = "" if relative == "." else relative
        common = Path(git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        raw = git(folder, "worktree", "list", "--porcelain", "-z")
        roots = [Path(x[9:]).resolve() for x in raw.split("\0") if x.startswith("worktree ")]
        roots = [p for p in roots if (p / relative).is_dir()] or [root]
        key = digest(relative)[:16]
        return cls(folder, root, common, relative, roots, common / "projectflow" / key,
                   ident("scope_", str(common), relative))


def collect_git(scope: Scope, *, history_limit: int = 50, exclude: list[str] | None = None) -> Snapshot:
    if not scope.root:
        return Snapshot([], ["Git 저장소가 아니므로 Git 근거는 없습니다."])
    records, warnings, seen = [], [], set()
    stamp = now()
    # Literal exclusion is obtained by individual changed-path filtering below.
    excluded = {str(Path(p).resolve()) for p in exclude or []}

    def paths_for(root: Path) -> list[str]:
        # Git pathspec exclusions are explicit host-generated patterns, never log commands.
        args = [scope.relative or "."]
        # --literal-pathspecs means magic would be literal. Use env override in a separate
        # post-filter instead; generated state lives under .git and is naturally untracked.
        return args

    def unquote_path(value: str) -> str:
        value = value.rstrip("\t")
        if not (value.startswith('"') and value.endswith('"')):
            return value
        value = value[1:-1]
        output = bytearray()
        index = 0
        escapes = {"n": 10, "t": 9, "r": 13, "b": 8, "f": 12, "v": 11, "a": 7,
                   "\\": 92, '"': 34}
        while index < len(value):
            if value[index] != "\\":
                output.extend(value[index].encode("utf-8")); index += 1
                continue
            index += 1
            match = re.match(r"[0-7]{1,3}", value[index:])
            if match:
                output.append(int(match[0], 8)); index += len(match[0])
            elif index < len(value):
                output.append(escapes.get(value[index], ord(value[index]))); index += 1
        return output.decode("utf-8", "replace")

    def scrub_patch(patch: str, root: Path) -> str:
        chunks = re.split(r"(?m)^diff --git ", patch)
        out = [chunks[0]]
        for chunk in chunks[1:]:
            first = chunk.splitlines()[0] if chunk else ""
            candidates = []
            for line in chunk.splitlines()[1:]:
                if line.startswith(("--- ", "+++ ")):
                    candidate = unquote_path(line[4:])
                    if candidate.startswith(("a/", "b/")):
                        candidates.append(candidate[2:])
            if not candidates:
                # Binary/mode/rename-only patches may have no ---/+++ headers.
                quoted = re.fullmatch(r'(“.*”) (“.*”)'.replace("“", '"').replace("”", '"'), first)
                if quoted:
                    candidates = [unquote_path(x)[2:] for x in quoted.groups()]
                elif " b/" in first:
                    left, right = first.rsplit(" b/", 1)
                    candidates = [left[2:], right]
            if any(str((root / path).resolve()) in excluded or ".projectflow" in Path(path).parts
                   for path in candidates):
                continue
            out.append("diff --git " + chunk)
        return "".join(out).strip()

    for root in scope.roots:
        worktree_id = ident("wt_", str(root))
        try:
            head = git(root, "rev-parse", "--verify", "HEAD", ok=True)
            branch = git(root, "symbolic-ref", "--short", "-q", "HEAD", ok=True) or None
            status_before = git(root, "status", "--porcelain=v1", "-uno", "--", *paths_for(root))
            if head:
                commits = git(root, "rev-list", f"--max-count={history_limit + 1}", "HEAD", "--", *paths_for(root)).splitlines()
                if len(commits) > history_limit:
                    warnings.append(f"Git 과거 커밋은 worktree별 최근 {history_limit}개만 선택했습니다.")
                for oid in commits[:history_limit]:
                    if oid in seen:
                        continue
                    seen.add(oid)
                    info = git(root, "show", "-s", "--format=%P%n%cI%n%s", oid).splitlines()
                    parents = info[0].split() if info else []
                    timestamp = info[1] if len(info) > 1 else None
                    message = "\n".join(info[2:])
                    try:
                        if parents:
                            patch = git(root, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all",
                                        parents[0], oid, "--", *paths_for(root))
                        else:
                            patch = git(root, "diff-tree", "--root", "--no-commit-id", "-r", "-p", "--no-ext-diff",
                                        "--no-textconv", "--ignore-submodules=all", oid, "--", *paths_for(root))
                    except FlowError as exc:
                        warnings.append(f"Git 커밋 {oid[:12]}의 diff를 보류했습니다: {exc}")
                        continue
                    patch = scrub_patch(patch, root)
                    if not patch:
                        continue
                    records.extend(segment_record(SourceRecord(ident("src_", "git-commit", oid, scope.relative), "git", None,
                        "git", f"Commit {oid}\nParent/base: {parents[0] if parents else '(root)'}\n{message}\n{patch}",
                        {"kind": "commit_diff", "commit": oid, "base": parents[0] if parents else None,
                         "root": str(root), "observed_at": stamp, "patch_hash": digest(patch)},
                        recorded_at=timestamp, cwd=str(root / scope.relative), worktree_id=worktree_id,
                        git={"head": oid, "base": parents[0] if parents else None,
                                                   "parents": parents})))
                    if len(parents) > 1:
                        warnings.append(f"병합 커밋 {oid[:12]}은 첫 parent 대비 diff만 선택했습니다.")
            current = []
            for kind, extra in (("staged", ["--cached"]), ("unstaged", [])):
                patch = git(root, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all",
                            *extra, "--", *paths_for(root))
                patch = scrub_patch(patch, root)
                # Empty patches still record a transition back to a clean worktree.
                content = f"Current {kind} snapshot; not historical execution proof.\nHEAD: {head or '(unborn)'}\n" + (patch or "(no changes)")
                current.append(SourceRecord(ident("src_", "git-current", worktree_id, kind, scope.relative),
                    "git", None, "git", content,
                    {"kind": kind + "_diff", "root": str(root), "observed_at": stamp,
                     "head": head, "patch_hash": digest(patch)},
                    cwd=str(root / scope.relative), worktree_id=worktree_id,
                    git={"head": head, "branch": branch, "observation": "current"}))
            status_after = git(root, "status", "--porcelain=v1", "-uno", "--", *paths_for(root))
            head_after = git(root, "rev-parse", "--verify", "HEAD", ok=True)
            # Re-read patches: status strings alone cannot detect content races.
            for record, extra in zip(current, (["--cached"], [])):
                again = scrub_patch(git(root, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all",
                                         *extra, "--", *paths_for(root)), root)
                if digest(again) != record.locator["patch_hash"]:
                    raise FlowError("현재 Git diff를 읽는 동안 내용이 변경되어 게시를 보류했습니다.")
            if head != head_after or status_before != status_after:
                raise FlowError("Git 상태 경쟁이 감지되어 현재 변경 snapshot을 보류했습니다.")
            records.extend(part for record in current for part in segment_record(record))
        except FlowError as exc:
            warnings.append(str(exc))
    warnings.append("Git 입력은 선택한 커밋과 tracked staged/unstaged diff입니다. untracked 파일 내용은 읽지 않습니다.")
    return Snapshot(records, list(dict.fromkeys(warnings)))
