from __future__ import annotations

import copy
import dataclasses
import difflib
import os
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .git_context import Scope, collect_git, git
from .i18n import tr
from .langsmith_trace import LangSmithTracer
from .model import Snapshot, SourceRecord, is_user_prompt
from .routing import ROUTING_VERSION, RunnerPool, TaskValidationError
from .runners.cli_runner import EFFORTS
from .schema import (DELTA_SCHEMA, EDIT_TOOL_NAMES, EXTRACT_SCHEMA, EvidenceValidator, delta_schema, docs_only,
                     draft_delta, edited_files, merge_review_patch, reconcile_review_patch,
                     record_evidence, review_patch_audit, review_patch_schema, validate_shape)
from .sources import collect_logs
from .store import Store
from .util import Cancelled, FlowError, digest, dumps, ident, now


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
    context_mode: str = "lean"
    read_rounds: int = 2
    read_chars: int = 36_000
    runner_name: str | None = None
    base_model: str | None = None
    extract_model: str | None = None
    integrate_model: str | None = None
    escalation_model: str | None = None
    # medium throughout since 2026-09-27: high made integration and review calls take 2-3 minutes each.
    extract_effort: str = "medium"
    integrate_effort: str = "medium"
    # reuse lets the integrator leave out quotes an input candidate already carries; the code cites those.
    integrate_evidence: str = "full"
    escalation_effort: str = "medium"
    semantic_review: bool = True
    # `patch`: the review answers only what it changes instead of rewriting the whole GraphDelta.
    review_output: str = "patch"
    # `patch`: code adds every candidate as extracted and the integrator answers only its changes;
    # `draft`: the same, with no integrate call while the graph is still empty.
    integrate_output: str = "draft"
    extract_workers: int = 1
    max_calls: int = 30
    # Work units for this run (never saved). Unset, the call cap alone bounds a run.
    max_units: int | None = None
    calls_fixed: bool = False  # max_calls was given for this run, so a unit count does not raise it
    # A session (with its sub-agents) analysed ahead of the oldest-first order.
    session: str | None = None
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
        for value in (self.extract_model, self.integrate_model, self.escalation_model):
            if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
                raise FlowError(tr("모델 식별자는 비어 있거나 제어 문자를 포함할 수 없습니다.",
                                   "A model identifier cannot be empty or contain control characters."))
        for value in (self.extract_effort, self.integrate_effort, self.escalation_effort):
            if value not in EFFORTS:
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
        if self.context_mode not in {"full", "lean"}:
            raise FlowError(tr("context_mode는 full 또는 lean이어야 합니다.", "context_mode must be full or lean."))
        if self.integrate_evidence not in {"full", "reuse"}:
            raise FlowError(tr("integrate_evidence는 full 또는 reuse이어야 합니다.",
                               "integrate_evidence must be full or reuse."))
        if self.review_output not in {"full", "patch"}:
            raise FlowError(tr("review_output은 full 또는 patch이어야 합니다.", "review_output must be full or patch."))
        if self.integrate_output not in {"full", "patch", "draft"}:
            raise FlowError(tr("integrate_output은 full, patch 또는 draft이어야 합니다.",
                               "integrate_output must be full, patch or draft."))
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
    return votes.most_common(1)[0][0] if votes else DEFAULT_LANGUAGE


def prompt(name: str) -> str:
    return files("contexttrail").joinpath("prompts", name + ".md").read_text(encoding="utf-8")


# Sent only in reuse mode, so the default integration request is the same bytes as before.
EVIDENCE_POLICY_REUSE = (
    "In events_to_add, edges_to_add, events_to_update, candidate_resolutions and change_attributions you may "
    "leave evidence empty when the only support is evidence an input candidate already carries; the code then "
    "cites that candidate's evidence. Quote only support the candidates do not already have.")


def build_task(stage: str, data: dict, language: str | None = None) -> dict:
    """One source of truth for the Runner request and the local input preview."""
    return {"system": prompt("common"), "stage": stage, "instructions": prompt(stage), "data": data,
            # Titles and summaries follow the project's language, whatever the records use (common.md).
            "output_language": language or DEFAULT_LANGUAGE,
            "wire_contract": "Use IDs in the short form shown in the input (S12, E3). "
                "evidence is an array of source_id/start_line/end_line/quote. Line numbers follow the provided lines; "
                "quote is contiguous exact source text within that range: whole lines joined by newlines, or a "
                "distinctive part of at least 8 characters. Copy escapes visible in the source (\\\" \\n and so on) "
                "as they are. Do not recreate context_only material as new events. "
                "With needs_evidence, still include every schema field and leave candidate/change arrays empty. "
                "null in events_to_update.changes means unchanged; quote again the source a changed claim needs. "
                "Every session_ids/worktree_ids value matches the cited sources exactly."}


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
    )
    return any(not any(re.fullmatch(pattern, message) for pattern in informational) for message in issues)


CONTEXT_POLICY_VERSION = "relevance_v2"
# `lean`: earlier work reaches the model as graph (events, edges, quotes) and a few records, not as
# whole records. Cut finely, a piece's neighbours are already in the graph.
LEAN_CITED_LINES, LEAN_PREVIOUS_RECORDS, LEAN_CONTEXT_EVENTS = 5, 2, 12
LEAN_INDEX_RECORDS, LEAN_INDEX_EVENTS, LEAN_INDEX_FILES = 100, 100, 50
# Repair hints: a few provided lines per failed quote, ranked by how close they are to it.
NEAREST_LINES, NEAREST_POOL, NEAREST_LINE_CHARS = 3, 12, 400
# What the extraction input carries besides the records: short IDs, a tool-step outline
# and the user's messages. Part of the cache signature, so older extractions are redone.
EXTRACT_INPUT_VERSION = "aliases_steps_requests_read_heads_v2"

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
        listing = {"-l", "--list", "-a", "--all", "-r", "--remotes", "--show-current", "-v", "-vv",
                   "--contains", "--merged", "--no-merged", "--points-at"}
        return not (flags & listing) and bool(words)
    return True


def _command_segments(text: str) -> list[str]:
    """Each command of a compound line without env assignments, wrappers or interpreter prefix."""
    segments = []
    for raw in _SEGMENT_SPLIT.split(_HEREDOC.split(text)[0]):
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


# A long tool result, injected instruction or summary is shown as its head and tail; the lines
# between are listed as omitted and read with read_records on request. What the person typed,
# the assistant's words and tool calls (patches included) are always shown whole.
VIEW_HEAD_CHARS, VIEW_TAIL_CHARS = 2_000, 1_000
# What a read-only call (a file read, a listing, a search) returned is rarely an event of its own:
# a shorter head and tail, the rest on request.
READ_HEAD_CHARS, READ_TAIL_CHARS = 400, 200


def _view(record: SourceRecord, head_chars: int = VIEW_HEAD_CHARS,
          tail_chars: int = VIEW_TAIL_CHARS) -> tuple[int, int] | None:
    """The last head line and first tail line to show, or None to show the record whole."""
    if len(record.content) <= head_chars + tail_chars or record.role in {"assistant", "tool_call"} or (
            record.role == "user" and is_user_prompt(record)):
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


def unit_cost(record: SourceRecord) -> int:
    """Characters this record puts into a unit's input."""
    view = _view(record)
    if view is None:
        return len(record.content)
    lines = record.content.splitlines()
    return sum(len(line) + 1 for line in lines[:view[0]] + lines[view[1] - 1:]) + 120


def _shown(record: SourceRecord, harness: "Harness", *, read: bool = False) -> dict:
    view = _view(record, READ_HEAD_CHARS, READ_TAIL_CHARS) if read else _view(record)
    if view is None:
        return harness.provide(record.source_id)
    head, tail = view
    total = len(record.content.splitlines())
    shown = harness.provide(record.source_id, 1, head)
    if tail <= total:
        shown["lines"] += harness.provide(record.source_id, tail, total)["lines"]
    shown["omitted"] = {"start_line": head + 1, "end_line": tail - 1,
                        "chars": len("\n".join(record.content.splitlines()[head:tail - 1])),
                        "read": "read_records with this source_id and a line range"}
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


# A unit takes an extraction and an integration call, and at worst read rounds, a repair each
# and a review: a run bounded by units gets this many calls per unit unless a cap was given.
CALLS_PER_UNIT = 6
PLAN_CHOICES = (5, 15, 30)
# Conservative estimate for the prompt, schema, context, and optional read, repair, or review
# calls a work unit may need. Actual usage depends on the input and the configured model.
_FIXED_TOKENS, _TYPICAL_RATIO, _TOKENS_PER_MINUTE = 30_000, 3.0, 20_000


def call_cap(config: "AnalysisConfig", limit: int | None) -> int:
    """A run bounded by units gets calls for them, unless a cap was given for this run."""
    return config.max_calls if config.calls_fixed or not limit else CALLS_PER_UNIT * limit


def session_family(records: list[SourceRecord], wanted: str) -> set[str]:
    """The session a prefix names, and the sub-agent sessions it started (at any depth)."""
    sessions = {r.session_id for r in records if r.session_id and r.provider in ("codex", "claude", "opencode")}
    found = sorted(s for s in sessions if s.startswith(wanted))
    if not found:
        raise FlowError(tr(f"이 프로젝트 범위의 기록에 세션 {wanted}가 없습니다.",
                           f"No session {wanted} in the records of this project scope."))
    if len(found) > 1 and wanted not in found:
        raise FlowError(tr(f"세션 {wanted}에 해당하는 세션이 {len(found)}개입니다. 더 길게 지정하세요.",
                           f"{len(found)} sessions match {wanted}; give a longer prefix."))
    family = {wanted if wanted in found else found[0]}
    parents = {r.session_id: r.lineage.get("parent_session_id") for r in records
               if r.session_id and r.lineage.get("parent_session_id")}
    while True:
        more = {child for child, parent in parents.items() if parent in family} - family
        if not more:
            return family
        family |= more


def _estimated_tokens(chars: int) -> int:
    return int(chars * 1.4 / 2.4) + _FIXED_TOKENS


# One or two units say little: a unit that needed read rounds and a repair can cost three times another.
MIN_PAST_UNITS = 3


def calibration(units: list[dict], calls: list[dict], pool: dict[str, SourceRecord]) -> dict | None:
    """How past integrated units of this project compare with the estimate: tokens and time.

    Only units whose every call reported its input tokens count; None until MIN_PAST_UNITS do.
    """
    by_unit: dict[str, list[dict]] = {}
    for call in calls:
        by_unit.setdefault(call["unit_id"], []).append(call)
    estimate = actual = duration = counted = 0
    for unit in units:
        rows = by_unit.get(unit["id"])
        if unit["status"] != "integrated" or not rows or not all(i in pool for i in unit["sources"]):
            continue
        tokens = [(row["details"].get("usage") or {}).get("input_tokens") for row in rows]
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in tokens):
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
    reach = min(len(units), limit if limit else max(1, max_calls // 2))
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
    return tr(f"대기 {plan['units']:,}개 단위 · 이번 실행 {plan['units_this_run']:,}개 "
              f"(AI 호출 ≤{plan['max_calls']}) · 입력 약 {tokens} 토큰 · 약 {minutes}분({basis}){language}",
              f"{plan['units']:,} units pending · {plan['units_this_run']:,} this run "
              f"(AI calls ≤{plan['max_calls']}) · input ≈{tokens} tokens · ≈{minutes} min ({basis}){language}")


def plan_choices_text(plan: dict) -> str:
    """The unit counts to choose from, with their tokens and time, on one line."""
    return " · ".join(tr(f"{c['units']:,}개 ≈{_tokens(c['input_tokens'])} 토큰 {c['minutes']}분",
                         f"{c['units']:,} units ≈{_tokens(c['input_tokens'])} tokens {c['minutes']} min")
                     for c in plan["choices"])


def _one_line(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def add_user_requests(output: dict, assigned: list[SourceRecord], validator: EvidenceValidator) -> list[str]:
    """A question candidate for each user message the extraction did not turn into a user event.

    Only when the extraction has candidates of its own, so the integration call that follows
    can link the message; otherwise the graph step adds the message without a model call.
    """
    if not output["event_candidates"]:
        return []
    covered = {citation["source_id"] for item in output["event_candidates"] if item["actor"] == "user"
               for citation in item["evidence"]}
    taken = {item["id"] for item in output["event_candidates"]}
    added = []
    for record in assigned:
        if not is_user_prompt(record) or record.source_id in covered:
            continue
        evidence = record_evidence(record)
        number = len(added) + 1
        while f"tmp:user_request_{number}" in taken:
            number += 1
        candidate = {"id": f"tmp:user_request_{number}", "kind": "question",
                     "title": _one_line(record.content), "summary": record.content.strip()[:4000],
                     "actor": "user", "status": "asked", "basis": "explicit_statement",
                     "session_ids": [record.session_id] if record.session_id else [],
                     "worktree_ids": [record.worktree_id] if record.worktree_id else [],
                     "recorded_at": record.recorded_at, "occurred_at": None,
                     "evidence": [{"source_id": record.source_id, "start_line": 1,
                                   "end_line": evidence["end_line"], "quote": evidence["quote"]}]}
        validator.citations(candidate["evidence"])
        output["event_candidates"].append(candidate)
        taken.add(candidate["id"])
        added.append(record.source_id)
    return added


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
    by_session: dict[str | None, list[SourceRecord]] = {}
    for record in prompts:
        by_session.setdefault(record.session_id, []).append(record)
    reached = {edge["to_event_id"] for edge in graph["edges"] if edge["active"]}
    old = {event["id"] for event in base["events"]}
    links = 0
    for event in list(graph["events"]):
        if event["id"] in old or event["id"] in reached or event.get("actor") == "user":
            continue
        cited = [source_id for source_id in sources(event) if source_id in position]
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
PAYLOAD_CONSTANTS = {"full": (3.5, 500, 40_000), "lean": (1.5, 500, 30_000)}
MIN_FILL = 0.4
RESULT_CUT_HINTS = {"run", "test", "commit", "vcs"}


def payload_budget(config: AnalysisConfig) -> int:
    return int(config.task_chars * PAYLOAD_SHARE)


def estimated_payload(record_chars: int, records: int, context_mode: str = "lean") -> int:
    factor, overhead, fixed = PAYLOAD_CONSTANTS[context_mode]
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
                   and estimated_payload(chars + costs[limit], count + 1, config.context_mode) <= payload_budget(config)):
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
            git=source.get("git", {}), lineage=source.get("lineage", {}), pinned_hash=items[0]["content_hash"])
        provided[source_id] = [(i["start_line"], i["end_line"]) for i in items]


def _prompt_evidence(evidence: dict[str, dict], context_only: list[dict] | None = None) -> dict[str, dict]:
    """Send citation substance without repeating filesystem/source metadata per quote."""
    visible_lines: dict[str, set[int]] = {}
    for item in context_only or []:
        visible_lines.setdefault(item["source_id"], set()).update(line["line"] for line in item["lines"])
    result = {}
    for evidence_id, item in evidence.items():
        source = item["source"]
        compact = {key: item[key] for key in ("id", "source_id", "start_line", "end_line")}
        compact["source"] = {key: source.get(key) for key in
                             ("provider", "session_id", "role", "recorded_at", "worktree_id", "derivation")}
        lineage = source.get("lineage") or {}
        compact["source"]["lineage_kind"] = lineage.get("kind") if isinstance(lineage, dict) else None
        if all(line in visible_lines.get(item["source_id"], set())
               for line in range(item["start_line"], item["end_line"] + 1)):
            compact["quote_in_context_only"] = True
        else:
            compact["quote"] = item["quote"]
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
        # Full read replies are transient. Persist only requests/digests for draft resume.
        self.read_history: list[list[dict]] = []
        self.read_manifest: list[dict] = []
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
        if len(text) > max(self.config.record_chars, self.config.read_chars):
            raise FlowError("requested text exceeds the limit. Request a smaller line range.")
        self.provided.setdefault(source_id, []).append((start, end))
        self.dependencies[source_id] = record.content_hash
        result = record.metadata()
        result.pop("locator")
        result["lines"] = [{"line": n, "text": lines[n - 1]} for n in range(start, end + 1)]
        return result

    def nearest_lines(self, source_id: str, quote: str, limit: int = NEAREST_LINES) -> list[dict]:
        """Provided lines closest to a quote the model got wrong, as lines it can copy instead.

        Only lines this record was shown are candidates, so the hint can name no text the
        model has not seen. A long line is cut to a window around its best-matching position.
        """
        record = self.pool.get(source_id)
        lines = record.content.splitlines() if record else []
        numbers = sorted({n for lo, hi in self.provided.get(source_id, []) for n in range(lo, min(hi, len(lines)) + 1)})
        # quick_ratio keeps a pass over a whole record cheap; the best few are ranked exactly.
        coarse = sorted(numbers, key=lambda n: -difflib.SequenceMatcher(
            None, quote, lines[n - 1], autojunk=False).quick_ratio())[:NEAREST_POOL]
        ranked = sorted(coarse, key=lambda n: (-difflib.SequenceMatcher(
            None, quote, lines[n - 1], autojunk=False).ratio(), n))[:limit]
        results = []
        for number in ranked:
            line = lines[number - 1]
            clipped = len(line) > NEAREST_LINE_CHARS
            if clipped:
                best = difflib.SequenceMatcher(None, quote, line, autojunk=False).find_longest_match(
                    0, len(quote), 0, len(line)).b
                begin = max(0, min(best - NEAREST_LINE_CHARS // 2, len(line) - NEAREST_LINE_CHARS))
                line = line[begin:begin + NEAREST_LINE_CHARS]
            results.append({"source_id": source_id, "line": number, "text": line, "clipped": clipped})
        return results

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
        lean = self.config.context_mode == "lean"
        selected = ordered[:min(self.config.context_events, LEAN_CONTEXT_EVENTS) if lean else self.config.context_events]
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
            cross = [record for _, _, record in sorted(candidates)[:4]]
        cross_ids = {r.source_id for r in cross}
        # Only four preceding records go directly into context; the rest are discoverable.
        previous_turn_ids = set([r.source_id for r in nearby if positions[r.source_id] < anchor][
            :LEAN_PREVIOUS_RECORDS if lean else 4])
        related_ids = {i for event in selected for i in event["evidence_ids"]}
        related_evidence = {i: self.saved[i] for i in related_ids if i in self.saved}
        _rehydrate(self.pool, self.provided, related_evidence)
        previous = []
        context_budget = 0
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
                for lo, hi in self._cited_ranges(record, related_evidence):
                    size = sum(len(line) + 1 for line in record.content.splitlines()[lo - 1:hi])
                    if context_budget + size > self.config.read_chars:
                        break
                    try:
                        previous.append({**self.provide(record.source_id, lo, hi), "context_only": True,
                                         "context_reason": "cited_lines"})
                        context_budget += size
                    except FlowError:
                        pass
                continue
            if context_budget + len(record.content) > self.config.read_chars or len(record.content) > self.config.record_chars:
                continue
            try:
                if record.locator.get("preserved_only"):
                    for lo, hi in list(self.provided[record.source_id]):
                        previous.append({**self.provide(record.source_id, lo, hi), "context_only": True})
                else:
                    previous.append({**self.provide(record.source_id), "context_only": True,
                                     "context_reason": ("same_worktree_nearby_time" if record.source_id in cross_ids
                                                        else "related_context")})
                context_budget += len(record.content)
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
    def _cited_ranges(record: SourceRecord, evidence: dict[str, dict]) -> list[tuple[int, int]]:
        total = len(record.content.splitlines())
        spans = sorted((max(1, e["start_line"] - LEAN_CITED_LINES), min(total, e["end_line"] + LEAN_CITED_LINES))
                       for e in evidence.values()
                       if e["source_id"] == record.source_id and e["content_hash"] == record.content_hash)
        merged: list[list[int]] = []
        for lo, hi in spans:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        return [(lo, hi) for lo, hi in merged]

    def read(self, request: dict) -> list[dict]:
        required = {"kind", "ids", "start_line", "end_line", "query", "unit_id"}
        if set(request) != required:
            raise FlowError("an evidence request must carry every field of the schema's single object.")
        if request["kind"] == "search_events":
            if (request["ids"] != [] or not isinstance(request["query"], str) or
                    not request["query"].strip() or len(request["query"]) > 500 or
                    request["unit_id"] != self.unit_id or
                    request["start_line"] is not None or request["end_line"] is not None):
                return [{"denied": "search_events takes only query and the current unit_id; ids and the line range must be empty."}]
        else:
            if (request["kind"] not in {"read_records", "read_existing_event", "read_diff", "read_file_at_revision"} or
                    not isinstance(request["ids"], list) or not 1 <= len(request["ids"]) <= 4 or
                    request["query"] is not None or request["unit_id"] is not None):
                return [{"denied": "read request has an invalid kind or IDs, or sets a search-only field."}]
            start, end = request["start_line"], request["end_line"]
            if (start is None) != (end is None) or (start is not None and start > end):
                return [{"denied": "line range needs both ends or neither."}]
        if request["kind"] == "search_events":
            tokens = set(re.findall(r"[\w가-힣]{2,}", request["query"].casefold()))
            matches = []
            for event in self.graph["events"]:
                if event["id"] not in self.allowed_event_ids:
                    continue
                words = set(re.findall(r"[\w가-힣]{2,}",
                    (event.get("title", "") + " " + event.get("summary", "")).casefold()))
                overlap = tokens & words
                if overlap:
                    matches.append((len(overlap), event, overlap))
            matches.sort(key=lambda item: (item[0], item[1].get("recorded_at") or ""), reverse=True)
            return [{"id": event["id"], "title": event["title"],
                     "description": event.get("summary", "")[:240],
                     "reason": "query terms matched: " + ", ".join(sorted(overlap))}
                    for _, event, overlap in matches[:8]]
        outputs = []
        for value in request["ids"]:
            try:
                if request["kind"] == "read_existing_event":
                    if value not in self.allowed_event_ids:
                        raise FlowError("ID outside the allowed event manifest")
                    event = next(e for e in self.graph["events"] if e["id"] == value)
                    preserved = self.store.evidence_many(event["evidence_ids"])
                    _rehydrate(self.pool, self.provided, preserved)
                    snippets = []
                    for item in preserved.values():
                        sid = item["source_id"]
                        if sid in self.pool and self.pool[sid].content_hash == item["content_hash"]:
                            snippets.append({**self.provide(sid, item["start_line"], item["end_line"]), "context_only": True})
                        else:
                            # Original source changed: provide immutable saved excerpt under an explicit alias.
                            alias = "saved:" + item["id"]
                            source = item["source"]
                            self.pool[alias] = SourceRecord(alias, source["provider"], source.get("session_id"),
                                source["role"], item["quote"], {"kind": "preserved_excerpt", "original": source["locator"],
                                "original_source_id": sid, "original_hash": item["content_hash"]},
                                recorded_at=source.get("recorded_at"), cwd=source.get("cwd"),
                                worktree_id=source.get("worktree_id"), derivation=source.get("derivation", "original"),
                                lineage=source.get("lineage", {}))
                            snippets.append({**self.provide(alias), "context_only": True})
                    outputs.append({"event": event, "evidence_records": snippets})
                elif request["kind"] == "read_file_at_revision":
                    allowed_files = dict((i, v) for i, v in self.file_manifest.items()
                                         if i in self.allowed_file_ids)
                    allowed_files = dict(list(allowed_files.items())[:100])
                    if value not in allowed_files:
                        raise FlowError("ID outside the allowed revision/file manifest")
                    root, oid, filename = allowed_files[value]
                    content = git(Path(root), "show", f"{oid}:{filename}", cap=1_000_000)
                    if "\x00" in content:
                        raise FlowError("binary file content is not provided.")
                    self.pool[value] = SourceRecord(value, "git", None, "git", content,
                        {"kind": "file_at_revision", "root": root, "commit": oid, "file": filename}, git={"head": oid})
                    outputs.append(self.provide(value, request["start_line"], request["end_line"]))
                else:
                    if value not in self.allowed_record_ids:
                        raise FlowError("ID outside the allowed snapshot record manifest")
                    if request["kind"] == "read_diff" and self.pool[value].provider != "git":
                        raise FlowError("ID is not a diff")
                    outputs.append(self.provide(value, request["start_line"], request["end_line"]))
            except (FlowError, StopIteration, KeyError) as exc:
                outputs.append({"id": value, "denied": str(exc)[:300]})
        return outputs

    def restore_read_context(self, manifest: list[dict]) -> None:
        """Re-read only authorized immutable references, verifying the original reply digest."""
        for entry in manifest:
            replies = [{"request": request, "results": self.read(request)} for request in entry["requests"]]
            if digest(replies) != entry["reply_digest"]:
                raise FlowError(tr("저장된 추가 근거가 변경/소실되어 추출을 다시 수행해야 합니다.",
                                   "The saved extra evidence changed or is missing; the extraction must be redone."))
            self.read_history.append(replies)
            self.read_manifest.append(copy.deepcopy(entry))

    def task(self, stage: str, data: dict, checker: Callable[[dict], Any],
             *, validator: EvidenceValidator | None = None, schema: dict | None = None,
             merge: Callable[[dict], dict] | None = None) -> dict:
        """`merge` folds the model's answer (a review patch) into what `checker` sees; the call
        record and the repair round keep the answer the model actually wrote."""
        schema = schema or (EXTRACT_SCHEMA if stage == "extract" else delta_schema(self.config.integrate_evidence == "reuse"))
        task = _trace_operation(self.detailed_trace, f"build_{stage}_request",
                                {"stage": stage, "data": data},
                                lambda: build_task(stage, data, self.config.output_language))
        read_count, repair_count, attempt = 0, 0, 0
        prompt_hash = digest([task["system"], task["instructions"], task["wire_contract"]])
        schema_hash = digest(schema)
        aliases = IdAliases()
        while True:
            if self.cancel.is_set():
                raise Cancelled(tr("분석 중단", "Analysis stopped"))
            # The Runner sees short IDs; everything the host checks or stores uses the full ones.
            sent = aliases.wire(task)
            if len(dumps(sent)) > self.config.task_chars:
                raise FlowError(tr("분석 입력 예산을 초과했습니다. 이 단위는 처리 완료로 저장하지 않습니다.",
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
                "schema_hash": schema_hash,
                "input_digest": digest(sent),
                "input_chars": len(dumps(sent)),
                "read_round": read_count,
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
                        return self.runner.run(request, response_schema, self.cancel)

                    output = model_call(sent, schema)
                else:
                    output = self.runner.run(sent, schema, self.cancel)
                output = aliases.expand(output)
                if self.review_capture:
                    raw_output = copy.deepcopy(output)
            except BaseException as exc:
                finish_call("failed", {"error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - call_started) * 1000)})
                raise
            output_digest = digest(output)
            # Claim validation canonicalizes quotes in place (a few words become whole source
            # lines). A repair must see what the model wrote, not those expanded lines.
            submitted = copy.deepcopy(output)
            output_chars = len(dumps(submitted))
            quote_chars = output_quote_chars(submitted)
            usage = getattr(self.runner, "last_usage", None)
            # Requested IDs/aliases are not evidence of the provider's actual model.
            actual_model = getattr(self.runner, "last_model", None)
            response_finalized = False
            try:
                def check_contract() -> dict:
                    validate_shape(output, schema)
                    if output["snapshot_id"] != data["snapshot_id"] or (
                            stage == "extract" and output["unit_id"] != data["unit_id"]) or (
                            stage == "integrate" and output["base_graph_version"] != data["base_graph_version"]):
                        raise FlowError("reply unit/snapshot/graph base differs from the request.")
                    return {"schema": "passed", "snapshot_and_base_ids": "passed",
                            "response_status": output["status"]}
                _trace_operation(self.detailed_trace, f"validate_{stage}_contract",
                                 {"response": output, "schema": schema}, check_contract)
                if output["status"] == "needs_evidence":
                    finish_call("needs_evidence", {
                        "output_digest": output_digest, "output_chars": output_chars, "usage": usage, "actual_model": actual_model,
                        "duration_ms": round((time.monotonic() - call_started) * 1000),
                        "read_request_count": len(output.get("read_requests", [])),
                    }, submitted)
                    response_finalized = True
                    if read_count >= self.config.read_rounds or not output["read_requests"]:
                        raise FlowError("evidence request limit exceeded or empty request.")
                    if len(output["read_requests"]) > 3:
                        raise FlowError("at most 3 evidence requests per round.")
                    before_provided, before_dependencies = copy.deepcopy(self.provided), dict(self.dependencies)
                    before_pool = dict(self.pool)
                    try:
                        replies = _trace_operation(
                            self.detailed_trace, "read_requested_evidence",
                            {"requests": output["read_requests"]},
                            lambda: [{"request": request, "results": self.read(request)}
                                     for request in output["read_requests"]], run_type="tool")
                        if len(dumps(replies)) > self.config.read_chars * 2:
                            raise FlowError("evidence reply budget exceeded.")
                    except BaseException:
                        self.provided.clear(); self.provided.update(before_provided)
                        self.dependencies.clear(); self.dependencies.update(before_dependencies)
                        self.pool.clear(); self.pool.update(before_pool)
                        raise
                    task.setdefault("evidence_rounds", []).append(replies)
                    self.read_history.append(copy.deepcopy(replies))
                    self.read_manifest.append({"requests": copy.deepcopy(output["read_requests"]),
                                               "reply_digest": digest(replies)})
                    read_count += 1
                    continue
                if validator is not None:
                    validator.normalizations.clear()
                    validator.mismatches.clear()
                if merge is not None:
                    output = merge(output)
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
                    "read_rounds_used": read_count, "repair_rounds_used": repair_count,
                    "citation_normalizations": len(validator.normalizations) if validator else 0,
                    "citation_normalization_audit": copy.deepcopy(validator.normalizations) if validator else [],
                    "quote_mismatch_audit": validator.mismatch_audit() if validator else [],
                    "output_quote_chars": quote_chars,
                }, submitted)
                return output
            except FlowError as exc:
                # If needs_evidence was already finalized, retain that observable state; the
                # subsequent bounded-read failure is recorded by the next call/run error path.
                if not response_finalized:
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
                    exact_lines = []
                    for mismatch in list(re.finditer(
                            r"quote (?:differs from the frozen source|not found uniquely in the cited lines): "
                            r"([^:]+):(\d+)-(\d+)", str(exc)))[:8]:
                        source_id, start, end = mismatch.group(1), int(mismatch.group(2)), int(mismatch.group(3))
                        if source_id in self.pool and any(lo <= start and end <= hi
                                                          for lo, hi in self.provided.get(source_id, [])):
                            exact_lines.append(self.provide(source_id, start, end))
                    if exact_lines:
                        task["repair"]["exact_source_lines"] = exact_lines
                    nearest = []
                    for item in [m for m in (validator.mismatches if validator else [])
                                 if m["matches"] == 0][:8]:
                        nearest.extend(self.nearest_lines(item["source_id"], item["quote"]))
                    if nearest:
                        task["repair"]["nearest_lines"] = nearest
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


def unlinked_observed_outcomes(delta: dict | None) -> list[dict]:
    """New observed results that no relation in the same delta leads to.

    A new event can only be reached by a relation added with it, so an observed result
    without one says what ran but not what it checked.
    """
    if not delta:
        return []
    reached = {edge["to_event_id"] for edge in delta["edges_to_add"]}
    return [item for item in delta["events_to_add"] if item["kind"] == "outcome" and
            item["status"] in {"observed_success", "observed_failure"} and item["id"] not in reached]


def unlinked_revisions(delta: dict | None) -> list[dict]:
    """New revisions that no revises relation in the same delta leads to."""
    if not delta:
        return []
    revised = {edge["to_event_id"] for edge in delta["edges_to_add"] if edge["relation"] == "revises"}
    return [item for item in delta["events_to_add"]
            if item["kind"] == "revision" and item["id"] not in revised]


# (operation, signal, question). Quotes are already checked in code, so a new observed
# result alone is not flagged; what code cannot tell is whether a link is missing.
REVIEW_SIGNALS = (
    ("events_to_update", "existing_claim_or_status_update", "Is this a real status change, or the same content restated?"),
    ("edges_to_invalidate", "existing_relation_invalidation", "Does the source directly support invalidating the existing relation?"),
    ("events_to_add", "unlinked_observed_outcome",
     "If the change this run result checked is in the source, should it be linked with verifies? "
     "If not, is the reason in limitations?"),
    ("events_to_add", "unlinked_revision",
     "If the earlier event (attempt, change or result) this revision fixed is in the source, should it be linked "
     "with revises? If not, is the reason in limitations?"),
)


def review_signal_items(delta: dict | None) -> dict[str, list[dict]]:
    """Delta items each review signal points at; signals with nothing to check are left out."""
    if not delta:
        return {}
    found = {"existing_claim_or_status_update": delta["events_to_update"],
             "existing_relation_invalidation": delta["edges_to_invalidate"],
             "unlinked_observed_outcome": unlinked_observed_outcomes(delta),
             "unlinked_revision": unlinked_revisions(delta)}
    return {signal: items for signal, items in found.items() if items}


REVIEW_CHECKS = ("Compare the proposed GraphDelta with the supplied original evidence and relevant history. "
                "Check duplicate versus genuine retry, state overstatement, and unsupported relation "
                "invalidation. ")
# The default: the review rewrites the delta, so the request and its schema are the integration ones.
REVIEW_INSTRUCTION = REVIEW_CHECKS + ("Return the complete corrected GraphDelta and preserve one resolution "
                                     "for every candidate.")
# The opt-in: the review sends back only what it changes, and the code merges it into the proposal.
REVIEW_PATCH_INSTRUCTION = REVIEW_CHECKS + (
    "Return only what changes: patch holds the new or replaced items, never an unchanged one, and an item "
    "whose id is a proposed item's id replaces that item. List proposed items to drop in remove. Give "
    "candidate_resolutions only for candidates whose resolution changes, and change_attributions only for "
    "added or replaced items. Still preserve one review resolution for every issue.")

INTEGRATE_PATCH_INSTRUCTION = (
    "draft_graph_delta is the GraphDelta code built from the validated candidates: every candidate added "
    "as extracted, and a candidate matched to one existing event recorded as its duplicate. Do the "
    "integration task on it and return only what changes: patch holds the new or replaced items, never "
    "an unchanged one, and an item whose id is a draft item's id replaces that item. List draft items to "
    "drop in remove. Give candidate_resolutions only for candidates whose resolution changes, and "
    "change_attributions only for added or replaced items. review_resolutions stays empty. An empty patch "
    "and remove mean the draft stands as it is.")


@dataclass
class PreparedExtraction:
    harness: Harness
    validator: EvidenceValidator
    context: dict
    data: dict
    graph_version: int


@dataclass
class PreparedIntegration:
    harness: Harness
    validator: EvidenceValidator
    data: dict | None
    graph_version: int
    delta: dict | None = None
    review_audit: dict = dataclasses.field(default_factory=dict)
    review_issue_inputs: list[dict] = dataclasses.field(default_factory=list)


class Engine:
    def __init__(self, scope: Scope, store: Store, config: AnalysisConfig | None = None):
        self.scope, self.store = scope, store
        self.config = config or AnalysisConfig()
        self.config.validate()
        self.tracer: LangSmithTracer | None = None
        self.detailed_trace = False
        self.review_capture: Callable[[dict], None] | None = None

    def scan(self) -> Snapshot:
        proven = [Path(p) for p in self.store.get_meta("known_worktree_roots", [])]
        source_scope = dataclasses.replace(self.scope, roots=list(dict.fromkeys(self.scope.roots + proven)))
        logs = collect_logs(source_scope, codex_home=self.config.codex_home, claude_home=self.config.claude_home,
                            opencode_home=self.config.opencode_home)
        artifacts = collect_git(self.scope, history_limit=self.config.history_limit,
                                exclude=self.store.get_meta("exports", []))
        self.store.set_meta("known_worktree_roots", [str(p) for p in source_scope.roots])
        return Snapshot(logs.records + artifacts.records, list(dict.fromkeys(logs.limitations + artifacts.limitations)), logs.files)

    def resolve_language(self, snapshot: Snapshot) -> str:
        """The output language: as configured, else as saved for the project, else detected and saved."""
        if not self.config.output_language:
            saved = self.store.get_meta("output_language")
            if not saved:
                saved = detect_language(snapshot.records, os.environ.get("LC_ALL") or os.environ.get("LANG"))
                self.store.set_meta("output_language", saved)
            self.config.output_language = saved
        return self.config.output_language

    def preview_plan(self, snapshot: Snapshot) -> dict:
        """What `analyze` would plan now, without a model call (for `scan` and an agent's question)."""
        with self.store.analyze_lock():
            self.store.ingest(snapshot.records)
            self.store.acknowledge_environment_context(snapshot.records)
            units, _, _ = self._plan_units(snapshot, [])
        pool = {r.source_id: r for r in snapshot.records}
        limit = self.config.max_units
        plan = plan_summary(units, pool, call_cap(self.config, limit), limit=limit,
                            past=calibration(self.store.units(), self.store.llm_calls(), pool))
        return {**plan, "output_language": self.resolve_language(snapshot)}

    def _routing_signature(self) -> str:
        # the default mode adds nothing, so changing the default does not resend finished units
        return digest([ROUTING_VERSION, "validation-failure-only-v3", CONTEXT_POLICY_VERSION, EXTRACT_INPUT_VERSION,
                       prompt("common"), prompt("extract"), EXTRACT_SCHEMA,
                       self.config.extract_model, self.config.escalation_model,
                       self.config.runner_name, self.config.base_model, self.config.extract_effort,
                       self.config.output_language, *([self.config.context_mode] if self.config.context_mode != "lean" else [])])

    def _prepare_extraction(self, unit: dict, pool: dict[str, SourceRecord], graph: dict,
                            snapshot_id: str, run_id: str, runners: RunnerPool,
                            cancel: threading.Event, issues: list[str]) -> PreparedExtraction:
        assigned = [pool[i] for i in unit["sources"]]
        if any(len(r.content) > self.config.record_chars for r in assigned):
            raise FlowError(tr("이전 보류 단위가 현재 입력 한도를 초과합니다.",
                               "A previously deferred unit exceeds the current input limit."))
        h = Harness(None, pool, graph, self.store, self.config, cancel, run_id, unit["id"],
                    budget=runners.budget, tracer=self.tracer,
                    detailed_trace=self.detailed_trace, review_capture=self.review_capture)
        context = h.context(assigned)
        data, required = extract_request_data(unit, snapshot_id, assigned, h, context, issues)
        validator = EvidenceValidator(h.pool, h.provided, assigned_source_ids=set(unit["sources"]),
                                      required_citations=required)
        return PreparedExtraction(h, validator, context, data, graph["version"])

    def _extract_unit(self, unit: dict, pool: dict[str, SourceRecord], graph: dict,
                      snapshot_id: str, run_id: str, runners: RunnerPool,
                      cancel: threading.Event, issues: list[str], *, defer_escalation: bool = False,
                      prepared: PreparedExtraction | None = None) -> dict:
        """Extract one unit against an immutable batch-base graph.

        Workers may save their own extraction ledger; only the caller publishes graphs.
        Validated extractions are reused if integration or post-delta review later fails.
        """
        if cancel.is_set():
            raise Cancelled(tr("분석 중단", "Analysis stopped"))
        prepared = prepared or self._prepare_extraction(unit, pool, graph, snapshot_id, run_id,
                                                         runners, cancel, issues)
        if prepared.graph_version != graph["version"] or prepared.data["unit_id"] != unit["id"] or (
                prepared.data["snapshot_id"] != snapshot_id):
            raise FlowError(tr("준비된 추출 입력의 작업·snapshot·graph 기준이 달라졌습니다.",
                               "The prepared extract input's unit, snapshot or graph base has changed."))
        h, validator, context, data = (prepared.harness, prepared.validator,
                                       prepared.context, prepared.data)
        signature = self._routing_signature()
        cached = unit.get("result") or {}
        current_context_digest = digest(context)
        reuse = bool(unit["status"] in {"extracted", "draft"} and
                     cached.get("routing_signature") == signature and
                     cached.get("context_digest") == current_context_digest)
        reasons: list[str] = []
        draft = None
        output = None
        waived: list[str] = list(cached.get("waived_citations", [])) if reuse else []
        dropped: list[str] = list(cached.get("dropped_candidates", [])) if reuse else []
        if reuse:
            if unit["status"] == "draft":
                try:
                    h.restore_read_context(cached.get("read_manifest", []))
                except FlowError:
                    return self._extract_unit({**unit, "status": "invalidated", "result": None},
                        pool, graph, snapshot_id, run_id, runners, cancel, issues)
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
            if unit["status"] == "extracted":
                return {"output": output, "evidence": validator.evidence, "dependencies": h.dependencies,
                        "cached": {**cached, "payload": output}, "reused": True}
            draft = output
            reasons = list(cached.get("routing_reasons", []))
        else:
            self.store.save_unit(unit["id"], unit["sources"], h.dependencies, "parsed")
            h.runner, h.routing_role = runners.get("extract"), "extract"
            try:
                output = h.task("extract", data,
                    lambda o: validator.check_extraction(o, unit["id"], snapshot_id, graph), validator=validator)
                draft = output
                # Semantics are reviewed against the proposed GraphDelta. Extraction
                # calls route to a second model only after bounded contract failure.
                reasons = []
            except TaskValidationError as exc:
                if not self.config.escalation_model:
                    output, waived, dropped = self._salvage_extraction(exc, validator, unit["id"], snapshot_id, graph)
                else:
                    reasons = ["bounded_validation_failure"]
                    data["previous_validation_error"] = str(exc)[:400]
        should_escalate = bool(self.config.escalation_model and reasons)
        if should_escalate:
            if draft is not None:
                saved_draft = {"payload": draft, "evidence": validator.evidence,
                    "routing_signature": signature, "routing_reasons": reasons,
                    "read_manifest": h.read_manifest, "context_digest": current_context_digest,
                    "context_selection": context.get("context_selection", {})}
                self.store.save_unit(unit["id"], unit["sources"], h.dependencies, "draft", saved_draft)
                if defer_escalation:
                    return {"output": draft, "evidence": validator.evidence,
                            "dependencies": h.dependencies, "cached": saved_draft,
                            "reused": reuse, "escalation_pending": True}
            h.runner, h.routing_role, h.routing_reasons = runners.get("escalation"), "escalation", reasons
            reviewed_data = {**data, "review_trigger": reasons,
                             "review_instruction": "Re-extract the candidates against the source. The draft below is not the answer. "
                                                   "Check an empty result for omissions too.",
                             "draft_candidates": draft,
                             "inherited_evidence_rounds": copy.deepcopy(h.read_history)}
            try:
                output = h.task("extract", reviewed_data,
                    lambda o: validator.check_extraction(o, unit["id"], snapshot_id, graph), validator=validator)
            except TaskValidationError as exc:
                output, waived, dropped = self._salvage_extraction(exc, validator, unit["id"], snapshot_id, graph)
        if output is None:
            raise FlowError(tr("검증된 추출 결과가 없습니다.", "No validated extraction result."))
        requests_added = add_user_requests(output, [pool[i] for i in unit["sources"]], validator)
        cached = {"payload": output, "evidence": validator.evidence, "routing_signature": signature,
                  "user_requests_added": requests_added, "waived_citations": waived,
                  "dropped_candidates": dropped,
                  "routing_reasons": reasons, "escalated": should_escalate,
                  "draft_payload": draft if should_escalate else None,
                  "extraction_graph_version": graph["version"], "read_manifest": h.read_manifest,
                  "context_selection": context.get("context_selection", {}),
                  "context_digest": current_context_digest}
        # Reuse is decided above from `routing_signature` and `context_digest`,
        # both already inside `cached`; the vestigial cache_key column is not used.
        self.store.save_unit(unit["id"], unit["sources"], h.dependencies, "extracted", cached)
        return {"output": output, "evidence": validator.evidence, "dependencies": h.dependencies,
                "cached": cached, "reused": reuse}

    @staticmethod
    def _salvage_extraction(error: TaskValidationError, validator: EvidenceValidator, unit_id: str,
                            snapshot_id: str, graph: dict) -> tuple[dict, list[str], list[str]]:
        """Keep the candidates that pass after the repair round failed, or fail the unit as before."""
        if error.output is None:
            raise error
        try:
            return validator.salvage_extraction(error.output, unit_id, snapshot_id, graph)
        except FlowError:
            raise error from None

    def _prepare_integration(self, unit: dict, extracted: dict, pool: dict[str, SourceRecord],
                             graph: dict, snapshot_id: str, run_id: str, runners: RunnerPool,
                             cancel: threading.Event) -> PreparedIntegration:
        """Build the exact integration request against the latest graph."""
        output = extracted["output"]
        assigned = [pool[i] for i in unit["sources"]]
        h = Harness(None, pool, graph, self.store, self.config, cancel, run_id, unit["id"], budget=runners.budget,
                    routing_role="integrate", tracer=self.tracer,
                    detailed_trace=self.detailed_trace, review_capture=self.review_capture)
        clues = "\n".join([*(item["title"] + " " + item["summary"]
                              for item in output["event_candidates"]),
                           *(item["text"] for item in output["open_items"]),
                           *(item["existing_event_id"] for item in output["existing_event_matches"])])
        context = h.context(assigned, clues=clues)
        _rehydrate(h.pool, h.provided, extracted["evidence"])
        h.dependencies.update(extracted["dependencies"])
        # Only exact quotes that are actually included in candidate_evidence count
        # as visible to the integrator. Other raw records require a ReadRequest.
        for item in extracted["evidence"].values():
            h.provided.setdefault(item["source_id"], []).append((item["start_line"], item["end_line"]))
        validator = EvidenceValidator(h.pool, h.provided, assigned_source_ids=set(unit["sources"]))
        validator.inherit_focus(extracted["evidence"])
        validator.check_extraction(output, unit["id"], snapshot_id, graph)
        if any(output[k] for k in ("event_candidates", "edge_candidates", "existing_event_matches", "open_items")):
            data = {"base_graph_version": graph["version"], "snapshot_id": snapshot_id,
                    "validated_candidates": output, "candidate_evidence": _prompt_evidence(extracted["evidence"]),
                    "assigned_source_ids": unit["sources"], **model_context(context)}
            if self.config.integrate_evidence == "reuse":
                data["evidence_policy"] = EVIDENCE_POLICY_REUSE
        else:
            data = None
        return PreparedIntegration(h, validator, data, graph["version"])

    def _integrate_unit(self, unit: dict, extracted: dict, pool: dict[str, SourceRecord],
                        graph: dict, snapshot_id: str, run_id: str, runners: RunnerPool,
                        cancel: threading.Event, *,
                        prepared: PreparedIntegration | None = None) -> tuple[dict, dict, dict]:
        """Serialize integration against the *latest* graph; do not resend all raw text."""
        prepared = prepared or self._prepare_integration(unit, extracted, pool, graph,
                                                          snapshot_id, run_id, runners, cancel)
        if prepared.graph_version != graph["version"] or (prepared.data is not None and
                prepared.data["snapshot_id"] != snapshot_id):
            raise FlowError(tr("준비된 통합 입력의 snapshot 또는 graph 기준이 달라졌습니다.",
                               "The prepared integrate input's snapshot or graph base has changed."))
        h, validator, data = prepared.harness, prepared.validator, prepared.data
        if data is not None:
            candidate_set = data["validated_candidates"]
            reuse = self.config.integrate_evidence == "reuse"
            check = lambda o: validator.apply_delta(o, graph, snapshot_id, run_id, candidate_set,
                                                    evidence_reuse=reuse)
            mode = self.config.integrate_output
            draft = draft_delta(candidate_set, graph["version"], snapshot_id) if mode != "full" else None
            published = False
            if mode == "draft" and not graph["events"]:
                # Nothing to join yet: the draft is the delta, under the same checks.
                try:
                    check(copy.deepcopy(draft))
                    delta, published = draft, True
                    validator.normalizations.append({"mode": "integrate_draft_published",
                                                     "items": len(draft["change_attributions"])})
                except FlowError:
                    pass  # the integrator gets the draft to patch instead
            if published:
                pass
            elif draft is not None:
                h.runner = runners.get("integrate")
                delta = h.task("integrate", {**data, "draft_graph_delta": draft,
                                             "integrate_instruction": INTEGRATE_PATCH_INSTRUCTION},
                               check, validator=validator, schema=review_patch_schema(reuse),
                               merge=lambda answer: self._merge_review_patch(
                                   answer, draft, validator, candidate_set, mode="integrate_patch_merged"))
            else:
                h.runner = runners.get("integrate")
                delta = h.task("integrate", data, check, validator=validator)
            prepared.delta = delta
            new_graph = validator.apply_delta(delta, graph, snapshot_id, run_id, candidate_set,
                                             evidence_reuse=reuse)
            # What code dropped from the extraction is said whatever the integrator wrote.
            new_graph["limitations"] = list(dict.fromkeys(new_graph["limitations"] +
                                                         extracted["cached"].get("dropped_candidates", [])))
        else:
            new_graph = copy.deepcopy(graph)
            new_graph["limitations"] = list(dict.fromkeys(new_graph["limitations"] +
                                                         extracted["output"]["limitations"]))
            new_graph["source_snapshot_id"] = snapshot_id
        new_graph["semantic_review_audit"] = prepared.review_audit or {"status": "pending_route"}
        return new_graph, {**extracted["evidence"], **validator.evidence}, h.dependencies

    def _review_delta(self, prepared: PreparedIntegration, graph: dict, snapshot_id: str,
                      run_id: str, runners: RunnerPool) -> dict:
        """Optionally review a risky, structurally valid proposed delta."""
        delta, data = prepared.delta, prepared.data
        reasons = list(review_signal_items(delta))
        issues = list(prepared.review_issue_inputs)
        executed = bool(issues and self.config.semantic_review and data is not None)
        if executed:
            harness, validator = prepared.harness, prepared.validator
            harness.runner, harness.routing_role, harness.routing_reasons = runners.get("escalation"), "integrate_review", reasons
            candidate_set = data["validated_candidates"]
            proposal, patch_mode = delta, self.config.review_output == "patch"
            review_data = {**data, "review_trigger": reasons, "review_issues": issues,
                "proposed_graph_delta": proposal,
                "review_instruction": REVIEW_PATCH_INSTRUCTION if patch_mode else REVIEW_INSTRUCTION}
            def check_review(output: dict) -> None:
                validator.apply_delta(output, graph, snapshot_id, run_id, candidate_set,
                                      expected_review_issues=issues,
                                      evidence_reuse=self.config.integrate_evidence == "reuse")
            if patch_mode:
                delta = harness.task("integrate", review_data, check_review, validator=validator,
                                     schema=review_patch_schema(self.config.integrate_evidence == "reuse"),
                                     merge=lambda answer: self._merge_review_patch(answer, proposal, validator, candidate_set))
            else:
                delta = harness.task("integrate", review_data, check_review, validator=validator)
            prepared.delta = delta
        resolutions = delta.get("review_resolutions", []) if executed else []
        if executed:
            resolutions = [{**item, "evidence_ids": validator.citations(item["evidence"]), "evidence": None}
                           for item in resolutions]
        unresolved_ids = ([item["issue_id"] for item in resolutions if item["status"] == "unresolved"]
                          if executed else [issue["id"] for issue in issues])
        prepared.review_audit = {"triggered": reasons, "executed": executed,
            "status": ("reviewed_with_unresolved" if unresolved_ids else "reviewed") if executed else
                      "skipped_disabled" if issues else "not_needed",
            "issues": issues, "resolutions": resolutions, "unresolved_issue_ids": unresolved_ids}
        result = (prepared.validator.apply_delta(prepared.delta, graph, snapshot_id, run_id,
                   data["validated_candidates"], expected_review_issues=issues if executed else None,
                   evidence_reuse=self.config.integrate_evidence == "reuse")
                  if data else copy.deepcopy(graph))
        result["semantic_review_audit"] = prepared.review_audit
        result["semantic_review_history"] = [*graph.get("semantic_review_history", []),
            {"unit_id": data["validated_candidates"]["unit_id"] if data else None,
             **prepared.review_audit}]
        return result

    @staticmethod
    def _merge_review_patch(answer: dict, proposal: dict, validator: EvidenceValidator,
                            candidates: dict | None = None, mode: str = "review_patch_merged") -> dict:
        """What the checker sees in patch mode: the proposal with the review's changes folded in."""
        merged = merge_review_patch(proposal, answer)
        settled = reconcile_review_patch(merged, proposal, candidates)
        validator.normalizations.append({"mode": mode, **review_patch_audit(proposal, answer),
                                         **{k: v for k, v in settled.items() if v}})
        return merged

    def _plan_units(self, snapshot: Snapshot, issues: list[str], *,
                    repair: bool = False) -> tuple[list[dict], set[str], list[str]]:
        """Plan changed source units after the snapshot has been ingested.

        `repair` lets an analysis run (under the analysis lock) correct the processed ledger;
        previews only read."""
        pool = {r.source_id: r for r in snapshot.records}
        source_rows = self.store.sources()
        missing = [i for i, row in source_rows.items() if not row["available"]]
        if missing:
            issues.append(f"{len(missing)} sources are missing from the current snapshot. Preserved evidence is kept; no fact is retracted.")
        pending = {i for i, row in source_rows.items()
                   if i in pool and row["processed_hash"] != pool[i].content_hash}
        plans, scheduled, settled, regrouped = [], set(), {}, {}
        for unit in self.store.units():
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
        family = session_family(snapshot.records, self.config.session) if self.config.session else None

        def in_scope(unit: dict) -> bool:
            return family is None or all(pool[i].session_id in family for i in unit["sources"])

        replaced = [unit for uid, unit in regrouped.items()
                    if uid not in {p["id"] for p in plans} and in_scope(unit)]
        if replaced:
            issues.append(f"regrouped {len(replaced)} stored unprocessed work units under the current rule.")
            if repair:
                for unit in replaced:
                    self.store.save_unit(unit["id"], unit["sources"], unit["dependencies"], "superseded")
        if family is not None:
            # Out of the oldest-first order on request: this session's units only, the rest wait.
            plans = [unit for unit in plans if in_scope(unit)]
        return plans, pending, missing

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
