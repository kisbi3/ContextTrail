"""`contexttrail note`: the agent that did the work writes one event into the graph itself.

No model is called. The agent gives the event's kind, status, title and summary and quotes what it
saw (a test's output, the edit it made, the person's words); code finds each quote in the current
session's own records, so the agent never names line numbers, and the event then goes through the
same path an analysis takes: candidate check (`EvidenceValidator.check_extraction`), the code-built
delta (`draft_delta`), `apply_delta` with every evidence, status and relation rule, the dialog-turn
links (`link_request_turns`) and one `publish`. A note that fails any check stores nothing and
says why in one message the agent can act on.

Only the current session is read (one transcript file, or one opencode session), so a note takes
about as long as parsing that file; the records are stored with `Store.ingest(partial=True)`.
The note's own command line (and its output) is never a quote's source: it holds every quote too.
"""
from __future__ import annotations

import dataclasses
import fcntl
import difflib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .analysis import (_GIT_CALL, _HEREDOC, _VCS_SUBCOMMANDS, _command_segments, _command_text, _vcs_mutates,
                       link_request_turns, step_hint)
from .git_context import Scope
from .i18n import tr
from .model import SourceRecord, Snapshot
from .schema import SHORT_QUOTE_CHARS, STATUSES, EvidenceValidator, draft_delta, occurrences, resolve_quote
from .sources.local import jsonl_files, parse_claude, parse_codex, source_homes
from .sources.opencode import opencode_databases, parse_opencode
from .store import Store, in_journal
from .util import FlowError, ident, now, private_dir, safe_text

# A person's request is made into an event by code from the transcript (`link_request_turns`), so the
# agent notes what it decided, proposed, changed and observed.
NOTE_KINDS = ("decision", "proposal", "goal", "action", "revision", "outcome")
DEFAULT_STATUS = {"decision": "adopted", "proposal": "proposed", "goal": "adopted",
                  "action": "applied", "revision": "applied"}
# Each relation runs from the named, earlier event to the note (relations go from earlier to later):
# the note verifies that change, revises that event, answers that question, or was motivated by it.
RELATION_OPTIONS = ("verifies", "revises", "answers", "motivates")
# Which variable names the session running this command. A host's variable can be inherited by a tool
# started from it (Claude Code started from a Codex shell sees CODEX_THREAD_ID; an opencode server started
# from Claude Code sees CLAUDE_CODE_SESSION_ID), so the most specific one wins: OPENCODE_SESSION_ID is put
# into each tool call's shell by ContextTrail's own opencode plugin, then Claude Code's when its marker
# CLAUDECODE is set, else whichever single one is present.
SESSION_VARIABLES = (("OPENCODE_SESSION_ID", None), ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE"), ("CODEX_THREAD_ID", None))
# A shell command that runs `note`, directly, through `$(…)`, an interpreter or a variable holding the program.
# A call that only mentions it (a file it writes, a grep) stays quotable.
NOTE_COMMAND = re.compile(r"^(?:\S*/)?(?:contexttrail|ct|project|\$\{?\w+\}?)\s+note\b")
_PIECES = re.compile(r"&&|\|\||[;|\n]|\$\(|`")
_PREFIXES = re.compile(r"^(?:\(\s*|[A-Za-z_]\w*=\S*\s+|(?:\S*/)?python3?\s+-m\s+)+")
ORIGIN = "note"
# A sandbox that cannot write the project's state (Codex's workspace-write keeps `.git` read-only, and the state
# lives in `.git/contexttrail`) gets its notes queued in the per-user temp directory, which it may write; the
# end-of-turn hook, which runs outside the sandbox, stores them with every check.
QUEUED_PREFIX = "q_"
QUEUE_DIR = "contexttrail-notes"
NEAREST = 3
NEAREST_CHARS = 200
NEAREST_RECORDS = 400
SETTINGS_KEY = "journal"            # {"enabled_at": …} when the person turned notes on for this project
ACTIVITY_KEY = "journal_activity"   # per session: when it last noted and when the hook last reminded it
# Work that is worth a note when a turn ends without one: a file edit, a commit, a test, a change to Git.
WORK_HINTS = {"edit", "commit", "test", "vcs"}
REFUSED_REASON = ("ContextTrail stored the notes queued in this turn except these, which failed its checks; write them "
                  "again with `contexttrail note`, fixed as the reason says, or leave them out:")
REMINDER = ("ContextTrail notes are on for this project, and the work since the last note changed files or ran checks "
            "without a note. Before finishing, use the ContextTrail note skill (contexttrail-note, or contexttrail:note from "
            "the plugin): record the decisions, changes and observed results since the last note with `contexttrail note` "
            "(one call per event, quoting the tool output or message exactly). If none of it is worth recording, just finish.")
# The reminder names what it is about: up to this many calls (kind and a short target, never the output), so the
# agent sees which edits and checks have no note instead of searching its turn for them.
REMINDER_CALLS = 5
REMINDER_CHARS = 80


def current_session(environ: dict[str, str] | None = None) -> str:
    """The session running this command, from the host tool's environment."""
    environ = os.environ if environ is None else environ
    found = {name: environ[name] for name, _ in SESSION_VARIABLES if environ.get(name)}
    if "OPENCODE_SESSION_ID" in found:
        return found["OPENCODE_SESSION_ID"]
    for name, marker in SESSION_VARIABLES:
        if marker and environ.get(marker) and name in found:
            return found[name]
    if len(found) == 1:
        return next(iter(found.values()))
    raise FlowError(tr("지금 대화 중인 세션을 알 수 없습니다", "Cannot tell which session is running this command")
                    + (tr(" (여러 도구의 세션이 보입니다: ", " (sessions of several tools are visible: ") + ", ".join(sorted(found)) + ")"
                       if found else "")
                    + tr(". --session에 세션 ID를 지정하세요.", ". Give --session a session ID."))


# The variable each host sets for its own shell commands. One started from another's shell inherits the outer
# one's too (a `codex exec` run by Claude Code sees CLAUDECODE and CLAUDE_CODE_SESSION_ID), so the environment
# alone cannot tell which is running this command; the nearest host process can.
TOOL_VARIABLES = {"opencode": "OPENCODE_SESSION_ID", "claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID"}
PACKAGE_TOOLS = {"@anthropic-ai/claude-code": "claude", "@openai/codex": "codex", "opencode-ai": "opencode"}
ANCESTORS = 16


def _tool_of(command: str) -> str | None:
    for word in command.split()[:2]:
        if Path(word).name in TOOL_VARIABLES:
            return Path(word).name
        found = next((tool for package, tool in PACKAGE_TOOLS.items() if package in word), None)
        if found:
            return found
    return None


def host_tool(pid: int | None = None) -> str | None:
    """The nearest ancestor process that is Codex, Claude Code or opencode; None when it cannot be told (a
    sandbox that hides other processes)."""
    pid = os.getppid() if pid is None else pid
    for _ in range(ANCESTORS):
        if pid <= 1:
            return None
        try:
            line = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=2, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        parent, _, command = line.partition(" ")
        if not parent.isdigit():
            return None
        tool = _tool_of(command.strip())
        if tool:
            return tool
        pid = int(parent)
    return None


def session_candidates(environ: dict[str, str] | None = None, tool: str | None = None) -> list[str]:
    """Every session the environment names, the most specific first: the nearest host's own, then the order
    of SESSION_VARIABLES."""
    environ = os.environ if environ is None else environ
    own = [environ[TOOL_VARIABLES[tool]]] if tool and environ.get(TOOL_VARIABLES[tool]) else []
    try:
        first = [current_session(environ)]
    except FlowError:
        first = []
    return list(dict.fromkeys(own + first + [environ[name] for name, _ in SESSION_VARIABLES if environ.get(name)]))


def current_in_project(scope: Scope, store: Store | None, environ: dict[str, str] | None = None,
                       quotes: list[str] | None = None, tool: str | None = None) -> str:
    """The session running this command, among those the environment names. The nearest host process names
    it when it can be found; otherwise, of the sessions with records in this project, the first whose records
    hold every quote, else the first with records. An inherited variable names an outer session, which may
    be in this project too (Claude Code running `codex exec` here); with none in the project, the most
    specific one is returned."""
    tool = host_tool() if tool is None and environ is None else tool
    candidates = session_candidates(environ, tool)
    if not candidates:
        return current_session(environ)  # raises the usual message
    if tool and (environ if environ is not None else os.environ).get(TOOL_VARIABLES[tool]):
        return candidates[0]
    found = [(candidate, records) for candidate in candidates if (records := session_records(scope, store, candidate))]
    for candidate, records in found:
        try:
            for quote in quotes or []:
                locate(quotable(records), quote)
        except FlowError:
            continue
        return candidate
    return found[0][0] if found else candidates[0]


class ReadOnlyMeta:
    """The project's saved settings read without writing anything (inside a sandbox); empty when unreadable."""

    def __init__(self, scope: Scope):
        self.path = scope.state_dir / "state.sqlite"

    def get_meta(self, key: str, default: Any = None) -> Any:
        if not self.path.is_file():
            return default
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as db:
                row = db.execute("SELECT value FROM project_meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        return json.loads(row[0]) if row else default


def _homes(store: Store | ReadOnlyMeta) -> dict[str, Path]:
    options = store.get_meta("options", {}) or {}
    return {key: Path(options[key]) for key in ("codex_home", "claude_home", "opencode_home") if options.get(key)}


def session_records(scope: Scope, store: Store | None, session_id: str, *, codex_home: Path | None = None,
                    claude_home: Path | None = None, opencode_home: Path | None = None) -> list[SourceRecord]:
    """The records of one session that belong to this project, in recorded order.

    Only transcript files whose name carries the session ID are parsed (a Claude Code transcript is
    `<id>.jsonl`, a Codex rollout ends with the ID), and only that session of an opencode database.
    """
    meta = store if store is not None else ReadOnlyMeta(scope)
    saved = _homes(meta)
    codex_home, claude_home, opencode_home = source_homes(codex_home or saved.get("codex_home"),
                                                          claude_home or saved.get("claude_home"),
                                                          opencode_home or saved.get("opencode_home"))
    proven = [Path(p) for p in meta.get_meta("known_worktree_roots", []) or []]
    scope = dataclasses.replace(scope, roots=list(dict.fromkeys(scope.roots + proven)))
    found: dict[str, SourceRecord] = {}
    for path, provider in jsonl_files(codex_home, claude_home):
        if session_id not in path.name or "subagents" in path.parts:
            continue
        snapshot = parse_claude(path, scope) if provider == "claude" else parse_codex(path, scope)
        found.update((r.source_id, r) for r in snapshot.records if r.session_id == session_id)
    if not found:
        for path in opencode_databases(opencode_home):
            snapshot = parse_opencode(path, scope, session=session_id)
            found.update((r.source_id, r) for r in snapshot.records if r.session_id == session_id)
    return sorted(found.values(), key=lambda r: (r.recorded_at or "", r.locator.get("line", 0),
                                                 r.locator.get("fragment_index", 0), r.source_id))


def runs_note(record: SourceRecord) -> bool:
    """A tool call whose shell command runs `contexttrail note`."""
    if record.role != "tool_call":
        return False
    text = _HEREDOC.split(_command_text(record.content.partition("\n")[2], "\n"))[0]
    return any(NOTE_COMMAND.match(_PREFIXES.sub("", piece.strip())) for piece in _PIECES.split(text))


def quotable(records: list[SourceRecord]) -> list[SourceRecord]:
    """The records a quote may come from: all but the `note` calls themselves and their output."""
    calls = {r.tool_call_id for r in records if r.tool_call_id and runs_note(r)}
    return [r for r in records if not runs_note(r) and not (r.role == "tool_result" and r.tool_call_id in calls)]


def locate(records: list[SourceRecord], quote: str) -> dict[str, Any]:
    """The citation of a quote: the most recent record holding it, at the lines that hold it."""
    if len(quote.strip()) < SHORT_QUOTE_CHARS:
        # A short output (`5`, `ok`) is quotable as a whole line: the most recent tool result that has it.
        for record in reversed(records):
            lines = record.content.splitlines()
            if record.role == "tool_result" and quote.strip() and quote.strip() in (line.strip() for line in lines):
                number = max(n for n, line in enumerate(lines, 1) if line.strip() == quote.strip())
                return {"source_id": record.source_id, "start_line": number, "end_line": number, "quote": lines[number - 1]}
        raise FlowError(f"quote too short to find: {quote!r} (quote at least {SHORT_QUOTE_CHARS} characters, or a whole "
                        "line of a tool's output, exactly as shown in this session)")
    for record in reversed(records):
        text = record.content
        found = occurrences(text, quote)
        if found:
            # The last copy in the record; the check expands it to whole lines with the quote as focus.
            start = text.count("\n", 0, found[-1]) + 1
            end = start + quote.count("\n")
            return {"source_id": record.source_id, "start_line": start, "end_line": end, "quote": quote}
        resolved = resolve_quote(text, quote, close_repeats=True)
        if resolved[0] is not None:
            _, first, last, _ = resolved
            return {"source_id": record.source_id, "start_line": first + 1, "end_line": last + 1, "quote": quote}
    hints = nearest(records, quote)
    shown = "; ".join(repr(line) for line in hints)
    raise FlowError(f"quote not found in this session's records: {quote[:120]!r}"
                    + (f". Closest lines: {shown}" if shown else "")
                    + ". Copy the text exactly as the tool output or message shows it.")


def nearest(records: list[SourceRecord], quote: str, limit: int = NEAREST) -> list[str]:
    """Lines of the latest records closest to a quote that was not found, cut around the best match."""
    lines = [line for record in records[-NEAREST_RECORDS:] for line in record.content.splitlines() if line.strip()]
    coarse = sorted(lines, key=lambda line: -difflib.SequenceMatcher(None, quote, line, autojunk=False).quick_ratio())[:40]
    ranked = sorted(coarse, key=lambda line: -difflib.SequenceMatcher(None, quote, line, autojunk=False).ratio())[:limit]
    result = []
    for line in ranked:
        if len(line) > NEAREST_CHARS:
            best = difflib.SequenceMatcher(None, quote, line, autojunk=False).find_longest_match(0, len(quote), 0, len(line)).b
            begin = max(0, min(best - NEAREST_CHARS // 2, len(line) - NEAREST_CHARS))
            line = line[begin:begin + NEAREST_CHARS]
        result.append(line)
    return result


def event_id_of(value: str, graph: dict, aliases: dict[str, str] | None = None) -> str:
    """An event named by its ID, an ID prefix, a copied reference (`contexttrail:ev_…@v12`), or the `q_…` ID of
    a queued note already stored (`aliases`)."""
    text = value.strip()
    if aliases and text in aliases:
        return aliases[text]
    if text.startswith(QUEUED_PREFIX):
        raise FlowError(f"queued note {text} was not stored (see the reason given for it), so nothing can link to it")
    if text.startswith("contexttrail:"):
        text = text[len("contexttrail:"):]
    text = text.split("@", 1)[0]
    matches = [event["id"] for event in graph["events"] if event["id"] == text] or \
              [event["id"] for event in graph["events"] if event["id"].startswith(text)]
    if len(matches) != 1:
        raise FlowError(f"no single event with ID {value!r} in the graph ({len(matches)} matches); "
                        "use an ID from `contexttrail note --list` or `contexttrail find`")
    return matches[0]


def prepare(scope: Scope, store: Store | None, session_id: str, *, kind: str, quotes: list[str],
            status: str | None, relations: dict[str, list[str]] | None = None,
            **homes: Path | None) -> tuple[list[SourceRecord], list[dict], str]:
    """The checks that need only the transcript: kind, status, and each quote found in the session."""
    if kind not in NOTE_KINDS:
        raise FlowError(f"note kind must be one of {', '.join(NOTE_KINDS)} (a person's request is added by code)")
    status = status or DEFAULT_STATUS.get(kind)
    if status is None:
        raise FlowError("an outcome needs --status: observed_success/observed_failure when a tool result is quoted, "
                        "else reported_complete/reported_failure")
    if status not in STATUSES:
        raise FlowError(f"unknown status {status!r}; one of {', '.join(STATUSES)}")
    if not quotes:
        raise FlowError("a note needs at least one --quote from this session's records")
    observed = status in ("observed_success", "observed_failure")
    linked = {name for name, values in (relations or {}).items() if values}
    if "verifies" in linked and not (kind == "outcome" and observed):
        raise FlowError("--verifies links a change to the observed result of a run that checked it: the note must be "
                        "--kind outcome with observed_success/observed_failure, quoting the tool's output. A reported "
                        "result stays unlinked; drop --verifies. Nothing was stored.")
    if "answers" in linked and kind == "goal":
        raise FlowError("--answers needs a note that answers the question (a decision, change or result). Nothing was stored.")
    records = session_records(scope, store, session_id, **homes)
    if not records:
        raise FlowError(f"no records of session {session_id} in this project yet "
                        "(the session must run in this folder and have written its transcript). If that is not "
                        "your session, give your own session ID with --session")
    sources = quotable(records)
    # An observed result rests on what the tool printed and an applied change on the call that made it; the
    # agent often repeats that output or code in its own message afterwards, so the tool's records come first.
    roles = {"tool_result"} if observed else {"tool_call", "tool_result"} if status == "applied" else set()
    first = [record for record in sources if record.role in roles]
    citations = []
    for quote in quotes:
        try:
            citations.append(locate(first, quote) if first else locate(sources, quote))
        except FlowError:
            citations.append(locate(sources, quote))
    pool = {record.source_id: record for record in records}
    cited = [pool[c["source_id"]] for c in citations]
    if status in ("observed_success", "observed_failure") and not any(r.role == "tool_result" for r in cited):
        found = ", ".join(sorted({r.role.replace("_", " ") for r in cited}))
        raise FlowError(f"{status} needs a quote of a tool's output (its result), but the quotes were found in: {found}. "
                        "Quote a line the command printed, or use reported_complete/reported_failure. Nothing was stored.")
    return records, citations, status


def write(scope: Scope, store: Store, session_id: str, *, kind: str, title: str, summary: str = "",
          quotes: list[str], status: str | None = None, actor: str = "assistant",
          relations: dict[str, list[str]] | None = None, aliases: dict[str, str] | None = None,
          **homes: Path | None) -> dict[str, Any]:
    """Check one note against the session's records and publish it; nothing is stored when a check fails."""
    if not enabled(store):
        raise FlowError("notes are off for this project; the person turns them on with `contexttrail note --enable` "
                        "in the project folder. Nothing was stored.")
    records, citations, status = prepare(scope, store, session_id, kind=kind, quotes=quotes, status=status,
                                         relations=relations, **homes)
    pool = {record.source_id: record for record in records}
    cited = [pool[c["source_id"]] for c in citations]
    tool = any(record.role in {"tool_call", "tool_result"} for record in cited)
    times = sorted(record.recorded_at for record in cited if record.recorded_at)
    try:
        with store.analyze_lock():
            # Notes account for the session from when they were turned on, or from the oldest record a note
            # cites if that is earlier; older records of the session stay with analysis (`Store.in_journal`).
            known = store.journal_since()
            since = min([t for t in [known, _seconds(times[0]) if times else None] if t is not None], default=None)
            if since is not None and since != known:
                stamp = datetime.fromtimestamp(since, timezone.utc).isoformat().replace("+00:00", "Z")
                store.set_meta(SETTINGS_KEY, {**(store.get_meta(SETTINGS_KEY) or {}), "since": stamp})
            ours = {record.source_id for record in records
                    if in_journal(record.session_id, record.lineage, record.recorded_at, {session_id}, since)}
            store.ingest(records, partial=True)
            graph = store.graph()
            relations = {name: [event_id_of(value, graph, aliases) for value in values]
                         for name, values in (relations or {}).items() if values}
            snapshot_id = Snapshot(records).id
            run_id = ident("note_", session_id, now(), quotes)
            unit_id = ident("unit_note_", session_id, run_id)
            event = {"id": "tmp:note", "kind": kind, "title": title, "summary": summary, "actor": actor,
                     "status": status, "basis": "tool_record" if tool else "explicit_statement",
                     "session_ids": [], "worktree_ids": [], "recorded_at": times[-1] if times else None,
                     "occurred_at": None, "evidence": citations}
            edges = [{"id": f"tmp:edge_{n}", "from_event_id": target, "to_event_id": "tmp:note",
                      "relation": name, "basis": "explicit", "evidence": [dict(c) for c in citations],
                      "rationale": f"stated by the agent that did the work ({name})", "active": True}
                     for n, (name, target) in enumerate(((name, target) for name, targets in relations.items()
                                                         for target in targets), 1)]
            output = {"status": "complete", "read_requests": [], "snapshot_id": snapshot_id, "unit_id": unit_id,
                      "event_candidates": [event], "edge_candidates": edges, "existing_event_matches": [],
                      "open_items": [], "limitations": [], "unprocessed_record_ids": []}
            provided = {record.source_id: [(1, max(1, len(record.content.splitlines())))] for record in records}
            validator = EvidenceValidator(pool, provided, assigned_source_ids=set(pool))
            validator.check_extraction(output, unit_id, snapshot_id, graph)
            delta = draft_delta(output, graph["version"], snapshot_id)
            new = validator.apply_delta(delta, graph, snapshot_id, run_id, output)
            note_id = ident("ev_", run_id, graph["version"], "tmp:note")
            before = {edge["id"] for edge in graph["edges"]}
            for item in new["events"]:
                if item["id"] == note_id:
                    item["origin"] = ORIGIN
            for item in new["edges"]:
                if item["id"] not in before:
                    item["origin"] = ORIGIN
            new, turn_evidence = link_request_turns(new, graph, pool, [i for i in pool if i in ours],
                                                    validator.evidence, store.evidence_many, run_id)
            if new.get("analysis_status") in (None, "no_data"):
                new["analysis_status"] = "partial"
            # The session is the agent's to write from now on: its records count as processed, here and at
            # every later scan (`Store.acknowledge_journaled`), so analysis does not write the same work again.
            published = store.publish(new, [], {r.source_id: r.content_hash for r in records if r.source_id in ours},
                                      {**validator.evidence, **turn_evidence}, expected_version=graph["version"])
            store.note_session(session_id)
            # Transcript time, not the clock: the hook compares it with the records' own times.
            _mark(store, session_id, "noted_at", max((r.recorded_at for r in records if r.recorded_at), default=now()))
    except FlowError as exc:
        if "already running" in str(exc) or "이미 분석이 진행 중" in str(exc):
            raise FlowError("an analysis is running in this project; nothing was stored. Run the same note again "
                            "in a minute") from exc
        raise
    stored = next(item for item in published["events"] if item["id"] == note_id)
    return {"event": stored, "version": published["version"],
            "ref": f"contexttrail:{note_id}@v{published['version']}",
            "edges": [edge for edge in published["edges"] if edge.get("origin") == ORIGIN and edge["to_event_id"] == note_id],
            "requests_added": published.get("request_turn_audit", {}).get("requests_added", 0),
            "normalizations": validator.normalizations}


def queue_dir(scope: Scope) -> Path:
    return Path(tempfile.gettempdir()) / QUEUE_DIR / scope.id


def enabled_readonly(scope: Scope) -> bool | None:
    """Whether notes are on, read without writing anything (inside a sandbox); None when it cannot be read."""
    meta = ReadOnlyMeta(scope)
    if not meta.path.is_file():
        return False
    value = meta.get_meta(SETTINGS_KEY, None)
    return None if value is None else bool(value.get("enabled_at"))


def queue(scope: Scope, session_id: str, *, kind: str, title: str, summary: str, quotes: list[str],
          status: str | None, actor: str, relations: dict[str, list[str]], **homes: Path | None) -> dict[str, Any]:
    """Check what the transcript alone can check, then queue the note for the end-of-turn hook to store."""
    if enabled_readonly(scope) is False:
        raise FlowError("notes are off for this project; the person turns them on with `contexttrail note --enable` "
                        "in the project folder. Nothing was stored.")
    _, _, status = prepare(scope, None, session_id, kind=kind, quotes=quotes, status=status, relations=relations, **homes)
    folder = queue_dir(scope)
    private_dir(folder)
    queued_id = QUEUED_PREFIX + uuid.uuid4().hex[:10]
    item = {"id": queued_id, "session": session_id, "folder": str(scope.folder), "kind": kind, "title": title,
            "summary": summary, "quotes": quotes, "status": status, "actor": actor,
            "relations": {k: v for k, v in relations.items() if v}, "queued_at": now()}
    path = folder / f"{time.time_ns()}-{queued_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(item, stream, ensure_ascii=False)
    return {"queued": queued_id, "path": str(path)}


def queued(scope: Scope, session_id: str | None = None) -> list[dict[str, Any]]:
    folder = queue_dir(scope)
    items = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if session_id is None or item.get("session") == session_id:
            items.append({**item, "_path": str(path)})
    return items


def flush(scope: Scope, store: Store, **homes: Path | None) -> tuple[list[dict], list[str]]:
    """Store the queued notes of this project, oldest first, with every check; returns (stored, refusals).

    A note that fails is dropped with its reason (the agent is told at the end of the turn); a note
    blocked by a running analysis stays queued for the next hook.
    """
    stored, refused, aliases = [], [], {}
    for item in queued(scope):
        path = Path(item["_path"])
        try:
            result = write(scope, store, item["session"], kind=item["kind"], title=item["title"],
                           summary=item.get("summary", ""), quotes=item["quotes"], status=item.get("status"),
                           actor=item.get("actor", "assistant"), relations=item.get("relations") or {},
                           aliases=aliases, **homes)
        except FlowError as exc:
            if "an analysis is running" in str(exc):
                break
            refused.append(f"{item['id']} ({item['title']}): {exc}")
        else:
            aliases[item["id"]] = result["event"]["id"]
            stored.append({"queued": item["id"], "ref": result["ref"]})
        path.unlink(missing_ok=True)
    return stored, refused


def noted(graph: dict, session_id: str) -> list[dict]:
    """The events notes wrote in one session, oldest first."""
    return [event for event in graph["events"] if event.get("origin") == ORIGIN and session_id in event.get("session_ids", [])]


# ---- per project switch, and the end-of-turn reminder ------------------------------------------

def enabled(store: Store) -> bool:
    return bool((store.get_meta(SETTINGS_KEY) or {}).get("enabled_at"))


def enable(store: Store) -> None:
    # `since` stays at the first time: what notes wrote before a pause must not go back to analysis.
    moment = now()
    store.set_meta(SETTINGS_KEY, {"enabled_at": moment, "since": (store.get_meta(SETTINGS_KEY) or {}).get("since") or moment})


def disable(store: Store) -> None:
    store.set_meta(SETTINGS_KEY, {key: value for key, value in (store.get_meta(SETTINGS_KEY) or {}).items() if key == "since"})


def _mark(store: Store, session_id: str, key: str, moment: str | None = None) -> None:
    activity = store.get_meta(ACTIVITY_KEY, {}) or {}
    activity[session_id] = {**activity.get(session_id, {}), key: moment or now()}
    store.set_meta(ACTIVITY_KEY, activity)


def _seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


# Output sent to a file (`> f`, `>> f`, `2> f`; not `2>&1` or /dev/null), or a command that edits,
# moves or removes files. An agent that edits through the shell changes files all the same.
_REDIRECT = re.compile(r"(?<![<>=-])>>?\s*([^\s;&|()<>]+)")
_FILE_COMMANDS = re.compile(r"\bsed\s+-i|\btee\s|\b(?:mv|cp|rm|touch|mkdir|truncate)\s|\bgit\s+apply\b|\bpatch\s|write_text|apply_patch")


_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\\\]|\\\\.)*\"")


def writes_files(command: str, inside: Callable[[str], bool] | None = None) -> bool:
    """True when a shell command line writes or removes a file (quoted text, like code passed to
    `python -c`, is not read as shell). With `inside` (is this absolute path in the project?), a line whose
    every write lands on an absolute path outside the project (a scratch folder, /tmp) is not counted; a
    write it cannot place (a relative path, a quoted one, code that writes) still is."""
    bare = _QUOTED.sub("''", command)
    if not (_FILE_COMMANDS.search(bare) or any(target != "/dev/null" for target in _REDIRECT.findall(bare))):
        return False
    if inside is None:
        return True
    targets = write_targets(command)
    return targets is None or any(inside(target) for target in targets)


_ASSIGNMENT = re.compile(r"(?:^|[\s;&|(])([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|()'\"`$]+)")
_VARIABLE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_PLACED = {"rm", "mkdir", "touch", "truncate", "tee", "cp", "mv", "sed"}


def write_targets(command: str) -> list[str] | None:
    """The absolute paths a shell command line writes to, or None when some write cannot be placed: a relative
    or quoted path, a variable not set on the same line, or a write by code (`write_text`, `apply_patch`, `patch`).
    Variables assigned earlier on the line (`S=/tmp/x; … > $S/out`) and `~` are expanded. The body of a heredoc
    fed to `cat` or `tee` is data and is left out; one fed to an interpreter is code and is read."""
    command = _drop_data_heredocs(command)
    values = dict(_ASSIGNMENT.findall(command))
    bare = _QUOTED.sub("''", command)

    def place(word: str) -> str | None:
        word = _VARIABLE.sub(lambda m: values.get(m.group(1), "\0"), word)
        if "\0" in word or "''" in word or "`" in word:
            return None
        word = os.path.expanduser(word)
        return word if word.startswith("/") else None

    targets: list[str | None] = [place(t) for t in _REDIRECT.findall(bare) if t != "/dev/null"]
    for segment in _SEGMENTS.split(bare):
        words = [w for w in segment.split() if not _ASSIGNMENT.fullmatch(" " + w)]
        if not words:
            continue
        name = words[0].rsplit("/", 1)[-1]
        if name not in _PLACED:
            if _FILE_COMMANDS.search(segment):
                return None  # git apply, patch, write_text, apply_patch: the code decides where
            continue
        if name == "sed" and not re.search(r"\s-i", segment):
            continue
        args = [w for w in words[1:] if not w.startswith("-")]
        if name == "sed":
            args = args[-1:]  # the script is quoted away; the file is the last word
        elif name in {"cp", "mv"}:
            args = args[-1:]
        targets.extend(place(arg) for arg in args)
    return None if any(target is None for target in targets) else [t for t in targets if t]


_SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
# On a command flattened with "; " between its lines (`_command_text(body, "; ")`): `<<'EOF'; …; EOF`.
_HEREDOC_BODY = re.compile(r"<<-?\s*(['\"]?)(\w+)\1(.*?);\s*\2(?=\s*(?:;|$))")


def _drop_data_heredocs(text: str) -> str:
    def body(match: re.Match) -> str:
        words = _SEGMENTS.split(text[:match.start()])[-1].split()
        return "" if words and words[0].rsplit("/", 1)[-1] in {"cat", "tee"} else match.group(0)
    return _HEREDOC_BODY.sub(body, text)


_REMOTE_ADDRESS = re.compile(r"://|^[\w.-]+@[\w.-]+:")


def _tracking_only(sub: str, rest: str) -> bool:
    """True for `git fetch` that only refreshes remote-tracking refs: no refspec writing a local ref
    (`origin main:main`) and no `--update-head-ok`. The working tree and the branches stay as they were,
    so a turn that only looked at the remote has nothing to note. (The analyzer still sees it as `vcs`.)"""
    if sub != "fetch":
        return False
    words = rest.split()
    if {"-u", "--update-head-ok"} & set(words):
        return False
    return not any(":" in word and not word.startswith("-") and not _REMOTE_ADDRESS.search(word) for word in words)


def _changes_git_state(command: str) -> bool:
    """A Git call in the command line that changes the working tree, a branch or the remote."""
    for segment in _command_segments(command):
        call = _GIT_CALL.match(segment)
        if (call and call["sub"] in _VCS_SUBCOMMANDS and _vcs_mutates(call["sub"], call["rest"])
                and not _tracking_only(call["sub"], call["rest"])):
            return True
    return False


def unnoted_work(records: list[SourceRecord], since: float,
                 inside: Callable[[str], bool] | None = None) -> list[SourceRecord]:
    """Edits (also through the shell), commits, tests and Git changes recorded after `since`,
    the note calls themselves aside. With `inside`, an edit of an absolute path outside the project and a
    shell line whose every write lands outside it are not the project's work (`writes_files`)."""
    return [record for record in quotable(records) if record.role == "tool_call"
            and _seconds(record.recorded_at) > since and _is_work(record, inside)]


def _is_work(record: SourceRecord, inside: Callable[[str], bool] | None = None) -> bool:
    """A tool call worth a note: an edit, commit, test or Git change, or a shell line that writes a file."""
    _, target, hint = step_hint(record)
    if hint == "vcs":
        return _changes_git_state(_command_text(record.content.partition("\n")[2], "; "))
    if hint == "edit" and inside is not None and target.startswith("/") and not inside(target):
        return False
    # Lines kept apart ("; "), so a heredoc's body can be told from the command that writes it.
    return hint in WORK_HINTS or (hint == "run" and writes_files(_command_text(record.content.partition("\n")[2], "; "), inside))


def describe_work(record: SourceRecord) -> str:
    """One call as the reminder names it: its kind (edit, test, commit, vcs, run) and a short target, the file
    edited or the first `REMINDER_CHARS` characters of the command. The call's output is never part of it."""
    _, target, hint = step_hint(record)
    text = target.removeprefix("patch ") if hint == "edit" else _command_text(record.content.partition("\n")[2])
    text = safe_text(text, multiline=False)
    return f"{hint} {text[:REMINDER_CHARS]}" + ("…" if len(text) > REMINDER_CHARS else "")


def reminder_text(work: list[SourceRecord]) -> str:
    return reminder_lines([describe_work(record) for record in work])


def reminder_lines(described: list[str]) -> str:
    """`REMINDER`, followed by the most recent distinct calls without a note (at most `REMINDER_CALLS`, oldest
    first) and how many more there were. The first sentence is unchanged: `model.HARNESS_TEXT` keeps the
    reminder from being taken for a person's request by that wording."""
    calls = list(dict.fromkeys(described))
    shown = calls[-REMINDER_CALLS:]
    lines = [REMINDER, "", "Calls since the last note:", *(f"- {call}" for call in shown)]
    if len(calls) > len(shown):
        lines.append(f"- … and {len(calls) - len(shown)} more earlier")
    return "\n".join(lines)


def hook(stdin_text: str | None, **homes: Path | None) -> dict[str, Any] | None:
    """What the end-of-turn hook answers: a `block` with the reminder, or None to let the turn end.

    Quiet unless the person turned notes on for the project. Reminds at most once per stretch of
    work: a turn the hook already sent back (`stop_hook_active`, or work no newer than the last
    reminder) ends. Never raises; a hook must not stop the host.
    """
    try:
        payload = json.loads(stdin_text or "")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    cwd, session_id = payload.get("cwd"), payload.get("session_id")
    if not (isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir() and isinstance(session_id, str) and session_id):
        return None
    try:
        scope = Scope.resolve(Path(cwd))
        if not (scope.state_dir / "state.sqlite").is_file():
            return None  # never create state for a project the person did not open with ContextTrail
        store = Store(scope.state_dir, scope.id)
        if not enabled(store):
            return None
        with _hook_lock(scope):
            return _remind(scope, store, payload, session_id, **homes)
    except Exception:  # a hook never fails the host's turn
        return None


@contextmanager
def _hook_lock(scope: Scope) -> Iterator[None]:
    """One hook at a time per project. A person with both `install-hooks` and the plugin runs two at
    a turn's end; the second waits, then finds the reminder sent and the queue already stored."""
    fd = os.open(scope.state_dir / "note-hook.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _remind(scope: Scope, store: Store, payload: dict, session_id: str, **homes: Path | None) -> dict[str, Any] | None:
    """The hook's answer for one turn, run under `_hook_lock`."""
    _, refused = flush(scope, store, **homes)
    away = unattended(payload, session_id, **homes)
    if refused:
        # A host may not send the agent back twice in a turn; `note --list` shows these too.
        activity = store.get_meta(ACTIVITY_KEY, {}) or {}
        entry = activity.setdefault(session_id, {})
        entry["refused"] = (entry.get("refused", []) + refused)[-10:]
        store.set_meta(ACTIVITY_KEY, activity)
    if refused and not payload.get("stop_hook_active") and not away:
        return {"decision": "block", "reason": REFUSED_REASON + " " + " | ".join(refused)[:3000]}
    if payload.get("stop_hook_active") or away:
        return None  # this turn was already sent back once, or no one is there to answer
    activity = (store.get_meta(ACTIVITY_KEY, {}) or {}).get(session_id, {})
    # A note still waiting in the queue (refused while an analysis ran) counts as written.
    waiting = [_seconds(item.get("queued_at")) for item in queued(scope, session_id)]
    # Records analysis took back after the session went quiet (`Store.journal_tails`) are not the agent's to note.
    given_back = store.journal_tails().get(session_id, (0.0, 0.0))[1]
    since = max(_seconds(activity.get("noted_at")), _seconds(activity.get("reminded_at")),
                _seconds((store.get_meta(SETTINGS_KEY) or {}).get("enabled_at")), *waiting, given_back)
    inside = lambda path: scope.worktree_for(path) is not None  # noqa: E731
    found = _work_since(scope, store, payload, session_id, since, inside, **homes)
    if found is None:  # no transcript file to read new lines of (opencode): the whole session, parsed
        records = session_records(scope, store, session_id, **homes)
        work = unnoted_work(records, since, inside)
        found = ([_work_item(r) for r in work], max((_seconds(r.recorded_at) for r in records), default=0.0))
    items, latest = found
    if not due(items, latest):
        return None
    _mark(store, session_id, "reminded_at", max(item["at"] for item in items))
    return {"decision": "block", "reason": reminder_lines([item["call"] for item in items])}


# The reminder waits until unnoted work piles up: this many calls, or this long after the first of them, or a
# commit (a natural point to write down what it holds). A session that ends below that gives the rest to
# analysis once it is quiet (`Store.journal_tails`).
REMIND_CALLS = 10
REMIND_AFTER_SECONDS = 30 * 60


def due(items: list[dict], latest: float) -> bool:
    """Whether unnoted work (`_work_item`s, oldest first) is worth sending the agent back for now."""
    if not items:
        return False
    return (len(items) >= REMIND_CALLS or any(item["commit"] for item in items)
            or latest - items[0]["at"] >= REMIND_AFTER_SECONDS)


def _work_item(record: SourceRecord) -> dict[str, Any]:
    return {"at": _seconds(record.recorded_at), "call": describe_work(record), "commit": step_hint(record)[2] == "commit"}


SCAN_KEY = "scan"  # per session in ACTIVITY_KEY: where the hook stopped reading its transcript, and what it found
SCAN_ITEMS = 200


def _work_since(scope: Scope, store: Store, payload: dict, session_id: str, since: float,
                inside: Callable[[str], bool], **homes: Path | None) -> tuple[list[dict], float] | None:
    """The unnoted work after `since` and the time of the session's newest line, reading only the lines the
    transcript gained since the last hook (`SCAN_KEY`); the whole file when it is new to the hook, was
    replaced or shrank. None when there is no JSONL transcript (an opencode session)."""
    path = payload.get("transcript_path")
    path = Path(path) if isinstance(path, str) and path else None
    if path is None or not path.is_file():
        codex, claude, _ = source_homes(homes.get("codex_home"), homes.get("claude_home"), homes.get("opencode_home"))
        path = next((p for p, _ in jsonl_files(codex, claude) if session_id in p.name and "subagents" not in p.parts), None)
    if path is None:
        return None
    activity = store.get_meta(ACTIVITY_KEY, {}) or {}
    entry = activity.setdefault(session_id, {})
    state = entry.get(SCAN_KEY) or {}
    size = path.stat().st_size
    if state.get("path") != str(path) or size < state.get("offset", 0):
        state = {"path": str(path), "offset": 0, "items": [], "latest": 0.0}
    with path.open("rb") as stream:
        stream.seek(state["offset"])
        data = stream.read(size - state["offset"])
    complete = data.rfind(b"\n") + 1  # a line still being written is read next time
    items, latest = list(state["items"]), state["latest"]
    for raw in data[:complete].splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        at = _seconds(row.get("timestamp"))
        latest = max(latest, at)
        for record in _calls_in_row(row, session_id):
            if not runs_note(record) and _is_work(record, inside):
                items.append(_work_item(record))
    items = [item for item in items if item["at"] > since][-SCAN_ITEMS:]
    entry[SCAN_KEY] = {"path": str(path), "offset": state["offset"] + complete, "items": items, "latest": latest}
    store.set_meta(ACTIVITY_KEY, activity)
    return items, latest


def _calls_in_row(row: dict, session_id: str) -> list[SourceRecord]:
    """The tool calls of one transcript line, written as the parsers write them (`Tool: name` and the
    arguments), enough for `step_hint`; only what the hook needs, not a parse of the session."""
    stamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None
    found = []
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    if row.get("type") == "assistant" and isinstance(message.get("content"), list):  # Claude Code
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                params = block.get("input", {})
                body = json.dumps(params, ensure_ascii=False, sort_keys=True) if isinstance(params, dict) else str(params)
                found.append(f"Tool: {block.get('name', 'unknown')}\n{body}")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if row.get("type") == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:  # Codex
        arguments = payload.get("arguments", payload.get("input", ""))
        body = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        found.append(f"Tool: {payload.get('name') or 'unknown'}\n{body}")
    cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else None  # an edited path is named relative to it
    return [SourceRecord("hook", "", session_id, "tool_call", text, {}, recorded_at=stamp, cwd=cwd) for text in found]


# How a transcript says no person is at the terminal: Codex `exec` (session_meta `originator`/`source`) and Claude
# Code's print mode or an SDK (`entrypoint` `sdk-cli`, `sdk-ts`, `sdk-py`).
UNATTENDED_LINES = 20


def unattended(payload: dict, session_id: str, *, codex_home: Path | None = None, claude_home: Path | None = None,
               opencode_home: Path | None = None) -> bool:
    """Whether the session runs without a person (`codex exec`, `claude -p`): its turn end is the run's end, and
    sending the agent back would replace the answer the caller waits for (`-o`) with a note attempt."""
    path = payload.get("transcript_path")
    path = Path(path) if isinstance(path, str) and path else None
    if path is None or not path.is_file():
        codex, claude, _ = source_homes(codex_home, claude_home, opencode_home)
        path = next((p for p, _ in jsonl_files(codex, claude) if session_id in p.name and "subagents" not in p.parts), None)
    if path is None:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(UNATTENDED_LINES), handle):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                meta = row.get("payload") if row.get("type") == "session_meta" else None
                if isinstance(meta, dict):
                    return meta.get("originator") == "codex_exec" or meta.get("source") == "exec"
                entry = row.get("entrypoint")
                if isinstance(entry, str):
                    return entry.startswith("sdk-")
    except OSError:
        return False
    return False


def refused(store: Store, session_id: str) -> list[str]:
    """Queued notes of this session the hook could not store, with why (the last ten)."""
    return ((store.get_meta(ACTIVITY_KEY, {}) or {}).get(session_id) or {}).get("refused", [])
