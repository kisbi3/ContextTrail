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

import re
from pathlib import PurePosixPath
from typing import Any

from .model import SourceRecord, is_user_prompt
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
    """The dialog turn of each record: a message the person typed starts a new one."""
    turn, turns = 0, {}
    for record in assigned:
        if is_user_prompt(record):
            turn += 1
        turns[record.source_id] = turn
    return turns


def _files(target: str) -> list[str]:
    names = [part for part in target.removeprefix("patch ").replace(",", " ").split() if part]
    return [PurePosixPath(name).name for name in names] or [target]


def covers(command: str, files: list[str]) -> str | None:
    """Why a test run counts as a check of edited files, or None: the run names one of them (its path, or its module
    as in `tests/test_calc.py` for `calc.py`), or it runs the whole suite (no test path or id given). A run of
    other, named tests is not a check of these edits, however close in time (`docs/DECISIONS.md` 2026-10-18)."""
    from .analysis import TEST_COMMAND, _command_segments
    runs = [segment for segment in _command_segments(command) if TEST_COMMAND.match(segment)]
    if not runs:
        return None
    for run in runs:
        named = next((f for f in files if f.rsplit("/", 1)[-1] in run
                      or len(f.rsplit("/", 1)[-1].rsplit(".", 1)[0]) > 2 and f.rsplit("/", 1)[-1].rsplit(".", 1)[0] in run), None)
        if named:
            return f"The test run names the edited {named.rsplit('/', 1)[-1]} (the file or its module)."
    for run in runs:
        targets = [word for word in run.split()[1:] if not word.startswith("-") and word not in ("./...", ".")
                   and any(mark in word for mark in ("/", ".", "::"))]
        if not targets:
            return "The whole test suite ran after this edit in the same turn."
    return None


def skeleton(assigned: list[SourceRecord]) -> tuple[list[dict], list[dict]]:
    """Events and `verifies` relations the tool steps show, with the records each rests on (call first)."""
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
