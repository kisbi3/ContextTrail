from __future__ import annotations

import copy
import functools
import json
import dataclasses
import os
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .freshness import INDEX_KEY, build_index
from .git_context import Scope, collect_git
from .i18n import tr
from .langsmith_trace import LangSmithTracer
from .model import IMAGE_NOTE, Snapshot, SourceRecord, is_queued_message, is_user_prompt, question_reply
from .routing import ROUTING_VERSION, RunnerPool, TaskValidationError
from .runners.cli_runner import EFFORTS
from .schema import (EDIT_TOOL_NAMES, EvidenceValidator, docs_only, draft_delta, edited_files, record_evidence,
                     refresh_matched, validate_shape)
from .sources import collect_logs
from .sources.local import ScanCache, scan_cache_key
from .store import Store, in_journal
from .util import BrokenOutput, Cancelled, FlowError, InputBudgetExceeded, digest, dumps, ident, input_tokens


@dataclass
class AnalysisConfig:
    codex_home: Path | None = None
    claude_home: Path | None = None
    opencode_home: Path | None = None
    history_limit: int = 50
    unit_chars: int = 60_000
    record_chars: int = 48_000
    task_chars: int = 240_000
    unit_records: int = 200
    context_events: int = 24
    runner_name: str | None = None
    base_model: str | None = None
    # The story call's model and effort (`--extract-model`, `--extract-effort`); the runner's default model when unset.
    extract_model: str | None = None
    # medium since 2026-09-27.
    extract_effort: str = "medium"
    extract_workers: int = 1
    max_calls: int = 30
    # Work units for this run (never saved). Unset, the call cap alone bounds a run.
    max_units: int | None = None
    calls_fixed: bool = False  # max_calls was given for this run, so a unit count does not raise it
    # A session (with its sub-agents) analysed ahead of the oldest-first order.
    session: str | None = None
    # The N most recent sessions without notes, and/or those that ended after `since` (a UTC ISO time; the
    # command line resolves `7d` or a date to it), with their sub-agents: the same out-of-order run as
    # `session`, for several sessions at once (docs/plans/RECENT_SESSIONS_FIRST.md). Per run, never saved.
    recent: int | None = None
    since: str | None = None
    # Who started this run: None for a person, "hook" for the opt-in automatic analysis. Per run, never saved.
    trigger: str | None = None
    # Re-read the sessions an agent wrote notes in and add only what the notes and earlier analyses did not
    # record (docs/plans/LIVE_JOURNAL.md §11). Per run, never saved; the semantic review is not run.
    audit: bool = False
    # Send again a unit that was skipped after a failure of its own even though its records and the
    # analysis settings are what they were. Per run, never saved.
    retry_failed: bool = False
    # Titles and summaries are written in this language; None: detected once per project and saved.
    output_language: str | None = None
    langsmith_enabled: bool = False
    langsmith_include_content: bool = False
    langsmith_project: str | None = None

    def validate(self) -> None:
        if not 1 <= self.extract_workers <= 8:
            raise FlowError(tr("extract_workers는 1~8이어야 합니다. 처음에는 1~2를 권장합니다.",
                               "extract_workers must be between 1 and 8; 1 or 2 is recommended at first."))
        if self.max_calls < 1:
            raise FlowError(tr("max_calls는 양수여야 합니다.", "max_calls must be a positive number."))
        if self.max_units is not None and self.max_units < 1:
            raise FlowError(tr("처리할 작업 단위 수는 1 이상이어야 합니다.", "The number of work units to process must be at least 1."))
        if self.recent is not None and self.recent < 1:
            raise FlowError(tr("--recent는 1 이상이어야 합니다.", "--recent must be at least 1."))
        if self.since is not None and _record_timestamp(self.since) is None:
            raise FlowError(tr(f"--since 시각을 읽을 수 없습니다: {self.since!r}", f"Cannot read the --since time: {self.since!r}"))
        if self.recent is not None or self.since is not None:
            if self.session:
                raise FlowError(tr("--session과 --recent/--since는 함께 쓸 수 없습니다: 세션을 직접 고르거나 최근 세션을 고르세요.",
                                   "--session cannot be combined with --recent/--since: name the session or take the recent ones."))
            if self.audit:
                raise FlowError(tr("--audit은 note가 있는 세션을 읽고 --recent/--since는 note가 없는 세션을 고릅니다. 감사할 세션은 --session으로 고르세요.",
                                   "--audit reads the sessions that have notes, and --recent/--since pick sessions without them; "
                                   "choose the session to audit with --session."))
            if self.trigger == "hook":
                raise FlowError(tr("자동 갱신은 오래된 것부터 처리합니다: --recent/--since를 쓸 수 없습니다.",
                                   "Automatic analysis goes oldest first: --recent/--since cannot be used."))
        if self.extract_model is not None and (not self.extract_model.strip() or any(ord(c) < 32 for c in self.extract_model)):
            raise FlowError(tr("모델 식별자는 비어 있거나 제어 문자를 포함할 수 없습니다.",
                               "A model identifier cannot be empty or contain control characters."))
        if self.extract_effort not in EFFORTS:
            raise FlowError(tr("추론 수준은 " + ", ".join(EFFORTS) + " 중 하나여야 합니다.",
                             "The reasoning effort must be one of " + ", ".join(EFFORTS) + "."))
        if self.langsmith_include_content and not self.langsmith_enabled:
            raise FlowError(tr("모델 입력·출력 추적에는 --langsmith도 필요합니다.",
                               "Tracing model inputs and outputs also requires --langsmith."))
        if self.langsmith_project is not None and not self.langsmith_enabled:
            raise FlowError(tr("LangSmith 프로젝트 이름을 지정하려면 --langsmith도 필요합니다.",
                               "Naming a LangSmith project also requires --langsmith."))
        if self.langsmith_project is not None and (
                not self.langsmith_project.strip() or any(ord(c) < 32 for c in self.langsmith_project)):
            raise FlowError(tr("LangSmith 프로젝트 이름이 비어 있거나 제어 문자를 포함합니다.",
                               "The LangSmith project name is empty or contains control characters."))
        if min(self.history_limit, self.unit_chars, self.record_chars, self.task_chars, self.unit_records) <= 0:
            raise FlowError(tr("입력 예산은 양수여야 합니다.", "Input budgets must be positive."))
        if self.audit and self.trigger == "hook":
            raise FlowError(tr("자동 갱신은 감사(--audit)를 하지 않습니다.", "Automatic updates never run an audit (--audit)."))
        if self.record_chars > self.unit_chars or self.unit_chars >= self.task_chars:
            raise FlowError(tr("record_chars ≤ unit_chars < task_chars 조건이 필요합니다.",
                               "record_chars ≤ unit_chars < task_chars is required."))


# The language titles and summaries are written in: one per project, so a graph does not mix
# languages when some sessions were in English (a CLI's built-in prompts) and others not.
DEFAULT_LANGUAGE = "English"
_SCRIPTS = (("Korean", re.compile(r"[\uac00-\ud7a3]")), ("Japanese", re.compile(r"[\u3040-\u30ff]")),
            ("Chinese", re.compile(r"[\u4e00-\u9fff]")), ("Russian", re.compile(r"[\u0400-\u04ff]")),
            ("Arabic", re.compile(r"[\u0600-\u06ff]")), ("Hebrew", re.compile(r"[\u0590-\u05ff]")),
            ("Thai", re.compile(r"[\u0e00-\u0e7f]")), ("Hindi", re.compile(r"[\u0900-\u097f]")),
            ("Greek", re.compile(r"[\u0370-\u03ff]")))
# Latin letters alone cannot tell English from Spanish: the system locale decides, else English.
_LATIN_LOCALES = {"es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian",
                  "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "vi": "Vietnamese", "id": "Indonesian",
                  "sv": "Swedish", "da": "Danish", "nb": "Norwegian", "fi": "Finnish", "cs": "Czech"}


def detect_language(records: list[SourceRecord], locale: str | None = None) -> str:
    """The language most of the person's own messages are written in (a vote per message)."""
    votes: Counter = Counter()
    for record in records:
        if not is_user_prompt(record):
            continue
        # A few characters of a script count: a Korean request is often mostly code and paths.
        script = next((name for name, pattern in _SCRIPTS if len(pattern.findall(record.content)) >= 2), None)
        if script:
            votes[script] += 1
        elif re.search(r"[A-Za-z]{3,}", record.content):
            code = (locale or "").split("_")[0].split(".")[0].lower()
            votes[_LATIN_LOCALES.get(code, DEFAULT_LANGUAGE)] += 1
    if votes:
        return votes.most_common(1)[0][0]
    # No message of the person's yet: the locale's language, for this plan only (`Engine.resolve_language` saves
    # nothing it did not read from a message).
    code = (locale or "").split("_")[0].split(".")[0].lower()
    return _LOCALE_LANGUAGES.get(code) or _LATIN_LOCALES.get(code, DEFAULT_LANGUAGE)


_LOCALE_LANGUAGES = {"ko": "Korean", "ja": "Japanese", "zh": "Chinese", "ru": "Russian", "en": "English"}


def prompt(name: str) -> str:
    return files("contexttrail").joinpath("prompts", name + ".md").read_text(encoding="utf-8")


def build_task(stage: str, data: dict, language: str | None = None) -> dict:
    """One source of truth for the Runner request and the local input preview. The story call is the only stage; an
    audit adds its own rules after the story's, so an ordinary request is the same bytes as before."""
    if stage != "story":
        raise FlowError(f"unknown analysis stage: {stage}")
    system = prompt("story") + ("\n\n" + prompt("audit") if data.get("journal_audit") else "")
    return {"system": system, "stage": stage, "instructions": "", "data": data,
            "output_language": language or DEFAULT_LANGUAGE,
            "wire_contract": "Use the IDs shown in the input (B1 blocks, Q1 requests, K1 work items, R1 runs, "
                             "E3 events and open requests, your own N1 for new story events). Every field of the schema is present; why and turn_summary may be null."}


def _trace_operation(enabled: bool, name: str, inputs: dict,
                     operation: Callable[[], Any], *, run_type: str = "chain") -> Any:
    """Expose the operation actually executed without tracing normal or metadata-only CLI runs."""
    if not enabled:
        return operation()
    from langsmith import traceable

    @traceable(name=name, run_type=run_type)
    def traced(details: dict) -> Any:
        return operation()

    return traced(inputs)


def _evidence_ids(graph: dict) -> list[str]:
    """Include the evidence explaining invalidations/resolutions, not just active claims."""
    result: list[str] = []
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_ids" and isinstance(item, list):
                    result.extend(i for i in item if isinstance(i, str))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(graph)
    return list(dict.fromkeys(result))


def _incomplete_input(issues: list[str]) -> bool:
    """Unknown diagnostics are conservative partial, not implicit success.

    The Git scope notices describe selected scope, and the re-analysis notices say why
    a unit was sent again; none of them is data loss.
    Keep this single classification path consistent for first run and no-op.
    """
    informational = (
        r"Not a Git repository, so there is no Git evidence\.",
        r"Git input is the selected commits and the tracked staged/unstaged diff\. Untracked file content is not read\.",
        r"Git history is limited to the latest \d+ commits per worktree\.",
        r"Merge commit [0-9a-f]+ is read as its diff against the first parent only\.",
        r"re-analyzing an already integrated work unit: .+",
        r"corrected the processed ledger of \d+ already integrated records \(not resent\)\.",
        r"audit: .+",
        # A unit skipped for a failure of its own (validation after repair, input over budget) and held back.
        r"skipped unit \w+: .+",
        r"skipped units held back: \d+\. .+",
    )
    return any(not any(re.fullmatch(pattern, message) for pattern in informational) for message in issues)


CONTEXT_POLICY_VERSION = "relevance_v2"
# `lean`: earlier work reaches the model as graph (events, edges, quotes) and a few records, not as
# whole records. Cut finely, a piece's neighbours are already in the graph.
LEAN_CITED_LINES, LEAN_PREVIOUS_RECORDS, LEAN_CONTEXT_EVENTS = 3, 2, 6
# An audit unit (`analyze --audit`) is stored under this prefix, so the ordinary plan leaves it alone, and
# shows the model up to AUDIT_NOTES note events of the unit's records in full, past the context event limit.
AUDIT_PREFIX, AUDIT_NOTES = "audit_", 40
# Lean mode's budget for context-only text and its cap on nearby records of the other source type.
LEAN_CONTEXT_CHARS, LEAN_CROSS_RECORDS = 8_000, 3
LEAN_INDEX_RECORDS, LEAN_INDEX_EVENTS, LEAN_INDEX_FILES = 100, 30, 50  # the event index is by relevance; it must not grow with the graph
# Repair hints: a few provided lines per failed quote, ranked by how close they are to it.
# What the extraction input carries besides the records: short IDs, a tool-step outline
# and the user's messages. Part of the cache signature, so older extractions are redone.
EXTRACT_INPUT_VERSION = "aliases_steps_requests_read_heads_v2"
# What earlier scans made of the transcript files that gave this project nothing (`ScanCache`).
SCAN_CACHE_META = "scan_cache"

# IDs a model must copy back exactly; they go to the Runner as S12 / E3 and come back whole.
ALIASED_ID = re.compile(r"(src|srcseg|ev|edge|open|file)_[0-9a-f]{24}")
ALIAS_LETTER = {"src": "S", "srcseg": "S", "ev": "E", "edge": "L", "open": "O", "file": "F"}


class IdAliases:
    """Short stand-ins for the 24-hex IDs a model has to copy (S12 for src_…, E3 for ev_…).

    Only whole string values are replaced, so source text that mentions an ID is sent as
    it is, and the reply is mapped back before anything checks it. The numbering holds
    across one task's read and repair rounds.
    """

    def __init__(self) -> None:
        self.short: dict[str, str] = {}
        self.full: dict[str, str] = {}
        self._counts: Counter = Counter()

    def shorten(self, value: Any) -> Any:
        if isinstance(value, str):
            if not ALIASED_ID.fullmatch(value):
                return value
            if value not in self.short:
                letter = ALIAS_LETTER[value.split("_", 1)[0]]
                self._counts[letter] += 1
                self.short[value] = alias = f"{letter}{self._counts[letter]}"
                self.full[alias] = value
            return self.short[value]
        if isinstance(value, list):
            return [self.shorten(item) for item in value]
        if isinstance(value, dict):
            return {key: self.shorten(item) for key, item in value.items()}
        return value

    def shorten_text(self, text: str) -> str:
        """IDs inside host-written text, such as a validation error quoted to the repair round."""
        return ALIASED_ID.sub(lambda match: self.short.get(match.group(0), match.group(0)), text)

    def expand(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.full.get(value, value)
        if isinstance(value, list):
            return [self.expand(item) for item in value]
        if isinstance(value, dict):
            return {key: self.expand(item) for key, item in value.items()}
        return value

    def wire(self, task: dict) -> dict:
        """The task exactly as the Runner receives it."""
        sent = self.shorten(task)
        if isinstance(sent.get("repair"), dict):
            sent["repair"]["instruction"] = self.shorten_text(sent["repair"]["instruction"])
        return sent


EDIT_TOOLS = EDIT_TOOL_NAMES
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "web_search", "web__run", "view_image",
              # opencode's tools are lowercase
              "read", "glob", "grep", "list", "webfetch", "websearch", "codesearch", "skill"}
# Handing work to another agent and waiting for it: neither a run nor an outcome of its own.
DELEGATE_TOOLS = {"spawn_agent", "wait_agent", "send_message", "followup_task", "list_agents", "close_agent",
                  "resume_agent", "Agent", "Task", "SendMessage", "TaskStop", "task"}
# Of those, the calls that hand another agent a task (start one, or give a running one a new task): each is a work
# item of its own (`light.delegation_items`), joined at publish to the work of the agent it started
# (`schema.link_delegations`). The rest only wait, message, list or stop.
DELEGATION_CALLS = {"spawn_agent", "followup_task", "Agent", "Task", "task"}
# Codex names its sub-agents by path from the root session's (`/root/plots`), as `agent_path` in their lineage.
CODEX_ROOT_PATH = "/root"
_TASK_PATH = re.compile(r'"task_name"\s*:\s*"([^"]+)"')
# Codex's exec tool runs JavaScript that calls these; they only read or look things up.
READ_INNER_TOOLS = {"web__run", "view_image", "clock__curr_time"}
READ_COMMANDS = re.compile(r"^\s*(cat|sed -n|grep|rg|ls|head|tail|find|wc|pwd|git (status|diff|log|show))\b")
# Anything in a command that can write, which makes it a run even if it starts by reading.
WRITE_MARKERS = re.compile(r">|\bsed -i\b|\btee\b|write_text|apply_patch|\b(mv|cp|rm|touch|mkdir|chmod)\b")


def _tool_target(name: str, body: str, cwd: str | None) -> str:
    """The file or command a tool call is about, in one short line."""
    def relative(path: str) -> str:
        return path[len(cwd):].lstrip("/") if cwd and path.startswith(cwd.rstrip("/") + "/") else path
    # A patch inside a tool's string argument keeps its escapes: stop at "\n" or a quote.
    files = re.findall(r"\*\*\* (?:Update|Add|Delete) File: ([^\s\\\"]+)", body)
    if files:
        return "patch " + ", ".join(dict.fromkeys(relative(f) for f in files))
    for key in ("file_path", "filePath", "notebook_path", "path"):
        found = re.search(rf'"{key}":\s*"([^"]+)"', body)
        if found:
            return relative(found.group(1))
    return _command_text(body)[:140]


def _command_text(body: str, newline: str = " ") -> str:
    """The whole command of a shell-like tool call on one line, or the body when there is none."""
    command = re.search(r'"command":\s*"((?:[^"\\]|\\.)*)"', body) or re.search(r'\bcmd"?\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    text = command.group(1).replace("\\n", newline).replace('\\"', '"') if command else body
    return " ".join(text.split())


_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_SHELL_C = re.compile(r"^(?:bash|sh|zsh)\s+-l?c\s+[\"']?")
_SHELL_FILE = re.compile(r"^(?:bash|sh|zsh)\s+(?=[^-\s])")
_HEREDOC = re.compile(r"<<-?\s*[\"']?\w+")
_HEREDOC_TAG = re.compile(r"<<-?\s*([\"']?)(\w+)\1")


def drop_heredoc_bodies(text: str) -> str:
    """The command line without the data of its heredocs: `cat > t.py <<'PY' … PY; pytest t.py` keeps
    `cat > t.py` and `pytest t.py` (the body's words are data, never commands). A heredoc with no terminator
    takes the rest of the line."""
    out, position = [], 0
    while (found := _HEREDOC_TAG.search(text, position)) is not None:
        out.append(text[position:found.start()])
        end = re.compile(rf"(?:^|[;\n])\s*{re.escape(found.group(2))}\s*(?=$|[;\n])").search(text, found.end())
        if end is None:
            return "".join(out)
        out.append(";")
        position = end.end()
    return "".join(out) + text[position:]
_INTERPRETER = re.compile(r"^(?:\S*/)?python3?\s+-m\s+")
_ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_WRAPPERS = re.compile(
    r"^(?:(?:uv|poetry|pipenv|pdm|hatch) run\s+|npx\s+|bunx\s+|pnpm exec\s+|time\s+|nohup\s+|sudo\s+|"
    r"xargs(?:\s+-\S+)*\s+|timeout(?:\s+-\S+)*\s+\d\S*\s+|"
    r"docker(?:\s+compose|-compose)?\s+(?:run|exec)(?:\s+-\S+)*\s+\S+\s+)")
_GIT = r"git(?:\s+-C\s+\S+|\s+-c\s+\S+)*\s+"
_GIT_CALL = re.compile(rf"^{_GIT}(?P<sub>[a-z][a-z-]*)(?P<rest>.*)$")
_VCS_SUBCOMMANDS = {"push", "pull", "fetch", "checkout", "switch", "merge", "rebase", "stash", "reset",
                    "cherry-pick", "tag", "branch", "restore", "clean", "add", "rm", "mv", "worktree", "revert"}
TEST_COMMAND = re.compile(
    r"^(?:pytest|py\.test|unittest|build|tox|nox|ruff|mypy|pyright|flake8|pylint|jest|vitest|tsc|eslint|playwright|"
    r"rspec|phpunit|scripts/test\.sh|\./gradlew(?:\s+-\S+)*\s+(?:test|check|build)|"
    r"gradle(?:\s+-\S+)*\s+(?:test|check|build)|mvn(?:\s+-\S+)*\s+(?:test|verify|package)|"
    r"cargo\s+(?:\+\S+\s+)?(?:test|check|clippy|build)|go\s+(?:test|vet|build)|"
    r"(?:swift|dotnet)\s+(?:test|build)|xcodebuild\b|make(?:\s+-\S+)*\s+(?:test|check|lint|build)|"
    r"node\s+--test|deno\s+test|bazel\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|build|typecheck|check)|npm\s+t)(?![\w-])")


def _vcs_mutates(sub: str, rest: str) -> bool:
    """False for a listing or a dry run of a git command that otherwise changes state."""
    words = rest.split()
    flags = {word for word in words if word.startswith("-")}
    if sub in {"add", "clean", "fetch", "rm", "mv", "push", "pull"} and flags & {"-n", "--dry-run"}:
        return False
    if sub in {"stash", "worktree"}:
        return not (words and words[0] in {"list", "show"})
    if sub in {"branch", "tag"}:
        # A name creates, or a flag changes; sorting or formatting options alone still list.
        names = {flag.split("=", 1)[0] for flag in flags}
        listing = {"-l", "--list", "-r", "--remotes", "--show-current", "-v", "-vv", "--contains",
                   "--no-contains", "--merged", "--no-merged", "--points-at"}
        listing |= {"-a", "--all"} if sub == "branch" else set()
        if names & listing or (sub == "tag" and any(re.fullmatch(r"-n\d*", flag) for flag in flags)):
            return False
        changing = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force",
                    "-u", "--set-upstream-to", "--unset-upstream", "--edit-description", "-s", "--sign",
                    "-a", "--annotate", "-F", "--file"}
        return bool(names & changing) or any(not word.startswith("-") for word in words)
    return True


def _command_segments(text: str) -> list[str]:
    """Each command of a compound line without env assignments, wrappers or interpreter prefix."""
    segments = []
    for raw in _SEGMENT_SPLIT.split(drop_heredoc_bodies(text)):
        segment = raw.strip().lstrip("(").strip()
        for _ in range(4):
            segment = _SHELL_FILE.sub("", _SHELL_C.sub("", segment))
            segment = _ENV_PREFIX.sub("", segment)
            segment = _WRAPPERS.sub("", segment)
            segment = _INTERPRETER.sub("", segment)
        # A path to the executable (.venv/bin/pytest) is the executable.
        first, _, rest = segment.partition(" ")
        if "/" in first and not first.startswith(("scripts/", "./gradlew")) and "bin/" in first:
            segment = f"{first.rsplit('/', 1)[-1]} {rest}".strip()
        if segment:
            segments.append(segment)
    return segments


def command_kind(text: str) -> str | None:
    """commit, test or vcs when a command in the line is one; commit outranks test outranks vcs."""
    segments = _command_segments(text)
    calls = [found for found in map(_GIT_CALL.match, segments) if found]
    if any(call["sub"] == "commit" for call in calls):
        return "commit"
    if any(TEST_COMMAND.match(segment) for segment in segments):
        return "test"
    if any(call["sub"] in _VCS_SUBCOMMANDS and _vcs_mutates(call["sub"], call["rest"]) for call in calls):
        return "vcs"
    return None


def step_hint(record: SourceRecord) -> tuple[str, str, str]:
    """(tool name, target, hint) of one tool call: edit / read / commit / test / vcs / run."""
    head, _, body = record.content.partition("\n")
    name = head.removeprefix("Tool: ").strip() or "unknown"
    target = _tool_target(name, body, record.cwd)
    kind = command_kind(_command_text(body, "; ")) if name not in EDIT_TOOLS | READ_TOOLS else None
    inner = set(re.findall(r"tools\.(\w+)", body)) if name == "exec" and "*** Begin Patch" not in body else set()
    if name in EDIT_TOOLS or "*** Begin Patch" in body:
        hint = "edit"
    elif name in DELEGATE_TOOLS:
        hint = "delegate"
    elif inner == {"write_stdin"}:
        hint = "poll"
    elif inner and inner <= READ_INNER_TOOLS:
        hint = "read"
    elif kind:
        hint = kind
    elif name in READ_TOOLS or (READ_COMMANDS.match(target) and not WRITE_MARKERS.search(target)):
        hint = "read"
    else:
        hint = "run"
    return name, target, hint


def classify_steps(records: list[SourceRecord]) -> dict:
    """How the tool calls in these records were classified by code, counts only (no content).

    `run` is what the code could not place; `ambiguous_run_commands` counts its distinct command
    lines, the part a later model pass could resolve. The share tells whether that pass is worth it.
    """
    hints: dict[str, int] = {}
    run_tools: dict[str, int] = {}
    commands: set[str] = set()
    for record in records:
        if record.role != "tool_call":
            continue
        name, _, hint = step_hint(record)
        hints[hint] = hints.get(hint, 0) + 1
        if hint == "run":
            run_tools[name] = run_tools.get(name, 0) + 1
            commands.add(digest(_command_text(record.content.partition("\n")[2], "; ")))
    calls = sum(hints.values())
    top = sorted(run_tools.items(), key=lambda item: (-item[1], item[0]))[:8]
    return {"tool_calls": calls, "hints": dict(sorted(hints.items())), "run_by_tool": dict(top),
            "ambiguous_run_commands": len(commands),
            "ambiguous_run_share": round(hints.get("run", 0) / calls, 3) if calls else 0.0}


def _failed_result(text: str) -> tuple[str, bool]:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first, first.startswith(("<tool_use_error>", "Error:", "error:"))


def tool_steps(assigned: list[SourceRecord]) -> list[dict]:
    """An outline of the unit's tool calls: what each did (edit / read / run / delegate / poll) and its result.

    Hints come from the tool name, patch markers and the command line only; the records stay the
    evidence. edit / read / run, and run is refined to commit, test (tests, lint, build) or vcs.
    """
    results = {r.tool_call_id: r for r in assigned if r.role == "tool_result" and r.tool_call_id}
    steps = []
    for record in assigned:
        if record.role != "tool_call":
            continue
        name, target, hint = step_hint(record)
        result = results.get(record.tool_call_id) if record.tool_call_id else None
        first, failed = _failed_result(result.content) if result else ("", False)
        step = {"call": record.source_id, "result": result.source_id if result else None,
                "tool": name, "hint": hint, "target": target, "result_head": first[:120], "failed": failed}
        if hint == "edit" and docs_only(edited_files(record)):
            step["doc"] = True
        steps.append(step)
    return steps


def _arguments(call: SourceRecord) -> dict:
    """A tool call's arguments when its body is one JSON object, else {}."""
    try:
        found = json.loads(call.content.partition("\n")[2])
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def delegated_path(call: SourceRecord, result: SourceRecord | None) -> str | None:
    """The path of the Codex sub-agent a delegation call gave its task to (`/root/plots`, what the sub-agent's
    lineage carries as `agent_path`): the path the spawn's result names, else the task name or target under the
    caller's own path. None for the other tools, whose sub-agents name the call itself (`parent_tool_call_id`)."""
    name = call.content.partition("\n")[0].removeprefix("Tool: ").strip()
    if call.provider != "codex" or name not in ("spawn_agent", "followup_task"):
        return None
    if name == "spawn_agent" and result is not None and (found := _TASK_PATH.search(result.content)):
        return found.group(1)
    target = _arguments(call).get("task_name" if name == "spawn_agent" else "target")
    if not isinstance(target, str) or not target.strip():
        return None
    if target.startswith("/"):
        return target.strip()
    own = (call.lineage or {}).get("agent_path") or CODEX_ROOT_PATH
    return f"{own.rstrip('/')}/{target.strip()}"


def delegated_to(call: SourceRecord, result: SourceRecord | None) -> str | None:
    """Whom a delegation call handed its task, as a person would call it: a Codex agent's task name (the last part
    of its path), else the sub-agent type the call asked for (Claude Code `Agent`/`Task`, opencode `task`)."""
    path = delegated_path(call, result)
    if path:
        return path.rstrip("/").rpartition("/")[2] or None
    kind = _arguments(call).get("subagent_type")
    return kind.strip() if isinstance(kind, str) and kind.strip() else None


def mark_steps(records: list[SourceRecord]) -> None:
    """Set `SourceRecord.step` on every tool call and its result: what `tool_steps` makes of the call. Run at
    scan, per session, so the flow can tell a check or a documentation edit from the records an event cites
    (`schema.fill_steps`) instead of from its title. A delegation also names whom it gave the task to (`to`, for
    the views) and, for Codex, that agent's path (`agent`), which `schema.link_delegations` matches to its lineage."""
    sessions: dict[str | None, list[SourceRecord]] = {}
    for record in records:
        if record.role in ("tool_call", "tool_result"):
            sessions.setdefault(record.session_id, []).append(record)
    by_id = {record.source_id: record for record in records}
    for session in sessions.values():
        for step in tool_steps(session):
            mark = {"hint": step["hint"], **({"doc": True} if step.get("doc") else {})}
            if step["hint"] == "delegate" and step["tool"] in DELEGATION_CALLS:
                call, result = by_id[step["call"]], by_id.get(step["result"])
                path, name = delegated_path(call, result), delegated_to(call, result)
                mark.update({"agent": path} if path else {}, **({"to": name} if name else {}))
            for key in (step["call"], step["result"]):
                if key in by_id:
                    by_id[key].step = mark


# A long tool result, injected instruction or summary is shown as its head and tail; the lines
# between are listed as omitted and cannot be cited. What the person typed, the assistant's words
# and tool calls (patches included) are always shown whole.
VIEW_HEAD_CHARS, VIEW_TAIL_CHARS = 2_000, 1_000
# A record line the model is shown is cut here (a minified file or a one-line JSON result otherwise
# slips past the head/tail view whole); what is cut is readable by line range, and a quote from the
# shown part is still an exact substring of the line.
LINE_CHARS = 1_500
LINE_TAIL_CHARS = 400  # of those, kept from the end of the line
# A record that is only context (the turn before, a commit near in time) is shown head and tail, whatever
# its role: one 39,000-character commit record was going into four units whole as "nearby" context.
CONTEXT_HEAD_CHARS, CONTEXT_TAIL_CHARS = 1_500, 500
# The lines around a cited quote are shown up to this many characters; past it only the cited lines go.
CITED_BLOCK_CHARS = 2_000
# A saved quote is the whole cited lines (`EvidenceValidator` canonicalizes partial quotes, keeping the
# cited part as `focus`); the model is shown the cited part, or the head of the lines, within this.
SHOWN_QUOTE_CHARS = 300
# What a read-only call (a file read, a listing, a search) returned is rarely an event of its own:
# a shorter head and tail, the rest on request.
READ_HEAD_CHARS, READ_TAIL_CHARS = 400, 200


def _view(record: SourceRecord, head_chars: int = VIEW_HEAD_CHARS,
          tail_chars: int = VIEW_TAIL_CHARS, *, force: bool = False) -> tuple[int, int] | None:
    """The last head line and first tail line to show, or None to show the record whole.

    Assistant text, tool calls and the person's messages go whole unless `force`, which the
    context-only view uses: a record that is not the unit's own is background, head and tail."""
    if len(record.content) <= head_chars + tail_chars or (not force and (
            record.role in {"assistant", "tool_call"} or (record.role == "user" and is_user_prompt(record)))):
        return None
    lines = record.content.splitlines()
    head, used = 0, 0
    while head < len(lines) and (head == 0 or used + len(lines[head]) <= head_chars):
        used += len(lines[head]) + 1
        head += 1
    tail, used = len(lines) + 1, 0
    while tail - 1 > head + 1 and (tail == len(lines) + 1 or used + len(lines[tail - 2]) <= tail_chars):
        used += len(lines[tail - 2]) + 1
        tail -= 1
    # Without a tail the end of the output (exit status, summary) would be hidden: send it whole.
    return (head, tail) if tail - head > 1 and tail <= len(lines) else None


def shown_line(text: str) -> str:
    """A record line as the model sees it: whole up to LINE_CHARS, else its head and its tail with a note between.
    The tail matters: Codex Desktop writes a whole command on one line, and what it ran comes last (a heredoc that
    writes a test file, then the `pytest` that runs it)."""
    if len(text) <= LINE_CHARS:
        return text
    head = LINE_CHARS - LINE_TAIL_CHARS
    return (text[:head] + f" [cut: {len(text) - LINE_CHARS} characters here; a quote must come from a part shown] "
            + text[-LINE_TAIL_CHARS:])


def shown_quote(quote: str, focus: list[list[int]] | None) -> str:
    """The part of a canonical quote the model is shown: the longest cited piece, else the head of the lines."""
    if len(quote) <= SHOWN_QUOTE_CHARS:
        return quote
    pieces = sorted((quote[a:b] for a, b in (focus or []) if 0 <= a < b <= len(quote)), key=len, reverse=True)
    piece = pieces[0] if pieces else quote[:SHOWN_QUOTE_CHARS]
    # A cut that ends on a line break would ask for the next line too (the checks read it as a run past its end).
    return piece[:SHOWN_QUOTE_CHARS].rstrip("\n") or piece[:SHOWN_QUOTE_CHARS]


def unit_cost(record: SourceRecord) -> int:
    """Characters this record puts into a unit's input."""
    view = _view(record)
    lines = record.content.splitlines()
    if view is None:
        return sum(min(len(line), LINE_CHARS + 90) + 1 for line in lines)
    return sum(min(len(line), LINE_CHARS + 90) + 1 for line in lines[:view[0]] + lines[view[1] - 1:]) + 120


# Record fields the model never needs: hashes and native IDs identify a record for the code (which reads them
# off the cited record itself), and the folder and worktree are the project's. Event provenance is derived from
# the citations (`event_provenance_from_evidence`). About 40% of a shown record on a Codex-heavy project.
HIDDEN_RECORD_FIELDS = ("locator", "content_hash", "source_version", "native_record_id", "parent_record_id",
                        "worktree_id", "cwd", "authoring", "fork", "step")


def record_for_model(record: SourceRecord) -> dict:
    """A record's metadata as the model sees it: what it can cite or reason with, without empty or default values."""
    result = {key: value for key, value in record.metadata().items() if key not in HIDDEN_RECORD_FIELDS}
    for key, default in (("derivation", "original"), ("git", {}), ("lineage", {}), ("tool_call_id", None),
                         ("session_id", None), ("recorded_at", None)):
        if result.get(key) == default:
            result.pop(key)
    return result


def _shown(record: SourceRecord, harness: "Harness", *, read: bool = False, context: bool = False) -> dict:
    view = (_view(record, READ_HEAD_CHARS, READ_TAIL_CHARS) if read else
            _view(record, CONTEXT_HEAD_CHARS, CONTEXT_TAIL_CHARS, force=True) if context else _view(record))
    if view is None:
        return harness.provide(record.source_id)
    head, tail = view
    total = len(record.content.splitlines())
    shown = harness.provide(record.source_id, 1, head)
    if tail <= total:
        shown["lines"] += harness.provide(record.source_id, tail, total)["lines"]
    shown["omitted"] = {"start_line": head + 1, "end_line": tail - 1,
                        "chars": len("\n".join(record.content.splitlines()[head:tail - 1]))}
    return shown


def model_context(context: dict) -> dict:
    """The context the model receives: the selection audit stays with us (ledger, preview, digest)."""
    return {key: value for key, value in context.items() if key != "context_selection"}


def extract_request_data(unit: dict, snapshot_id: str, assigned: list[SourceRecord], harness: "Harness",
                         context: dict, issues: list[str]) -> tuple[dict, dict[str, tuple[str, str | None]]]:
    """The extraction input, and the file edits its answer has to cite. Shared with the preview."""
    steps = tool_steps(assigned)
    reads = {step["result"] for step in steps if step["hint"] == "read" and step["result"]}
    records = [{**_shown(r, harness, read=r.source_id in reads), "context_only": False} for r in assigned]
    requests = [{"source_id": r.source_id, "recorded_at": r.recorded_at, "text": _one_line(r.content, 200)}
                for r in assigned if is_user_prompt(r)]
    data = {"unit_id": unit["id"], "snapshot_id": snapshot_id, "new_records": records,
            "assigned_source_ids": unit["sources"], "user_requests": requests,
            "tool_steps": [{key: value for key, value in step.items() if key != "failed"} for step in steps],
            "input_limitations": issues, **model_context(context)}
    return data, _required_edits(steps)


def _required_edits(steps: list[dict]) -> dict[str, tuple[str, str | None]]:
    """Tool calls that certainly changed a file; each must back some event."""
    return {step["call"]: (f"{step['tool']} {step['target']}", step["result"])
            for step in steps if step["hint"] == "edit" and not step["failed"]}


# A unit takes one story call, at worst a repair and a retry of an unreadable answer: a run bounded by units gets
# this many calls per unit unless a cap was given.
CALLS_PER_UNIT = 3
# A unit is not started with fewer calls left than it needs at the least: its story call (none when its saved
# extraction is still valid).
MIN_UNIT_CALLS = 1
PLAN_CHOICES = (5, 15, 30)
# Estimate for the story call of a work unit: it reads what was written and the work outline, not the tool output,
# so about a quarter of the estimate from the unit's characters (measured 0.20-0.26 on two projects, 2026-10-20).
# Actual usage depends on the input and the configured model.
_FIXED_TOKENS, _TYPICAL_RATIO, _TOKENS_PER_MINUTE = 30_000, 0.25, 15_000


def call_cap(config: "AnalysisConfig", limit: int | None) -> int:
    """A run bounded by units gets calls for them, unless a cap was given for this run."""
    return config.max_calls if config.calls_fixed or not limit else CALLS_PER_UNIT * limit


TRANSCRIPT_PROVIDERS = ("codex", "claude", "opencode")


def _transcript_sessions(records: list[SourceRecord]) -> set[str]:
    return {r.session_id for r in records if r.session_id and r.provider in TRANSCRIPT_PROVIDERS}


def _with_sub_agents(family: set[str], records: list[SourceRecord]) -> set[str]:
    """The sessions, and the sub-agent sessions they started (at any depth)."""
    parents = {r.session_id: r.lineage.get("parent_session_id") for r in records
               if r.session_id and r.lineage.get("parent_session_id")}
    family = set(family)
    while True:
        more = {child for child, parent in parents.items() if parent in family} - family
        if not more:
            return family
        family |= more


def session_family(records: list[SourceRecord], wanted: str) -> set[str]:
    """The session a prefix names, and the sub-agent sessions it started (at any depth)."""
    found = sorted(s for s in _transcript_sessions(records) if s.startswith(wanted))
    if not found:
        raise FlowError(tr(f"이 프로젝트 범위의 기록에 세션 {wanted}가 없습니다.",
                           f"No session {wanted} in the records of this project scope."))
    if len(found) > 1 and wanted not in found:
        raise FlowError(tr(f"세션 {wanted}에 해당하는 세션이 {len(found)}개입니다. 더 길게 지정하세요.",
                           f"{len(found)} sessions match {wanted}; give a longer prefix."))
    return _with_sub_agents({wanted if wanted in found else found[0]}, records)


_SINCE_AGE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$")


def since_cutoff(text: str, now_: datetime | None = None) -> str:
    """`--since` as a UTC time: an age back from now (`36h`, `7d`, `2w`), or a date or time (`2026-10-01`; UTC
    when it names no zone)."""
    found = _SINCE_AGE.match(text or "")
    if found:
        seconds = int(found.group(1)) * {"h": 3600, "d": 86400, "w": 7 * 86400}[found.group(2)]
        moment = (now_ or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    else:
        try:
            parsed = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
        except ValueError:
            raise FlowError(tr(f"--since는 기간(36h, 7d, 2w)이나 날짜(2026-10-01)여야 합니다: {text!r}",
                               f"--since is an age (36h, 7d, 2w) or a date (2026-10-01): {text!r}")) from None
        moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SELECTION_LISTED = 5   # sessions a plan names; the rest are counted


def recent_sessions(records: list[SourceRecord], *, recent: int | None, since: str | None,
                    noted: set[str]) -> dict:
    """The most recent sessions of the project, each with its sub-agents (docs/plans/RECENT_SESSIONS_FIRST.md).

    A session is a transcript session that no other session of the project started; a sub-agent follows its
    parent, and a session whose parent is not in the project counts as one of its own. A session is as recent
    as the latest record of the session and its sub-agents. Sessions with notes are not candidates (the agent
    writes those; they would only use up the count). Selected: those that ended at or after `since`, then
    the latest `recent` of them. Returned oldest first: the order they are analysed in."""
    sessions = _transcript_sessions(records)
    # A Claude Code sub-agent's records keep its parent's session ID and name that same session as parent.
    parent = {r.session_id: r.lineage.get("parent_session_id") for r in records
              if r.session_id in sessions and r.lineage.get("parent_session_id") not in (None, r.session_id)}
    tops = sorted(s for s in sessions if parent.get(s) not in sessions)
    last: dict[str, float] = {}
    for record in records:
        stamp = _record_timestamp(record.recorded_at)
        if record.session_id in sessions and stamp is not None:
            last[record.session_id] = max(stamp, last.get(record.session_id, stamp))
    families = {top: _with_sub_agents({top}, records) for top in tops}
    noted_tops = [top for top in tops if top in noted]
    found = [(max((last[s] for s in families[top] if s in last), default=None), top)
             for top in tops if top not in noted]
    cutoff = _record_timestamp(since) if since else None
    if cutoff is not None:
        found = [(at, top) for at, top in found if at is not None and at >= cutoff]
    found.sort(key=lambda item: (item[0] is not None, item[0] or 0.0, item[1]))
    candidates = len(tops) - len(noted_tops)
    if recent:
        found = found[-recent:]
    if not found:
        raise FlowError(tr(f"고를 최근 세션이 없습니다 (note가 없는 세션 {candidates}개"
                           f"{f', 제외한 note 세션 {len(noted_tops)}개' if noted_tops else ''}"
                           f"{f', {since} 이후 끝난 세션 없음' if cutoff is not None else ''}).",
                           f"No recent session to take ({candidates} sessions without notes"
                           f"{f', {len(noted_tops)} noted sessions left out' if noted_tops else ''}"
                           f"{f', none ended since {since}' if cutoff is not None else ''})."))
    chosen = [{"id": top, "last": (datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                   if at is not None else None), "family": families[top]} for at, top in found]
    return {"kind": "recent", "family": set().union(*(item["family"] for item in chosen)), "sessions": chosen,
            "candidates": candidates, "noted": len(noted_tops), "recent": recent, "since": since}


def _estimated_tokens(chars: int) -> int:
    return int(chars * 1.4 / 2.4) + _FIXED_TOKENS


# One or two units say little: a unit that needed a repair costs twice another.
MIN_PAST_UNITS = 3


def calibration(units: list[dict], calls: list[dict], pool: dict[str, SourceRecord]) -> dict | None:
    """How past integrated units of this project compare with the estimate: tokens and time.

    Only units read with the story call whose every call reported its input tokens count (a unit of the old
    extract and integrate calls cost about ten times as much); None until MIN_PAST_UNITS do.
    """
    by_unit: dict[str, list[dict]] = {}
    for call in calls:
        by_unit.setdefault(call["unit_id"], []).append(call)
    estimate = actual = duration = counted = 0
    for unit in units:
        rows = by_unit.get(unit["id"])
        if (unit["status"] != "integrated" or unit["id"].startswith(AUDIT_PREFIX) or not rows
                or not all(i in pool for i in unit["sources"])):
            continue
        tokens = [input_tokens(row["details"].get("usage")) for row in rows]
        if any(t is None for t in tokens) or any(row["stage"] != "story" for row in rows):
            continue
        estimate += _estimated_tokens(sum(unit_cost(pool[i]) for i in unit["sources"]))
        actual += sum(tokens)
        duration += sum(row["details"].get("duration_ms") or 0 for row in rows)
        counted += 1
    if counted < MIN_PAST_UNITS or not estimate:
        return None
    return {"units": counted, "token_ratio": actual / estimate,
            "tokens_per_minute": actual / (duration / 60_000) if duration else None}


def plan_summary(units: list[dict], pool: dict[str, SourceRecord], max_calls: int, *,
                 limit: int | None = None, past: dict | None = None) -> dict:
    """What a run is about to send, for the choice before it: units, calls, tokens and time.

    `max_calls` is the run's cap (`call_cap`). Estimated from characters, or scaled by this
    project's past runs (`past`, from `calibration`).
    `choices` gives the same figures for a few unit counts to pick from.
    """
    ratio = past["token_ratio"] if past else _TYPICAL_RATIO
    per_minute = (past or {}).get("tokens_per_minute") or _TOKENS_PER_MINUTE
    tokens = [int(_estimated_tokens(sum(unit_cost(pool[i]) for i in unit["sources"] if i in pool)) * ratio)
              for unit in units]
    def figures(count: int) -> dict:
        total = sum(tokens[:count])
        return {"units": count, "input_tokens": total, "minutes": max(1, round(total / per_minute)) if count else 0}
    reach = min(len(units), limit if limit else max(1, max_calls))
    counts = sorted({n for n in PLAN_CHOICES if n < len(units)} |
                    ({len(units)} if 0 < len(units) <= max(PLAN_CHOICES) else set()))
    return {"units": len(units), "units_this_run": reach,
            "max_calls": max_calls,
            "input_tokens_this_run": figures(reach)["input_tokens"], "minutes_this_run": figures(reach)["minutes"],
            "basis": "past_runs" if past else "estimate", "past_units": (past or {}).get("units", 0),
            "choices": [figures(n) for n in counts]}


def _tokens(value: int) -> str:
    """Tens of thousands in Korean (12만), thousands in English (120k); smaller counts as they are."""
    return tr(f"{value / 10_000:,.0f}만", f"{value / 1_000:,.0f}k") if value >= 10_000 else f"{value:,}"


def plan_text(plan: dict) -> str:
    if not plan["units"]:
        return tr("대기 작업 단위 없음 · 보낼 것이 없습니다", "No pending work units · nothing to send")
    basis = (tr(f"지난 {plan['past_units']}개 단위 기준", f"based on the last {plan['past_units']} units")
             if plan.get("basis") == "past_runs" else tr("추정", "estimate"))
    language = (tr(f" · 출력 언어 {plan['output_language']}", f" · output language {plan['output_language']}")
                if plan.get("output_language") else "")
    tokens, minutes = _tokens(plan['input_tokens_this_run']), plan.get('minutes_this_run', 0)
    audit = (tr(f"감사: note가 있는 {plan.get('sessions', 0)}개 세션을 다시 읽어 빠진 것만 더함 · ",
                f"Audit: re-reads {plan.get('sessions', 0)} noted session(s) and adds only what is missing · ")
             if plan.get("audit") else "")
    return audit + selection_text(plan.get("selection")) + tr(f"대기 {plan['units']:,}개 단위 · 이번 실행 {plan['units_this_run']:,}개 "
              f"(AI 호출 ≤{plan['max_calls']}) · 입력 약 {tokens} 토큰 · 약 {minutes}분({basis}){language}",
              f"{plan['units']:,} units pending · {plan['units_this_run']:,} this run "
              f"(AI calls ≤{plan['max_calls']}) · input ≈{tokens} tokens · ≈{minutes} min ({basis}){language}")


def selection_plan_fields(selection: dict | None, units: list[dict], pool: dict[str, SourceRecord]) -> dict:
    """What a plan says about the recent sessions a run takes first: which, how many units each, what is left out."""
    if not selection or selection["kind"] != "recent":
        return {}
    def count(family: set[str]) -> int:
        return sum(1 for unit in units if pool[unit["sources"][0]].session_id in family)
    return {"selection": {"recent": selection["recent"], "since": selection["since"],
                          "candidates": selection["candidates"], "noted": selection["noted"],
                          "sessions": [{"id": item["id"], "last": item["last"], "units": count(item["family"])}
                                       for item in selection["sessions"]]}}


def selection_text(selection: dict | None) -> str:
    """The line a consent shows when a run takes the most recent sessions first; empty for an ordinary run."""
    if not selection:
        return ""
    sessions = selection["sessions"]
    days = sorted(item["last"][:10] for item in sessions if item["last"])
    span = (days[0] if days[0] == days[-1] else f"{days[0]} ~ {days[-1]}") if days else "?"
    named = ", ".join(item["id"][:8] for item in sessions[-SELECTION_LISTED:])
    names = (("… " if len(sessions) > SELECTION_LISTED else "") + named)
    noted = selection["noted"]
    return tr(f"최근 세션 먼저: {len(sessions)}개 세션({span}; {names})의 단위만"
              f"{f' · note 세션 {noted}개 제외' if noted else ''} · 나머지 세션은 나중의 일반 실행(오래된 것부터)이 채움 · ",
              f"Recent sessions first: only the units of {len(sessions)} sessions ({span}; {names})"
              f"{f' · {noted} noted sessions left out' if noted else ''} · the other sessions wait for a later ordinary run (oldest first) · ")


def plan_choices_text(plan: dict) -> str:
    """The unit counts to choose from, with their tokens and time, on one line."""
    return " · ".join(tr(f"{c['units']:,}개 ≈{_tokens(c['input_tokens'])} 토큰 {c['minutes']}분",
                         f"{c['units']:,} units ≈{_tokens(c['input_tokens'])} tokens {c['minutes']} min")
                     for c in plan["choices"])


SKIP_REASONS = {"validation": "the answer still failed validation after the repair round",
                "input_budget": "the request is over the input budget and nothing is left to trim"}


def skipped_unit_message(unit_id: str, kind: str) -> str:
    return f"skipped unit {unit_id}: {SKIP_REASONS[kind]}; it is held back until its records or the analysis settings change."


def held_back_message(count: int) -> str:
    return (f"skipped units held back: {count}. They are sent again when their records or the analysis settings "
            f"change, or with --retry-failed.")


def audit_plan_fields(units: list[dict], pool: dict[str, SourceRecord]) -> dict:
    """What an audit plan says besides the figures of any plan: that it is one, and how many sessions it reads."""
    return {"audit": True, "sessions": len({pool[i].session_id for unit in units for i in unit["sources"] if i in pool})}


def audit_notes(graph: dict, saved: dict[str, dict], source_ids: set[str]) -> tuple[list[str], int]:
    """The note events that cite these records (oldest first, the latest AUDIT_NOTES), and how many were left out."""
    cited = [event["id"] for event in graph["events"] if event.get("origin") == "note"
             and any((saved.get(i) or {}).get("source_id") in source_ids for i in event.get("evidence_ids", []))]
    return cited[-AUDIT_NOTES:], max(0, len(cited) - AUDIT_NOTES)


def uncited_edits(required: dict[str, tuple[str, str | None]], saved: dict[str, dict]) -> dict[str, tuple[str, str | None]]:
    """The file edits an audit still has to see cited: those no event of the graph cites yet."""
    cited = {item["source_id"] for item in saved.values()}
    return {call: pair for call, pair in required.items() if call not in cited and pair[1] not in cited}


def match_noted_steps(output: dict, graph: dict, saved: dict[str, dict], note_ids: set[str],
                      edit_calls: frozenset[str] = frozenset()) -> int:
    """In an audit, a candidate that records a step a note recorded: the same kind, sharing a cited record with exactly
    one of the note events (a note cites the record holding its quote, code cites the whole step), and citing no edit
    (`edit_calls`) that note leaves out (settling it would leave that edit in no event). Each becomes an
    `existing_event_matches` entry, so the draft settles it as that note's duplicate and its relations land on the
    note. Returns how many."""
    notes = [(event, frozenset(saved[i]["source_id"] for i in event.get("evidence_ids", []) if i in saved))
             for event in graph["events"] if event["id"] in note_ids]
    matched = {item["candidate_id"] for item in output["existing_event_matches"]}
    added = 0
    for candidate in output["event_candidates"]:
        if candidate["id"] in matched or not candidate["evidence"]:
            continue
        cited = {c["source_id"] for c in candidate["evidence"]}
        same = [event for event, records in notes if event["kind"] == candidate["kind"] and records & cited
                and cited & edit_calls <= records]
        if len(same) == 1:
            output["existing_event_matches"].append({
                "candidate_id": candidate["id"], "existing_event_id": same[0]["id"],
                "reason": "audit: the same kind as this note event, citing a record it cites",
                "evidence": copy.deepcopy(candidate["evidence"])})
            added += 1
    return added


def match_existing_duplicates(output: dict, graph: dict, saved: dict[str, dict]) -> int:
    """Candidates that repeat an event already in the graph: the same kind and exactly the same cited records (a
    unit read again after one of its records changed, or a step a note recorded). Each becomes an
    `existing_event_matches` entry when exactly one event fits, so the draft settles it as that event's duplicate
    and its relations follow it there. Returns how many."""
    def records(evidence: list[dict]) -> frozenset:
        return frozenset(c["source_id"] for c in evidence)
    existing: dict[tuple[str, frozenset], list[str]] = {}
    for event in graph["events"]:
        cited = records([saved[i] for i in event.get("evidence_ids", []) if i in saved])
        if cited:
            existing.setdefault((event["kind"], cited), []).append(event["id"])
    # The person's own events by the message they cite: a message one already records (a note's decision, say) is
    # not made a request a second time.
    theirs: dict[str, set[str]] = {}
    for event in graph["events"]:
        if event.get("actor") == "user":
            for evidence_id in event.get("evidence_ids", []):
                if evidence_id in saved:
                    theirs.setdefault(saved[evidence_id]["source_id"], set()).add(event["id"])
    matched = {item["candidate_id"] for item in output["existing_event_matches"]}
    added = 0
    for candidate in output["event_candidates"]:
        if candidate["id"] in matched or not candidate["evidence"]:
            continue
        same, reason = existing.get((candidate["kind"], records(candidate["evidence"])), []), \
            "same kind and the same cited records as this event"
        if not same and candidate["kind"] == "question" and candidate.get("actor") == "user":
            same = sorted(set().union(*(theirs.get(r, set()) for r in records(candidate["evidence"]))))
            reason = "the person's message an event of theirs already records"
        if len(same) != 1:
            continue
        output["existing_event_matches"].append({"candidate_id": candidate["id"], "existing_event_id": same[0],
                                                 "reason": reason, "evidence": copy.deepcopy(candidate["evidence"])})
        added += 1
        kind = next(event["kind"] for event in graph["events"] if event["id"] == same[0])
        if candidate["kind"] == "question" and kind != "question":
            # Only a request is answered: the answers that would now leave the person's decision are not kept.
            dropped = [edge for edge in output["edge_candidates"]
                       if edge["from_event_id"] == candidate["id"] and edge["relation"] == "answers"]
            if dropped:
                output["edge_candidates"] = [edge for edge in output["edge_candidates"] if edge not in dropped]
                output["limitations"].append(f"story: answers of {candidate['id']} not kept: the person's message is "
                                             f"recorded as a {kind} event, which nothing answers.")
    return added


def audit_guard(delta: dict, graph: dict) -> None:
    """An audit only adds. It never changes what the notes or earlier analyses recorded."""
    problems = [f"{key} must be empty" for key in ("events_to_update", "edges_to_invalidate", "open_items_to_resolve")
                if delta.get(key)]
    existing = {item["id"] for item in graph["open_items"]}
    if any(item.get("id") in existing for item in delta.get("open_items_to_upsert", [])):
        problems.append("open_items_to_upsert may only add new open items")
    if problems:
        raise FlowError("audit run adds events, relations and open items and changes nothing that exists: "
                        + "; ".join(problems) + ". Add the missing event instead and leave the existing one as it is.")


def _one_line(text: str, limit: int = 240) -> str:
    # A request titled by its words, not by the image note Claude Code puts before an attached image.
    text = " ".join(IMAGE_NOTE.sub("", text).split()) or " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def link_request_turns(graph: dict, base: dict, pool: dict[str, SourceRecord], unit_sources: list[str],
                       evidence: dict[str, dict], saved: Callable[[list[str]], dict[str, dict]],
                       run_id: str) -> tuple[dict, dict[str, dict]]:
    """Every user message gets an event, and each new event hangs from the message it was made under.

    A turn link says only that, in the same session, this work came after this message and
    before the next one: the dialog's structure. It is stored as follows/structural, never
    as a cause, and only for a new event that no other relation reaches.
    Returns the graph and the evidence the step cited.
    """
    graph = copy.deepcopy(graph)
    known = dict(evidence)
    missing = [i for e in graph["events"] for i in e["evidence_ids"] if i not in known]
    if missing:
        known.update(saved(list(dict.fromkeys(missing))))
    added: dict[str, dict] = {}
    position = {source_id: n for n, source_id in enumerate(pool)}
    prompts = [r for r in pool.values() if is_user_prompt(r)]

    def sources(event: dict) -> list[str]:
        return [known[i]["source_id"] for i in event["evidence_ids"] if i in known]

    nodes: dict[str, tuple[dict, str]] = {}  # prompt source → (user event, the citing evidence ID)
    for event in graph["events"]:
        if event.get("actor") != "user":
            continue
        for evidence_id in event["evidence_ids"]:
            source_id = known.get(evidence_id, {}).get("source_id")
            if source_id in pool and is_user_prompt(pool[source_id]):
                current = nodes.get(source_id)
                if current is None or (event["kind"] == "question" and current[0]["kind"] != "question"):
                    nodes[source_id] = (event, evidence_id)
    requests_added = 0
    assigned = set(unit_sources)
    for record in prompts:
        if record.source_id not in assigned or record.source_id in nodes:
            continue
        item = record_evidence(record)
        added[item["id"]] = known[item["id"]] = item
        event = {"id": ident("ev_", "user_request", record.source_id), "kind": "question",
                 "title": _one_line(record.content), "summary": record.content.strip()[:4000],
                 "actor": "user", "status": "asked", "basis": "explicit_statement",
                 "session_ids": [record.session_id] if record.session_id else [],
                 "worktree_ids": [record.worktree_id] if record.worktree_id else [],
                 "recorded_at": record.recorded_at, "occurred_at": None, "evidence_ids": [item["id"]],
                 "created_in_run": run_id, "updated_in_run": run_id}
        # Keep the list in recorded order, which is the order every view draws.
        later = next((n for n, other in enumerate(graph["events"])
                      if record.recorded_at and (other.get("recorded_at") or "") > record.recorded_at), None)
        graph["events"].insert(len(graph["events"]) if later is None else later, event)
        nodes[record.source_id] = (event, item["id"])
        requests_added += 1
    # The person's answer to the agent's question is a choice only when it is one of the options the agent offered;
    # then it is the person's decision. A free answer stays in the records and the story (no event of its own).
    cited_by_user = {source_id for event in graph["events"] if event.get("actor") == "user" for source_id in sources(event)}
    ordered = list(pool.values())
    for record in ordered:
        replies = question_reply(record) if record.source_id in assigned else None
        if not replies or record.source_id in cited_by_user:
            continue
        offered = offered_options(record, ordered)
        chosen = [reply for reply in replies if reply["answer"].strip() in offered.get(reply["question"].strip(), ())]
        if not chosen:
            continue
        item = record_evidence(record)
        added[item["id"]] = known[item["id"]] = item
        event = {"id": ident("ev_", "user_choice", record.source_id), "kind": "decision",
                 "title": "; ".join(reply["answer"].strip() for reply in chosen)[:240],
                 "summary": "\n".join(f"{reply['question'].strip()} → {reply['answer'].strip()}" for reply in chosen)[:4000],
                 "actor": "user", "status": "adopted", "basis": "explicit_statement",
                 "session_ids": [record.session_id] if record.session_id else [],
                 "worktree_ids": [record.worktree_id] if record.worktree_id else [],
                 "recorded_at": record.recorded_at, "occurred_at": None, "evidence_ids": [item["id"]],
                 "created_in_run": run_id, "updated_in_run": run_id}
        later = next((n for n, other in enumerate(graph["events"])
                      if record.recorded_at and (other.get("recorded_at") or "") > record.recorded_at), None)
        graph["events"].insert(len(graph["events"]) if later is None else later, event)
        requests_added += 1
    # A message sent through the tool's queue ends the turn before it; its own turn has no request to hang work on.
    by_session: dict[str | None, list[SourceRecord]] = {}
    for record in pool.values():
        if is_user_prompt(record) or is_queued_message(record):
            by_session.setdefault(record.session_id, []).append(record)
    reached = {edge["to_event_id"] for edge in graph["edges"] if edge["active"]}
    old = {event["id"] for event in base["events"]}
    links = 0
    for event in list(graph["events"]):
        cited = [source_id for source_id in sources(event) if source_id in position]
        # The person's choice in answer to the agent hangs in the turn of the request it continues.
        reply = bool(cited) and all(question_reply(pool[source_id]) is not None for source_id in cited)
        if event["id"] in old or event["id"] in reached or (event.get("actor") == "user" and not reply):
            continue
        if not cited:
            continue
        first = min(cited, key=position.get)
        before = [r for r in by_session.get(pool[first].session_id, []) if position[r.source_id] <= position[first]]
        rationale = "work done after this user message in the same session (dialog structure, not a causal claim)"
        spawned_by = (pool[first].lineage or {}).get("parent_session_id")
        stamp = _record_timestamp(pool[first].recorded_at)
        if not before and spawned_by and stamp is not None:
            # A sub-agent has no messages of its own from the person: its work belongs to the turn
            # of its parent session in which it ran, i.e. the parent's last message before it.
            before = [r for r in by_session.get(spawned_by, [])
                      if (_record_timestamp(r.recorded_at) or float("inf")) <= stamp]
            rationale = ("work of a sub-agent the parent session spawned after this user message "
                         "(dialog structure, not a causal claim)")
        if not before or before[-1].source_id not in nodes:
            continue
        node, evidence_id = nodes[before[-1].source_id]
        if node["id"] == event["id"]:
            continue
        graph["edges"].append({"id": ident("edge_", "turn", node["id"], event["id"]),
                               "from_event_id": node["id"], "to_event_id": event["id"],
                               "relation": "follows", "basis": "structural", "evidence_ids": [evidence_id],
                               "rationale": rationale, "active": True, "origin": "dialog_turn"})
        links += 1
    graph["request_turn_audit"] = {"requests_added": requests_added, "turn_links": links}
    return graph, added


def offered_options(reply: SourceRecord, ordered: list[SourceRecord]) -> dict[str, tuple[str, ...]]:
    """The options the agent's last question before this reply offered, by question title (Codex
    `request_user_input`: `{"questions": [{"title", "options": [...]}]}`), in the reply's session."""
    found: dict[str, tuple[str, ...]] = {}
    for record in ordered:
        if record.source_id == reply.source_id:
            break
        if record.role != "tool_call" or record.session_id != reply.session_id:
            continue
        head, _, body = record.content.partition("\n")
        if not head.startswith("Tool: request_user_input"):
            continue
        try:
            questions = json.loads(body).get("questions") or []
        except (ValueError, AttributeError):
            continue
        found = {str(q.get("title") or "").strip(): tuple(str(o).strip() for o in q.get("options") or [])
                 for q in questions if isinstance(q, dict)}
    return found


def _record_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


# The model input is larger than the records a unit owns (line numbers, metadata, context). Until the
# assembled payload is measured at plan time this is a proxy per context mode: full was calibrated on
# one real unit (28.7k of records became a 270k payload), lean on zero-AI previews of the two eval
# fixtures (payload ≈ 1.3 × records + 32k), with margin for the capped context a later unit carries.
PAYLOAD_SHARE = 0.6
PAYLOAD_FACTOR, PAYLOAD_OVERHEAD, PAYLOAD_FIXED = 1.5, 500, 30_000
MIN_FILL = 0.4
RESULT_CUT_HINTS = {"run", "test", "commit", "vcs"}


def payload_budget(config: AnalysisConfig) -> int:
    return int(config.task_chars * PAYLOAD_SHARE)


def estimated_payload(record_chars: int, records: int) -> int:
    factor, overhead, fixed = PAYLOAD_FACTOR, PAYLOAD_OVERHEAD, PAYLOAD_FIXED
    return int(record_chars * factor) + records * overhead + fixed


def _session_unit_chunks(records: list[SourceRecord], config: AnalysisConfig,
                         issues: list[str]) -> list[list[SourceRecord]]:
    """Keep sessions together and choose stable, human-readable cut points."""
    sessions: dict[tuple[str, str | None, str | None], list[tuple[int, SourceRecord]]] = {}
    for index, record in enumerate(records):
        sessions.setdefault((record.provider, record.session_id, record.worktree_id), []).append((index, record))

    chunks: list[tuple[int, list[SourceRecord]]] = []
    for entries in sessions.values():
        ordered = [record for _, record in entries]
        last_link: dict[tuple[str, str], int] = {}
        for index, record in enumerate(ordered):
            for kind, value in (("tool", record.tool_call_id),
                                ("fragment", record.lineage.get("fragment_of"))):
                if value:
                    last_link[(kind, str(value))] = index

        call_hint = {r.tool_call_id: step_hint(r)[2] for r in ordered if r.role == "tool_call" and r.tool_call_id}
        costs = [unit_cost(r) for r in ordered]
        safe, natural, turns, results, commits = set(), set(), set(), set(), set()
        open_until = -1
        for index, record in enumerate(ordered):
            for kind, value in (("tool", record.tool_call_id),
                                ("fragment", record.lineage.get("fragment_of"))):
                if value:
                    open_until = max(open_until, last_link[(kind, str(value))])
            boundary = index + 1
            if boundary == len(ordered) or open_until >= boundary:
                continue
            safe.add(boundary)
            following = ordered[boundary]
            before_time = _record_timestamp(record.recorded_at)
            after_time = _record_timestamp(following.recorded_at)
            gap = after_time - before_time if before_time is not None and after_time is not None else None
            if following.lineage.get("kind") == "compaction" or (gap is not None and gap >= 3 * 3600):
                natural.add(boundary)
            elif (gap is not None and gap >= 3600
                  and datetime.fromtimestamp(before_time, timezone.utc).date()
                  != datetime.fromtimestamp(after_time, timezone.utc).date()):
                natural.add(boundary)
            if is_user_prompt(following):
                turns.add(boundary)
            hint = call_hint.get(record.tool_call_id) if record.role == "tool_result" else None
            if hint in RESULT_CUT_HINTS:
                # After the result: edit, run and result stay together, so `verifies` need not cross pieces.
                results.add(boundary)
                if hint == "commit" and not _failed_result(record.content)[1]:
                    commits.add(boundary)

        start = 0
        while start < len(ordered):
            count, chars, limit = 0, 0, start
            while (limit < len(ordered) and count < config.unit_records
                   and chars + costs[limit] <= config.unit_chars
                   and estimated_payload(chars + costs[limit], count + 1) <= payload_budget(config)):
                chars += costs[limit]
                count += 1
                limit += 1
            if limit == start:  # Caller already filters records above record_chars.
                limit = start + 1
            natural_cuts = [cut for cut in natural if start < cut <= limit]
            if natural_cuts:
                end = min(natural_cuts)
            elif limit == len(ordered):
                end = limit
            else:
                candidates = [cut for cut in safe if start < cut <= limit]
                floor = MIN_FILL * sum(costs[start:limit])
                # Meaning first: after a commit, before a person's message, after a run result. A cut
                # that would leave a sliver behind is not worth it; then the last safe cut is used.
                for group in (commits, turns, results):
                    picked = [cut for cut in candidates if cut in group and sum(costs[start:cut]) >= floor]
                    if picked:
                        end = max(picked)
                        break
                else:
                    end = max(candidates, default=limit)
                if end not in safe:
                    warning = "the work unit size limit split a linked tool call and result or the fragments of one record."
                    if warning not in issues:
                        issues.append(warning)
            chunks.append((entries[start][0], ordered[start:end]))
            start = end
    return [chunk for _, chunk in sorted(chunks, key=lambda item: item[0])]


def _rehydrate(pool: dict[str, SourceRecord], provided: dict[str, list[tuple[int, int]]],
               evidence: dict[str, dict]) -> None:
    """Recover only retained ranges, with the original source hash. Gaps are never readable."""
    grouped: dict[str, list[dict]] = {}
    for item in evidence.values():
        grouped.setdefault(item["source_id"], []).append(item)
    for source_id, items in grouped.items():
        if source_id in pool:
            continue
        maximum = max(i["end_line"] for i in items)
        if maximum > 100_000:
            continue
        lines = [""] * maximum
        for item in items:
            lines[item["start_line"] - 1:item["end_line"]] = item["quote"].splitlines()
        source = items[0]["source"]
        pool[source_id] = SourceRecord(source_id, source["provider"], source.get("session_id"),
            source["role"], "\n".join(lines), {**source["locator"], "preserved_only": True},
            native_record_id=source.get("native_record_id"), parent_record_id=source.get("parent_record_id"),
            recorded_at=source.get("recorded_at"), cwd=source.get("cwd"), worktree_id=source.get("worktree_id"),
            tool_call_id=source.get("tool_call_id"), derivation=source.get("derivation", "original"),
            git=source.get("git", {}), lineage=source.get("lineage", {}), authoring=source.get("authoring", {}),
            fork=source.get("fork", {}), step=source.get("step", {}), pinned_hash=items[0]["content_hash"])
        provided[source_id] = [(i["start_line"], i["end_line"]) for i in items]


def _prompt_evidence(evidence: dict[str, dict], context_only: list[dict] | None = None,
                     quoted: set[tuple[str, int, int]] | None = None) -> dict[str, dict]:
    """Send citation substance without repeating filesystem/source metadata per quote.

    A quote whose lines are all in `context_only`, or whose lines a candidate in the same request
    already cites (`quoted`), is marked rather than repeated; the rest is shown as `shown_quote`."""
    visible_lines: dict[str, set[int]] = {}
    for item in context_only or []:
        visible_lines.setdefault(item["source_id"], set()).update(line["line"] for line in item["lines"])
    result = {}
    for evidence_id, item in evidence.items():
        source = item["source"]
        compact = {key: item[key] for key in ("id", "source_id", "start_line", "end_line")}
        # Only what says something: a missing value or the default derivation is left out.
        compact["source"] = {key: source.get(key) for key in
                             ("provider", "session_id", "role", "recorded_at", "worktree_id", "derivation")
                             if source.get(key) not in (None, "", "original")}
        lineage = source.get("lineage") or {}
        if isinstance(lineage, dict) and lineage.get("kind"):
            compact["source"]["lineage_kind"] = lineage["kind"]
        if all(line in visible_lines.get(item["source_id"], set())
               for line in range(item["start_line"], item["end_line"] + 1)):
            compact["quote_in_context_only"] = True
        elif quoted and (item["source_id"], item["start_line"], item["end_line"]) in quoted:
            compact["quote_in_candidates"] = True
        else:
            compact["quote"] = shown_quote(item["quote"], item.get("focus"))
            if len(compact["quote"]) < len(item["quote"]):
                compact["quote_is_part_of_lines"] = True
        result[evidence_id] = compact
    return result


class Harness:
    def __init__(self, runner: Any, records: dict[str, SourceRecord], graph: dict, store: Store,
                 config: AnalysisConfig, cancel: threading.Event, run_id: str = "run_unknown", unit_id: str = "unit_unknown",
                 budget: Any = None, routing_role: str | None = None, routing_reasons: list[str] | None = None,
                 tracer: LangSmithTracer | None = None, detailed_trace: bool = False,
                 review_capture: Callable[[dict], None] | None = None):
        self.runner, self.graph, self.store, self.config, self.cancel = runner, graph, store, config, cancel
        self.run_id, self.unit_id = run_id, unit_id
        self.budget, self.routing_role = budget, routing_role
        self.tracer = tracer
        self.detailed_trace = detailed_trace
        self.review_capture = review_capture
        self.routing_reasons = routing_reasons or []
        self.selection_audit: dict = {}
        self.pool = dict(records)
        self.provided: dict[str, list[tuple[int, int]]] = {}
        self.dependencies: dict[str, str] = {}
        self.saved = store.evidence_many(_evidence_ids(graph))
        self.file_manifest: dict[str, tuple[str, str, str]] = {}
        self.file_source_ids: dict[str, str] = {}
        # File IDs are derived ONLY from host-collected, pinned Git diffs.
        for record in records.values():
            locator = record.locator
            if record.provider != "git" or locator.get("kind") != "commit_diff":
                continue
            for filename in re.findall(r"^\+\+\+ b/(.+)$", record.content, re.M):
                path = PurePosixPath(filename)
                if path.is_absolute() or ".." in path.parts or any(c in filename for c in '\n\r\x00"'):
                    continue
                file_id = ident("file_", locator["commit"], filename)
                self.file_manifest[file_id] = (locator["root"], locator["commit"], filename)
                self.file_source_ids[file_id] = record.source_id
        self.allowed_event_ids: set[str] = set()
        self.allowed_record_ids: set[str] = set()
        self.allowed_file_ids: set[str] = set()
        # Events the context shows whatever its limit (an audit shows every note of the unit's records).
        self.keep_events: set[str] = set()

    def provide(self, source_id: str, start: int | None = None, end: int | None = None) -> dict:
        record = self.pool[source_id]
        lines = record.content.splitlines()
        start, end = start or 1, end or len(lines)
        if not (1 <= start <= end <= len(lines)):
            raise FlowError("requested line range is outside the record.")
        if record.locator.get("preserved_only") and not any(
                lo <= start and end <= hi for lo, hi in self.provided.get(source_id, [])):
            raise FlowError("only the preserved cited lines of this record are readable.")
        text = "\n".join(lines[start - 1:end])
        if len(text) > self.config.record_chars:
            raise FlowError("requested text exceeds the limit. Request a smaller line range.")
        self.provided.setdefault(source_id, []).append((start, end))
        self.dependencies[source_id] = record.content_hash
        result = record_for_model(record)
        result["lines"] = [{"line": n, "text": shown_line(lines[n - 1])} for n in range(start, end + 1)]
        return result

    def context(self, assigned: list[SourceRecord], *, clues: str = "") -> dict:
        sessions = {r.session_id for r in assigned if r.session_id}
        trees = {r.worktree_id for r in assigned if r.worktree_id}
        assigned_ids = {r.source_id for r in assigned}
        positions = {r.source_id: i for i, r in enumerate(self.pool.values())}
        # A unit may inspect its own records and the past, never a later record.
        # Session order is authoritative within a transcript; cross-source order
        # needs a known timestamp because Git is appended after transcript logs.
        session_ends: dict[tuple[str, str | None, str | None], int] = {}
        for record in assigned:
            key = (record.provider, record.session_id, record.worktree_id)
            session_ends[key] = max(session_ends.get(key, -1), positions[record.source_id])
        assigned_times = [t for r in assigned if (t := _record_timestamp(r.recorded_at)) is not None]
        horizon = max(assigned_times) if len(assigned_times) == len(assigned) else None
        def visible(record: SourceRecord) -> bool:
            if record.source_id in assigned_ids:
                return True
            key = (record.provider, record.session_id, record.worktree_id)
            if key in session_ends:
                return positions[record.source_id] < session_ends[key]
            stamp = _record_timestamp(record.recorded_at)
            return horizon is not None and stamp is not None and stamp <= horizon
        visible_records = [r for r in self.pool.values() if visible(r) and
                           (r.source_id in assigned_ids or r.lineage.get("kind") != "environment_context")]
        visible_ids = {r.source_id for r in visible_records}
        event_order = {e["id"]: i for i, e in enumerate(self.graph["events"])}
        search_text = "\n".join([*(r.content for r in assigned), clues])
        def paths(value: str) -> set[str]:
            return {match.casefold() for match in re.findall(
                r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9_]{1,12}(?![\w./-])", value)}
        def terms(value: str) -> set[str]:
            return {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[가-힣]{2,}", value)
                    if word.casefold() not in {"the", "and", "for", "with", "from", "this", "that", "file",
                                               "code", "project", "result", "output", "수정", "확인", "작업", "결과"}}
        query_paths, query_terms = paths(search_text), terms(search_text)
        relevance: dict[str, dict] = {}
        def rank(event: dict) -> tuple:
            event_text = "\n".join((event.get("title") or "", event.get("summary") or ""))
            quotes = "\n".join(self.saved[i].get("quote", "") for i in event.get("evidence_ids", [])
                               if i in self.saved)
            matched_paths = query_paths & paths(event_text + "\n" + quotes)
            matched_terms = query_terms & terms(event_text)
            explicit = event["id"] in search_text
            same_session = bool(set(event.get("session_ids", [])) & sessions)
            same_tree = bool(set(event.get("worktree_ids", [])) & trees)
            reasons = (["explicit_event_reference"] if explicit else []) + (
                ["same_file_path"] if matched_paths else []) + (
                ["shared_terms"] if matched_terms else []) + (
                ["same_session"] if same_session else []) + (
                ["same_worktree"] if same_tree else [])
            relevance[event["id"]] = {"event_id": event["id"], "reasons": reasons or ["recency"],
                                      "matched_paths": sorted(matched_paths)[:5],
                                      "matched_terms": sorted(matched_terms)[:8]}
            return (int(explicit), int(bool(matched_paths)), min(len(matched_terms), 8),
                    int(same_session), int(same_tree), event_order[event["id"]])
        def visible_evidence(evidence_id: str) -> bool:
            evidence = self.saved.get(evidence_id)
            if not evidence:
                return False
            source_id = evidence["source_id"]
            stamp = _record_timestamp(evidence.get("source", {}).get("recorded_at"))
            record = self.pool.get(source_id)
            if record is not None and record.content_hash == evidence.get("content_hash"):
                return source_id in visible_ids
            # A missing or changed original must be ordered using the immutable
            # saved source metadata. Unknown timestamps cannot establish that the
            # excerpt was available at this unit's point in time.
            return horizon is not None and stamp is not None and stamp <= horizon
        def visible_event(event: dict) -> bool:
            event_stamp = _record_timestamp(event.get("recorded_at"))
            if horizon is not None and event_stamp is not None and event_stamp > horizon:
                return False
            return all(visible_evidence(i) for i in event.get("evidence_ids", []))
        visible_events = [e for e in self.graph["events"] if visible_event(e)]
        ordered = sorted(visible_events, key=rank, reverse=True)
        lean = True  # the graph-shaped context (`existing_*`) the story reads
        limit = min(self.config.context_events, LEAN_CONTEXT_EVENTS) if lean else self.config.context_events
        # Named events rank first (`clues`), so the first `kept` of `ordered` are the kept ones when visible.
        kept = sum(1 for e in ordered if e["id"] in self.keep_events)
        selected = ordered[:max(limit, kept)]
        selected_ids = {e["id"] for e in selected}
        visible_ids_for_events = {e["id"] for e in visible_events}
        selected_event_ids = {e["id"] for e in selected}
        excluded_events = []
        for event in self.graph["events"]:
            if event["id"] in selected_event_ids:
                continue
            if event["id"] not in visible_ids_for_events:
                stamp = _record_timestamp(event.get("recorded_at"))
                reason = "future_event_timestamp" if horizon is not None and stamp is not None and stamp > horizon else "unsafe_or_unavailable_evidence"
            else:
                reason = "context_limit"
            if len(excluded_events) < 20:
                excluded_events.append({"event_id": event["id"], "reason": reason})
        self.selection_audit = {"policy": CONTEXT_POLICY_VERSION, "visible_events": len(ordered),
                                "selected": [relevance[e["id"]] for e in selected],
                                "excluded_by_limit": max(0, len(ordered) - len(selected)),
                                "excluded_future_or_unsafe": max(0, len(self.graph["events"]) - len(visible_events)),
                                "excluded_events": excluded_events,
                                "excluded_events_truncated": max(0, len(self.graph["events"]) - len(excluded_events) - len(selected_event_ids))}
        self.allowed_event_ids = {e["id"] for e in ordered[:200]}
        # Include known open items and same-tool-call context without reassigning old records.
        call_keys = {(r.provider, r.session_id, r.tool_call_id) for r in assigned if r.tool_call_id}
        session_keys = {(r.provider, r.session_id) for r in assigned if r.session_id}
        anchor = min((positions[r.source_id] for r in assigned), default=0)
        nearby = sorted((r for r in visible_records if r.source_id not in assigned_ids
                         and (r.provider, r.session_id) in session_keys),
                        key=lambda r: (abs(positions[r.source_id] - anchor), positions[r.source_id]))
        nearby_ids = {r.source_id for r in nearby}
        # Bring a few same-worktree records from the opposite source type into
        # view when their timestamps are close. Time proximity is context, not
        # evidence that a commit caused a conversation or vice versa.
        cross = []
        if assigned_times and trees:
            want_git = assigned[0].provider != "git"
            candidates = []
            for record in visible_records:
                if record.source_id in assigned_ids or record.worktree_id not in trees:
                    continue
                if (record.provider == "git") != want_git:
                    continue
                stamp = _record_timestamp(record.recorded_at)
                if stamp is None:
                    continue
                gap = min(abs(stamp - t) for t in assigned_times)
                if gap <= 15 * 60:
                    candidates.append((gap, positions[record.source_id], record))
            cross = [record for _, _, record in sorted(candidates)[:LEAN_CROSS_RECORDS if lean else 4]]
        cross_ids = {r.source_id for r in cross}
        # Only four preceding records go directly into context; the rest are discoverable.
        previous_turn_ids = set([r.source_id for r in nearby if positions[r.source_id] < anchor][
            :LEAN_PREVIOUS_RECORDS if lean else 4])
        related_ids = {i for event in selected for i in event["evidence_ids"]}
        related_evidence = {i: self.saved[i] for i in related_ids if i in self.saved}
        _rehydrate(self.pool, self.provided, related_evidence)
        previous = []
        context_budget = 0
        context_limit = LEAN_CONTEXT_CHARS
        context_order = cross + nearby + [r for r in visible_records
                                           if r.source_id not in nearby_ids | cross_ids]
        for record in context_order:
            if record.source_id in assigned_ids:
                continue
            is_cited = any(e["source_id"] == record.source_id and e["content_hash"] == record.content_hash
                           for e in related_evidence.values())
            same_call = record.tool_call_id and (record.provider, record.session_id, record.tool_call_id) in call_keys
            if not is_cited and not same_call and record.source_id not in previous_turn_ids | cross_ids:
                continue
            if (lean and is_cited and not same_call and not record.locator.get("preserved_only")
                    and record.source_id not in previous_turn_ids | cross_ids):
                # Only the cited lines and a few around them; the rest is readable on request.
                for lo, hi, core_lo, core_hi in self._cited_ranges(record, related_evidence):
                    size = sum(len(line) + 1 for line in record.content.splitlines()[lo - 1:hi])
                    if size > CITED_BLOCK_CHARS:
                        lo, hi = core_lo, core_hi  # the cited lines themselves, without their surroundings
                        size = sum(len(line) + 1 for line in record.content.splitlines()[lo - 1:hi])
                    if context_budget + size > context_limit:
                        break
                    try:
                        previous.append({**self.provide(record.source_id, lo, hi), "context_only": True,
                                         "context_reason": "cited_lines"})
                        context_budget += size
                    except FlowError:
                        pass
                continue
            cost = min(len(record.content), CONTEXT_HEAD_CHARS + CONTEXT_TAIL_CHARS + 200) if lean else len(record.content)
            if context_budget + cost > context_limit or len(record.content) > self.config.record_chars:
                continue
            try:
                if record.locator.get("preserved_only"):
                    for lo, hi in list(self.provided[record.source_id]):
                        previous.append({**self.provide(record.source_id, lo, hi), "context_only": True})
                else:
                    previous.append({**_shown(record, self, context=lean), "context_only": True,
                                     "context_reason": ("same_worktree_nearby_time" if record.source_id in cross_ids
                                                        else "related_context")})
                context_budget += cost
            except FlowError:
                pass
        # A bounded discovery index, not all account/project text. Event reads add exact evidence.
        excluded_record_ids = assigned_ids | nearby_ids | cross_ids
        ordered_records = assigned + cross + nearby + [r for r in visible_records
            if r.source_id not in excluded_record_ids
            and (r.worktree_id in trees or r.provider == "git")]
        self.allowed_record_ids = {r.source_id for r in ordered_records[:250]} | set(self.provided)
        self.allowed_file_ids = {file_id for file_id, source_id in self.file_source_ids.items()
                                 if source_id in visible_ids}
        for item in related_evidence.values():
            sid = item["source_id"]
            if sid in self.pool and self.pool[sid].content_hash == item["content_hash"]:
                self.dependencies[sid] = item["content_hash"]
            else:
                self.dependencies["saved:" + item["id"]] = item["content_hash"]
        ordered_ids = {e["id"] for e in ordered}
        visible_open_items = [item for item in self.graph["open_items"]
                              if set(item.get("related_event_ids", [])) <= ordered_ids
                              and all(visible_evidence(i) for i in item.get("evidence_ids", []))
                              and all(visible_evidence(i) for i in
                                      item.get("resolution", {}).get("evidence_ids", []))]
        def open_item_score(item: dict) -> tuple:
            text = item.get("text", "")
            overlap = query_terms & terms(text)
            related = set(item.get("related_event_ids", [])) & selected_ids
            reasons = ((["shared_terms"] if overlap else []) +
                       (["related_selected_event"] if related else []))
            return (int(bool(related)), min(len(overlap), 8), self.graph["open_items"].index(item))
        visible_open_items.sort(key=open_item_score, reverse=True)
        selected_open = visible_open_items[:50]
        self.selection_audit["open_items"] = [{"id": item["id"],
            "related_selected_events": sorted(set(item.get("related_event_ids", [])) & selected_ids),
            "matched_terms": sorted(query_terms & terms(item.get("text", "")))[:8],
            "reason": "related_event_or_shared_terms" if (set(item.get("related_event_ids", [])) & selected_ids
                or query_terms & terms(item.get("text", ""))) else "recent_visible_open_item"}
            for item in selected_open]
        self.selection_audit["excluded_open_items_by_limit"] = max(0, len(visible_open_items) - len(selected_open))
        selected_open_ids = {item["id"] for item in selected_open}
        visible_open_ids = {item["id"] for item in visible_open_items}
        excluded_open = []
        for item in self.graph["open_items"]:
            if item["id"] in selected_open_ids:
                continue
            if item["id"] not in visible_open_ids:
                related = set(item.get("related_event_ids", []))
                reason = "related_event_unavailable" if not related <= ordered_ids else "unsafe_or_unavailable_evidence"
            else:
                reason = "open_item_limit"
            if len(excluded_open) < 20:
                excluded_open.append({"open_item_id": item["id"], "reason": reason})
        self.selection_audit["excluded_open_items"] = excluded_open
        self.selection_audit["excluded_open_items_truncated"] = max(
            0, len(self.graph["open_items"]) - len(excluded_open) - len(selected_open_ids))
        existing_edges = [e for e in self.graph["edges"] if e["active"] and
                          e["from_event_id"] in selected_ids and
                          e["to_event_id"] in selected_ids and
                          all(visible_evidence(i) for i in e.get("evidence_ids", []))]
        index_records = ordered_records[:LEAN_INDEX_RECORDS if lean else 250]
        manifest = {
            "records": [({"id": r.source_id, "role": r.role, "lines": len(r.content.splitlines())} if lean else
                         {"id": r.source_id, "role": r.role, "provider": r.provider,
                          "recorded_at": r.recorded_at, "lines": len(r.content.splitlines())})
                        for r in index_records],
            "events": [{"id": e["id"], "title": e["title"]} for e in ordered[:LEAN_INDEX_EVENTS if lean else 200]],
            "files_at_revision": [{"id": i, "revision": v[1], "path": v[2]}
                                  for i, v in list(self.file_manifest.items())
                                  if i in self.allowed_file_ids][:LEAN_INDEX_FILES if lean else 100],
            "limits": "index of 200 past events, 250 records and 100 files at fixed revisions. Not an unlimited full search."}
        delivered_parts = {"context_only": previous, "existing_events": selected,
                           "existing_evidence": _prompt_evidence(related_evidence, previous),
                           "existing_edges": existing_edges, "existing_open_items": selected_open,
                           "manifest": manifest}
        self.selection_audit["delivered_chars"] = {key: len(dumps(value)) for key, value in delivered_parts.items()}
        self.selection_audit["delivered_chars"]["total"] = sum(
            self.selection_audit["delivered_chars"].values())
        return {"context_selection": copy.deepcopy(self.selection_audit),
                "context_only": previous, "existing_events": selected,
                "existing_evidence": delivered_parts["existing_evidence"],
                "existing_edges": existing_edges,
                "existing_open_items": selected_open,
                "manifest": manifest}

    @staticmethod
    def _cited_ranges(record: SourceRecord, evidence: dict[str, dict]) -> list[tuple[int, int, int, int]]:
        """(first, last, cited_first, cited_last) line ranges: the cited lines with their surroundings, merged."""
        total = len(record.content.splitlines())
        spans = sorted((max(1, e["start_line"] - LEAN_CITED_LINES), min(total, e["end_line"] + LEAN_CITED_LINES),
                        e["start_line"], e["end_line"])
                       for e in evidence.values()
                       if e["source_id"] == record.source_id and e["content_hash"] == record.content_hash)
        merged: list[list[int]] = []
        for lo, hi, core_lo, core_hi in spans:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1], merged[-1][3] = max(merged[-1][1], hi), max(merged[-1][3], core_hi)
            else:
                merged.append([lo, hi, core_lo, core_hi])
        return [(lo, hi, core_lo, core_hi) for lo, hi, core_lo, core_hi in merged]

    def task(self, stage: str, data: dict, checker: Callable[[dict], Any],
             *, validator: EvidenceValidator | None = None, schema: dict, thread: Any = None) -> dict:
        """One model task: the call, and one repair round when its answer fails `checker`. `thread` is a runner
        conversation to continue; by default the task keeps its own, so the repair resumes it."""
        task = _trace_operation(self.detailed_trace, f"build_{stage}_request",
                                {"stage": stage, "data": data},
                                lambda: build_task(stage, data, self.config.output_language))
        # One conversation for this task's calls where the runner keeps one (Codex): the repair resumes it with only
        # the repair, so the earlier turn hits the prompt cache instead of being resent cold.
        own = thread is None
        if own:
            thread = self.runner.thread() if callable(getattr(self.runner, "thread", None)) else None
        try:
            return self._task(stage, data, checker, task, schema, validator, thread)
        finally:
            if own and thread is not None:
                thread.close()

    def _task(self, stage, data, checker, task, schema, validator, thread) -> dict:
        run = self.runner.run if thread is None else functools.partial(self.runner.run, thread=thread)
        repair_count, attempt = 0, 0
        broken_retried = False  # an unreadable answer is asked for once more, as a call of its own
        prompt_hash = digest([task["system"], task["instructions"], task["wire_contract"]])
        aliases = IdAliases()
        while True:
            if self.cancel.is_set():
                raise Cancelled(tr("분석 중단", "Analysis stopped"))
            # The Runner sees short IDs; everything the host checks or stores uses the full ones.
            sent = aliases.wire(task)
            if len(dumps(sent)) > self.config.task_chars:
                raise InputBudgetExceeded(tr("분석 입력 예산을 초과했습니다. 이 단위는 처리 완료로 저장하지 않습니다.",
                                             "The analysis input budget was exceeded; this unit is not saved as completed."))
            attempt += 1
            call_id = "llm_" + uuid.uuid4().hex
            metadata = {
                "runner": getattr(self.runner, "name", type(self.runner).__name__),
                "runner_version": getattr(self.runner, "version", None),
                "adapter_version": getattr(self.runner, "adapter_version", None),
                "model": getattr(self.runner, "model", None),
                "requested_model": getattr(self.runner, "model", None),
                "reasoning_effort": getattr(self.runner, "effort", None),
                "routing_role": self.routing_role or stage,
                "routing_version": ROUTING_VERSION,
                "routing_reasons": self.routing_reasons,
                "worker_id": str(threading.get_ident()),
                "prompt_hash": prompt_hash,
                "schema_hash": digest(schema),
                "input_digest": digest(sent),
                "input_chars": len(dumps(sent)),
                "repair_round": repair_count,
            }
            if self.budget is not None:
                self.budget.reserve()
            self.store.start_llm_call(call_id, self.run_id, self.unit_id, stage, attempt, metadata)
            call_started = time.monotonic()
            call_started_at = datetime.now(timezone.utc)
            raw_output: dict | None = None
            def finish_call(status: str, details: dict, response: dict | None = None) -> None:
                if self.review_capture:
                    try:
                        self.review_capture({"call_id": call_id, "run_id": self.run_id,
                            "unit_id": self.unit_id, "stage": stage, "attempt": attempt,
                            "status": status, "task": copy.deepcopy(task), "schema": schema,
                            "response": raw_output, "metadata": metadata, "details": details,
                            "id_aliases": dict(aliases.full)})
                        details["review_capture"] = "saved"
                    except Exception as capture_error:
                        details["review_capture"] = "failed"
                        details["review_capture_error_type"] = type(capture_error).__name__
                if self.tracer:
                    try:
                        self.tracer.record(call_id=call_id, run_id=self.run_id, unit_id=self.unit_id,
                                           stage=stage, status=status, task=task, schema=schema,
                                           output=response, metadata=metadata, details=details,
                                           started_at=call_started_at, finished_at=datetime.now(timezone.utc))
                        details["langsmith_trace"] = "sent"
                    except Exception as trace_error:
                        # Tracing must not invalidate a completed model call or publish raw errors.
                        details["langsmith_trace"] = "failed"
                        details["langsmith_error_type"] = type(trace_error).__name__
                self.store.finish_llm_call(call_id, status, details)
            try:
                if self.config.langsmith_enabled and self.config.langsmith_include_content:
                    from langsmith import traceable

                    is_mock = bool(getattr(self.runner, "is_mock", False))
                    name = ("Synthetic model response (Mock, no LLM)" if is_mock else
                            f"{metadata['runner']} structured response")

                    @traceable(name=name, run_type="chain" if is_mock else "llm")
                    def model_call(request: dict, response_schema: dict) -> dict:
                        return run(request, response_schema, self.cancel)

                    output = model_call(sent, schema)
                else:
                    output = run(sent, schema, self.cancel)
                output = aliases.expand(output)
                if self.review_capture:
                    raw_output = copy.deepcopy(output)
            except BaseException as exc:
                finish_call("failed", {"error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - call_started) * 1000)})
                if isinstance(exc, BrokenOutput) and not broken_retried:
                    broken_retried = True
                    continue
                raise
            output_digest = digest(output)
            # Claim validation canonicalizes quotes in place (a few words become whole source
            # lines). A repair must see what the model wrote, not those expanded lines.
            submitted = copy.deepcopy(output)
            output_chars = len(dumps(submitted))
            quote_chars = output_quote_chars(submitted)
            usage = getattr(self.runner, "last_usage", None)
            if thread is not None and usage is not None:
                usage = {**usage, "continued_session": bool(getattr(self.runner, "last_continued", False))}
            # Requested IDs/aliases are not evidence of the provider's actual model.
            actual_model = getattr(self.runner, "last_model", None)
            try:
                def check_contract() -> dict:
                    validate_shape(output, schema)
                    if output["snapshot_id"] != data["snapshot_id"] or output["unit_id"] != data["unit_id"]:
                        raise FlowError("reply unit/snapshot differs from the request.")
                    return {"schema": "passed", "snapshot_and_base_ids": "passed",
                            "response_status": output["status"]}
                _trace_operation(self.detailed_trace, f"validate_{stage}_contract",
                                 {"response": output, "schema": schema}, check_contract)
                if validator is not None:
                    validator.normalizations.clear()
                    validator.mismatches.clear()
                def validate_claims() -> dict:
                    checker(output)
                    # What the trace shows for a passing check: how citations were matched.
                    modes = Counter(item["mode"] for item in validator.normalizations) if validator else {}
                    return {"passed": True, "citation_normalizations": sum(modes.values()),
                            "normalization_modes": dict(modes)}
                _trace_operation(self.detailed_trace, f"validate_{stage}_claims",
                                 {"response": output}, validate_claims)
                finish_call("complete", {
                    "output_digest": output_digest, "output_chars": output_chars, "usage": usage, "actual_model": actual_model,
                    "duration_ms": round((time.monotonic() - call_started) * 1000),
                    "repair_rounds_used": repair_count,
                    "citation_normalizations": len(validator.normalizations) if validator else 0,
                    "citation_normalization_audit": copy.deepcopy(validator.normalizations) if validator else [],
                    "quote_mismatch_audit": validator.mismatch_audit() if validator else [],
                    "output_quote_chars": quote_chars,
                }, submitted)
                return output
            except Exception as exc:
                if not isinstance(exc, FlowError):
                    # A check that breaks on an answer is that answer failing the checks, not a reason to stop the
                    # run with the call left open: it is recorded and gets the repair round like any other refusal.
                    exc = FlowError(f"answer check failed in code ({type(exc).__name__}): {exc}")
                finish_call("validation_error", {
                    "output_digest": output_digest, "output_chars": output_chars, "usage": usage, "actual_model": actual_model,
                    "duration_ms": round((time.monotonic() - call_started) * 1000),
                    "error": str(exc)[:300],
                    "citation_normalizations": len(validator.normalizations) if validator else 0,
                    "citation_normalization_audit": copy.deepcopy(validator.normalizations) if validator else [],
                    "quote_mismatch_audit": validator.mismatch_audit() if validator else [],
                }, submitted)
                if repair_count >= 1:
                    raise TaskValidationError(str(exc), submitted) from exc
                def prepare_repair() -> dict:
                    task["repair"] = {"instruction": prompt("repair").replace("{validation_errors}", str(exc)),
                                      "previous_output": submitted}
                    return task["repair"]
                _trace_operation(self.detailed_trace, "prepare_repair_request",
                                 {"validation_error": str(exc)[:400], "previous_output": submitted},
                                 prepare_repair)
                repair_count += 1


def output_quote_chars(output: object) -> dict[str, int]:
    """Characters of quoted text per top-level section of a model answer (sizes only)."""
    def quoted(value: object) -> int:
        if isinstance(value, dict):
            return (len(value["quote"]) if isinstance(value.get("quote"), str) else 0) + sum(
                quoted(v) for k, v in value.items() if k != "quote")
        return sum(quoted(v) for v in value) if isinstance(value, list) else 0
    if not isinstance(output, dict):
        return {}
    return {key: n for key, value in output.items() if (n := quoted(value))}


@dataclass
class PreparedExtraction:
    harness: Harness
    validator: EvidenceValidator
    context: dict
    data: dict
    graph_version: int


class Engine:
    def __init__(self, scope: Scope, store: Store, config: AnalysisConfig | None = None):
        self.scope, self.store = scope, store
        self.config = config or AnalysisConfig()
        self.config.validate()
        self.tracer: LangSmithTracer | None = None
        self.detailed_trace = False
        self.review_capture: Callable[[dict], None] | None = None
        # Records of units the last plan held back after a failure of their own (`_hold_back_failed`).
        self.held_back: set[str] = set()
        # The output language was detected for this plan, not configured or saved (`resolve_language`).
        self._language_detected = False
        # The sessions this run takes ahead of the oldest-first order, and whether records outside them still
        # wait (then what the run adds is out of order); set by `_plan_units`.
        self.selection: dict | None = None
        self.ahead_of_order = False

    def scan(self) -> Snapshot:
        proven = [Path(p) for p in self.store.get_meta("known_worktree_roots", [])]
        source_scope = dataclasses.replace(self.scope, roots=list(dict.fromkeys(self.scope.roots + proven)))
        # Transcript files that gave this project nothing last time and have not changed are not read again.
        saved, key = self.store.get_meta(SCAN_CACHE_META, {}) or {}, scan_cache_key(source_scope)
        cache = ScanCache(key, saved.get("files", {}) if saved.get("key") == key else {})
        logs = collect_logs(source_scope, codex_home=self.config.codex_home, claude_home=self.config.claude_home,
                            opencode_home=self.config.opencode_home, cache=cache)
        self.store.set_meta(SCAN_CACHE_META, {"key": key, "files": cache.fresh})
        artifacts = collect_git(self.scope, history_limit=self.config.history_limit,
                                exclude=self.store.get_meta("exports", []))
        self.store.set_meta("known_worktree_roots", [str(p) for p in source_scope.roots])
        # What this scan saw, so `status` and `find` can count what changed since without parsing everything.
        self.store.set_meta(INDEX_KEY, build_index(source_scope, logs, codex_home=self.config.codex_home,
                                                   claude_home=self.config.claude_home, opencode_home=self.config.opencode_home))
        mark_steps(logs.records)
        return Snapshot(logs.records + artifacts.records, list(dict.fromkeys(logs.limitations + artifacts.limitations)), logs.files)

    def resolve_language(self, snapshot: Snapshot, *, save: bool = False) -> str:
        """The output language: as configured, else as saved for the project, else detected. A detected language is
        saved only when a run is about to send (`save`, after the consent) and it was read from the person's own
        messages: a preview (`scan`), a declined run or a project with no message yet decides nothing for later."""
        if not self.config.output_language or self._language_detected:
            saved = self.store.get_meta("output_language")
            self._language_detected = not saved
            if not saved:
                saved = detect_language(snapshot.records, os.environ.get("LC_ALL") or os.environ.get("LANG"))
                if save and any(is_user_prompt(record) for record in snapshot.records):
                    self.store.set_meta("output_language", saved)
                    self._language_detected = False
            self.config.output_language = saved
        return self.config.output_language

    def preview_plan(self, snapshot: Snapshot) -> dict:
        """What `analyze` would plan now, without a model call (for `scan` and an agent's question)."""
        with self.store.analyze_lock():
            self.store.ingest(snapshot.records)
            self.store.acknowledge_environment_context(snapshot.records)
            self.store.acknowledge_journaled(snapshot.records)
            units, _, _ = self._plan_units(snapshot, [])
        pool = {r.source_id: r for r in snapshot.records}
        limit = self.config.max_units
        plan = plan_summary(units, pool, call_cap(self.config, limit), limit=limit,
                            past=calibration(self.store.units(), self.store.llm_calls(), pool))
        return {**plan, **self.plan_fields(units, pool), "output_language": self.resolve_language(snapshot)}

    def plan_fields(self, units: list[dict], pool: dict[str, SourceRecord]) -> dict:
        """What the plan of this run says besides its figures: an audit, or the recent sessions it takes first."""
        if self.config.audit:
            return audit_plan_fields(units, pool)
        return selection_plan_fields(self.selection, units, pool)

    def _routing_signature(self) -> str:
        """What a unit was asked with: a saved extraction is reused only while this is unchanged."""
        from . import light
        return digest([ROUTING_VERSION, CONTEXT_POLICY_VERSION, EXTRACT_INPUT_VERSION,
                       "story-v2", prompt("story"), json.dumps(light.STORY_SCHEMA, sort_keys=True),
                       self.config.extract_model, self.config.runner_name, self.config.base_model,
                       self.config.extract_effort, self.config.output_language,
                       *(["audit-v2", prompt("audit")] if self.config.audit else [])])

    def _prepare_extraction(self, unit: dict, pool: dict[str, SourceRecord], graph: dict,
                            snapshot_id: str, run_id: str, runners: RunnerPool,
                            cancel: threading.Event, issues: list[str]) -> PreparedExtraction:
        assigned = [pool[i] for i in unit["sources"]]
        if any(len(r.content) > self.config.record_chars for r in assigned):
            raise InputBudgetExceeded(tr("이전 보류 단위가 현재 입력 한도를 초과합니다.",
                                         "A previously deferred unit exceeds the current input limit."))
        h = Harness(None, pool, graph, self.store, self.config, cancel, run_id, unit["id"],
                    budget=runners.budget, tracer=self.tracer,
                    detailed_trace=self.detailed_trace, review_capture=self.review_capture)
        notes: list[str] = []
        if self.config.audit:
            # The notes of these records are shown in full (named, so they rank first and pass the event limit).
            notes, omitted = audit_notes(graph, h.saved, set(unit["sources"]))
            h.keep_events = set(notes)
            if omitted:
                issues.append(f"audit: {omitted} older note events of these records are not shown to the model.")
        # The session's last request before these records, whose turn they continue: shown whatever the event
        # limit, so the story can answer it by ID (`docs/plans/REQUEST_ANSWERS.md` §13).
        from . import light
        earlier = light.open_requests(graph, assigned, lambda i: (h.saved.get(i) or {}).get("source"))
        h.keep_events |= set(earlier)
        context = h.context(assigned, clues=" ".join([*notes, *earlier]))
        data, required = extract_request_data(unit, snapshot_id, assigned, h, context, issues)
        if earlier:
            data["open_requests"] = sorted(earlier)
        if self.config.audit:
            data["journal_audit"] = {"note_event_ids": notes}
            required = uncited_edits(required, h.saved)
        validator = EvidenceValidator(h.pool, h.provided, assigned_source_ids=set(unit["sources"]),
                                      required_citations=required)
        return PreparedExtraction(h, validator, context, data, graph["version"])

    def story_input(self, prepared: PreparedExtraction, unit: dict, pool: dict[str, SourceRecord],
                    snapshot_id: str) -> tuple[dict, dict]:
        """The story request (the data the runner receives) and the parts code reads the answer with: the work it
        found (`light.skeleton`, `light.delegation_items`, `light.run_items`) and what was written, cut into blocks.
        In an audit, the items and blocks a note already cites say so (`noted_by`)."""
        from . import light
        h, data = prepared.harness, prepared.data
        assigned = [pool[i] for i in unit["sources"]]
        items, edges = light.skeleton(assigned, split_at_runs=True)
        # The tasks handed to other agents stand among the work in the order they were written.
        position = {r.source_id: n for n, r in enumerate(assigned)}
        items = sorted(items + light.delegation_items(assigned), key=lambda item: position[item["sources"][0]])
        blocks = light.written_blocks(assigned)
        runs = light.run_items(assigned)
        noted: dict[str, list[str]] = {}
        note_ids = set((data.get("journal_audit") or {}).get("note_event_ids", []))
        for event in h.graph["events"]:
            if event["id"] in note_ids:
                for evidence_id in event.get("evidence_ids", []):
                    if evidence_id in h.saved:
                        noted.setdefault(h.saved[evidence_id]["source_id"], []).append(event["id"])
        request = light.story_request(unit, snapshot_id, data, items, assigned, blocks, runs, noted)
        return request, {"assigned": assigned, "items": items, "edges": edges, "blocks": blocks, "runs": runs,
                         "note_ids": note_ids}

    def _read_story(self, prepared: PreparedExtraction, unit: dict, pool: dict[str, SourceRecord], graph: dict,
                    snapshot_id: str) -> tuple[dict, list[str], list[str], dict]:
        """One story call over the work code found. When the call fails its checks, the work is kept on its own and
        the unit says so; when nothing of the unit passes, the unit fails (and is skipped), not the run."""
        from . import light
        h, validator = prepared.harness, prepared.validator
        request, parts = self.story_input(prepared, unit, pool, snapshot_id)
        try:
            answer = h.task("story", request, light.story_check, validator=validator, schema=light.STORY_SCHEMA)
            failed = False
        except TaskValidationError:
            answer, failed = {"work": [], "story": [], "requests": [], "links": [], "turn_summary": None}, True
        earlier = light.open_requests(graph, parts["assigned"], lambda i: (h.saved.get(i) or {}).get("source"))
        result, learned = light.story_extraction(answer, parts["items"], parts["edges"], unit, snapshot_id, h.pool,
                                                 h.provided, validator, parts["blocks"], parts["runs"],
                                                 self.config.output_language, earlier)
        if failed:
            result["limitations"].append(light.STORY_FALLBACK_LIMITATION)
        # A unit read again (one of its records changed) or a step a note recorded: code settles a candidate that
        # repeats an event as that event's duplicate, so nothing is added twice.
        match_existing_duplicates(result, graph, h.saved)
        if parts["note_ids"]:
            edit_calls = frozenset(s for item in parts["items"] if item.get("edit") for s in item["sources"]
                                   if s in h.pool and h.pool[s].role == "tool_call")
            match_noted_steps(result, graph, h.saved, parts["note_ids"], edit_calls)
        try:
            validator.check_extraction(result, unit["id"], snapshot_id, graph)
            return result, [], [], learned
        except FlowError:
            try:
                kept, waived, dropped = validator.salvage_extraction(result, unit["id"], snapshot_id, graph)
            except FlowError as exc:
                raise TaskValidationError(f"story: nothing of this unit passed the checks: {exc}") from None
            return kept, waived, dropped, learned

    def _extract_unit(self, unit: dict, pool: dict[str, SourceRecord], graph: dict,
                      snapshot_id: str, run_id: str, runners: RunnerPool,
                      cancel: threading.Event, issues: list[str], *,
                      prepared: PreparedExtraction | None = None) -> dict:
        """Read one unit against an immutable batch-base graph. Workers may save their own extraction ledger; only
        the caller publishes graphs. A saved extraction is reused while the routing signature and the context it was
        read with are unchanged."""
        if cancel.is_set():
            raise Cancelled(tr("분석 중단", "Analysis stopped"))
        prepared = prepared or self._prepare_extraction(unit, pool, graph, snapshot_id, run_id,
                                                         runners, cancel, issues)
        if prepared.graph_version != graph["version"] or prepared.data["unit_id"] != unit["id"] or (
                prepared.data["snapshot_id"] != snapshot_id):
            raise FlowError(tr("준비된 추출 입력의 작업·snapshot·graph 기준이 달라졌습니다.",
                               "The prepared extract input's unit, snapshot or graph base has changed."))
        h, validator, context = prepared.harness, prepared.validator, prepared.context
        signature = self._routing_signature()
        cached = unit.get("result") or {}
        current_context_digest = digest(context)
        if (unit["status"] == "extracted" and cached.get("routing_signature") == signature
                and cached.get("context_digest") == current_context_digest):
            _rehydrate(h.pool, h.provided, cached.get("evidence", {}))
            validator.inherit_focus(cached.get("evidence", {}))
            for item in cached.get("evidence", {}).values():
                record = h.pool.get(item["source_id"])
                if record and record.content_hash == item["content_hash"]:
                    # This exact preserved quote was visible in the saved extraction.
                    h.provided.setdefault(item["source_id"], []).append((item["start_line"], item["end_line"]))
            output = copy.deepcopy(cached["payload"])
            output["snapshot_id"] = snapshot_id
            waived = set(cached.get("waived_citations", []))
            validator.required_citations = {call: pair for call, pair in validator.required_citations.items()
                                            if call not in waived}
            validator.check_extraction(output, unit["id"], snapshot_id, graph)
            h.dependencies.update(unit["dependencies"])
            return {"output": output, "evidence": validator.evidence, "dependencies": h.dependencies,
                    "cached": {**cached, "payload": output}, "reused": True}
        self.store.save_unit(unit["id"], unit["sources"], h.dependencies, "parsed")
        h.runner, h.routing_role = runners.get("extract"), "extract"
        output, waived, dropped, learned = self._read_story(prepared, unit, pool, graph, snapshot_id)
        text = learned.get("turn_summary")
        summary = " ".join(text.split()) if isinstance(text, str) and text.strip() else None
        cached = {"payload": output, "evidence": validator.evidence, "routing_signature": signature,
                  "waived_citations": waived, "dropped_candidates": dropped,
                  **({"unit_summary": summary} if summary else {}),
                  "unclaimed_blocks": learned["unclaimed_blocks"],
                  "extraction_graph_version": graph["version"],
                  "context_selection": context.get("context_selection", {}),
                  "context_digest": current_context_digest}
        self.store.save_unit(unit["id"], unit["sources"], h.dependencies, "extracted", cached)
        return {"output": output, "evidence": validator.evidence, "dependencies": h.dependencies,
                "cached": cached, "reused": False}

    def _integrate_unit(self, unit: dict, extracted: dict, pool: dict[str, SourceRecord],
                        graph: dict, snapshot_id: str, run_id: str,
                        cancel: threading.Event) -> tuple[dict, dict, dict]:
        """Publish the unit's extraction against the latest graph as the code-built delta (`draft_delta`: every
        candidate added, one matched to exactly one existing event settled as its duplicate), under the same
        checks; no model call. A draft the checks refuse fails the unit."""
        output = extracted["output"]
        h = Harness(None, pool, graph, self.store, self.config, cancel, run_id, unit["id"], tracer=self.tracer,
                    detailed_trace=self.detailed_trace, review_capture=self.review_capture)
        _rehydrate(h.pool, h.provided, extracted["evidence"])
        h.dependencies.update(extracted["dependencies"])
        for item in extracted["evidence"].values():
            h.provided.setdefault(item["source_id"], []).append((item["start_line"], item["end_line"]))
        validator = EvidenceValidator(h.pool, h.provided, assigned_source_ids=set(unit["sources"]))
        validator.inherit_focus(extracted["evidence"])
        validator.check_extraction(output, unit["id"], snapshot_id, graph)
        if any(output[k] for k in ("event_candidates", "edge_candidates", "existing_event_matches", "open_items")):
            draft = draft_delta(output, graph["version"], snapshot_id)
            if not self.config.audit:
                # A reading of changed records updates what it repeats; an audit only adds.
                refresh_matched(draft, output, graph)
            try:
                if self.config.audit:
                    audit_guard(draft, graph)
                new_graph = validator.apply_delta(draft, graph, snapshot_id, run_id, output)
            except FlowError as exc:
                raise TaskValidationError(f"draft failed its checks: {exc}") from None
            # What code dropped from the extraction is said with the unit.
            new_graph["limitations"] = list(dict.fromkeys(new_graph["limitations"] +
                                                         extracted["cached"].get("dropped_candidates", [])))
        else:
            new_graph = copy.deepcopy(graph)
            new_graph["limitations"] = list(dict.fromkeys(new_graph["limitations"] + output["limitations"]))
            new_graph["source_snapshot_id"] = snapshot_id
        return new_graph, {**extracted["evidence"], **validator.evidence}, h.dependencies

    def _plan_units(self, snapshot: Snapshot, issues: list[str], *,
                    repair: bool = False) -> tuple[list[dict], set[str], list[str]]:
        """Plan changed source units after the snapshot has been ingested.

        `repair` lets an analysis run (under the analysis lock) correct the processed ledger;
        previews only read."""
        if self.config.audit:
            return self._plan_audit_units(snapshot, issues, repair=repair)
        pool = {r.source_id: r for r in snapshot.records}
        source_rows = self.store.sources()
        missing = [i for i, row in source_rows.items() if not row["available"]]
        if missing:
            issues.append(f"{len(missing)} sources are missing from the current snapshot. Preserved evidence is kept; no fact is retracted.")
        pending = {i for i, row in source_rows.items()
                   if i in pool and row["processed_hash"] != pool[i].content_hash}
        plans, scheduled, settled, regrouped = [], set(), {}, {}
        for unit in self.store.units():
            if unit["id"].startswith(AUDIT_PREFIX):
                continue  # an audit unit belongs to `analyze --audit` (`_plan_audit_units`)
            if unit["status"] == "integrated":
                if not all(i in pool for i in unit["sources"]):
                    continue  # a deleted record is not a retraction
                # An integrated unit is sent again only when one of its own records changed. Context it
                # merely looked at (neighbouring records, Git near in time) has its own unit for that.
                seen = {i: unit["dependencies"].get(i, source_rows.get(i, {}).get("processed_hash"))
                        for i in unit["sources"]}
                changed = sorted(i for i, h in seen.items() if h != pool[i].content_hash)
                if not changed:
                    settled.update({i: pool[i].content_hash for i in seen
                                    if source_rows[i]["processed_hash"] != pool[i].content_hash})
                    continue
                issues.append(f"re-analyzing an already integrated work unit: {unit['id']} "
                              f"({len(changed)} records changed: {', '.join(changed[:3])}{' and more' if len(changed) > 3 else ''})")
                deps_changed, sources_changed = False, True
            else:
                deps_changed = any(i in pool and pool[i].content_hash != h
                                   for i, h in unit["dependencies"].items())
                sources_changed = False
            if unit["status"] == "superseded":
                continue
            ids = unit["sources"]
            if not all(i in pool for i in ids):
                if unit["status"] != "integrated":
                    issues.append("retry of a pending work unit held back because some of its sources are missing.")
                continue
            if scheduled.intersection(ids):
                continue
            if unit["status"] == "parsed" and unit["result"] is None and not deps_changed and not pending.intersection(ids):
                # Nothing paid for and nothing left to do: its records were settled without a model
                # (an agent's notes cover its session, `Store.acknowledge_journaled`).
                if repair:
                    self.store.save_unit(unit["id"], unit["sources"], unit["dependencies"], "superseded")
                continue
            if (unit["status"] == "parsed" and unit["result"] is None and not deps_changed
                    and all(i in pending for i in ids)):
                # Nothing was paid for yet: cut it again under the current rule. A stored draft or
                # extraction is never regrouped, that would discard a model answer.
                regrouped[unit["id"]] = unit
                continue
            if deps_changed or sources_changed:
                unit["result"], unit["status"] = None, "invalidated"
            plans.append(unit)
            scheduled.update(ids)
        if settled:
            # Records an unchanged integrated unit already covers are not pending, whatever the ledger says.
            issues.append(f"corrected the processed ledger of {len(settled)} already integrated records (not resent).")
            pending -= set(settled)
            if repair:
                self.store.mark_processed(settled)
        remaining, waiting = [], pending - scheduled
        for record in snapshot.records:
            if record.source_id not in waiting:
                continue
            if len(record.content) > self.config.record_chars:
                issues.append(f"record over the size limit, not processed: {record.source_id} ({len(record.content)} chars)")
                continue
            remaining.append(record)
        for chunk in _session_unit_chunks(remaining, self.config, issues):
            unit_id = ident("unit_", [(r.source_id, r.content_hash) for r in chunk])
            plans.append({"id": unit_id, "sources": [r.source_id for r in chunk],
                          "dependencies": {}, "status": "parsed", "result": None})
        self.selection = self._select_sessions(snapshot)
        family = self.selection["family"] if self.selection else None

        def in_scope(unit: dict) -> bool:
            return family is None or all(pool[i].session_id in family for i in unit["sources"])

        replaced = [unit for uid, unit in regrouped.items()
                    if uid not in {p["id"] for p in plans} and in_scope(unit)]
        if replaced:
            issues.append(f"regrouped {len(replaced)} stored unprocessed work units under the current rule.")
            if repair:
                for unit in replaced:
                    self.store.save_unit(unit["id"], unit["sources"], unit["dependencies"], "superseded")
        self.ahead_of_order = False
        if family is not None:
            # Out of the oldest-first order on request: the chosen sessions' units only (oldest first among
            # them), the rest wait. What is added is out of order while any record outside them waits.
            order = {r.source_id: n for n, r in enumerate(snapshot.records)}
            plans = sorted((unit for unit in plans if in_scope(unit)), key=lambda unit: min(order[i] for i in unit["sources"]))
            self.ahead_of_order = any(pool[i].session_id not in family for i in pending)
        elif self.config.trigger == "hook":
            plans = self._tails_first(plans, pool, pending)
        return self._hold_back_failed(plans, pool, issues, repair=repair), pending, missing

    def _tails_first(self, plans: list[dict], pool: dict[str, SourceRecord], pending: set[str]) -> list[dict]:
        """The automatic analysis takes first the units holding what a noted session left after its last note
        once it went quiet (`Store.journal_tails`): recent work in no note, which oldest-first would leave
        behind the whole backlog. The rest keeps the oldest-first order; what is added ahead of an older
        waiting record is out of order, as with `--recent`."""
        tails = self.store.journal_tails()
        if not tails:
            return plans

        def in_tail(source_id: str) -> bool:
            record = pool[source_id]
            owner = record.session_id if record.session_id in tails else (record.lineage or {}).get("parent_session_id")
            stamp = _record_timestamp(record.recorded_at)
            return owner in tails and stamp is not None and tails[owner][0] < stamp <= tails[owner][1]
        first = [unit for unit in plans if any(in_tail(i) for i in unit["sources"])]
        if not first:
            return plans
        taken = {i for unit in first for i in unit["sources"]}
        self.ahead_of_order = any(i not in taken for i in pending)
        return first + [unit for unit in plans if unit not in first]

    def _select_sessions(self, snapshot: Snapshot) -> dict | None:
        """The sessions an ordinary run takes ahead of the oldest-first order (`--session`, `--recent`, `--since`)."""
        config = self.config
        if config.session:
            return {"kind": "session", "family": session_family(snapshot.records, config.session)}
        if config.recent is not None or config.since is not None:
            return recent_sessions(snapshot.records, recent=config.recent, since=config.since,
                                   noted=set(self.store.journaled_sessions()))
        return None

    def failure_input(self, unit: dict, pool: dict[str, SourceRecord]) -> dict:
        """What makes a unit the same input again: the routing signature and the hash of each of its records."""
        return {"signature": self._routing_signature(),
                "hashes": {i: pool[i].content_hash for i in unit["sources"] if i in pool}}

    def _hold_back_failed(self, plans: list[dict], pool: dict[str, SourceRecord], issues: list[str], *,
                          repair: bool) -> list[dict]:
        """Leave out the units that were skipped for a failure of their own and are still the same input.

        Same means the same `_routing_signature` and the same record hashes as when it failed; a record that
        changed or a setting that changed sends it again (as does `retry_failed`). A skipped unit whose
        session has notes by now is superseded instead: the agent writes that session. Nothing is written
        unless `repair` (a preview only reads).
        """
        failures = self.store.unit_failures()
        self.held_back = set()
        if not failures:
            return plans
        noted, since, tails = self.store.journaled_sessions(), self.store.journal_since(), self.store.journal_tails()
        kept, held = [], []
        for unit in plans:
            entry = failures.get(unit["id"])
            if not entry:
                kept.append(unit)
                continue
            records = [pool[i] for i in unit["sources"] if i in pool]
            if not unit["id"].startswith(AUDIT_PREFIX) and records and all(
                    in_journal(r.session_id, r.lineage, r.recorded_at, noted, since, tails) for r in records):
                if repair:
                    self.store.save_unit(unit["id"], unit["sources"], unit["dependencies"], "superseded")
                    self.store.clear_unit_failure(unit["id"])
                continue
            if not self.config.retry_failed and entry.get("signature") == self._routing_signature() and (
                    entry.get("hashes") == self.failure_input(unit, pool)["hashes"]):
                held.append(unit)
                continue
            kept.append(unit)
        self.held_back = {i for unit in held for i in unit["sources"]}
        if held:
            issues.append(held_back_message(len(held)))
        return kept

    def _plan_audit_units(self, snapshot: Snapshot, issues: list[str], *,
                          repair: bool = False) -> tuple[list[dict], set[str], list[str]]:
        """The work units of `analyze --audit` (docs/plans/LIVE_JOURNAL.md §11.1).

        The records of the sessions an agent wrote notes in (and their sub-agents) that no integrated unit
        covers yet: an earlier analysis or an earlier audit. Cut like any session, stored as `audit_` units.
        Returns the units, the records still to audit and no missing sources. `repair` is unused: nothing
        in the ledger needs correcting."""
        pool = {r.source_id: r for r in snapshot.records}
        noted, since, tails = self.store.journaled_sessions(), self.store.journal_since(), self.store.journal_tails()
        stored = self.store.units()
        covered = {i for unit in stored if unit["status"] == "integrated" for i in unit["sources"]}

        def of_noted_session(record: SourceRecord) -> bool:
            # A noted session's records from before notes were on are ordinary analysis, not an audit.
            return record.provider != "git" and in_journal(record.session_id, record.lineage, record.recorded_at,
                                                           noted, since, tails)
        family = None
        if self.config.session:
            family = session_family(snapshot.records, self.config.session)
            if not any(of_noted_session(r) for r in snapshot.records if r.session_id in family):
                raise FlowError(tr(f"세션 {self.config.session}에는 note가 없어 감사할 것이 없습니다. "
                                   f"note가 없는 세션은 analyze --session으로 일반 분석합니다.",
                                   f"Session {self.config.session} has no notes, so there is nothing to audit; "
                                   f"analyze a session without notes with analyze --session."))
        eligible = []
        for record in snapshot.records:
            if not of_noted_session(record) or record.source_id in covered:
                continue
            if family is not None and record.session_id not in family:
                continue
            if len(record.content) > self.config.record_chars:
                issues.append(f"audit: record over the size limit, not read: {record.source_id} ({len(record.content)} chars)")
                continue
            eligible.append(record)
        if not noted:
            issues.append("audit: no session has notes, so there is nothing to audit.")
        waiting = {r.source_id for r in eligible}
        plans, scheduled = [], set()
        for unit in stored:
            if not unit["id"].startswith(AUDIT_PREFIX) or unit["status"] in {"integrated", "superseded"}:
                continue
            ids = unit["sources"]
            if not set(ids) <= waiting or scheduled.intersection(ids):
                continue  # not in this run's scope, or its records are covered by now
            if any(pool[i].content_hash != h for i, h in unit["dependencies"].items() if i in pool):
                unit["result"], unit["status"] = None, "invalidated"
            plans.append(unit)
            scheduled.update(ids)
        for chunk in _session_unit_chunks([r for r in eligible if r.source_id not in scheduled], self.config, issues):
            plans.append({"id": ident(AUDIT_PREFIX, [(r.source_id, r.content_hash) for r in chunk]),
                          "sources": [r.source_id for r in chunk], "dependencies": {}, "status": "parsed", "result": None})
        order = {r.source_id: n for n, r in enumerate(snapshot.records)}
        plans.sort(key=lambda unit: min(order[i] for i in unit["sources"]))
        return self._hold_back_failed(plans, pool, issues, repair=repair), waiting, []

    def analyze(self, runner_factory: Callable[[], Any], *, cancel: threading.Event | None = None,
                update: Callable[[str], None] | None = None,
                consent: Callable[[Snapshot, dict], bool] | None = None) -> dict:
        # The terminal and Studio invoke the same compiled execution graph.
        # Import here to keep the Engine primitives independent of orchestration.
        from .studio_graph import run_engine

        self.tracer = (LangSmithTracer(self.config.langsmith_project,
                                      include_content=self.config.langsmith_include_content)
                       if self.config.langsmith_enabled else None)
        # Step spans follow the tracer's content choice: metadata-only unless content was chosen.
        self.detailed_trace = self.config.langsmith_enabled
        return run_engine(self, runner_factory, cancel=cancel, update=update, consent=consent)
