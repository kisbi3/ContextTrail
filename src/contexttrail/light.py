"""Light analysis (`--light`): code builds what the records show, one model call writes the rest.

The full pipeline asks the model to quote its evidence exactly, checks every quote and sends the whole unit
again when one is off, then integrates and reviews. Here the evidence is a record ID and a line range only, and
the code cuts the quoted lines itself, so a citation is always real source text and can never fail to match:

- Code makes the skeleton from the tool steps (`tool_steps`): a change event per run of edits in a turn, an
  observed result per test run (failed or passed by its result; the same check run again with no edit between
  is one result, settled by the later run), a change per commit, and a `verifies` from the
  turn's last code edit to a test run after it in the same turn (never for a documentation-only edit). The
  person's requests and the dialog-turn links are made by code as in the full pipeline.
- The model gets the unit's records (the same view as an extraction) and the skeleton, and answers with a title
  and summary for each skeleton item plus the events code cannot see (decisions and why, proposals, goals,
  explained failures, reports), each naming the records it comes from. No quotes, no relations, no repair round.
- The result is an ordinary extraction (`event_candidates`, `edge_candidates`), published as the code-built
  draft with no integrate or review call. A citation the checks still refuse is dropped, not repaired.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from .model import SourceRecord, is_queued_message, is_user_prompt, question_reply
from .schema import STATUSES, STR, arr, enum, obj

# Kinds the model may add; the person's requests (question) are made by code.
LIGHT_KINDS = ["goal", "decision", "proposal", "action", "revision", "outcome"]
LINE = {"type": ["integer", "null"], "minimum": 1}
LIGHT_SCHEMA = obj({
    "status": enum(["complete"]), "snapshot_id": STR, "unit_id": STR,
    "skeleton": arr(obj({"id": STR, "title": STR, "summary": STR})),
    "events": arr(obj({"kind": enum(LIGHT_KINDS), "status": enum(STATUSES), "actor": enum(["assistant", "user"]),
                       "title": STR, "summary": STR,
                       "sources": arr(obj({"source_id": STR, "start_line": LINE, "end_line": LINE}), 1)})),
})
# A citation code cuts is at most this many lines of what the model was shown; the rest is one `show` away.
CITED_LINES = 20
TITLE_FILES = 3


# How a check's result says it failed: an exit status (Codex `Exit code: 1`, `Process exited with code 1`, a JSON
# `"exit_code": 1`, Claude's `Error: Exit code 1`) and, without one, a test runner's summary (`2 failed`, `1 error`).
EXIT_CODE = re.compile(r'(?:exit(?:ed with)? code|"exit_code")\s*[:=]?\s*(-?\d+)', re.IGNORECASE)
TEST_FAILURES = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b|^FAILED\b|^ERROR\b", re.MULTILINE)


def check_failed(text: str, tool_error: bool) -> bool:
    """Whether a check's result shows a failure: its exit status when it has one, else a failure count."""
    codes = EXIT_CODE.findall(text)
    if codes:
        return int(codes[-1]) != 0
    return tool_error or bool(TEST_FAILURES.search(text))


def _turns(assigned: list[SourceRecord]) -> dict[str, int]:
    """The dialog turn of each record: a message the person typed starts a new one, and so does one sent through
    the tool's queue (whose turn has no request)."""
    turn, turns = 0, {}
    for record in assigned:
        if is_user_prompt(record) or is_queued_message(record):
            turn += 1
        turns[record.source_id] = turn
    return turns


def _files(target: str) -> list[str]:
    names = [part for part in target.removeprefix("patch ").replace(",", " ").split() if part]
    return [PurePosixPath(name).name for name in names] or [target]


# How a test file names the module it tests: `test_calc.py`, `calc_test.go`, `calc.test.ts`, `calc.spec.js`, ...
_TEST_NAMES = ("test_{stem}.py", "{stem}_test.py", "tests_{stem}.py", "{stem}_test.go", "{stem}.test.ts", "{stem}.test.tsx",
               "{stem}.test.js", "{stem}.spec.ts", "{stem}.spec.js", "{stem}_spec.rb", "{stem}Test.java", "{stem}Tests.swift")


def _named(edited: str, targets: list[str]) -> bool:
    """Whether a test run's targets name an edited file: the file itself, or its module's test file, compared as
    whole file names (never a substring: `base.py` is not `test_database.py`)."""
    name = edited.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    tests = {pattern.format(stem=stem) for pattern in _TEST_NAMES}
    for target in targets:
        base = target.split("::", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if base == name or base in tests:
            return True
    return False


def covers(command: str, files: list[str]) -> str | None:
    """Why a test run counts as a check of every one of the edited files, or None. The run must name each file, or
    its module's test file, among its targets (`_named`). A run of the whole suite, of a selection (`-k`, `-m`), or
    of tests that name only some of the files is not a check of these edits: code cannot tell what it covered, so it
    is left to the records and the model, never inferred from time (`docs/DECISIONS.md` 2026-10-18)."""
    from .analysis import TEST_COMMAND, _command_segments
    if not files:
        return None
    for run in (segment for segment in _command_segments(command) if TEST_COMMAND.match(segment)):
        targets = [word.strip("'\"") for word in run.split()[1:] if not word.startswith("-")
                   and any(mark in word for mark in ("/", ".", "::"))]
        if targets and all(_named(f, targets) for f in files):
            shown = ", ".join(f.rsplit("/", 1)[-1] for f in files)
            return f"The test run names the edited {shown} (each file or its module's test file)."
    return None


# A Codex `exec` call can patch files and then run a command in one script: `tools.apply_patch(…)` before
# `tools.exec_command({cmd: "…"})`. Its result holds what the command printed (a test run right after the patch).
_INLINE_PATCH = re.compile(r"tools\.apply_patch\(")
_INLINE_RUN = re.compile(r'tools\.exec_command\(\s*\{\s*"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"')


def inline_run(call: str) -> str | None:
    """The command a call runs right after patching files in the same call (Codex `exec`), or None."""
    patch, run = _INLINE_PATCH.search(call), _INLINE_RUN.search(call)
    if patch is None or run is None or run.start() < patch.start():
        return None
    return " ".join(run.group(1).replace("\\n", " ").replace('\\"', '"').split())


def skeleton(assigned: list[SourceRecord], split_at_runs: bool = False) -> tuple[list[dict], list[dict]]:
    """Events and `verifies` relations the tool steps show, with the records each rests on (call first).
    `split_at_runs` (pipeline v2) ends a run of edits at any other run in between, so the edits a run could have
    tried are a change of their own, the one a story's claimed check can name."""
    from .analysis import tool_steps
    turns = _turns(assigned)
    content = {record.source_id: record.content for record in assigned}
    items: list[dict] = []
    edges: list[dict] = []
    last_edit: dict[int, dict] = {}   # per turn: the latest code edit a later test may verify
    previous = None
    for step in tool_steps(assigned):
        turn = turns.get(step["call"], 0)
        sources = [step["call"]] + ([step["result"]] if step["result"] else [])
        if step["hint"] == "edit" and not step["failed"]:
            if previous is not None and previous["kind"] == "action" and previous.get("edit") and previous["turn"] == turn:
                previous["sources"] += sources
                previous["files"] += [f for f in _files(step["target"]) if f not in previous["files"]]
                previous["doc"] = previous["doc"] and bool(step.get("doc"))
            else:
                previous = {"id": f"K{len(items) + 1}", "kind": "action", "status": "applied", "edit": True,
                            "turn": turn, "sources": sources, "files": _files(step["target"]), "doc": bool(step.get("doc"))}
                items.append(previous)
            if not previous["doc"]:
                last_edit[turn] = previous
            if split_at_runs and inline_run(content.get(step["call"], "")):
                previous = None  # the call ran a command after its patch: the edits up to it are a change of their own
            continue
        if step["hint"] == "test" and step["result"]:
            failed = check_failed(content.get(step["result"], ""), step["failed"])
            item = {"id": f"K{len(items) + 1}", "kind": "outcome", "turn": turn, "sources": [step["result"], step["call"]],
                    "status": "observed_failure" if failed else "observed_success", "check": step["target"],
                    "title": ("Check failed: " if failed else "Check passed: ") + step["target"][:60]}
            if (previous is not None and previous["kind"] == "outcome" and previous["turn"] == turn
                    and previous.get("check") == step["target"]):
                # The same check run again with nothing changed between: one step, settled by the later run,
                # which keeps the earlier run's records as evidence of the repeat.
                previous.update(status=item["status"], title=item["title"], sources=item["sources"] + previous["sources"])
                continue
            items.append(item)
            # The whole command line (the outline's target is cut short), with its lines kept apart for heredocs.
            from .analysis import _command_text
            command = _command_text(content.get(step["call"], "").partition("\n")[2], "; ")
            why = covers(command, last_edit[turn]["files"]) if turn in last_edit else None
            if why:
                edges.append({"from": last_edit[turn]["id"], "to": item["id"], "why": why})
        elif step["hint"] == "commit" and step["result"] and not step["failed"]:
            items.append({"id": f"K{len(items) + 1}", "kind": "action", "status": "applied", "turn": turn,
                          "sources": sources, "title": "Commit: " + step["target"][:60]})
        else:
            if split_at_runs and step["hint"] == "run" and previous is not None and previous.get("edit"):
                previous = None
            continue
        previous = items[-1]
    for item in items:
        if item.get("edit"):
            shown = item["files"][:TITLE_FILES]
            more = len(item["files"]) - len(shown)
            item["title"] = "Edit " + ", ".join(shown) + (f" and {more} more" if more > 0 else "")
    return items, edges


def request_data(unit: dict, snapshot_id: str, data: dict, items: list[dict]) -> dict:
    """What the model reads: the unit's records as the extraction shows them, the person's messages and the skeleton."""
    return {"unit_id": unit["id"], "snapshot_id": snapshot_id, "new_records": data["new_records"],
            "user_requests": data["user_requests"],
            "skeleton": [{"id": item["id"], "kind": item["kind"], "status": item["status"], "title": item["title"],
                          "source_ids": item["sources"]} for item in items]}


def cite(record: SourceRecord, provided: list[tuple[int, int]], start: int | None = None,
         end: int | None = None) -> dict | None:
    """A citation of lines the model was shown, cut by code: within the requested range when it overlaps a shown
    one, else the first shown lines; at most `CITED_LINES` lines; None when nothing shown is left."""
    lines = record.content.splitlines()
    for lo, hi in provided or [(1, len(lines))]:
        hi = min(hi, len(lines))
        s, e = (lo, hi) if start is None else (max(lo, start), min(hi, end or start))
        if s > e:
            continue
        e = min(e, s + CITED_LINES - 1)
        quote = "\n".join(lines[s - 1:e])
        if quote.strip():
            return {"source_id": record.source_id, "start_line": s, "end_line": e, "quote": quote}
    if start is not None:
        return cite(record, provided)  # the range named nothing shown: the record's first shown lines
    return None


def _checked(citations: list[dict], validator) -> list[dict]:
    """The citations the evidence checks accept (a code-cut quote is exact; a line past the shown part may not be)."""
    from .util import FlowError
    kept = []
    for citation in citations:
        try:
            validator.citations([dict(citation)])
            kept.append(citation)
        except FlowError:
            continue
    return kept


def extraction(answer: dict, items: list[dict], edges: list[dict], unit: dict, snapshot_id: str,
               pool: dict[str, SourceRecord], provided: dict[str, list[tuple[int, int]]], validator) -> dict:
    """The model's answer and the skeleton as an ordinary extraction output."""
    assigned = set(unit["sources"])
    named = {entry["id"]: entry for entry in answer.get("skeleton", [])}
    candidates, dropped = [], []

    def citations(sources: list[dict]) -> list[dict]:
        found = []
        for source in sources:
            record = pool.get(source["source_id"])
            if record is None or record.source_id not in assigned:
                continue
            citation = cite(record, provided.get(record.source_id, []), source.get("start_line"), source.get("end_line"))
            if citation and citation not in found:
                found.append(citation)
        return _checked(found, validator)

    def candidate(key: str, kind: str, status: str, actor: str, title: str, summary: str, evidence: list[dict]) -> dict:
        records = [pool[c["source_id"]] for c in evidence]
        tool = any(r.role in {"tool_call", "tool_result"} for r in records)
        return {"id": f"tmp:{key}", "kind": kind, "title": title[:240] or kind, "summary": summary[:4000], "actor": actor,
                "status": status, "basis": "tool_record" if tool else "explicit_statement",
                "session_ids": [], "worktree_ids": [], "occurred_at": None,
                "recorded_at": next((r.recorded_at for r in records if r.recorded_at), None), "evidence": evidence}

    for item in items:
        evidence = citations([{"source_id": s} for s in item["sources"]])
        if not evidence:
            dropped.append(item["id"])
            continue
        entry = named.get(item["id"], {})
        candidates.append(candidate(item["id"], item["kind"], item["status"], "assistant",
                                    entry.get("title") or item["title"], entry.get("summary") or "", evidence))
    for number, event in enumerate(answer.get("events", []), 1):
        evidence = citations(event["sources"])
        if not evidence:
            dropped.append(f"N{number}")
            continue
        candidates.append(candidate(f"N{number}", event["kind"], event["status"], event["actor"],
                                    event["title"], event["summary"], evidence))
    present = {c["id"] for c in candidates}
    relations = []
    for number, edge in enumerate(edges, 1):
        source, target = f"tmp:{edge['from']}", f"tmp:{edge['to']}"
        if source in present and target in present:
            outcome = next(c for c in candidates if c["id"] == target)
            relations.append({"id": f"tmp:V{number}", "from_event_id": source, "to_event_id": target,
                              "relation": "verifies", "basis": "structural", "evidence": outcome["evidence"][:1],
                              "rationale": edge.get("why") or "A test ran after this edit in the same turn.", "active": True})
    limitations = [f"light analysis: no evidence left for {', '.join(dropped)}."] if dropped else []
    return {"status": "complete", "read_requests": [], "snapshot_id": snapshot_id, "unit_id": unit["id"],
            "event_candidates": candidates, "edge_candidates": relations, "existing_event_matches": [],
            "open_items": [], "limitations": limitations, "unprocessed_record_ids": []}


def answer_check(answer: dict) -> Any:
    """The light answer has no claims to check beyond its shape; a source the checks refuse is dropped later."""
    from .schema import validate_shape
    validate_shape(answer, LIGHT_SCHEMA)
    return None


# ---- Pipeline v2: the story (docs/plans/PIPELINE_V2.md). Code keeps the skeleton as the record of the work; the model
# reads only what people and the assistant wrote (as evidence blocks), the work outline and failed runs' last lines,
# and writes the story: goals, decisions and their recorded reasons, findings, changes of course, and the links.
STORY_KINDS = ["goal", "decision", "proposal", "outcome", "revision"]
STORY_RELATIONS = ["motivates", "answers", "revises", "produces", "verifies"]
_NULLSTR = {"type": ["string", "null"]}
STORY_SCHEMA = obj({
    "status": enum(["complete"]), "snapshot_id": STR, "unit_id": STR,
    # `checked_by`: for an edit item, the later runs or checks that tried it, each with the block reporting what it
    # showed; asked of every item so each edit gets a decision, read by code as claimed `verifies` (`claimed_check`).
    "work": arr(obj({"id": STR, "title": STR, "summary": STR,
                     "checked_by": arr(obj({"run": STR, "blocks": arr(STR)}))})),
    # Every story event is the assistant's: the person's requests and choices are made by code (`request_items`).
    "story": arr(obj({"id": STR, "kind": enum(STORY_KINDS), "status": enum(STATUSES),
                      "title": STR, "why": _NULLSTR, "summary": STR, "blocks": arr(STR), "work": arr(STR)})),
    # `requests`: for each request (a Q, or the open request's E), the story events or work items where the assistant
    # answered it, each with the reply's blocks, or []; asked of every request so each gets a decision (as
    # `checked_by` is of every edit), read by code as claimed `answers` (`claimed_answer`).
    "requests": arr(obj({"id": STR, "answered_by": arr(obj({"item": STR, "blocks": arr(STR)}))})),
    "links": arr(obj({"from": STR, "to": STR, "relation": enum(STORY_RELATIONS), "blocks": arr(STR)})),
    "turn_summary": _NULLSTR,
})
FAILURE_TAIL_LINES = 8
STORY_FALLBACK_LIMITATION = ("story not written: the story call for this unit failed its checks, so only the work "
                             "code found (edits, checks, commits) was kept.")


BACKGROUND = re.compile(r'"run_in_background"\s*:\s*true|running in background|(?<!&)&\s*(?:"|$)', re.MULTILINE)


def run_status(call: SourceRecord | None, result: SourceRecord | None, tool_error: bool) -> str:
    """passed, failed or unknown, by the run's exit status. Codex writes one for every run; Claude Code writes one
    only when the command failed, so a finished Claude Code run with no failure passed. A run left in the
    background (or with no result) is unknown: its status says only that it started."""
    if result is None or call is None:
        return "unknown"
    if BACKGROUND.search(call.content) or BACKGROUND.search(result.content[:400]):
        return "unknown"
    if check_failed(result.content, tool_error):
        return "failed"
    codes = EXIT_CODE.findall(result.content)
    if codes:
        return "passed" if all(int(code) == 0 for code in codes) else "failed"
    return "passed" if result.provider == "claude" else "unknown"


# Codex returns a command still running after its wait as a session (`"session_id": 31341` and no exit code); what it
# printed and how it ended come in later `write_stdin({session_id: 31341, …})` polls of that session.
_RUNNING = re.compile(r'"session_id"\s*:\s*(\d+)')
_POLLED = re.compile(r'write_stdin\(\s*\{\s*"?session_id"?\s*:\s*(\d+)')


def _follow_polls(run: dict, steps: list[dict], by_id: dict[str, SourceRecord]) -> None:
    """A run Codex left running: its later polls join its records, and the last one with an exit status settles it."""
    result = by_id.get(run["sources"][-1]) if len(run["sources"]) > 1 else None
    if result is None or run["status"] != "unknown" or EXIT_CODE.search(result.content):
        return
    running = _RUNNING.findall(result.content)
    if not running:
        return
    later = False
    for step in steps:
        if step["call"] == run["sources"][0]:
            later = True
            continue
        call = by_id.get(step["call"])
        if not later or step["hint"] != "poll" or call is None or running[-1] not in _POLLED.findall(call.content):
            continue
        run["sources"] += [step["call"]] + ([step["result"]] if step["result"] else [])
        polled = by_id.get(step["result"])
        if polled is not None and EXIT_CODE.search(polled.content):
            run["status"] = run_status(call, polled, step["failed"])
            break


def run_items(assigned: list[SourceRecord]) -> list[dict]:
    """The other runs of these records (scripts, analyses, jobs started), as work items `R1`, `R2`, … for the story's
    outline. A run becomes an event only when the story points at it, so a slice of many runs stays readable."""
    from .analysis import tool_steps
    by_id = {r.source_id: r for r in assigned}
    found = []
    steps = tool_steps(assigned)
    for step in steps:
        call = by_id.get(step["call"])
        inline = inline_run(call.content) if step["hint"] == "edit" and not step["failed"] and call is not None else None
        if step["hint"] != "run" and not inline:
            continue
        status = run_status(call, by_id.get(step["result"]), step["failed"] and not inline)
        target = inline or step["target"]
        found.append({"id": f"R{len(found) + 1}", "kind": "run", "run": True, "status": status, "target": target,
                      "title": "Run: " + target[:80], "sources": [step["call"]] + ([step["result"]] if step["result"] else []),
                      # Run by the same call right after its patch (the call's edit item ends with it).
                      **({"inline": True} if inline else {})})
        _follow_polls(found[-1], steps, by_id)
    return found


def code_title(item: dict, language: str | None) -> str:
    """A work item's title as code writes it into the graph, in the output language when it is Korean (the model's
    own name for the item replaces it when given)."""
    if language != "Korean":
        return item["title"]
    if item.get("edit"):
        shown = item["files"][:TITLE_FILES]
        more = len(item["files"]) - len(shown)
        return "수정: " + ", ".join(shown) + (f" 외 {more}개" if more > 0 else "")
    if item.get("check"):
        return ("검사 실패: " if item["status"] == "observed_failure" else "검사 통과: ") + item["check"][:60]
    if item["title"].startswith("Commit: "):
        return "커밋: " + item["title"].removeprefix("Commit: ")
    if item.get("run"):
        return "실행: " + item.get("target", "")[:80]
    return item["title"]


# The requests the story may answer from earlier stretches: one per session, whose turn these records continue.
OPEN_REQUESTS = 5


def request_items(assigned: list[SourceRecord]) -> list[dict]:
    """The person's requests in these records as `Q1`, `Q2`, …, made before the story call so the story can link
    them (`docs/plans/REQUEST_ANSWERS.md` §3)."""
    return [{"id": f"Q{n}", "source_id": r.source_id} for n, r in enumerate((r for r in assigned if is_user_prompt(r)), 1)]


def open_requests(graph: dict, assigned: list[SourceRecord], source_of) -> dict[str, dict]:
    """The request whose turn these records continue, per session: the session's last request before them, answered
    in part or not at all (a request a later one followed has had its turn; offering it let a reply to the later one be
    taken for its answer). Event ID → its session (provider, session). `source_of(evidence_id)` gives the cited
    record's metadata. Judged on the whole graph, not on the events shown."""
    starts: dict[tuple, str] = {}
    for record in assigned:
        if record.session_id and record.recorded_at:
            key = (record.provider, record.session_id)
            starts[key] = min(starts.get(key, record.recorded_at), record.recorded_at)
    last: dict[tuple, tuple[str, str]] = {}
    for event in graph.get("events", []):
        if event["kind"] != "question" or event.get("actor") != "user" or not event.get("recorded_at"):
            continue
        meta = next((m for m in map(source_of, event.get("evidence_ids", [])) if m), None)
        key = (meta.get("provider"), meta.get("session_id")) if meta else None
        if key in starts and event["recorded_at"] < starts[key] and event["recorded_at"] > last.get(key, ("", ""))[0]:
            last[key] = (event["recorded_at"], event["id"])
    return {event_id: {"provider": key[0], "session_id": key[1]}
            for key, (_, event_id) in sorted(last.items(), key=lambda item: item[1], reverse=True)[:OPEN_REQUESTS]}


def written_blocks(assigned: list[SourceRecord]) -> list[dict]:
    """The evidence blocks of what the person and the assistant wrote in these records (harness text left out)."""
    from .blocks import number_blocks
    written = [r for r in assigned if r.role == "assistant" or is_user_prompt(r) or question_reply(r) is not None]
    return number_blocks(written)


def readable_output(text: str) -> list[str]:
    """A run's result as lines a reader can use: a line that is a JSON object carrying the command's `output` (Codex
    wraps what a command printed as `{"chunk_id": …, "exit_code": 0, "output": "…"}` on one line) is replaced by the
    lines of that output, so its last lines are what the command printed, not the wrapper's head."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and '"output"' in stripped:
            try:
                wrapped = json.loads(stripped)
            except ValueError:
                wrapped = None
            if isinstance(wrapped, dict) and isinstance(wrapped.get("output"), str):
                lines.extend(wrapped["output"].splitlines())
                continue
        lines.append(line)
    return [line for line in lines if line.strip()]


def _failure_tail(text: str) -> list[str]:
    return [line[:300] for line in readable_output(text)[-FAILURE_TAIL_LINES:]]


# How much of a run's command the story sees beside its last lines (one line; an inline script shows what it reads).
COMMAND_CHARS = 400


# The runs and checks right after a run of edits show their last lines, so the story can tell which one tried them.
RUNS_AFTER_EDIT = 3


def _runs_after_edits(items: list[dict], runs: list[dict], position: dict[str, int]) -> set[str]:
    """The IDs of the first RUNS_AFTER_EDIT runs and checks after each edit item, in record order, same session."""
    def at(entry):
        return min((position[s] for s in entry["sources"] if s in position), default=-1)
    steps = sorted([e for e in items if e.get("check")] + list(runs), key=at)
    chosen = set()
    for edit in (e for e in items if e.get("edit")):
        end = max((position[s] for s in edit["sources"] if s in position), default=-1)
        # A run in the edit's own last call (after its patch) comes first.
        chosen |= {e["id"] for e in [e for e in steps if at(e) > end or (e.get("inline") and e["sources"][0] in edit["sources"])]
                   [:RUNS_AFTER_EDIT]}
    return chosen


def _command(entry: dict, by_id: dict[str, SourceRecord]) -> str:
    """The run's whole command on one line, read from its call (the outline's target is cut short), cut at
    COMMAND_CHARS."""
    from .analysis import _command_text
    call = next((by_id[s] for s in entry["sources"] if s in by_id and by_id[s].role == "tool_call"), None)
    text = _command_text(call.content.partition("\n")[2] or call.content) if call is not None else ""
    return (text or " ".join(str(entry.get("target") or entry.get("check") or "").split()))[:COMMAND_CHARS]


def _last_result(entry: dict, by_id: dict[str, SourceRecord]) -> SourceRecord | None:
    """The result that settled a run: its own, or the last poll of a run Codex left running (`_follow_polls`)."""
    return next((by_id[s] for s in reversed(entry["sources"]) if s in by_id and by_id[s].role == "tool_result"), None)


def _output_tail(entry: dict, by_id: dict[str, SourceRecord]) -> list[str]:
    result = _last_result(entry, by_id)
    return _failure_tail(result.content) if result is not None else []


def story_request(unit: dict, snapshot_id: str, data: dict, items: list[dict], assigned: list[SourceRecord],
                  blocks: list[dict], runs: list[dict] | None = None) -> dict:
    """What the story call reads: no tool output, only the written blocks, the work outline and failed runs' tails."""
    from .analysis import shown_line
    by_id = {r.source_id: r for r in assigned}
    work = []
    for item in items:
        entry = {"id": item["id"], "kind": item["kind"], "status": item["status"], "title": item["title"]}
        lineage = next(((by_id[s].lineage or {}) for s in item["sources"] if s in by_id), {})
        if lineage.get("kind") == "subagent":
            entry["agent"] = lineage.get("agent_type") or lineage.get("agent_nickname") or "sub-agent"
        if item["status"] == "observed_failure":
            result = next((by_id[s] for s in item["sources"] if s in by_id and by_id[s].role == "tool_result"), None)
            if result is not None:
                entry["failure_tail"] = _failure_tail(result.content)
        work.append(entry)
    position = {r.source_id: n for n, r in enumerate(assigned)}
    after_edit = _runs_after_edits(items, runs or [], position)
    for item, entry in zip(items, work):
        if item.get("check") and item["id"] in after_edit:
            entry["command"] = _command(item, by_id)
            if item["status"] == "observed_success":
                entry["output_tail"] = _output_tail(item, by_id)
    for run in runs or []:
        entry = {"id": run["id"], "kind": "run", "status": run["status"], "title": run["title"]}
        if run["id"] in after_edit:
            entry["command"] = _command(run, by_id)
        if run["status"] == "failed":
            result = _last_result(run, by_id)
            if result is not None:
                entry["failure_tail"] = _failure_tail(result.content)
        elif run["status"] == "passed" and run["id"] in after_edit:
            entry["output_tail"] = _output_tail(run, by_id)
        work.append(entry)
    # Work items carry no time, so the story could not tell which written block came after a run: each names the
    # first block the assistant wrote after it in its session, where a report of it would start.
    for item, entry in zip(items + list(runs or []), work):
        following = next_written(item, blocks, by_id, position)
        if following:
            entry["next_written"] = following
    asked = {item["source_id"]: item["id"] for item in request_items(assigned)}
    requests = [{"id": asked[r["source_id"]], **r} for r in data["user_requests"] if r["source_id"] in asked]
    return {"unit_id": unit["id"], "snapshot_id": snapshot_id, "user_requests": requests,
            "open_requests": sorted(data.get("open_requests", [])),
            "written": [{"block": b["id"], "source_id": b["source_id"],
                         "role": by_id[b["source_id"]].role, "recorded_at": by_id[b["source_id"]].recorded_at,
                         "text": "\n".join(shown_line(line) for line in b["text"].splitlines())} for b in blocks],
            "work": work, "existing_events": data.get("existing_events", []),
            "existing_edges": data.get("existing_edges", [])}


def next_written(item: dict, blocks: list[dict], by_id: dict[str, SourceRecord], position: dict[str, int]) -> str | None:
    """The first block the assistant wrote after the item's last record, in the item's session, or None."""
    ends = [s for s in item["sources"] if s in position]
    if not ends:
        return None
    finished, first = max(position[s] for s in ends), by_id[ends[0]]
    return next((b["id"] for b in blocks if b["source_id"] in position and position[b["source_id"]] > finished
                 and by_id[b["source_id"]].role == "assistant"
                 and (by_id[b["source_id"]].provider, by_id[b["source_id"]].session_id) == (first.provider, first.session_id)),
                None)


def story_check(answer: dict) -> Any:
    from .schema import validate_shape
    validate_shape(answer, STORY_SCHEMA)
    return None


def claimed_check(link: dict, work: dict[str, dict], blocks: list[dict], pool: dict[str, SourceRecord],
                  position: dict[str, int]) -> str | None:
    """Why a `verifies` the story claims from an edit to a run is not kept, or None to keep it. The story reads no
    tool output, so the claim rests on the assistant's own report of what the run showed; code keeps it only when
    the facts it can see agree: an edit item and a run or check with a known result (exit status), the run after
    the edit's last record in the same session, and a cited block the assistant wrote after the run."""
    edit, run = work.get(link["from"]), work.get(link["to"])
    if edit is None or not edit.get("edit"):
        return "not from an edit"
    if run is None or not (run.get("check") or run.get("run")):
        return "not to a run"
    if run["status"] not in ("passed", "failed", "observed_success", "observed_failure"):
        return "the run's result is unknown"
    # Order is the records' order in the unit, not their time stamps (equal or missing stamps stay in order).
    edited = [pool[s] for s in edit["sources"] if s in pool and s in position]
    ran = [pool[s] for s in run["sources"] if s in pool and s in position]
    if not edited or not ran or {(r.provider, r.session_id) for r in edited} != {(r.provider, r.session_id) for r in ran}:
        return "not in the edit's session"
    call = next((r for r in ran if r.role == "tool_call"), ran[0])
    # A run in the edit's own call ran after its patch (`inline_run`); any other must start after the edit's records.
    same_call = run.get("inline") and call.source_id in edit["sources"]
    if position[call.source_id] < max(position[r.source_id] for r in edited) and not same_call:
        return "the run came before the edit"
    if not report_blocks(link, work, blocks, pool, position):
        return "no block where the assistant reports the run"
    return None


def report_blocks(link: dict, work: dict[str, dict], blocks: list[dict], pool: dict[str, SourceRecord],
                  position: dict[str, int]) -> list[str]:
    """The cited blocks of a claimed check that the assistant wrote after the run finished: the only ones kept as its
    evidence."""
    finished = max((position[s] for s in work[link["to"]]["sources"] if s in position), default=None)
    if finished is None:
        return []
    by_id = {b["id"]: b for b in blocks}
    return [b for b in link.get("blocks", []) if b in by_id and by_id[b]["source_id"] in position
            and pool[by_id[b]["source_id"]].role == "assistant" and position[by_id[b]["source_id"]] > finished]


def reply_blocks(link: dict, requests: dict[str, SourceRecord], earlier: dict[str, dict], blocks: list[dict],
                 pool: dict[str, SourceRecord], position: dict[str, int]) -> list[str]:
    """The cited blocks of a claimed answer that the assistant wrote in the request's session, after the request when
    it is in this stretch: the only ones kept as its evidence."""
    before = len(position)
    if link["from"] in requests:
        asked = requests[link["from"]]
        session, after = (asked.provider, asked.session_id), position[asked.source_id]
    elif link["from"] in earlier:
        session, after = (earlier[link["from"]]["provider"], earlier[link["from"]]["session_id"]), -1
        before = turn_end(session, pool, position)
    else:
        return []
    by_id = {b["id"]: b for b in blocks}
    kept = []
    for block in link.get("blocks", []):
        record = pool.get(by_id[block]["source_id"]) if block in by_id else None
        if record is not None and record.source_id in position and record.role == "assistant" \
                and (record.provider, record.session_id) == session and after < position[record.source_id] < before:
            kept.append(block)
    return kept


def turn_end(session: tuple, pool: dict[str, SourceRecord], position: dict[str, int]) -> int:
    """Where the turn an earlier request left open ends in these records: the session's first message of the person's
    (or one sent through the tool's queue); past it, a reply belongs to that message's turn."""
    return min((n for source_id, n in position.items() if source_id in pool
                and (pool[source_id].provider, pool[source_id].session_id) == session
                and (is_user_prompt(pool[source_id]) or is_queued_message(pool[source_id]))), default=len(position))


def reversed_link(link: dict, work: dict[str, dict], requests: dict, earlier: dict) -> bool:
    """A claimed `verifies` from a run or check to an edit, or an `answers` to a request from something that is not
    one, has one reading: the other way round (as `schema.orient_relation` reads a written-in-reverse relation)."""
    if link["relation"] == "verifies":
        source, target = work.get(link["from"]), work.get(link["to"])
        return bool(source and target and not source.get("edit") and (source.get("check") or source.get("run"))
                    and target.get("edit"))
    if link["relation"] == "answers":
        asks = set(requests) | set(earlier)
        return link["to"] in asks and link["from"] not in asks
    return False


def claimed_answer(link: dict, requests: dict[str, SourceRecord], earlier: dict[str, dict], present: set[str],
                   blocks: list[dict], pool: dict[str, SourceRecord], position: dict[str, int]) -> str | None:
    """Why an `answers` the story claims is not kept, or None to keep it. Code checks only what it can see: the link
    leaves a request (`Q…` of this stretch, or the earlier request whose turn the stretch continues) for an event of
    this stretch that is not a request, and cites a block the assistant wrote in the request's session after it (for
    the earlier request, before the session's next request here). Whether the block
    answers this request and not another is the story's reading (`docs/plans/REQUEST_ANSWERS.md` §4)."""
    if link["from"] not in requests and link["from"] not in earlier:
        return "not from a request"
    if link["to"] in requests or link["to"] in earlier or f"tmp:{link['to']}" not in present:
        return "not to an event of this stretch"
    by_id = {b["id"]: b for b in blocks}
    replies = [pool[by_id[b]["source_id"]] for b in link.get("blocks", []) if b in by_id and by_id[b]["source_id"] in position]
    replies = [r for r in replies if r.role == "assistant"]
    if not replies:
        return "no block of the assistant's reply"
    if link["from"] in requests:
        asked = requests[link["from"]]
        if not any((r.provider, r.session_id) == (asked.provider, asked.session_id)
                   and position[r.source_id] > position[asked.source_id] for r in replies):
            return "no reply after the request in its session"
    elif not any((r.provider, r.session_id) == (earlier[link["from"]]["provider"], earlier[link["from"]]["session_id"])
                 for r in replies):
        return "no reply in the request's session"
    elif not reply_blocks(link, requests, earlier, blocks, pool, position):
        return "the reply came after a later request"
    return None


def _request_title(text: str) -> str:
    from .analysis import _one_line
    return _one_line(text)


def story_extraction(answer: dict, items: list[dict], edges: list[dict], unit: dict, snapshot_id: str,
                     pool: dict[str, SourceRecord], provided: dict[str, list[tuple[int, int]]], validator,
                     blocks: list[dict], runs: list[dict] | None = None, language: str | None = None,
                     earlier: dict[str, dict] | None = None) -> tuple[dict, dict]:
    """The story answer and the skeleton as an ordinary extraction output, and what code learned about it
    (`unclaimed`: the assistant's blocks no story event rests on; `turn_summary`). `earlier`: the open requests of
    earlier stretches the story may answer (`open_requests`)."""
    from .blocks import citations as block_citations
    from .schema import record_evidence
    runs, earlier = runs or [], earlier or {}
    position = {source_id: n for n, source_id in enumerate(unit["sources"])}
    assigned = [pool[i] for i in unit["sources"] if i in pool]
    requests = {item["id"]: pool[item["source_id"]] for item in request_items(assigned)}
    named = {entry["id"]: entry for entry in answer.get("work", [])}
    # The runs the story points at become results of their own; the rest stay in the records.
    work_items = {item["id"]: item for item in items + runs}
    story_ids = {f"tmp:{entry['id']}" for entry in answer.get("story", []) if not entry["id"].startswith("ev_")}
    links, turned = [], []
    claimed = [{"from": entry["id"], "to": check["run"], "relation": "verifies", "blocks": check.get("blocks", [])}
               for entry in answer.get("work", []) for check in entry.get("checked_by", [])]
    claimed += [{"from": asked["id"], "to": reply["item"], "relation": "answers", "blocks": reply.get("blocks", [])}
                for asked in answer.get("requests", []) for reply in asked.get("answered_by", [])]
    seen: dict[tuple, dict] = {}
    for link in answer.get("links", []) + claimed:
        if reversed_link(link, work_items, requests, earlier):
            turned.append(f"{link['from']} → {link['to']}")
            link = {**link, "from": link["to"], "to": link["from"]}
        key = (link["from"], link["to"], link["relation"])
        if key in seen:
            # The same claim in the links and in its own field: one claim, with the blocks of both.
            seen[key]["blocks"] = list(dict.fromkeys(seen[key]["blocks"] + list(link.get("blocks", []))))
            continue
        seen[key] = link = {**link, "blocks": list(link.get("blocks", []))}
        links.append(link)
    # A check or an answer the story claims and code refuses names no run: the refused claim keeps no event alive.
    refusals = {}
    for n, link in enumerate(links):
        if link["relation"] == "verifies":
            refusals[n] = claimed_check(link, work_items, blocks, pool, position)
        elif link["relation"] == "answers":
            refusals[n] = claimed_answer(link, requests, earlier, story_ids | {f"tmp:{w}" for w in work_items},
                                         blocks, pool, position)
    pointed = {w for entry in answer.get("story", []) for w in entry.get("work", [])} | {
        end for n, link in enumerate(links) if not refusals.get(n) for end in (link["from"], link["to"])}
    shown_runs = [{**run, "kind": "outcome", "status": {"failed": "observed_failure", "passed": "observed_success"}.get(run["status"], "unknown")}
                  for run in runs if run["id"] in pointed]
    work_names = [{"id": item["id"], "title": (named.get(item["id"]) or {}).get("title") or code_title(item, language),
                   "summary": (named.get(item["id"]) or {}).get("summary") or ""} for item in items + shown_runs]
    base = extraction({"skeleton": work_names, "events": []}, items + shown_runs, edges, unit, snapshot_id,
                      pool, provided, validator)
    candidates, relations = base["event_candidates"], base["edge_candidates"]
    # The person's requests, made by code before the story was asked, so it can answer them by ID.
    for key, record in requests.items():
        evidence = record_evidence(record)
        candidates.append({"id": f"tmp:{key}", "kind": "question", "title": _request_title(record.content),
                           "summary": record.content.strip()[:4000], "actor": "user", "status": "asked",
                           "basis": "explicit_statement", "session_ids": [], "worktree_ids": [], "occurred_at": None,
                           "recorded_at": record.recorded_at,
                           "evidence": [{"source_id": record.source_id, "start_line": 1, "end_line": evidence["end_line"],
                                         "quote": evidence["quote"]}]})
    present = {c["id"] for c in candidates}
    reserved = set(requests) | set(work_items) | {b["id"] for b in blocks}
    dropped, taken, cited = [], [], set()
    item_sources = {item["id"]: item["sources"] for item in items + runs}

    def blocks_cited(block_ids: list[str]) -> list[dict]:
        # A block is cut by code like any light citation: its lines as shown, the quote kept by code.
        return [cite(pool[c["source_id"]], provided.get(c["source_id"], []), c["start_line"], c["end_line"])
                for c in block_citations(block_ids, blocks) if c["source_id"] in pool]

    def evidence_of(block_ids: list[str], work_ids: list[str]) -> list[dict]:
        found = blocks_cited(block_ids)
        if not found:
            found = [cite(pool[s], provided.get(s, [])) for w in work_ids for s in item_sources.get(w, []) if s in pool]
        return _checked([c for c in found if c], validator)

    by_tmp = {}
    for entry in answer.get("story", []):
        key = f"tmp:{entry['id']}"
        # A story event's ID is its own: one that names a request, a work item, a block or an existing event (E…, which
        # the reply's aliases turn into `ev_…`) would be taken for that and is refused.
        if key in present or entry["id"] in reserved or entry["id"].startswith("ev_"):
            taken.append(entry["id"])
            continue
        evidence = evidence_of(entry.get("blocks", []), entry.get("work", []))
        if not evidence:
            dropped.append(entry["id"])
            continue
        cited |= set(entry.get("blocks", []))
        records = [pool[c["source_id"]] for c in evidence]
        summary = " ".join(part for part in (entry.get("why") or "", entry.get("summary") or "") if part.strip())
        candidate = {"id": key, "kind": entry["kind"], "title": entry["title"][:240] or entry["kind"],
                     "summary": summary[:4000], "actor": "assistant", "status": entry["status"],
                     "basis": "tool_record" if any(r.role in {"tool_call", "tool_result"} for r in records) else "explicit_statement",
                     "session_ids": [], "worktree_ids": [], "occurred_at": None,
                     "recorded_at": next((r.recorded_at for r in records if r.recorded_at), None), "evidence": evidence}
        candidates.append(candidate)
        present.add(key)
        by_tmp[key] = candidate
        # What the story event says about the work: a decision or goal led to it, a finding rests on it.
        for work in entry.get("work", []):
            target = f"tmp:{work}"
            if target not in present:
                continue
            leads = entry["kind"] != "outcome"
            relations.append({"id": f"tmp:W{len(relations) + 1}", "from_event_id": key if leads else target,
                              "to_event_id": target if leads else key, "relation": "motivates" if leads else "produces",
                              "basis": "inferred", "evidence": evidence[:1], "active": True,
                              "rationale": "The story names this work as what the event led to or rests on."})
    refused = []
    for n, link in enumerate(links):
        source = f"tmp:{link['from']}" if f"tmp:{link['from']}" in present else link["from"]
        target = f"tmp:{link['to']}" if f"tmp:{link['to']}" in present else link["to"]
        if link["relation"] == "verifies":
            why = refusals[n]
            if why:
                refused.append(f"{link['from']} → {link['to']} ({why})")
                continue
            ran = [cite(pool[s], provided.get(s, [])) for s in work_items[link["to"]]["sources"]
                   if s in pool and pool[s].role == "tool_result"]
            evidence = _checked([c for c in blocks_cited(report_blocks(link, work_items, blocks, pool, position)) + ran if c],
                                validator)
            if not any(pool[c["source_id"]].role == "assistant" for c in evidence):
                refused.append(f"{link['from']} → {link['to']} (its report block was not kept)")
                continue
            if any((r["from_event_id"], r["to_event_id"], r["relation"]) == (source, target, "verifies") for r in relations):
                continue  # code already proved it
            cited |= set(link["blocks"])
            relations.append({"id": f"tmp:L{len(relations) + 1}", "from_event_id": source, "to_event_id": target,
                              "relation": "verifies", "basis": "explicit", "evidence": evidence, "active": True,
                              "rationale": "The assistant reports what this run showed about the edit; the run came "
                                           "after the edit in the same session, and its exit status gives the result."})
            continue
        if link["relation"] == "answers":
            why = refusals[n] or (None if target in present or target in earlier else "its answer was not kept")
            # An answer rests on the reply's blocks alone, never on its ends' evidence.
            evidence = [] if why else _checked(
                [c for c in blocks_cited(reply_blocks(link, requests, earlier, blocks, pool, position)) if c], validator)
            if not any(pool[c["source_id"]].role == "assistant" for c in evidence):
                refused.append(f"{link['from']} → {link['to']} ({why or 'its reply block was not kept'})")
                continue
            cited |= set(link["blocks"])
            relations.append({"id": f"tmp:L{len(relations) + 1}", "from_event_id": source, "to_event_id": target,
                              "relation": "answers", "basis": "explicit", "evidence": evidence, "active": True,
                              "rationale": "The story reads this reply of the assistant as the answer to the request."})
            continue
        evidence = _checked([c for c in blocks_cited(link.get("blocks", [])) if c], validator)
        if not evidence:
            ends = [c for c in candidates if c["id"] in (source, target)]
            evidence = ends[0]["evidence"][:1] if ends else []
        if not evidence:
            continue
        same = next((r for r in relations if (r["from_event_id"], r["to_event_id"], r["relation"]) == (source, target, link["relation"])), None)
        if same is not None:
            # The story named this work for the event and linked them as well: one relation, shown by its blocks if any.
            if link.get("blocks") and same["basis"] == "inferred":
                same.update(basis="explicit", evidence=evidence, rationale="Linked by the story; its evidence blocks show the link.")
            continue
        relations.append({"id": f"tmp:L{len(relations) + 1}", "from_event_id": source, "to_event_id": target,
                          "relation": link["relation"], "basis": "explicit" if link.get("blocks") else "inferred",
                          "evidence": evidence, "active": True,
                          "rationale": "Linked by the story; its evidence blocks show the link." if link.get("blocks")
                          else "Linked by the story without a block that shows it."})
    if taken:
        base["limitations"] = base["limitations"] + [
            f"story: event not kept, its ID names a request, work item, block or earlier event: {', '.join(taken)}."]
    if dropped:
        base["limitations"] = base["limitations"] + [f"story: no evidence left for {', '.join(dropped)}."]
    if turned:
        base["limitations"] = base["limitations"] + [f"story: link written in reverse, read the other way: {'; '.join(turned)}."]
    if refused:
        base["limitations"] = base["limitations"] + [f"story: link not kept: {'; '.join(refused)}."]
    unclaimed = [b["id"] for b in blocks if b["id"] not in cited and pool[b["source_id"]].role == "assistant"]
    return base, {"unclaimed_blocks": [(b["source_id"], b["start_line"], b["end_line"]) for b in blocks if b["id"] in unclaimed],
                  "turn_summary": answer.get("turn_summary")}
