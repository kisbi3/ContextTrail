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
import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import link_request_turns, step_hint
from .git_context import Scope
from .i18n import tr
from .model import SourceRecord, Snapshot
from .schema import SHORT_QUOTE_CHARS, STATUSES, EvidenceValidator, draft_delta, occurrences, resolve_quote
from .sources.local import jsonl_files, parse_claude, parse_codex, source_homes
from .sources.opencode import opencode_databases, parse_opencode
from .store import Store
from .util import FlowError, ident, now

# A person's request is made into an event by code from the transcript (`link_request_turns`), so the
# agent notes what it decided, proposed, changed and observed.
NOTE_KINDS = ("decision", "proposal", "goal", "action", "revision", "outcome")
DEFAULT_STATUS = {"decision": "adopted", "proposal": "proposed", "goal": "adopted",
                  "action": "applied", "revision": "applied"}
# Each relation runs from the named, earlier event to the note (relations go from earlier to later):
# the note verifies that change, revises that event, answers that question, or was motivated by it.
RELATION_OPTIONS = ("verifies", "revises", "answers", "motivates")
# Which variable names the session running this command. Inside Claude Code the Codex variable can be
# inherited from the shell that started it (and the reverse is far less common), so Claude Code wins
# when its own marker is set.
SESSION_VARIABLES = (("CLAUDE_CODE_SESSION_ID", "CLAUDECODE"), ("CODEX_THREAD_ID", None), ("OPENCODE_SESSION_ID", None))
NOTE_COMMAND = re.compile(r"\b(?:contexttrail|ct|project)\s+note\b")
ORIGIN = "note"
NEAREST = 3
NEAREST_CHARS = 200
NEAREST_RECORDS = 400
SETTINGS_KEY = "journal"            # {"enabled_at": …} when the person turned notes on for this project
ACTIVITY_KEY = "journal_activity"   # per session: when it last noted and when the hook last reminded it
# Work that is worth a note when a turn ends without one: a file edit, a commit, a test, a change to Git.
WORK_HINTS = {"edit", "commit", "test", "vcs"}
REMINDER = ("ContextTrail notes are on for this project, and this turn changed files or ran checks without a note. "
            "Before finishing, use the contexttrail-note skill: record the decisions, changes and observed results of "
            "this turn with `contexttrail note` (one call per event, quoting the tool output or message exactly). "
            "If nothing in this turn is worth recording, just finish.")


def current_session(environ: dict[str, str] | None = None) -> str:
    """The session running this command, from the host tool's environment."""
    environ = os.environ if environ is None else environ
    found = {name: environ[name] for name, _ in SESSION_VARIABLES if environ.get(name)}
    for name, marker in SESSION_VARIABLES:
        if marker and environ.get(marker) and name in found:
            return found[name]
    if len(found) == 1:
        return next(iter(found.values()))
    raise FlowError(tr("지금 대화 중인 세션을 알 수 없습니다", "Cannot tell which session is running this command")
                    + (tr(" (여러 도구의 세션이 보입니다: ", " (sessions of several tools are visible: ") + ", ".join(sorted(found)) + ")"
                       if found else "")
                    + tr(". --session에 세션 ID를 지정하세요.", ". Give --session a session ID."))


def _homes(store: Store) -> dict[str, Path]:
    options = store.get_meta("options", {}) or {}
    return {key: Path(options[key]) for key in ("codex_home", "claude_home", "opencode_home") if options.get(key)}


def session_records(scope: Scope, store: Store, session_id: str, *, codex_home: Path | None = None,
                    claude_home: Path | None = None, opencode_home: Path | None = None) -> list[SourceRecord]:
    """The records of one session that belong to this project, in recorded order.

    Only transcript files whose name carries the session ID are parsed (a Claude Code transcript is
    `<id>.jsonl`, a Codex rollout ends with the ID), and only that session of an opencode database.
    """
    saved = _homes(store)
    codex_home, claude_home, opencode_home = source_homes(codex_home or saved.get("codex_home"),
                                                          claude_home or saved.get("claude_home"),
                                                          opencode_home or saved.get("opencode_home"))
    proven = [Path(p) for p in store.get_meta("known_worktree_roots", []) or []]
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


def quotable(records: list[SourceRecord]) -> list[SourceRecord]:
    """The records a quote may come from: all but the `note` calls themselves and their output."""
    calls = {r.tool_call_id for r in records if r.role == "tool_call" and r.tool_call_id and NOTE_COMMAND.search(r.content)}
    return [r for r in records if not (r.role == "tool_call" and NOTE_COMMAND.search(r.content))
            and not (r.role == "tool_result" and r.tool_call_id in calls)]


def locate(records: list[SourceRecord], quote: str) -> dict[str, Any]:
    """The citation of a quote: the most recent record holding it, at the lines that hold it."""
    if len(quote.strip()) < SHORT_QUOTE_CHARS:
        raise FlowError(f"quote too short to find: {quote!r} (quote at least {SHORT_QUOTE_CHARS} characters, "
                        "exactly as shown in this session)")
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


def event_id_of(value: str, graph: dict) -> str:
    """An event named by its ID, an ID prefix, or a copied reference (`contexttrail:ev_…@v12`)."""
    text = value.strip()
    if text.startswith("contexttrail:"):
        text = text[len("contexttrail:"):]
    text = text.split("@", 1)[0]
    matches = [event["id"] for event in graph["events"] if event["id"] == text] or \
              [event["id"] for event in graph["events"] if event["id"].startswith(text)]
    if len(matches) != 1:
        raise FlowError(f"no single event with ID {value!r} in the graph ({len(matches)} matches); "
                        "use an ID from `contexttrail note --list` or `contexttrail find`")
    return matches[0]


def write(scope: Scope, store: Store, session_id: str, *, kind: str, title: str, summary: str = "",
          quotes: list[str], status: str | None = None, actor: str = "assistant",
          relations: dict[str, list[str]] | None = None, **homes: Path | None) -> dict[str, Any]:
    """Check one note against the session's records and publish it; nothing is stored when a check fails."""
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
    if not enabled(store):
        raise FlowError("notes are off for this project; the person turns them on with `contexttrail note --enable` "
                        "in the project folder. Nothing was stored.")
    records = session_records(scope, store, session_id, **homes)
    if not records:
        raise FlowError(f"no records of session {session_id} in this project yet "
                        "(the session must run in this folder and have written its transcript)")
    candidates = quotable(records)
    citations = [locate(candidates, quote) for quote in quotes]
    pool = {record.source_id: record for record in records}
    cited = [pool[c["source_id"]] for c in citations]
    tool = any(record.role in {"tool_call", "tool_result"} for record in cited)
    times = sorted(record.recorded_at for record in cited if record.recorded_at)
    try:
        with store.analyze_lock():
            store.ingest(records, partial=True)
            graph = store.graph()
            relations = {name: [event_id_of(value, graph) for value in values]
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
            new, turn_evidence = link_request_turns(new, graph, pool, list(pool), validator.evidence,
                                                    store.evidence_many, run_id)
            if new.get("analysis_status") in (None, "no_data"):
                new["analysis_status"] = "partial"
            # The session is the agent's to write from now on: its records count as processed, here and at
            # every later scan (`Store.acknowledge_journaled`), so analysis does not write the same work again.
            published = store.publish(new, [], {record.source_id: record.content_hash for record in records},
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


def noted(graph: dict, session_id: str) -> list[dict]:
    """The events notes wrote in one session, oldest first."""
    return [event for event in graph["events"] if event.get("origin") == ORIGIN and session_id in event.get("session_ids", [])]


# ---- per project switch, and the end-of-turn reminder ------------------------------------------

def enabled(store: Store) -> bool:
    return bool((store.get_meta(SETTINGS_KEY) or {}).get("enabled_at"))


def enable(store: Store) -> None:
    store.set_meta(SETTINGS_KEY, {"enabled_at": now()})


def disable(store: Store) -> None:
    store.set_meta(SETTINGS_KEY, {})


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


def unnoted_work(records: list[SourceRecord], since: float) -> list[SourceRecord]:
    """Edits, commits, tests and Git changes recorded after `since` (the note calls themselves aside)."""
    return [record for record in quotable(records) if record.role == "tool_call"
            and _seconds(record.recorded_at) > since and step_hint(record)[2] in WORK_HINTS]


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
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
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
        activity = (store.get_meta(ACTIVITY_KEY, {}) or {}).get(session_id, {})
        since = max(_seconds(activity.get("noted_at")), _seconds(activity.get("reminded_at")),
                    _seconds((store.get_meta(SETTINGS_KEY) or {}).get("enabled_at")))
        work = unnoted_work(session_records(scope, store, session_id, **homes), since)
        if not work:
            return None
        _mark(store, session_id, "reminded_at", max(record.recorded_at for record in work))
    except Exception:  # a hook never fails the host's turn
        return None
    return {"decision": "block", "reason": REMINDER}
