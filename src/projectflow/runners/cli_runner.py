from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..i18n import tr
from ..util import Cancelled, FlowError, dumps, safe_text

ADAPTER_VERSION = "cli-platform-sandbox-v3-output-checks"
MAX_OUTPUT = 8 * 1024 * 1024
# A run always names its model: the sandbox hides the user's CLI config, so without this
# the CLI's own default (which can change between releases) would silently decide.
DEFAULT_MODELS = {"codex": "gpt-6-sol", "claude": "sonnet"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def execute(args: list[str], *, input_text: str = "", timeout: float = 300,
            cancel: threading.Event | None = None, env: dict[str, str] | None = None,
            cwd: Path | None = None) -> tuple[int, str, str]:
    """No shell. Disk-spooled bounded output. Kill the complete process group on cancellation."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
        stdin.write(input_text.encode("utf-8"))
        stdin.seek(0)
        try:
            process = subprocess.Popen(args, stdin=stdin, stdout=stdout, stderr=stderr,
                                       env=env, cwd=cwd, start_new_session=True)
        except OSError as exc:
            raise FlowError(tr("CLI 프로세스를 실행할 수 없습니다.", "The CLI process could not be started.")) from exc
        start = time.monotonic()
        try:
            while process.poll() is None:
                if cancel and cancel.is_set():
                    raise Cancelled(tr("사용자 요청으로 분석을 중단했습니다.", "Analysis stopped at the person's request."))
                if time.monotonic() - start > timeout:
                    raise FlowError(tr("Runner timeout: 저장된 이전 결과와 추출 단계는 유지됩니다.",
                                       "Runner timeout: saved earlier results and extraction stages are kept."))
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_OUTPUT:
                    raise FlowError(tr("Runner 출력 한도를 초과했습니다.", "The Runner output limit was exceeded."))
                time.sleep(0.05)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_OUTPUT:
            raise FlowError(tr("Runner 출력 한도를 초과했습니다.", "The Runner output limit was exceeded."))
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")


def cli_error(name: str, text: str) -> str | None:
    """The provider's own reason for a failed call ("out of credits", "unknown model"), one short line."""
    messages = []
    for line in text.splitlines() if name == "codex" else [text]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        error = item.get("error")
        if item.get("type") == "error":
            messages.append(item.get("message"))
        elif item.get("type") == "turn.failed" and isinstance(error, dict):
            messages.append(error.get("message"))
        elif name == "claude" and item.get("is_error"):
            messages.append(item.get("result"))
    message = next((m for m in reversed(messages) if isinstance(m, str) and m.strip()), None)
    return " ".join(safe_text(message).split())[:200] if message else None


def parse_codex_output(text: str, result_file: Path | None = None) -> tuple[dict, dict | None]:
    usage, final, completed = None, None, False
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            if line.strip():
                raise FlowError(tr("지원하지 않는 Codex JSONL 출력입니다.", "Unsupported Codex JSONL output."))
            continue
        if not isinstance(item, dict):
            raise FlowError(tr("지원하지 않는 Codex 이벤트 구조입니다.", "Unsupported Codex event structure."))
        if item.get("type") in {"item.started", "item.updated", "item.completed"}:
            payload = item.get("item", {})
            if not isinstance(payload, dict):
                raise FlowError(tr("지원하지 않는 Codex item 구조입니다.", "Unsupported Codex item structure."))
            if payload.get("type") not in {"agent_message", "reasoning"}:
                raise FlowError(tr("금지되었거나 미지원인 Codex item 이벤트입니다. 결과를 게시하지 않습니다.",
                                   "A forbidden or unsupported Codex item event; the result is not published."))
        if item.get("type") in {"turn.failed", "error"}:
            reason = cli_error("codex", line)
            raise FlowError(tr("Codex가 분석 실패를 반환했습니다", "Codex returned an analysis failure") + (
                f": {reason}" if reason else tr(". CLI 인증/계정 상태를 확인하세요.",
                                                ". Check the CLI's login and account status.")))
        if item.get("type") == "turn.completed":
            usage = item.get("usage")
            completed = True
        if item.get("type") == "item.completed":
            payload = item.get("item", {})
            if payload.get("type") == "agent_message":
                final = payload.get("text")
            elif payload.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                raise FlowError(tr("금지된 도구 실행 이벤트가 감지되었습니다. 결과를 게시하지 않습니다.",
                                   "A forbidden tool execution event was detected; the result is not published."))
    if not completed:
        raise FlowError(tr("Codex turn.completed가 없는 부분 출력입니다. 결과를 게시하지 않습니다.",
                           "Partial Codex output without turn.completed; the result is not published."))
    if result_file and result_file.is_file():
        if result_file.stat().st_size > MAX_OUTPUT:
            raise FlowError(tr("Codex 최종 출력 한도 초과", "Codex final output over the limit"))
        final = result_file.read_text(encoding="utf-8")
    try:
        value = json.loads(final or "")
        if not isinstance(value, dict):
            raise ValueError("not object")
    except ValueError as exc:
        raise FlowError(tr("Codex의 최종 구조화 JSON을 읽을 수 없습니다.",
                           "Codex's final structured JSON could not be read.")) from exc
    return value, usage


def parse_claude_output(text: str) -> tuple[dict, dict | None]:
    try:
        envelope = json.loads(text)
        if not isinstance(envelope, dict) or envelope.get("is_error") or envelope.get("permission_denials"):
            raise ValueError("error or denied tool envelope")
        if envelope.get("subtype") not in (None, "success"):
            raise ValueError("non-success result envelope")
        value = envelope.get("structured_output")
        if value is None:
            # Supported older wrappers put the JSON string in result.
            value = json.loads(envelope.get("result", ""))
        if not isinstance(value, dict):
            raise ValueError("not object")
    except (ValueError, TypeError) as exc:
        raise FlowError(tr("Claude의 최종 구조화 JSON을 읽을 수 없습니다.",
                           "Claude's final structured JSON could not be read.")) from exc
    return value, envelope.get("usage")


def claude_reported_model(text: str) -> str | None:
    """The model Claude says produced the answer (the one with the most output tokens)."""
    try:
        usage = json.loads(text).get("modelUsage")
    except (ValueError, AttributeError):
        return None
    if not isinstance(usage, dict) or not usage:
        return None
    return max(usage, key=lambda name: (usage[name] or {}).get("outputTokens", 0)
               if isinstance(usage[name], dict) else 0)


class CLIRunner:
    """CLI adapter; native account authentication is consumed by the CLI, never parsed here.

    Linux bubblewrap mounts a minimal runtime. macOS Seatbelt uses a deny-default
    file profile and a temporary home. Both expose only an individual credential
    file; user config, hooks, logs, projects, plugins and SSH keys are excluded.
    This is defense in depth, not a substitute for live CLI/tool-denial release tests.
    """

    def __init__(self, name: str, *, model: str | None = None, effort: str | None = None,
                 timeout: float = 600):
        # Integration at high effort took 200-300 s in live runs; 600 s still ends a hung call.
        if name not in {"codex", "claude"}:
            raise FlowError(tr("Runner는 codex 또는 claude여야 합니다.", "The Runner must be codex or claude."))
        if effort is not None and effort not in EFFORTS:
            raise FlowError(tr("추론 수준은 " + ", ".join(EFFORTS) + " 중 하나여야 합니다.",
                                 "The reasoning effort must be one of " + ", ".join(EFFORTS) + "."))
        self.name, self.model, self.effort, self.timeout = name, model or DEFAULT_MODELS[name], effort, timeout
        self.adapter_version = ADAPTER_VERSION
        self.calls, self.version, self.last_usage, self.last_model = 0, "not_checked", None, None
        self.executable: str | None = None
        self.ready = False
        self.features: set[str] = set()

    def _credential(self) -> tuple[Path, str]:
        if self.name == "codex":
            home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
            return home / "auth.json", "/pf-home/.codex/auth.json"
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
        return home / ".credentials.json", "/pf-home/.claude/.credentials.json"

    def _require_executable(self) -> str:
        """`assert` is stripped under `python -O`, turning a clear failure into a TypeError."""
        if not self.executable:
            raise FlowError(tr(f"{self.name} CLI 실행 파일을 찾지 못했습니다. PATH를 확인하거나 해당 CLI를 설치하세요.",
                               f"The {self.name} CLI executable was not found. Check PATH or install that CLI."))
        return self.executable

    def _runtime_roots(self) -> list[Path]:
        executable = Path(self._require_executable()).resolve()
        roots: list[Path] = []
        # System runtime roots are already mounted below. Node/nvm native CLI installs
        # require their version directory, not the user's whole home.
        for candidate in [executable, Path(shutil.which("node") or "/usr/bin/node").resolve()]:
            if str(candidate).startswith(("/usr/", "/bin/", "/lib/", "/lib64/")):
                continue
            parts = candidate.parts
            if "versions" in parts and "node" in parts:
                index = parts.index("node", parts.index("versions"))
                roots.append(Path(*parts[:index + 2]))
            elif "node_modules" in parts:
                index = parts.index("node_modules")
                roots.append(Path(*parts[:index + 1]))
            elif self.name == "claude" and ".local" in parts and "claude" in parts:
                roots.append(candidate.parent)
            else:
                # A user supplied executable file is mounted, never its project directory.
                roots.append(candidate)
        return list(dict.fromkeys(roots))

    def _macos_runtime_roots(self) -> list[Path]:
        executable = Path(self._require_executable()).resolve()
        parts = executable.parts
        if "node_modules" in parts:
            index = parts.index("node_modules")
            package_end = index + (3 if parts[index + 1].startswith("@") else 2)
            cli_root = Path(*parts[:package_end])
        else:
            cli_root = executable.parent
        roots = [cli_root]
        if self.name == "codex" and cli_root.name == "codex" and cli_root.parent.name == "@openai":
            arch = {"x86_64": "x64", "arm64": "arm64"}.get(platform.machine())
            if arch:
                native = cli_root.parent / f"codex-darwin-{arch}"
                if native.is_dir() and not native.is_symlink():
                    roots.append(native)
        node = shutil.which("node")
        if node:
            roots.append(Path(node).resolve().parent.parent)
        return list(dict.fromkeys(roots))

    def _macos_profile(self, work: Path, output: Path, home: Path) -> str:
        credential, _ = self._credential()
        system = [Path(path).resolve() for path in ("/System", "/usr/bin", "/usr/lib", "/usr/libexec",
                                                  "/usr/sbin", "/usr/share", "/bin", "/sbin", "/private/etc")]
        readable = [*system, work.resolve(), output.resolve(), home.resolve(), *self._macos_runtime_roots()]
        credential = credential.resolve()
        ancestors = {str(parent) for path in [*readable, credential] for parent in path.parents}
        # macOS exposes /etc, /var and /tmp through symlinks into /private.
        # Directory traversal must be permitted without granting their contents.
        literals = sorted(ancestors | {str(credential), "/etc", "/var", "/tmp",
                                       "/dev", "/dev/null", "/dev/random", "/dev/urandom", "/dev/tty"})
        read_rules = " ".join(f"(literal {json.dumps(path)})" for path in literals)
        read_rules += " " + " ".join(f"(subpath {json.dumps(str(path))})" for path in readable)
        # Codex's native macOS transport needs Apple XPC services for its model
        # connection. Keep third-party Mach services unavailable to the runner.
        mach_rule = '(allow mach-lookup (global-name-regex #"^com\\.apple\\."))' if self.name == "codex" else ""
        # codex-cli 0.157 reads the global preference domain through cfprefsd at startup
        # and aborts when it cannot. Allow only that read and cfprefsd's read-only shared
        # memory; no app-specific domain, preference writes or other shared memory.
        if self.name == "codex":
            mach_rule += ('(allow user-preference-read (preference-domain "kCFPreferencesAnyApplication"))'
                          '(allow ipc-posix-shm-read-data (ipc-posix-name "apple.cfprefs.daemonv1")'
                          f' (ipc-posix-name "apple.cfprefs.{os.getuid()}v1"))')
        return ("(version 1)(deny default)(allow process*)(allow sysctl-read)"
                "(allow network-outbound)" + mach_rule + "(allow file-read* " + read_rules + ")"
                "(allow file-write* (subpath " + json.dumps(str(output.resolve())) + ")"
                " (subpath " + json.dumps(str(home.resolve())) + "))")

    def _macos_home(self, work: Path) -> Path:
        home = work.parent / "home"
        config = home / (".codex" if self.name == "codex" else ".claude")
        config.mkdir(parents=True, mode=0o700, exist_ok=True)
        credential, _ = self._credential()
        link = config / credential.name
        if credential.is_file() and not credential.is_symlink() and not link.exists():
            link.symlink_to(credential.resolve())
        return home.resolve()

    def _sandbox_env(self, work: Path, output: Path) -> dict[str, str] | None:
        if sys.platform != "darwin":
            return None
        home = self._macos_home(work)
        paths = ["/usr/local/bin", "/usr/bin", "/bin", str(Path(self.executable or "").parent)]
        node = shutil.which("node")
        if node:
            paths.append(str(Path(node).resolve().parent))
        return {"PATH": ":".join(dict.fromkeys(paths)), "HOME": str(home), "TMPDIR": str(output.resolve()),
                "LANG": "en_US.UTF-8", "TERM": "dumb", "CODEX_HOME": str(home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "CLAUDE_CODE_TMPDIR": str(output.resolve()),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}

    def sandbox_command(self, work: Path, output: Path, command: list[str]) -> list[str]:
        if sys.platform == "darwin":
            sandbox = shutil.which("sandbox-exec")
            if not sandbox:
                raise FlowError(tr("macOS 실제 AI 분석에는 sandbox-exec가 필요합니다. 격리 없이 실행하지 않습니다.",
                                   "Real AI analysis on macOS requires sandbox-exec; it does not run without isolation."))
            home = self._macos_home(work)
            return [sandbox, "-p", self._macos_profile(work, output, home), *command]
        if sys.platform != "linux":
            raise FlowError(tr("이 운영체제의 실제 AI 격리 실행은 지원하지 않습니다.",
                             "Isolated real AI runs are not supported on this operating system."))
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise FlowError(tr("실제 AI 분석에는 bubblewrap(bwrap)이 필요합니다. 안전하지 않은 실행으로 전환하지 않습니다.",
                               "Real AI analysis requires bubblewrap (bwrap); it does not fall back to an unsafe run."))
        args = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--share-net", "--cap-drop", "ALL"]
        for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
            if Path(directory).exists():
                args += ["--ro-bind", directory, directory]
        for path in self._runtime_roots():
            args += ["--ro-bind", str(path), str(path)]
        for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ld.so.cache", "/etc/ssl/certs"):
            if Path(path).exists():
                args += ["--ro-bind", path, path]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                 "--dir", "/pf-home", "--dir", "/pf-home/.codex", "--dir", "/pf-home/.claude",
                 "--ro-bind", str(work), "/work", "--bind", str(output), "/out"]
        credential, destination = self._credential()
        if credential.is_file() and not credential.is_symlink():
            # Bind a path, do not read/copy authentication bytes. Refresh writes may fail;
            # re-authentication is performed by the original CLI outside this program.
            args += ["--ro-bind", str(credential), destination]
        path_entries = ["/usr/local/bin", "/usr/bin", "/bin", str(Path(self.executable or "").parent)]
        node = shutil.which("node")
        if node:
            path_entries.append(str(Path(node).resolve().parent))
        args += ["--clearenv", "--setenv", "PATH", ":".join(dict.fromkeys(path_entries)),
                 "--setenv", "HOME", "/pf-home", "--setenv", "LANG", "C.UTF-8",
                 "--setenv", "TERM", "dumb", "--setenv", "CODEX_HOME", "/pf-home/.codex",
                 "--setenv", "CLAUDE_CONFIG_DIR", "/pf-home/.claude",
                 "--setenv", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1",
                 "--setenv", "DISABLE_AUTOUPDATER", "1", "--chdir", "/work", "--", *command]
        return args

    def _temporary(self):
        if sys.platform == "darwin":
            return tempfile.TemporaryDirectory(prefix="projectflow-run-", dir="/private/tmp")
        return tempfile.TemporaryDirectory(prefix="projectflow-run-")

    def preflight(self) -> dict:
        executable = shutil.which(self.name)
        if not executable:
            raise FlowError(tr(f"{self.name} CLI가 PATH에 없습니다. 설치·로그인은 해당 CLI에서 진행하세요.",
                               f"The {self.name} CLI is not on PATH. Install it and log in through that CLI."))
        self.executable = str(Path(executable).resolve())
        if sys.platform == "darwin" and not shutil.which("sandbox-exec"):
            raise FlowError(tr("macOS sandbox-exec가 없습니다. 실제 분석은 차단됩니다.",
                               "macOS sandbox-exec is missing; real analysis is blocked."))
        if sys.platform == "linux" and not shutil.which("bwrap"):
            raise FlowError(tr("bubblewrap(bwrap)이 없습니다. 실제 분석은 차단되며 view/export/demo는 사용할 수 있습니다.",
                               "bubblewrap (bwrap) is missing; real analysis is blocked, while view/export/demo still work."))
        if sys.platform not in {"linux", "darwin"}:
            raise FlowError(tr("이 운영체제의 실제 AI 격리 실행은 지원하지 않습니다.",
                             "Isolated real AI runs are not supported on this operating system."))
        with self._temporary() as temp:
            work, output = Path(temp) / "work", Path(temp) / "out"
            work.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            env = self._sandbox_env(work, output)
            cwd = work if sys.platform == "darwin" else None
            if sys.platform == "darwin":
                forbidden = Path(temp) / "outside-sandbox"
                forbidden.write_text("synthetic probe", encoding="utf-8")
                link = self._macos_home(work) / "outside-link"
                link.symlink_to(forbidden)
                for probe in (["/bin/test", "-r", str(forbidden)], ["/usr/bin/touch", str(forbidden)],
                              ["/usr/bin/touch", str(link)]):
                    code, _, _ = execute(self.sandbox_command(work, output, probe),
                                         timeout=15, env=env, cwd=cwd)
                    if code == 0:
                        raise FlowError(tr("macOS 격리가 범위 밖 파일 접근을 차단하지 못했습니다.",
                                           "macOS isolation failed to block file access outside the scope."))
                credential, _ = self._credential()
                if credential.is_file() and not credential.is_symlink():
                    auth_link = self._macos_home(work) / (".codex" if self.name == "codex" else ".claude") / credential.name
                    code, _, _ = execute(self.sandbox_command(work, output,
                                           ["/bin/test", "-r", str(auth_link)]), timeout=15, env=env, cwd=cwd)
                    if code:
                        raise FlowError(tr("macOS 격리에서 CLI 인증 파일을 읽을 수 없습니다.",
                                           "The CLI auth file cannot be read inside macOS isolation."))
            code, text, _ = execute(self.sandbox_command(work, output, [self.executable, "--version"]),
                                    timeout=15, env=env, cwd=cwd)
            if code:
                raise FlowError(tr("격리된 CLI 시작 실패: user namespace/bubblewrap 및 CLI 설치 경로를 확인하세요.",
                                   "The isolated CLI failed to start: check user namespaces/bubblewrap and the CLI's install path."))
            self.version = text.strip()[:120]
            commands = [self.executable, "exec", "--help"] if self.name == "codex" else [self.executable, "--help"]
            code, help_text, _ = execute(self.sandbox_command(work, output, commands),
                                         timeout=15, env=env, cwd=cwd)
            required = (["--output-schema", "--ephemeral", "--sandbox", "--json", "--output-last-message"]
                        if self.name == "codex" else ["--tools", "--strict-mcp-config", "--json-schema",
                            "--no-session-persistence", "--setting-sources", "--settings", "--safe-mode", "--restricted"])
            if self.model:
                required = [*required, "--model"]
            if self.effort and self.name == "claude":
                required = [*required, "--effort"]
            if code or any(flag not in help_text for flag in required):
                raise FlowError(tr(f"{self.name} CLI에 필수 안전/구조화 옵션이 없습니다. 호환 버전을 확인하세요.",
                                   f"The {self.name} CLI lacks the required safety/structured-output options. Check for a compatible version."))
            if self.name == "codex":
                code, feature_text, _ = execute(self.sandbox_command(work, output,
                    [self.executable, "features", "list"]), timeout=15, env=env, cwd=cwd)
                self.features = {line.split()[0] for line in feature_text.splitlines() if line.split()}
                if code or not {"shell_tool", "unified_exec"} <= self.features:
                    raise FlowError(tr("Codex 도구 비활성화 capability를 확인할 수 없습니다.",
                                       "Could not confirm Codex's capability to disable tools."))
        credential, _ = self._credential()
        if not credential.is_file() or credential.is_symlink():
            raise FlowError(tr("파일 기반 CLI 인증을 찾지 못했습니다. keyring 전용 인증은 이 alpha에서 미지원입니다.",
                               "No file-based CLI auth was found. Keyring-only auth is not supported in this alpha."))
        self.ready = True
        return {"runner": self.name, "version": self.version, "adapter": ADAPTER_VERSION,
                "auth": "credential_file_present_not_authenticated_tested",
                "sandbox": "macos-seatbelt" if sys.platform == "darwin" else "bubblewrap",
                "live_model_test": False}

    def build_cli(self, schema: dict, *, work: str = "/work", output: str = "/out") -> list[str]:
        executable = self._require_executable()
        if self.name == "claude":
            command = [executable, "--restricted", "--safe-mode", "--print", "--output-format", "json",
                       "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config",
                       "--mcp-config", f"{work}/mcp.json", "--setting-sources", "",
                       "--settings", f"{work}/settings.json", "--no-session-persistence",
                       "--system-prompt-file", f"{work}/system.md", "--json-schema", dumps(schema)]
        else:
            command = [executable]
            risky = {"shell_tool", "unified_exec", "shell_snapshot", "js_repl", "apply_patch_freeform",
                     "multi_agent", "hooks", "codex_hooks", "remote_plugin", "plugins", "apps", "memories",
                     "skill_mcp_dependency_install", "image_generation", "browser", "goals"}
            for feature in sorted(self.features & risky):
                command += ["--disable", feature]
            for config in ['approval_policy="never"', 'web_search="disabled"', 'notify=[]',
                           'history.persistence="none"', 'project_doc_max_bytes=0', 'hide_agent_reasoning=true',
                           f'model_instructions_file={json.dumps(f"{work}/system.md")}', 'mcp_servers={}', 'shell_environment_policy.inherit="none"']:
                command += ["-c", config]
            command += ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "--json",
                        "--output-schema", f"{work}/schema.json", "--output-last-message", f"{output}/result.json"]
        if self.model:
            command += ["--model", self.model]
        if self.effort:
            command += (["--effort", self.effort] if self.name == "claude" else
                        ["-c", f"model_reasoning_effort={json.dumps(self.effort)}"])
        if self.name == "codex":
            command += ["-"]
        return command

    def run(self, task: dict, schema: dict, cancel: threading.Event) -> dict:
        if not self.ready:
            self.preflight()
        if cancel.is_set():
            raise Cancelled(tr("분석 중단", "Analysis stopped"))
        with self._temporary() as temp:
            work, output = Path(temp) / "work", Path(temp) / "out"
            work.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            (work / "schema.json").write_text(dumps(schema), encoding="utf-8")
            (work / "system.md").write_text(task["system"], encoding="utf-8")
            (work / "mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
            (work / "settings.json").write_text('{"disableAllHooks":true,"enabledPlugins":{},"autoMemoryEnabled":false}', encoding="utf-8")
            command = self.build_cli(schema, work=str(work.resolve()), output=str(output.resolve())) if sys.platform == "darwin" else self.build_cli(schema)
            self.calls += 1
            code, text, _ = execute(self.sandbox_command(work, output, command),
                input_text=dumps({k: v for k, v in task.items() if k != "system"}), timeout=self.timeout, cancel=cancel,
                env=self._sandbox_env(work, output), cwd=work if sys.platform == "darwin" else None)
            if code:
                reason = cli_error(self.name, text)
                raise FlowError(tr(f"{self.name} 실행 실패(exit {code})", f"{self.name} run failed (exit {code})") + (
                    f": {reason}" if reason else tr(". CLI 인증·계정 한도·옵션 호환성을 확인하세요.",
                                                    ". Check the CLI's login, account limits and option compatibility.")))
            if self.name == "codex":
                # Codex exec events do not name the model; the requested one is recorded instead.
                value, usage = parse_codex_output(text, output / "result.json")
                self.last_model = None
            else:
                value, usage = parse_claude_output(text)
                self.last_model = claude_reported_model(text)
            self.last_usage = usage  # None stays unknown, not zero or an estimated bill.
            return value
