from __future__ import annotations

import bisect
import copy
import re
from typing import Any, Callable

from jsonschema import Draft202012Validator

from .model import GRAPH_SCHEMA_VERSION, SourceRecord, is_user_prompt
from .util import FlowError, digest, ident, merge_focus


KINDS = ["goal", "question", "proposal", "decision", "action", "outcome", "revision"]
STATUSES = ["proposed", "adopted", "in_progress", "asked", "applied", "reported_complete", "observed_success",
            "observed_failure", "withdrawn", "unknown"]
BASES = ["explicit_statement", "tool_record", "git_artifact", "inference"]
RELATIONS = ["follows", "motivates", "produces", "revises", "verifies", "answers"]
# A change and the run that checked it are separate events joined by `verifies`, so a
# success badge never covers more than the output actually exercised.
CHANGE_KINDS = {"action", "revision"}
OBSERVED = {"observed_success", "observed_failure"}
QUESTION_STATUSES = {"asked", "withdrawn", "unknown"}
# Quotes shorter than this are accepted only where they cannot mean two places.
SHORT_QUOTE_CHARS = 8
# The two quote errors the repair round answers with the exact source lines; the mismatch audit
# and `Harness` match these heads.
QUOTE_MISMATCH_HEADS = ("quote differs from the frozen source: ", "quote not found uniquely in the cited lines: ")
STR = {"type": "string"}
NULLSTR = {"type": ["string", "null"]}
STRS = {"type": "array", "items": STR}


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def arr(schema: dict, minimum: int = 0) -> dict:
    return {"type": "array", "items": schema, "minItems": minimum}


def enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


# `quote` is optional: without it, code keeps the cited lines themselves (`EvidenceValidator.fill_cut_quotes`),
# so a citation can never fail for a quote copied wrongly; with it, the quote names what the lines show.
# Every key stays required (OpenAI's strict structured output refuses an optional key); `quote` may be null.
CITATION = obj({"source_id": STR, "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1}, "quote": {"type": ["string", "null"]}})
# A quote code cuts covers at most this many of the cited lines; the rest is one `show` away.
CUT_QUOTE_LINES = 20
CITATIONS = arr(CITATION, 1)
EVENT_FIELDS = {"kind": enum(KINDS), "title": {"type": "string", "minLength": 1, "maxLength": 240},
                "summary": {"type": "string", "maxLength": 4000}, "actor": STR, "status": enum(STATUSES),
                "basis": enum(BASES), "session_ids": STRS, "worktree_ids": STRS,
                "recorded_at": NULLSTR, "occurred_at": NULLSTR}
EVENT = obj({"id": STR, **EVENT_FIELDS, "evidence": CITATIONS})
EDGE = obj({"id": STR, "from_event_id": STR, "to_event_id": STR,
            "relation": enum(RELATIONS),
            "basis": enum(["explicit", "structural", "inferred"]), "evidence": CITATIONS,
            "rationale": {"type": "string", "maxLength": 2000}, "active": {"type": "boolean", "const": True}})
OPEN_ITEM = obj({"id": STR, "text": STR, "status": enum(["open", "resolved"]),
                 "related_event_ids": STRS, "evidence": CITATIONS})
REVIEW_ISSUE = obj({"id": STR, "origin": enum(["code_signal", "model_uncertainty"]),
    "stage": enum(["extract", "integrate"]), "target_kind": enum(["event_candidate", "edge_candidate",
        "open_item_candidate", "event", "edge", "open_item"]), "target_id": STR,
    "signal": {"type": "string", "minLength": 1, "maxLength": 500},
    "question": {"type": "string", "minLength": 1, "maxLength": 1000}, "evidence": CITATIONS})
REVIEW_RESOLUTION = obj({"issue_id": STR, "status": enum(["resolved", "modified", "excluded", "unresolved"]),
    "reason": {"type": "string", "minLength": 1, "maxLength": 1000}, "evidence": CITATIONS})
READ_KINDS = ["read_records", "read_existing_event", "read_diff", "read_file_at_revision", "search_events"]
READ_REQUEST = obj({"kind": enum(READ_KINDS), "ids": arr(STR),
    "start_line": {"type": ["integer", "null"], "minimum": 1},
    "end_line": {"type": ["integer", "null"], "minimum": 1},
    "query": {"type": ["string", "null"], "minLength": 1, "maxLength": 500},
    "unit_id": NULLSTR})
ENVELOPE = {"status": enum(["needs_evidence", "complete"]), "read_requests": arr(READ_REQUEST),
            "snapshot_id": STR}
EXTRACT_SCHEMA = obj({**ENVELOPE, "unit_id": STR, "event_candidates": arr(EVENT), "edge_candidates": arr(EDGE),
                      "existing_event_matches": arr(obj({"candidate_id": STR, "existing_event_id": STR,
                                                          "reason": STR, "evidence": CITATIONS})),
                      "open_items": arr(OPEN_ITEM), "limitations": STRS, "unprocessed_record_ids": STRS})
CHANGES = obj({name: {"anyOf": [schema, {"type": "null"}]} for name, schema in EVENT_FIELDS.items()})
DELTA_SCHEMA = obj({**ENVELOPE, "base_graph_version": {"type": "integer", "minimum": 0},
                    "events_to_add": arr(EVENT),
                    "events_to_update": arr(obj({"id": STR, "reason": STR, "evidence": CITATIONS, "changes": CHANGES})),
                    "edges_to_add": arr(EDGE),
                    "edges_to_invalidate": arr(obj({"id": STR, "reason": STR, "evidence": CITATIONS})),
                    "open_items_to_upsert": arr(OPEN_ITEM),
                    "open_items_to_resolve": arr(obj({"id": STR, "reason": STR, "evidence": CITATIONS})),
                    "candidate_resolutions": arr(obj({"candidate_id": STR,
                        "candidate_kind": enum(["event", "edge", "open_item"]),
                        "disposition": enum(["added", "updated", "duplicate", "excluded"]),
                        "target_ids": STRS, "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "evidence": CITATIONS})),
                    "change_attributions": arr(obj({"operation": enum(["events_to_add", "events_to_update",
                        "edges_to_add", "edges_to_invalidate", "open_items_to_upsert", "open_items_to_resolve"]),
                        "item_id": STR, "candidate_ids": arr(STR, 1),
                        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "evidence": CITATIONS})),
                     "review_issues": arr(REVIEW_ISSUE), "review_resolutions": arr(REVIEW_RESOLUTION),
                     "limitations": STRS})
# The delta sections that change the graph.
DELTA_ITEM_ARRAYS = ("events_to_add", "events_to_update", "edges_to_add", "edges_to_invalidate",
                     "open_items_to_upsert", "open_items_to_resolve")


DRAFT_ADDED_REASON = "extraction candidate added as is"
DRAFT_DUPLICATE_REASON = "this candidate repeats an existing event"
DRAFT_SELF_EDGE_REASON = "both ends became the same event after the existing-event match"


REFRESH_REASON = "the unit was read again after its records changed, and this reading says something else about the event"


def refresh_matched(delta: dict, candidates: dict, graph: dict) -> int:
    """A candidate the draft settled as an existing event's duplicate that says something else about it (the unit was
    read again after one of its records changed) updates that event instead: its changed title, summary, status or
    time, with the candidate's evidence (the old evidence stays for audit). Returns how many events it updates."""
    events = {event["id"]: event for event in graph["events"]}
    by_id = {candidate["id"]: candidate for candidate in candidates["event_candidates"]}
    updated = set()
    for resolution in delta["candidate_resolutions"]:
        if resolution["candidate_kind"] != "event" or resolution["disposition"] != "duplicate":
            continue
        candidate, target = by_id.get(resolution["candidate_id"]), events.get(resolution["target_ids"][0])
        # Only a reading of the same kind of event says something else about it (a request repeating a person's
        # decision is not that decision rewritten).
        if candidate is None or target is None or target["id"] in updated or candidate["kind"] != target["kind"]:
            continue
        changes = {key: None for key in EVENT_FIELDS}
        changes.update({key: candidate[key] for key in ("title", "summary", "status", "recorded_at")
                        if candidate.get(key) and candidate[key] != target.get(key)})
        if all(value is None for value in changes.values()):
            continue
        updated.add(target["id"])
        delta["events_to_update"].append({"id": target["id"], "reason": REFRESH_REASON,
                                          "evidence": copy.deepcopy(candidate["evidence"]), "changes": changes})
        resolution.update(disposition="updated", reason=REFRESH_REASON)
        delta["change_attributions"].append({"operation": "events_to_update", "item_id": target["id"],
                                             "candidate_ids": [candidate["id"]], "reason": REFRESH_REASON,
                                             "evidence": copy.deepcopy(candidate["evidence"])})
    return len(updated)


def draft_delta(candidates: dict, graph_version: int, snapshot_id: str) -> dict:
    """The GraphDelta that takes the validated candidates as extracted, built in code.

    Every candidate is added under its own tmp id; a candidate matched to exactly one existing event
    (`analysis.match_existing_duplicates`, `match_noted_steps`) is a duplicate of it, and relations and
    open items follow it there. This is what a unit publishes (`Engine._integrate_unit`).
    """
    matches: dict[str, set[str]] = {}
    for match in candidates["existing_event_matches"]:
        matches.setdefault(match["candidate_id"], set()).add(match["existing_event_id"])
    same = {cid: next(iter(ids)) for cid, ids in matches.items() if len(ids) == 1}
    delta = {"status": "complete", "read_requests": [], "snapshot_id": snapshot_id,
             "base_graph_version": graph_version, "events_to_add": [], "events_to_update": [],
             "edges_to_add": [], "edges_to_invalidate": [], "open_items_to_upsert": [],
             "open_items_to_resolve": [], "candidate_resolutions": [], "change_attributions": [],
             "review_issues": [], "review_resolutions": [], "limitations": list(candidates["limitations"])}
    def settle(kind: str, item: dict, operation: str | None, target: str | None, reason: str) -> None:
        disposition = "excluded" if target is None else "added" if operation else "duplicate"
        delta["candidate_resolutions"].append({"candidate_id": item["id"], "candidate_kind": kind,
            "disposition": disposition, "target_ids": [target] if target else [], "reason": reason,
            "evidence": copy.deepcopy(item["evidence"])})
        if operation:
            delta["change_attributions"].append({"operation": operation, "item_id": target,
                "candidate_ids": [item["id"]], "reason": reason, "evidence": copy.deepcopy(item["evidence"])})
    for item in candidates["event_candidates"]:
        if item["id"] in same:
            settle("event", item, None, same[item["id"]], DRAFT_DUPLICATE_REASON)
        else:
            delta["events_to_add"].append(copy.deepcopy(item))
            settle("event", item, "events_to_add", item["id"], DRAFT_ADDED_REASON)
    for item in candidates["edge_candidates"]:
        edge = {**copy.deepcopy(item), "from_event_id": same.get(item["from_event_id"], item["from_event_id"]),
                "to_event_id": same.get(item["to_event_id"], item["to_event_id"])}
        if edge["from_event_id"] == edge["to_event_id"]:
            settle("edge", item, None, None, DRAFT_SELF_EDGE_REASON)
            continue
        delta["edges_to_add"].append(edge)
        settle("edge", item, "edges_to_add", item["id"], DRAFT_ADDED_REASON)
    for item in candidates["open_items"]:
        delta["open_items_to_upsert"].append({**copy.deepcopy(item), "related_event_ids": list(dict.fromkeys(
            same.get(i, i) for i in item["related_event_ids"]))})
        settle("open_item", item, "open_items_to_upsert", item["id"], DRAFT_ADDED_REASON)
    return delta


# Escapes that appear verbatim when a tool call embeds code in a string literal
# (e.g. Codex `apply_patch("...\"$x\"...")`). Models often quote the decoded text.
_ESCAPES = {'"': '"', "'": "'", "\\": "\\", "n": "\n", "t": "\t", "r": "\r", "/": "/"}


def decode_escapes(text: str) -> tuple[str, list[int]]:
    """Decode one layer of string-literal escapes.

    Returns the decoded text and the raw offset of every decoded position plus the end,
    so a match in the decoded view maps back to an exact raw span.
    """
    decoded: list[str] = []
    offsets: list[int] = []
    i = 0
    while i < len(text):
        offsets.append(i)
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in _ESCAPES:
            decoded.append(_ESCAPES[text[i + 1]])
            i += 2
        else:
            decoded.append(text[i])
            i += 1
    offsets.append(len(text))
    return "".join(decoded), offsets


def occurrences(text: str, needle: str) -> list[int]:
    found, offset = [], 0
    while True:
        index = text.find(needle, offset)
        if index < 0:
            return found
        found.append(index)
        offset = index + 1


# A partial quote repeated inside the lines it expands to stores the same evidence
# whichever occurrence was meant; only the highlighted spans differ.
FOCUS_MAX_SPANS = 5
# The same text on both sides of one diff hunk (removed and added lines) is kept as
# one evidence span covering those lines, so neither side is picked on the model's behalf.
DIFF_PAIR_MAX_LINES = 20
# Repeats this close together are cited as the few lines that hold them all, not one guessed copy.
REPEAT_SPAN_MAX_LINES = 3


def _diff_body(line: str) -> bool:
    return bool(line) and line[0] in "+- " and not line.startswith(("+++ ", "--- "))


def resolve_quote(region: str, quote: str, *,
                  close_repeats: bool = False) -> tuple[list[tuple[int, int]], int, int, str] | tuple[None, int]:
    """Resolve a partial quote inside `region` (the provided lines joined by newlines).

    Returns (raw spans, first line index, last line index, mode) when the quote maps to
    one line span, else (None, match_count). Spans are raw offsets in `region`: an
    escape-decoded view (of the region, or of the quote) is only a lookup aid and never
    becomes stored text.
    """
    spans = [(found, found + len(quote)) for found in occurrences(region, quote)]
    prefix = ""
    if not spans and "\\" in region:
        text, raw_offsets = decode_escapes(region)
        spans = [(raw_offsets[found], raw_offsets[found + len(quote)]) for found in occurrences(text, quote)]
        prefix = "escape_decoded_" if spans else ""
    if not spans and "\\" in quote:
        # The reverse slip: the quote escapes a character the source holds plainly
        # (`content_included\": false` for `content_included": false`).
        plain = decode_escapes(quote)[0]
        spans = [(found, found + len(plain)) for found in occurrences(region, plain)] if plain != quote else []
        prefix = "quote_unescaped_" if spans else ""
    if not spans:
        return None, 0
    lines = region.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    bounds = [(bisect.bisect_right(offsets, s) - 1, bisect.bisect_right(offsets, e - 1) - 1) for s, e in spans]
    first, last = min(b[0] for b in bounds), max(b[1] for b in bounds)
    if len(spans) == 1:
        mode = prefix + "substring_expanded_to_lines" if prefix else "unique_exact_substring_expanded_to_lines"
    elif len(set(bounds)) == 1:
        mode = prefix + "repeated_substring_within_same_lines"
    elif (last - first < DIFF_PAIR_MAX_LINES
          and all(_diff_body(lines[i]) for a, b in bounds for i in range(a, b + 1))
          and not any(lines[i].startswith(("@@", "diff --git ")) for i in range(first, last + 1))):
        mode = prefix + "diff_hunk_substring_expanded_to_lines"
    elif close_repeats and last - first < REPEAT_SPAN_MAX_LINES:
        mode = prefix + "repeats_within_few_lines_expanded"
    else:
        return None, len(spans)
    return spans, first, last, mode


# A quote can miss the cited lines by exactly one slip and still say one thing: whitespace the
# model reflowed, `...` it added to skip lines, or a run past the end of a line. Each is read
# back only where the source still leaves a single reading, and only inside the cited lines.
ELLIPSIS = re.compile(r"\.\.\.|…")
TAIL_MIN_CHARS = 20
TAIL_MAX_CHARS = 2000
SLIP_CATEGORIES = {"whitespace_normalized": "whitespace_only", "ellipsis_pieces": "ellipsis_join",
                   "tail_past_line_end": "tail_past_line_end"}


def line_offsets(region: str) -> list[int]:
    offsets = [0]
    for line in region.split("\n"):
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def span_lines(offsets: list[int], spans: list[tuple[int, int]]) -> tuple[int, int]:
    bounds = [(bisect.bisect_right(offsets, s) - 1, bisect.bisect_right(offsets, e - 1) - 1) for s, e in spans]
    return min(b[0] for b in bounds), max(b[1] for b in bounds)


def collapse_whitespace(text: str) -> tuple[str, list[int], list[int]]:
    """One view of `text` with every whitespace run a single space, mapped back to raw offsets."""
    chars, starts, ends = [], [], []
    index = 0
    while index < len(text):
        end = index
        while end < len(text) and text[end].isspace():
            end += 1
        if end == index:  # a non-space character is a run of one
            end = index + 1
        chars.append(" " if text[index].isspace() else text[index])
        starts.append(index)
        ends.append(end)
        index = end
    return "".join(chars), starts, ends


def whitespace_span(region: str, quote: str) -> list[tuple[int, int]] | None:
    """The one raw span the quote names once both sides have their whitespace runs collapsed."""
    squeezed, starts, ends = collapse_whitespace(region)
    needle, _, _ = collapse_whitespace(quote)
    if not needle.strip():
        return None
    found = occurrences(squeezed, needle)
    if len(found) != 1:
        return None
    return [(starts[found[0]], ends[found[0] + len(needle) - 1])]


def ellipsis_spans(region: str, quote: str) -> list[tuple[int, int]] | None:
    """The spans of ellipsis pieces that each occur once in the region and read in order."""
    pieces = [piece.strip() for piece in ELLIPSIS.split(quote)]
    if len(pieces) < 2 or any(len(piece) < SHORT_QUOTE_CHARS for piece in pieces):
        return None
    spans, position = [], 0
    for piece in pieces:
        hits = occurrences(region, piece)
        if len(hits) != 1 or hits[0] < position:
            return None
        position = hits[0] + len(piece)
        spans.append((hits[0], position))
    return spans


def tail_span(region: str, quote: str) -> tuple[int, int] | None:
    """The longest quote prefix the region holds once, when it stops where a line of it stops.

    Only lengths that can still satisfy the 80% rule are looked at, largest first, so the
    first hit is the longest prefix the region has.
    """
    found, best = None, 0
    floor = max(TAIL_MIN_CHARS, -(-len(quote) * 4 // 5))
    for length in range(min(len(quote), len(region), TAIL_MAX_CHARS), floor - 1, -1):
        hits = occurrences(region, quote[:length])
        if hits:
            found, best = hits, length
            break
    if best == 0 or len(found) != 1:
        return None
    start = found[0]
    if found[0] + best == len(region) or region[found[0] + best] == "\n":
        return start, start + best
    return None


def resolve_quote_slips(region: str, quote: str) -> tuple[list[tuple[int, int]], int, int, str] | None:
    """Read a quote that missed the cited lines by one slip, or None when it still says more.

    Same contract as `resolve_quote`: the spans are raw offsets in `region`, so the stored
    quote stays exact source text and only the focus inside it is narrowed.
    """
    offsets = line_offsets(region)
    spans = whitespace_span(region, quote)
    if spans is not None:
        return (spans, *span_lines(offsets, spans), "whitespace_normalized")
    pieces = ellipsis_spans(region, quote)
    if pieces is not None:
        return (pieces, *span_lines(offsets, pieces), "ellipsis_pieces")
    tail = tail_span(region, quote)
    if tail is not None:
        return [tail], *span_lines(offsets, [tail]), "tail_past_line_end"
    return None


DOC_VERIFIES_REASON = ("dropped a verifies relation between a documentation-only change and a run "
                       "whose command never names that document (no evidence it checked the change)")
# Claude Code and Codex names, then opencode's lowercase ones.
EDIT_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "edit", "write", "multiedit"}
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")


def edited_files(record: SourceRecord) -> list[str]:
    """Files a tool call edits, read off patch markers or an edit tool's path argument."""
    if record.role != "tool_call":
        return []
    head, _, body = record.content.partition("\n")
    files = re.findall(r"\*\*\* (?:Update|Add|Delete) File: ([^\s\\\"]+)", body)
    if files:
        return list(dict.fromkeys(files))
    if head.removeprefix("Tool: ").strip() in EDIT_TOOL_NAMES:
        for key in ("file_path", "filePath", "notebook_path", "path"):
            found = re.search(rf'"{key}":\s*"([^"]+)"', body)
            if found:
                return [found.group(1)]
    return []


def docs_only(files: list[str]) -> bool:
    return bool(files) and all(f.lower().endswith(DOC_SUFFIXES) for f in files)


def record_evidence(record: SourceRecord, max_chars: int = 2000) -> dict:
    """Evidence the code itself cites: the record's opening whole lines, at least the first."""
    lines = record.content.splitlines() or [""]
    end = 1
    while end < len(lines) and len("\n".join(lines[:end + 1])) <= max_chars:
        end += 1
    quote = "\n".join(lines[:end])
    return {"id": ident("evi_", record.source_id, record.content_hash, 1, end, quote),
            "source_id": record.source_id, "source_version": record.source_version,
            "content_hash": record.content_hash, "start_line": 1, "end_line": end, "quote": quote,
            "quote_hash": digest(quote), "source": record.metadata()}


def orient_relation(edge: dict, events: dict[str, dict]) -> bool:
    """Turn a verifies/answers relation written the other way round; True if it was turned.

    "The test verifies the change" reads naturally, so models write outcome → change. The
    endpoint kinds leave only one meaning, so the pair is kept and only the direction fixed.
    """
    source, target = events.get(edge["from_event_id"]), events.get(edge["to_event_id"])
    if not source or not target:
        return False
    reversed_check = (edge["relation"] == "verifies" and source["kind"] == "outcome" and
                      source["status"] in OBSERVED and target["kind"] in CHANGE_KINDS)
    reversed_answer = (edge["relation"] == "answers" and target["kind"] == "question" and
                       source["kind"] != "question")
    if reversed_check or reversed_answer:
        edge["from_event_id"], edge["to_event_id"] = edge["to_event_id"], edge["from_event_id"]
        return True
    return False


def retype_answered_user_goals(events: list[dict], edges: list[dict],
                               records: dict[str, SourceRecord]) -> list[str]:
    """A person's message written as a goal (or proposal) is read as a question (asked).

    A typed message is a question (asked) or a decision (adopted), never a goal or proposal
    (the prompts say so; a model still writes one now and then, and then the request is lost
    from the request views and a forked session's first request cannot be linked). Only events
    citing nothing but the person's own messages are changed. Returns the changed IDs.
    """
    changed = []
    for event in events:
        if event["actor"] != "user" or event["kind"] not in {"goal", "proposal"}:
            continue
        sources = [records.get(citation["source_id"]) for citation in event["evidence"]]
        if sources and all(record is not None and is_user_prompt(record) for record in sources):
            event["kind"], event["status"] = "question", "asked"
            changed.append(event["id"])
    return changed


CALLER_KINDS = ("subagent", "unattended")


def drop_caller_instructions(output: dict, records: dict[str, SourceRecord]) -> list[str]:
    """A user-role message a calling agent wrote is not the person's: the instructions a sub-agent got from its
    parent, or the prompt of a run with no person at the terminal (`lineage.kind` subagent/unattended). An
    `actor=user` event citing nothing but such messages is removed from an extraction, with its relations and
    the open items' and matches' references to it; the delegation is drawn as the sub-agent's stack, and
    `link_request_turns` hangs the sub-agent's work under the parent's message. Returns the removed IDs."""
    dropped = set()
    for event in output["event_candidates"]:
        if event.get("actor") != "user":
            continue
        sources = [records.get(citation["source_id"]) for citation in event["evidence"]]
        if sources and all(record is not None and record.role == "user"
                           and (record.lineage or {}).get("kind") in CALLER_KINDS for record in sources):
            dropped.add(event["id"])
    if dropped:
        output["event_candidates"] = [e for e in output["event_candidates"] if e["id"] not in dropped]
        output["edge_candidates"] = [e for e in output["edge_candidates"]
                                     if e["from_event_id"] not in dropped and e["to_event_id"] not in dropped]
        output["existing_event_matches"] = [m for m in output["existing_event_matches"]
                                            if m["candidate_id"] not in dropped]
        for item in output["open_items"]:
            item["related_event_ids"] = [i for i in item["related_event_ids"] if i not in dropped]
    return sorted(dropped)


def check_relation(edge: dict, events: dict[str, dict]) -> None:
    """Endpoint rules for relations whose meaning depends on what they connect."""
    source, target = events[edge["from_event_id"]], events[edge["to_event_id"]]
    if edge["relation"] == "verifies":
        if source["kind"] not in CHANGE_KINDS or target["kind"] != "outcome" or target["status"] not in OBSERVED:
            raise FlowError("verifies must link a change (action/revision) to an observed outcome that ran it: "
                            f"{edge['id']} (kind=outcome, observed status)")
        if edge["basis"] == "inferred":
            raise FlowError("verifies must be explicit or structural, not inferred: "
                            f"{edge['id']} (leave it unlinked without evidence that the run covers the change)")
    elif edge["relation"] == "answers" and (source["kind"] != "question" or target["kind"] == "question"):
        raise FlowError(f"answers must lead from a question event to the event that answered it: {edge['id']}")


def validate_shape(value: dict, schema: dict) -> None:
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        messages = ["/".join(map(str, e.absolute_path)) + ": " + e.message[:220] for e in errors[:8]]
        raise FlowError("JSON schema error: " + "; ".join(messages))


class EvidenceValidator:
    def __init__(self, records: dict[str, SourceRecord], provided: dict[str, list[tuple[int, int]]],
                 existing: dict[str, dict] | None = None,
                 assigned_source_ids: set[str] | None = None,
                 required_citations: dict[str, tuple[str, str | None]] | None = None):
        self.records, self.provided = records, provided
        self.evidence: dict[str, dict] = existing or {}
        self.assigned_source_ids = assigned_source_ids
        # Tool calls that certainly changed a file (call → label, result): each must back
        # some extracted event, through the call or its result. Checked at extraction only.
        self.required_citations = required_citations or {}
        self.normalizations: list[dict] = []
        # Quotes that did not match, with why: the audit copy of these carries no quote text.
        self.mismatches: list[dict] = []
        # Partial quotes are canonicalized to whole lines before later checks re-read
        # them, so the span the model actually pointed at is remembered per evidence ID.
        self.focus: dict[str, list[list[int]]] = {}

    def _evidence_id(self, source_id: str, start: int, end: int, quote: str) -> str:
        return ident("evi_", source_id, self.records[source_id].content_hash, start, end, quote)

    def _remember_focus(self, evidence_id: str, focus: list[list[int]] | None) -> None:
        if focus:
            self.focus[evidence_id] = merge_focus(self.focus.get(evidence_id), focus)

    def inherit_focus(self, evidence: dict[str, dict]) -> None:
        """Keep focus from an earlier stage when its canonical citations are re-validated."""
        for evidence_id, item in evidence.items():
            if item.get("focus"):
                self.focus[evidence_id] = merge_focus(self.focus.get(evidence_id), item["focus"])

    def fill_cut_quotes(self, value: Any) -> int:
        """Give every citation without a quote the cited lines as its quote, cut by code: the lines the model was
        shown within the range, at most `CUT_QUOTE_LINES` (the range is narrowed to them). A range naming nothing
        shown is left for the checks to refuse. Walks the whole answer; returns how many were cut."""
        cut = 0
        if isinstance(value, dict):
            evidence = value.get("evidence")
            if isinstance(evidence, list):
                for citation in evidence:
                    if isinstance(citation, dict) and not citation.get("quote") and isinstance(citation.get("source_id"), str):
                        record = self.records.get(citation["source_id"])
                        start, end = citation.get("start_line"), citation.get("end_line")
                        if record is None or not isinstance(start, int) or not isinstance(end, int):
                            continue
                        lines = record.content.splitlines()
                        for lo, hi in self.provided.get(citation["source_id"], []):
                            s, e = max(lo, start, 1), min(hi, end, len(lines))
                            if s > e:
                                continue
                            e = min(e, s + CUT_QUOTE_LINES - 1)
                            quote = "\n".join(lines[s - 1:e])
                            if quote.strip():
                                citation.update(start_line=s, end_line=e, quote=quote)
                                self.normalizations.append({"mode": "quote_cut_by_code", "source_id": citation["source_id"],
                                                            "lines": [s, e]})
                                cut += 1
                                break
            for child in value.values():
                cut += self.fill_cut_quotes(child)
        elif isinstance(value, list):
            for child in value:
                cut += self.fill_cut_quotes(child)
        return cut

    def citations(self, citations: list[dict]) -> list[str]:
        ids = []
        for citation in citations:
            if not citation.get("quote"):
                raise FlowError(f"cited lines were not provided to the model: {citation.get('source_id')}:"
                                f"{citation.get('start_line')}-{citation.get('end_line')} (no quote to keep)")
            source_id = citation["source_id"]
            record = self.records.get(source_id)
            start, end, quote, focus = self._citation_span(citation, track=True)
            evidence_id = self._evidence_id(source_id, start, end, quote)
            self._remember_focus(evidence_id, focus)
            previous = self.evidence.get(evidence_id)
            item = {"id": evidence_id, "source_id": source_id,
                "source_version": record.source_version, "content_hash": record.content_hash,
                "start_line": start, "end_line": end, "quote": quote, "quote_hash": digest(quote),
                "source": record.metadata()}
            cited = merge_focus(previous.get("focus") if previous else None, self.focus.get(evidence_id))
            if cited:
                item["focus"] = cited
            self.evidence[evidence_id] = item
            ids.append(evidence_id)
        return list(dict.fromkeys(ids))

    def _citation_span(self, citation: dict, *, track: bool) -> tuple[int, int, str, list[list[int]] | None]:
        try:
            return self._citation_span_at(citation, track=track)
        except FlowError as error:
            record = self.records.get(citation["source_id"])
            # With no quote (null is the rule) there is nothing to look for elsewhere: the bad range stands.
            if not record or not citation.get("quote") or len(citation["quote"]) < SHORT_QUOTE_CHARS:
                self._note_mismatch(citation, error)
                raise
            # A locator slip: the exact quote is in lines the model was shown, only not at the
            # lines it named. Accept it when those lines hold it in exactly one place.
            last = len(record.content.splitlines())
            # Focus offsets are relative to the canonical quote, so any search range agrees.
            found: dict[tuple[int, int, str], list[list[int]] | None] = {}
            for lo, hi in self.provided.get(citation["source_id"], []):
                lo, hi = max(1, lo), min(hi, last)
                if lo > hi:
                    continue
                try:
                    start, end, quote, focus = self._citation_span_at(
                        {**citation, "start_line": lo, "end_line": hi}, track=False, corrections=False)
                except FlowError:
                    continue
                found.setdefault((start, end, quote), focus)
            if len(found) != 1:
                self._note_mismatch(citation, error)
                raise error
            (start, end, quote), focus = found.popitem()
            if track:
                self.normalizations.append({"source_id": citation["source_id"], "mode": "relocated_within_provided_lines",
                    "requested_lines": [citation["start_line"], citation["end_line"]], "actual_lines": [start, end],
                    "requested_quote_chars": len(citation["quote"])})
            return start, end, quote, focus

    def _citation_span_at(self, citation: dict, *, track: bool,
                          corrections: bool = True) -> tuple[int, int, str, list[list[int]] | None]:
        source_id, start, end = citation["source_id"], citation["start_line"], citation["end_line"]
        record = self.records.get(source_id)
        if not record:
            raise FlowError(f"unknown source ID: {source_id}")
        if isinstance(citation.get("quote"), str) and citation["quote"].endswith("\n") and citation["quote"].strip():
            # A quote ending on a line break says nothing more than the same quote without it (one reading).
            citation = {**citation, "quote": citation["quote"].rstrip("\n")}
        lines = record.content.splitlines()
        if not (1 <= start <= end <= len(lines)):
            raise FlowError(f"cited line range is outside the source: {source_id}:{start}-{end}")
        if not any(lo <= start and end <= hi for lo, hi in self.provided.get(source_id, [])):
            raise FlowError(f"cited source lines were not provided to the model: {source_id}")
        quote = "\n".join(lines[start - 1:end])
        focus = None
        if quote != citation["quote"]:
            span = citation["quote"].count("\n") + 1
            matches = [(lo, lo + span - 1) for lo in range(start, end - span + 2)
                       if "\n".join(lines[lo - 1:lo + span - 1]) == citation["quote"]]
            if len(matches) == 1:
                corrected_start, corrected_end = matches[0]
                if track:
                    self.normalizations.append({"source_id": source_id, "requested_lines": [start, end],
                        "actual_lines": [corrected_start, corrected_end], "mode": "exact_line_span_narrowed",
                        "requested_quote_chars": len(citation["quote"])})
                start, end = corrected_start, corrected_end
                quote = citation["quote"]
            else:
                requested_quote = citation["quote"]
                region = "\n".join(lines[start - 1:end])
                # Close repeats count only inside the lines the model itself named, never after a relocation.
                resolved = resolve_quote(region, requested_quote, close_repeats=corrections)
                # A fragment under SHORT_QUOTE_CHARS says little alone: it is kept only where it
                # appears once, exactly as written, in the cited lines.
                if len(requested_quote) < SHORT_QUOTE_CHARS and (
                        not requested_quote.strip() or resolved[0] is None or
                        resolved[3] != "unique_exact_substring_expanded_to_lines"):
                    raise FlowError(f"quote differs from the frozen source: {source_id}:{start}-{end} "
                                    f"(a fragment under {SHORT_QUOTE_CHARS} chars is accepted only where it appears "
                                    "exactly once in the cited lines. Quote a longer piece containing it, exactly as written)")
                if len(requested_quote) < SHORT_QUOTE_CHARS:
                    resolved = (*resolved[:3], "short_unique_substring_expanded_to_lines")
                # One reading of one slip left: the exact and escape views found nothing, and
                # these are only read inside the lines the model named.
                if resolved[0] is None and corrections:
                    slip = resolve_quote_slips(region, requested_quote)
                    if slip is not None:
                        resolved = slip
                if resolved[0] is None:
                    count = resolved[1]
                    rows = sorted({start + bisect.bisect_right(line_offsets(region), found) - 1
                                   for found in occurrences(region, requested_quote)}) if count > 1 else []
                    where = f", on different lines: cite one of lines {', '.join(map(str, rows[:8]))}" if rows else (
                        ", on different lines" if count > 1 else "")
                    raise FlowError(f"quote not found uniquely in the cited lines: "
                                    f"{source_id}:{start}-{end} ({count} matches{where})")
                spans, first, last, mode = resolved
                corrected_start, corrected_end = start + first, start + last
                quote = "\n".join(lines[corrected_start - 1:corrected_end])
                base = len("\n".join(lines[start - 1:corrected_start - 1])) + (1 if first else 0)
                relative = [[s - base, e - base] for s, e in spans]
                if any(quote[a:b] != region[s:e] for (a, b), (s, e) in zip(relative, spans)):
                    raise FlowError(f"quote normalization did not preserve a source substring: {source_id}")
                focus = relative if len(relative) <= FOCUS_MAX_SPANS else None
                if track:
                    self.normalizations.append({"source_id": source_id, "requested_lines": [start, end],
                        "actual_lines": [corrected_start, corrected_end], "mode": mode,
                        "matches": len(spans), "requested_quote_hash": digest(requested_quote),
                        "requested_quote_chars": len(requested_quote), "canonical_quote_chars": len(quote)})
                start, end = corrected_start, corrected_end
        return start, end, quote, focus

    def _provided_text(self, source_id: str) -> str:
        """The lines of one record the model was shown, as one text."""
        record = self.records.get(source_id)
        if not record:
            return ""
        lines = record.content.splitlines()
        numbers = {n for lo, hi in self.provided.get(source_id, []) for n in range(lo, min(hi, len(lines)) + 1)}
        return "\n".join(lines[n - 1] for n in sorted(numbers))

    def _classify_mismatch(self, citation: dict, region: str, matches: int) -> str:
        """Where a failed quote went, in one category. Whitespace and word order stay unstated."""
        quote, source_id = citation["quote"], citation["source_id"]
        if matches > 1:  # it says one thing, but the cited lines hold it in several places
            return "multiple_in_cited_lines"
        if len(occurrences(self._provided_text(source_id), quote)) > 1:
            return "other_provided_lines_multiple"
        if any(occurrences(self._provided_text(other), quote) for other in self.provided if other != source_id):
            return "other_record"
        slip = resolve_quote_slips(region, quote)
        if slip is not None:
            return SLIP_CATEGORIES[slip[3]]
        if len(quote) < SHORT_QUOTE_CHARS:
            return "short_fragment"
        return "not_found"

    def _note_mismatch(self, citation: dict, error: FlowError) -> None:
        """Why this quote did not match, kept in memory: the audit copy holds no quote text."""
        record = self.records.get(citation["source_id"])
        source_id, quote = citation["source_id"], citation["quote"]
        start, end = citation["start_line"], citation["end_line"]
        if not str(error).startswith(QUOTE_MISMATCH_HEADS) or not record:
            return
        if not 1 <= start <= end <= len(record.content.splitlines()):
            return
        if any(item["source_id"] == source_id and item["lines"] == [start, end] and item["quote"] == quote
               for item in self.mismatches):
            return
        region = "\n".join(record.content.splitlines()[start - 1:end])
        resolved = resolve_quote(region, quote)
        matches = len(resolved[0]) if resolved[0] is not None else resolved[1]
        item = {"source_id": source_id, "lines": [start, end], "quote": quote,
                "quote_chars": len(quote), "matches": matches,
                "category": self._classify_mismatch(citation, region, matches)}
        if item["category"] == "multiple_in_cited_lines":
            # Counts only: whether the cited line numbers single out one of the repeats.
            offsets = line_offsets(region)
            rows = [bisect.bisect_right(offsets, found) - 1 for found in occurrences(region, quote)]
            item["shape"] = {"matches": len(rows), "span_lines": end - start + 1,
                             "on_start_line": rows.count(0), "on_end_line": rows.count(end - start),
                             "whole_block": [start, end] in [list(b) for b in self.provided.get(source_id, [])]}
        self.mismatches.append(item)

    def mismatch_audit(self) -> list[dict]:
        """The mismatch list as it is written to the ledger: category and size, never the quote."""
        return [{key: item[key] for key in ("source_id", "lines", "category", "quote_chars", "shape") if key in item}
                for item in self.mismatches]

    def _check_all_citations(self, value: dict) -> None:
        errors: list[str] = []
        def visit(item: object) -> None:
            if isinstance(item, dict):
                if isinstance(item.get("evidence"), list):
                    for citation in item["evidence"]:
                        try:
                            start, end, quote, focus = self._citation_span(citation, track=True)
                            self._remember_focus(
                                self._evidence_id(citation["source_id"], start, end, quote), focus)
                            # Keep the validated contract canonical. Later stages
                            # rebuild visibility from persisted evidence excerpts;
                            # retaining the model's broader requested range would
                            # make a safe citation appear to reach unprovided lines.
                            citation["start_line"] = start
                            citation["end_line"] = end
                            citation["quote"] = quote
                        except FlowError as exc:
                            if len(errors) < 8:
                                errors.append(str(exc))
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(value)
        if errors:
            raise FlowError("; ".join(errors))

    def event(self, event: dict) -> dict:
        result = copy.deepcopy(event)
        evidence_ids = self.citations(result.pop("evidence"))
        sources = [self.evidence[eid]["source"] for eid in evidence_ids]
        kind, status, label = result["kind"], result["status"], result.get("id", result["title"])
        if status in OBSERVED and not any(
            s["role"] == "tool_result" and s["derivation"] == "original" for s in sources):
            raise FlowError("observed status needs original tool_result evidence.")
        if status in OBSERVED and kind != "outcome":
            raise FlowError(f"observed status is only for outcome events (kind=outcome): {label} "
                            "(split the check of a change into its own outcome event linked by verifies)")
        if status == "applied" and (kind not in CHANGE_KINDS or not any(
                s["role"] in {"tool_call", "tool_result"} or s["provider"] == "git" for s in sources)):
            raise FlowError(f"applied status is only for action/revision events citing a patch or diff: {label}")
        if kind == "question" and status not in QUESTION_STATUSES:
            raise FlowError(f"question event status must be asked, withdrawn or unknown: {label}")
        if status == "asked" and kind != "question":
            raise FlowError(f"asked status is only for question events: {label}")
        if result["basis"] == "tool_record" and not any(s["role"] in {"tool_call", "tool_result"} for s in sources):
            raise FlowError("tool_record basis cites no tool call or result.")
        if result["basis"] == "git_artifact" and not any(s["provider"] == "git" for s in sources):
            raise FlowError("git_artifact basis cites no Git evidence.")
        # Where and when an event was recorded is read off its citations, not trusted from the
        # model: a copying slip here is corrected (and audited) instead of failing the unit.
        trees = sorted({s["worktree_id"] for s in sources if s.get("worktree_id")})
        sessions = sorted({s["session_id"] for s in sources if s.get("session_id")})
        times = sorted(s["recorded_at"] for s in sources if s.get("recorded_at"))
        derived = {}
        if set(result["worktree_ids"]) != set(trees) or set(result["session_ids"]) != set(sessions):
            derived.update(worktree_ids=trees, session_ids=sessions)
        if result["recorded_at"] is not None and result["recorded_at"] not in times:
            derived["recorded_at"] = times[0] if times else None
        if derived:
            result.update(derived)
            event.update(copy.deepcopy(derived))
            self.normalizations.append({"mode": "event_provenance_from_evidence", "fields": sorted(derived)})
        result["evidence_ids"] = evidence_ids
        return result

    def check_extraction(self, output: dict, unit_id: str, snapshot_id: str, graph: dict) -> None:
        self.fill_cut_quotes(output)  # before the shape check: a citation may come without its quote key at all
        validate_shape(output, EXTRACT_SCHEMA)
        if output["unit_id"] != unit_id or output["snapshot_id"] != snapshot_id:
            raise FlowError("extraction unit/snapshot identifiers differ from the task.")
        if output["status"] != "complete" or output["read_requests"]:
            raise FlowError("an unfinished evidence-request reply cannot be used as the extraction result.")
        if output["unprocessed_record_ids"]:
            raise FlowError("unit not published because it still has unprocessed records.")
        for event_id in drop_caller_instructions(output, self.records):
            self.normalizations.append({"mode": "caller_instructions_not_user", "event": event_id})
        for event_id in retype_answered_user_goals(output["event_candidates"], output["edge_candidates"], self.records):
            self.normalizations.append({"mode": "user_goal_as_question", "event": event_id})
        # Quote, status and relation errors are reported together: there is only one repair round.
        try:
            self._check_all_citations(output)
            quoted = ""
        except FlowError as exc:
            quoted = str(exc)
        candidates = set()
        claim_errors: list[str] = [quoted] if quoted else []
        self.settle_unbacked_claims(output)
        for item in output["event_candidates"]:
            if not item["id"].startswith("tmp:") or item["id"] in candidates:
                raise FlowError("event candidate IDs must be unique tmp: IDs.")
            if self.assigned_source_ids is not None and not any(
                    citation["source_id"] in self.assigned_source_ids for citation in item["evidence"]):
                raise FlowError("a new event candidate needs evidence from the sources of this work unit.")
            candidates.add(item["id"])
            try:
                self.event(item)
            except FlowError as exc:
                if not quoted or str(exc) not in quoted:  # a bad quote is already listed
                    claim_errors.append(str(exc))
        existing = {e["id"] for e in graph["events"]}
        endpoints = {e["id"]: e for e in graph["events"]} | {e["id"]: e for e in output["event_candidates"]}
        broken: list[tuple[dict, str]] = []
        for edge in output["edge_candidates"]:
            if edge["from_event_id"] not in candidates | existing or edge["to_event_id"] not in candidates | existing:
                raise FlowError("an extracted relation refers to an event that does not exist.")
            if orient_relation(edge, endpoints):
                self.normalizations.append({"mode": "relation_direction_corrected", "relation": edge["relation"]})
            try:
                check_relation(edge, endpoints)
            except FlowError as exc:
                broken.append((edge, str(exc)))
        self.drop_rule_breaking_relations(output, broken)
        for edge in output["edge_candidates"]:
            try:
                self.citations(edge["evidence"])
            except FlowError:
                if not quoted:
                    raise
        cited = {citation["source_id"] for key in ("event_candidates", "existing_event_matches")
                 for item in output[key] for citation in item["evidence"]}
        uncovered = [f"{call}({label})" for call, (label, result) in self.required_citations.items()
                     if call not in cited and result not in cited]
        if uncovered:
            claim_errors.append("tool call that changed a file is cited by no event: " + ", ".join(uncovered[:6]) +
                                ". Make a change event (action/revision) or add it to the evidence of the fitting candidate.")
        if claim_errors:
            raise FlowError("; ".join(claim_errors[:8]))
        for match in output["existing_event_matches"]:
            if match["candidate_id"] not in candidates or match["existing_event_id"] not in existing:
                raise FlowError("existing event match names an unknown candidate or event ID.")
            self.citations(match["evidence"])
        for item in output["open_items"]:
            self.citations(item["evidence"])
            if not set(item["related_event_ids"]) <= candidates | existing:
                raise FlowError("open item refers to an event that does not exist.")

    def salvage_extraction(self, output: dict, unit_id: str, snapshot_id: str,
                           graph: dict) -> tuple[dict, list[str], list[str]]:
        """What stays of an extraction that still failed after its repair round.

        Each candidate is checked on its own and dropped if it fails; the dropped ones are
        named in limitations and the rest must pass the whole check. A file edit only a
        dropped event cited is waived; an edit no event mentioned still fails the unit.
        Returns the trimmed output, the waived tool calls and the notes on what was dropped.
        """
        output = copy.deepcopy(output)
        self.fill_cut_quotes(output)  # before the shape check: a citation may come without its quote key at all
        validate_shape(output, EXTRACT_SCHEMA)
        if (output["unit_id"] != unit_id or output["snapshot_id"] != snapshot_id or output["status"] != "complete"
                or output["read_requests"] or output["unprocessed_record_ids"]):
            raise FlowError("extraction result cannot be salvaged.")
        drop_caller_instructions(output, self.records)
        retype_answered_user_goals(output["event_candidates"], output["edge_candidates"], self.records)
        existing = {event["id"]: event for event in graph["events"]}
        kept: dict[str, dict] = {}
        dropped: list[dict] = []
        for item in output["event_candidates"]:
            try:
                if not item["id"].startswith("tmp:") or item["id"] in kept:
                    raise FlowError(item["id"])
                if self.assigned_source_ids is not None and not any(
                        citation["source_id"] in self.assigned_source_ids for citation in item["evidence"]):
                    raise FlowError(item["id"])
                self._check_all_citations(item)
                self.event(item)
            except FlowError:
                dropped.append(item)
                continue
            kept[item["id"]] = item
        endpoints = existing | kept
        def valid(item: dict, check=lambda: None) -> bool:
            try:
                self._check_all_citations(item)
                check()
                return True
            except FlowError:
                return False
        edges = [edge for edge in output["edge_candidates"]
                 if edge["from_event_id"] in endpoints and edge["to_event_id"] in endpoints and
                 valid(edge, lambda edge=edge: (orient_relation(edge, endpoints), check_relation(edge, endpoints)))]
        dropped_edges = len(output["edge_candidates"]) - len(edges)
        if not kept or not (dropped or dropped_edges):
            raise FlowError("no candidates to salvage.")
        matches = [match for match in output["existing_event_matches"]
                   if match["candidate_id"] in kept and match["existing_event_id"] in existing and valid(match)]
        items = [item for item in output["open_items"]
                 if set(item["related_event_ids"]) <= set(endpoints) and valid(item)]
        cited = {citation["source_id"] for item in [*kept.values(), *matches] for citation in item["evidence"]}
        described = {citation["source_id"] for item in dropped for citation in item["evidence"]}
        waived = sorted(call for call, (_, result) in self.required_citations.items()
                        if call not in cited and result not in cited and ({call, result} & described))
        notes = [*(f"event candidate dropped for failing evidence checks: {item['title']}" for item in dropped),
                 *([f"relation candidates dropped for failing evidence checks: {dropped_edges}"] if dropped_edges else [])]
        output.update(event_candidates=list(kept.values()), edge_candidates=edges,
                      existing_event_matches=matches, open_items=items,
                      limitations=[*output["limitations"], *notes])
        required = self.required_citations
        self.required_citations = {call: pair for call, pair in required.items() if call not in waived}
        try:
            self.check_extraction(output, unit_id, snapshot_id, graph)
        finally:
            self.required_citations = required
        self.normalizations.append({"mode": "invalid_candidates_dropped", "events": len(dropped),
                                    "edges": dropped_edges, "waived_edit_calls": len(waived)})
        return output, waived, notes

    def settle_unbacked_claims(self, output: dict) -> None:
        """Claims a candidate's own citations do not back, read as the weaker claim they do back, instead of costing
        a repair round with the whole unit resent (each was seen live on a Codex-heavy project):

        - a change marked `applied` that cites no tool record or Git evidence is the agent's report of a change:
          `reported_complete` (`applied_without_patch_as_reported`);
        - an outcome marked `observed_*` that cites no original tool result is a reported result: `reported_complete`
          (`observed_without_result_as_reported`);
        - an event whose basis is `tool_record` but cites no tool call or result rests on what it does cite:
          `git_artifact` with Git evidence, else `explicit_statement` (`tool_basis_without_tool_record`)."""
        for item in output["event_candidates"]:
            records = [self.records.get(citation["source_id"]) for citation in item["evidence"]]
            records = [r for r in records if r is not None]
            tool = any(r.role in {"tool_call", "tool_result"} for r in records)
            git = any(r.provider == "git" for r in records)
            if item["status"] == "applied" and item["kind"] in CHANGE_KINDS and not (tool or git):
                item["status"] = "reported_complete"
                self.normalizations.append({"mode": "applied_without_patch_as_reported", "event": item["id"]})
            if item["status"] in OBSERVED and item["kind"] == "outcome" and records and not any(
                    r.role == "tool_result" and r.derivation == "original" for r in records):
                item["status"] = "reported_complete"
                self.normalizations.append({"mode": "observed_without_result_as_reported", "event": item["id"]})
            if item["basis"] == "tool_record" and records and not tool:
                item["basis"] = "git_artifact" if git else "explicit_statement"
                self.normalizations.append({"mode": "tool_basis_without_tool_record", "event": item["id"]})

    def drop_rule_breaking_relations(self, output: dict, broken: list[tuple[dict, str]]) -> None:
        """Drop extracted relations whose endpoints break a relation rule (`check_relation`: a verifies that does not
        lead from a change to an observed outcome or rests on inference, an answers that does not leave a question).

        Repairing one resends the whole unit for a single link, and the link is a claim the rules already refuse; the
        events stay, the relation is left out and named in limitations. Audited as `relation_dropped_by_rule`."""
        if not broken:
            return
        gone = {edge["id"] for edge, _ in broken}
        output["edge_candidates"] = [edge for edge in output["edge_candidates"] if edge["id"] not in gone]
        for edge, reason in broken:
            self.normalizations.append({"mode": "relation_dropped_by_rule", "relation": edge["relation"]})
            output["limitations"].append(f"dropped relation {edge['id']} ({edge['relation']}): {reason.split(':', 1)[0]}.")

    def drop_unchecked_doc_verifies(self, output: dict, graph: dict) -> int:
        """Drop a verifies between a documentation-only change and a run that never names that file.

        A test passing does not check prose, so such a link claims a verification no record shows.
        The edge is removed (not repaired), its candidate marked excluded, and the drop noted in
        limitations and normalizations.
        """
        added = {event["id"]: [c["source_id"] for c in event["evidence"]] for event in output["events_to_add"]}
        def sources(event_id: str) -> list[str]:
            if event_id in added:
                return added[event_id]
            event = next((e for e in graph["events"] if e["id"] == event_id), None)
            return [self.evidence[i]["source_id"] for i in (event or {}).get("evidence_ids", []) if i in self.evidence]
        calls = {r.tool_call_id: r for r in self.records.values() if r.role == "tool_call" and r.tool_call_id}
        def commands(event_id: str) -> list[str]:
            found = []
            for source_id in sources(event_id):
                record = self.records.get(source_id)
                if record and record.role == "tool_result" and record.tool_call_id in calls:
                    found.append(calls[record.tool_call_id].content)
                elif record and record.role == "tool_call" and not edited_files(record):
                    found.append(record.content)
            return found
        def unchecked(change: str, outcome: str) -> bool:
            files = [f for s in sources(change) if s in self.records for f in edited_files(self.records[s])]
            if not docs_only(files):
                return False
            runs = commands(outcome)
            names = {name for f in files for name in (f, f.rsplit("/", 1)[-1])}
            return bool(runs) and not any(name in run for run in runs for name in names)
        dropped = [edge["id"] for edge in output["edges_to_add"] if edge["relation"] == "verifies" and (
            unchecked(edge["from_event_id"], edge["to_event_id"]) or unchecked(edge["to_event_id"], edge["from_event_id"]))]
        if not dropped:
            return 0
        gone = set(dropped)
        output["edges_to_add"] = [e for e in output["edges_to_add"] if e["id"] not in gone]
        output["change_attributions"] = [a for a in output["change_attributions"]
                                         if not (a["operation"] == "edges_to_add" and a["item_id"] in gone)]
        output["review_issues"] = [i for i in output["review_issues"] if i.get("target_id") not in gone]
        for row in output["candidate_resolutions"]:
            if set(row["target_ids"]) & gone:
                row["target_ids"] = [t for t in row["target_ids"] if t not in gone]
                if not row["target_ids"]:
                    row["disposition"] = "excluded"
                    row["reason"] = DOC_VERIFIES_REASON
        output["limitations"] = [*output["limitations"], f"{DOC_VERIFIES_REASON} ({len(dropped)} relations)"]
        self.normalizations.append({"mode": "doc_verifies_dropped", "edges": len(dropped)})
        return len(dropped)

    def settle_in_delta_duplicates(self, output: dict) -> int:
        """A candidate called a duplicate or update of an item this same delta adds is folded into
        that new item, which is what `added` with that target says; only when every target is one."""
        added = {"event": {e["id"] for e in output["events_to_add"]}, "edge": {e["id"] for e in output["edges_to_add"]},
                 "open_item": {i["id"] for i in output["open_items_to_upsert"] if i["id"].startswith("tmp:")}}
        settled = 0
        for row in output["candidate_resolutions"]:
            if (row["disposition"] in {"duplicate", "updated"} and row["target_ids"]
                    and set(row["target_ids"]) <= added[row["candidate_kind"]]):
                row["disposition"] = "added"
                settled += 1
        if settled:
            self.normalizations.append({"mode": "in_delta_duplicate_as_added", "items": settled})
        return settled

    def restore_tool_evidence(self, output: dict, candidates: dict | None) -> int:
        """Give back the tool or Git citation an integrated event's basis or status rests on.

        Only when the event's own candidate (a resolution targets it) carried that citation,
        validated at extraction: the integrator dropped it while copying. Otherwise the event
        still fails its check below.
        """
        if candidates is None:
            return 0
        by_id = {candidate["id"]: candidate for candidate in candidates["event_candidates"]}
        origin: dict[str, list[dict]] = {}
        for row in output["candidate_resolutions"]:
            if row["candidate_kind"] == "event" and row["candidate_id"] in by_id:
                for target in row["target_ids"]:
                    origin.setdefault(target, []).append(by_id[row["candidate_id"]])
        def source(citation: dict) -> dict:
            record = self.records.get(citation["source_id"])
            return {"role": record.role, "derivation": record.derivation, "provider": record.provider} if record else {}
        tests = {"tool_record": lambda s: s.get("role") in {"tool_call", "tool_result"},
                 "observed": lambda s: s.get("role") == "tool_result" and s.get("derivation") == "original",
                 "git_artifact": lambda s: s.get("provider") == "git"}
        restored = 0
        for event in output["events_to_add"]:
            needs = [name for name, wanted in (("tool_record", event["basis"] == "tool_record"),
                                               ("observed", event["status"] in OBSERVED),
                                               ("git_artifact", event["basis"] == "git_artifact")) if wanted]
            for name in needs:
                if any(tests[name](source(c)) for c in event["evidence"]):
                    continue
                have = {(c["source_id"], c["start_line"], c["end_line"], c["quote"]) for c in event["evidence"]}
                found = [dict(c) for candidate in origin.get(event["id"], []) for c in candidate["evidence"]
                         if tests[name](source(c)) and (c["source_id"], c["start_line"], c["end_line"], c["quote"]) not in have]
                if found:
                    event["evidence"].extend(found)
                    restored += 1
        if restored:
            self.normalizations.append({"mode": "tool_evidence_restored_from_candidates", "events": restored})
        return restored

    def apply_delta(self, output: dict, graph: dict, snapshot_id: str, run_id: str,
                    candidates: dict | None = None,
                    expected_review_issues: list[dict] | None = None) -> dict:
        self.fill_cut_quotes(output)
        validate_shape(output, DELTA_SCHEMA)
        if output["status"] != "complete" or output["read_requests"]:
            raise FlowError("GraphDelta is not complete.")
        if output["base_graph_version"] != graph["version"] or output["snapshot_id"] != snapshot_id:
            raise FlowError("GraphDelta base graph version or snapshot differs from the task.")
        self.settle_in_delta_duplicates(output)
        self.restore_tool_evidence(output, candidates)
        self.drop_unchecked_doc_verifies(output, graph)
        self._check_all_citations(output)
        review_issues = output["review_issues"]
        issue_ids = [item["id"] for item in review_issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise FlowError("review_issues IDs must be unique.")
        candidate_kind = {"event_candidate": "event", "edge_candidate": "edge",
                          "open_item_candidate": "open_item"}
        for issue in review_issues:
            target = issue["target_id"]
            if issue["target_kind"] in candidate_kind:
                if candidates is None or target not in {x["id"] for x in candidates[
                        {"event": "event_candidates", "edge": "edge_candidates",
                         "open_item": "open_items"}[candidate_kind[issue["target_kind"]]]]}:
                    raise FlowError("review issue targets a candidate that is not in the input.")
            else:
                key = {"event": "events", "edge": "edges", "open_item": "open_items"}[issue["target_kind"]]
                delta_key = {"event": "events_to_add", "edge": "edges_to_add",
                             "open_item": "open_items_to_upsert"}[issue["target_kind"]]
                if target not in ({x["id"] for x in graph[key]} | {x["id"] for x in output[delta_key]}):
                    raise FlowError("review issue targets an item that is not in the existing graph.")
            self.citations(issue["evidence"])
        review_resolutions = output["review_resolutions"]
        if expected_review_issues is None:
            if review_resolutions:
                raise FlowError("review_resolutions returned outside a review call.")
        else:
            expected_issue_ids = {item["id"] for item in expected_review_issues}
            returned_issue_ids = [item["issue_id"] for item in review_resolutions]
            if len(returned_issue_ids) != len(set(returned_issue_ids)) or set(returned_issue_ids) != expected_issue_ids:
                raise FlowError("review_resolutions must resolve every issue sent exactly once.")
            for item in review_resolutions:
                self.citations(item["evidence"])
        if candidates is not None:
            expected = {(item["id"], "event") for item in candidates["event_candidates"]}
            expected |= {(item["id"], "edge") for item in candidates["edge_candidates"]}
            expected |= {(item["id"], "open_item") for item in candidates["open_items"]}
            resolutions = output["candidate_resolutions"]
            actual = [(item["candidate_id"], item["candidate_kind"]) for item in resolutions]
            if len(actual) != len(set(actual)) or set(actual) != expected:
                raise FlowError("candidate_resolutions must resolve every candidate exactly once.")
            targets_by_kind = {
                "event": ({e["id"] for e in graph["events"]} | {e["id"] for e in output["events_to_add"]}),
                "edge": ({e["id"] for e in graph["edges"]} | {e["id"] for e in output["edges_to_add"]}),
                "open_item": ({i["id"] for i in graph["open_items"]} | {i["id"] for i in output["open_items_to_upsert"]})}
            added_by_kind = {"event": {e["id"] for e in output["events_to_add"]},
                "edge": {e["id"] for e in output["edges_to_add"]},
                "open_item": {i["id"] for i in output["open_items_to_upsert"] if i["id"].startswith("tmp:")}}
            updated_by_kind = {"event": {e["id"] for e in output["events_to_update"]},
                "edge": set(),
                "open_item": {i["id"] for i in output["open_items_to_upsert"] if not i["id"].startswith("tmp:")}}
            existing_by_kind = {"event": {e["id"] for e in graph["events"]},
                "edge": {e["id"] for e in graph["edges"]},
                "open_item": {i["id"] for i in graph["open_items"]}}
            for item in resolutions:
                if item["disposition"] == "excluded":
                    if item["target_ids"]:
                        raise FlowError("an excluded candidate cannot name target IDs.")
                elif not item["target_ids"] or not set(item["target_ids"]) <= targets_by_kind[item["candidate_kind"]]:
                    missing = sorted(set(item["target_ids"]) - targets_by_kind[item["candidate_kind"]])
                    other = any(t in ids for t in missing for kind, ids in targets_by_kind.items()
                                if kind != item["candidate_kind"])
                    why = "no target" if not item["target_ids"] else "other kind" if other else "unknown ID"
                    raise FlowError(f"candidate resolution target not in GraphDelta or graph "
                                    f"({item['candidate_kind']}, {why}): {item['candidate_id']} → {', '.join(missing)}")
                if item["disposition"] == "added" and not set(item["target_ids"]) <= added_by_kind[item["candidate_kind"]]:
                    raise FlowError("an added resolution must point at a new GraphDelta item of the same kind.")
                if item["disposition"] in {"duplicate", "updated"} and any(i.startswith("tmp:") for i in item["target_ids"]):
                    raise FlowError("a duplicate or updated resolution must point at existing graph IDs.")
                if item["disposition"] == "duplicate" and not set(item["target_ids"]) <= existing_by_kind[item["candidate_kind"]]:
                    raise FlowError("a duplicate resolution must point at an existing graph item.")
                if item["disposition"] == "updated" and not set(item["target_ids"]) <= updated_by_kind[item["candidate_kind"]]:
                    raise FlowError("an updated resolution must point at a GraphDelta update of the same kind.")
                self.citations(item["evidence"])
            operations = {"events_to_add": [x["id"] for x in output["events_to_add"]],
                "events_to_update": [x["id"] for x in output["events_to_update"]],
                "edges_to_add": [x["id"] for x in output["edges_to_add"]],
                "edges_to_invalidate": [x["id"] for x in output["edges_to_invalidate"]],
                "open_items_to_upsert": [x["id"] for x in output["open_items_to_upsert"]],
                "open_items_to_resolve": [x["id"] for x in output["open_items_to_resolve"]]}
            expected_ops = {(op, item_id) for op, ids in operations.items() for item_id in ids}
            attributions = output["change_attributions"]
            actual_ops = [(x["operation"], x["item_id"]) for x in attributions]
            valid_candidate_ids = {cid for cid, _ in expected}
            if len(actual_ops) != len(set(actual_ops)) or set(actual_ops) != expected_ops:
                raise FlowError("change_attributions must attribute every GraphDelta change exactly once.")
            resolution_by_id = {row["candidate_id"]: row for row in resolutions}

            def linked_ends(item: dict) -> set[str]:
                if item["operation"] == "edges_to_add":
                    edge = next(x for x in output["edges_to_add"] if x["id"] == item["item_id"])
                    ends = {edge["from_event_id"], edge["to_event_id"]}
                elif item["operation"] == "edges_to_invalidate":
                    edge = next(x for x in graph["edges"] if x["id"] == item["item_id"])
                    ends = {edge["from_event_id"], edge["to_event_id"]}
                elif item["operation"] == "open_items_to_upsert":
                    ends = set(next(x for x in output["open_items_to_upsert"] if x["id"] == item["item_id"])["related_event_ids"])
                elif item["operation"] == "open_items_to_resolve":
                    ends = set(next(x for x in graph["open_items"] if x["id"] == item["item_id"]).get("related_event_ids", []))
                else:
                    ends = set()
                return ends

            def attribution_linked(item: dict) -> bool:
                attributed = [resolution_by_id[cid] for cid in item["candidate_ids"]]
                if any(item["item_id"] in row["target_ids"] for row in attributed):
                    return True
                if item["operation"] == "open_items_to_resolve":
                    # A new event closes an old open item: the candidate it came from is the source,
                    # though it is not among the events the item was opened on.
                    return any(row["candidate_kind"] == "event" and row["disposition"] != "excluded"
                               for row in attributed)
                ends = linked_ends(item)
                return any(row["candidate_kind"] == "event" and set(row["target_ids"]) & ends for row in attributed)

            for item in attributions:
                if not item["candidate_ids"] or not set(item["candidate_ids"]) <= valid_candidate_ids:
                    raise FlowError("change attribution refers to an ID that is not an extraction candidate.")
                if not attribution_linked(item):
                    direct = sorted(row["candidate_id"] for row in resolutions if item["item_id"] in row["target_ids"])
                    if direct:
                        item["candidate_ids"] = direct
                        self.normalizations.append({"mode": "attribution_candidates_from_resolutions",
                                                    "operation": item["operation"], "item_id": item["item_id"]})
                if not attribution_linked(item):
                    # A relation or open item no candidate names is tied to the event candidates at its ends.
                    ends = linked_ends(item)
                    owners = sorted(row["candidate_id"] for row in resolutions
                                    if row["candidate_kind"] == "event" and set(row["target_ids"]) & ends)
                    if owners:
                        item["candidate_ids"] = owners
                        self.normalizations.append({"mode": "attribution_candidates_from_endpoints",
                                                    "operation": item["operation"], "item_id": item["item_id"]})
                if not attribution_linked(item):
                    raise FlowError(f"change attribution not linked to its GraphDelta change "
                                    f"({item['operation']}): {item['item_id']}")
                if item["operation"] == "events_to_add" and self.assigned_source_ids is not None and not any(
                        quote["source_id"] in self.assigned_source_ids for quote in item["evidence"]):
                    raise FlowError("attribution of a new event needs a citation from this WorkUnit.")
                self.citations(item["evidence"])
        else:
            attributions = output["change_attributions"]
            resolutions = output["candidate_resolutions"]
        result = copy.deepcopy(graph)
        event_map = {event["id"]: event for event in result["events"]}
        local_ids: dict[str, str] = {}
        claim_errors: list[str] = []
        for event_id in retype_answered_user_goals(output["events_to_add"], output["edges_to_add"], self.records):
            self.normalizations.append({"mode": "user_goal_as_question", "event": event_id})
        first_new = len(result["events"])
        for item in output["events_to_add"]:
            temp_id = item["id"]
            if not temp_id.startswith("tmp:") or temp_id in local_ids:
                raise FlowError("a new event must use a unique tmp: ID.")
            try:
                event = self.event(item)
            except FlowError as exc:
                # Keep the proposed event so its relations are still checked in the same round.
                claim_errors.append(str(exc))
                event = {**{k: copy.deepcopy(v) for k, v in item.items() if k != "evidence"}, "evidence_ids": []}
            event_id = ident("ev_", run_id, graph["version"], temp_id)
            local_ids[temp_id] = event_id
            event.update(id=event_id, created_in_run=run_id, updated_in_run=run_id)
            result["events"].append(event)
            event_map[event_id] = event
        # The list is the order the views number and draw: a unit's new events go in the order their records were
        # written, whatever order the model listed them in (a stable sort; an event with no time keeps its place last).
        result["events"][first_new:] = sorted(result["events"][first_new:],
                                              key=lambda event: (not event.get("recorded_at"), event.get("recorded_at") or ""))
        updated = set()
        for update in output["events_to_update"]:
            event_id = update["id"]
            if event_id not in event_map or event_id in updated:
                raise FlowError("event update targets an unknown event or updates it twice.")
            updated.add(event_id)
            current = event_map[event_id]
            merged = {k: copy.deepcopy(current[k]) for k in EVENT_FIELDS}
            for key, value in update["changes"].items():
                if value is not None:
                    merged[key] = value
            # The revised claim is supported by the NEW citations; old evidence remains for audit.
            try:
                normalized = self.event({"id": event_id, **merged, "evidence": update["evidence"]})
            except FlowError as exc:
                claim_errors.append(str(exc))
                normalized = {"id": event_id, **merged, "evidence_ids": []}
            previous_ids = current["evidence_ids"]
            current.update(normalized)
            current["evidence_ids"] = list(dict.fromkeys(previous_ids + normalized["evidence_ids"]))
            current["updated_in_run"] = run_id
            current["last_change_reason"] = update["reason"]
        def mapped(value: str) -> str:
            target = local_ids.get(value, value)
            if target not in event_map:
                raise FlowError("relation refers to an event that does not exist: " + value)
            return target
        edge_map = {e["id"]: e for e in result["edges"]}
        edge_tmp = set()
        edge_local_ids: dict[str, str] = {}
        for original in output["edges_to_add"]:
            edge = copy.deepcopy(original)
            if not edge["id"].startswith("tmp:") or edge["id"] in edge_tmp:
                raise FlowError("a new relation needs a unique tmp: ID.")
            edge_tmp.add(edge["id"])
            edge["evidence_ids"] = self.citations(edge.pop("evidence"))
            edge["from_event_id"] = mapped(edge["from_event_id"])
            edge["to_event_id"] = mapped(edge["to_event_id"])
            if orient_relation(edge, event_map):
                # The model's own delta is turned too, so a review sees the stored direction.
                original["from_event_id"], original["to_event_id"] = original["to_event_id"], original["from_event_id"]
                self.normalizations.append({"mode": "relation_direction_corrected", "relation": edge["relation"]})
            edge_local_ids[edge["id"]] = ident("edge_", run_id, graph["version"], edge["id"])
            edge["id"] = edge_local_ids[edge["id"]]
            result["edges"].append(edge)
        for invalidation in output["edges_to_invalidate"]:
            edge = edge_map.get(invalidation["id"])
            if not edge:
                raise FlowError("relation to invalidate does not exist.")
            edge["active"] = False
            edge["invalidation"] = {"reason": invalidation["reason"],
                                    "evidence_ids": self.citations(invalidation["evidence"]), "run_id": run_id}
        # Updates can change an endpoint's kind or status, so every active relation is rechecked.
        temp_edge_ids = {final: temp for temp, final in edge_local_ids.items()}
        for edge in result["edges"]:
            if edge["active"]:
                try:
                    check_relation({**edge, "id": temp_edge_ids.get(edge["id"], edge["id"])}, event_map)
                except FlowError as exc:
                    claim_errors.append(str(exc))
        if claim_errors:
            raise FlowError("; ".join(claim_errors[:8]))
        items = {i["id"]: i for i in result["open_items"]}
        open_local_ids: dict[str, str] = {}
        for original in output["open_items_to_upsert"]:
            item = copy.deepcopy(original)
            item["related_event_ids"] = [mapped(i) for i in item["related_event_ids"]]
            item["evidence_ids"] = self.citations(item.pop("evidence"))
            if item["id"].startswith("tmp:"):
                open_local_ids[item["id"]] = ident("open_", run_id, graph["version"], item["id"])
                item["id"] = open_local_ids[item["id"]]
            elif item["id"] not in items:
                raise FlowError("existing open item ID does not exist.")
            items[item["id"]] = item
        for resolution in output["open_items_to_resolve"]:
            item = items.get(resolution["id"])
            if not item:
                raise FlowError("open item to resolve does not exist.")
            item["status"] = "resolved"
            item["resolution"] = {"reason": resolution["reason"],
                                  "evidence_ids": self.citations(resolution["evidence"])}
        result["open_items"] = list(items.values())
        result["limitations"] = list(dict.fromkeys(result["limitations"] + output["limitations"]))
        result["source_snapshot_id"] = snapshot_id
        result["schema_version"] = GRAPH_SCHEMA_VERSION
        result["last_candidate_resolutions"] = [{**item,
            "target_ids": [local_ids.get(i, edge_local_ids.get(i, open_local_ids.get(i, i)))
                           for i in item["target_ids"]],
            "evidence_ids": self.citations(item["evidence"]), "evidence": None} for item in resolutions]
        def final_id(value: str) -> str:
            return local_ids.get(value, edge_local_ids.get(value, open_local_ids.get(value, value)))
        history = list(result.get("candidate_resolution_history", []))
        history.append({"unit_id": candidates.get("unit_id") if candidates else None,
            "resolutions": copy.deepcopy(result["last_candidate_resolutions"]),
            "change_attributions": [{**item, "item_id": final_id(item["item_id"]),
                                     "evidence_ids": self.citations(item["evidence"]),
                                     "evidence": None} for item in attributions]})
        result["candidate_resolution_history"] = history
        return result


# The tools whose records an agent wrote; a Git record has no agent behind it.
AGENT_TOOLS = ("claude", "codex", "opencode")


def agent_name(lineage: dict) -> str:
    """What a sub-agent is called: its type (Claude Code `Explore`, opencode's agent), else the task it was given (the
    last part of a Codex agent's path, `/root/plots` → `plots`, the name its delegation used), else Codex's own
    nickname for it (`Dirac`, a random name that says nothing about the work), else its role; empty when none."""
    path = lineage.get("agent_path")
    task = path.rstrip("/").rpartition("/")[2] if isinstance(path, str) else ""
    for value in (lineage.get("agent_type"), task, lineage.get("agent_nickname"), lineage.get("agent_role")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def authoring_of(sources: list[dict]) -> list[dict]:
    """The agents behind the cited records: each one's tool, with the model, effort and provider its log named
    (`SourceRecord.authoring`); unique and sorted. Read off the records by code, never written by the model."""
    found: dict[tuple, dict] = {}
    for source in sources:
        tool = source.get("provider")
        if tool not in AGENT_TOOLS:
            continue
        named = {key: value for key, value in (source.get("authoring") or {}).items()
                 if key in ("model", "effort", "provider") and isinstance(value, str) and value}
        entry = {"tool": tool, **named}
        # A sub-agent (or a tool another session's call ran) and a run with no person keep their role and name.
        lineage = source.get("lineage") or {}
        if lineage.get("kind") in ("subagent", "unattended"):
            entry["role"] = lineage["kind"]
            agent = agent_name(lineage)
            if agent:
                entry["agent"] = agent
        found.setdefault(tuple(sorted(entry.items())), entry)
    return [found[key] for key in sorted(found)]


FORK_ORIGIN = "fork"


def link_forks(graph: dict, source_of: Callable[[str], dict | None]) -> int:
    """A session forked from another (`SourceRecord.fork`) continues that conversation: its first request hangs
    under the parent's last request before the fork point, as a structural `follows` of origin `fork`, like a
    dialog turn. Run when a graph is published, once per forked session, when the parent's request is in the
    graph; returns how many links were added."""
    users = [event for event in graph["events"] if event.get("actor") == "user" and event.get("kind") == "question"]
    by_session: dict[str, list[dict]] = {}
    for event in users:
        for session in event.get("session_ids") or []:
            by_session.setdefault(session, []).append(event)
    linked = {edge["to_event_id"] for edge in graph["edges"] if edge.get("origin") == FORK_ORIGIN}
    added = 0
    def fork_of(event: dict) -> dict | None:
        return next((source["fork"] for source in (source_of(i) for i in event["evidence_ids"])
                     if source and source.get("fork")), None)

    for session, events in by_session.items():
        # The fork's own requests carry the fork; an event made earlier from a copied row (before the copies were
        # dropped) does not, and is not the fork's first request.
        own = [(event, info) for event in events for info in [fork_of(event)] if info]
        if not own:
            continue
        first, info = min(own, key=lambda pair: pair[0].get("recorded_at") or "~")
        if first["id"] in linked:
            continue
        before = [event for event in by_session.get(info.get("from_session_id"), [])
                  if (event.get("recorded_at") or "") <= info.get("point_at", "")]
        if not before:
            continue
        source = max(before, key=lambda event: event.get("recorded_at") or "")
        graph["edges"].append({"id": ident("edge_", FORK_ORIGIN, source["id"], first["id"]),
                               "from_event_id": source["id"], "to_event_id": first["id"], "relation": "follows",
                               "basis": "structural", "evidence_ids": list(first["evidence_ids"][:1]),
                               "rationale": "the first request of a session forked from this conversation after "
                                            "this user message (dialog structure, not a causal claim)",
                               "active": True, "origin": FORK_ORIGIN, "fork_point_at": info.get("point_at", "")})
        added += 1
    return added


DELEGATION_ORIGIN = "delegation"
DELEGATED = "delegated_by"
DELEGATION_RATIONALE = ("work of the agent this delegation handed its task to (dialog structure, not a causal "
                        "claim)")


def link_delegations(graph: dict, source_of: Callable[[str], dict | None]) -> int:
    """A sub-agent's work belongs to the delegation that gave it its task, not to whatever the parent's person
    said last before it ran. Every event of a sub-agent gets `delegated_by`: the delegation event (`step`
    `delegate`) whose call started that agent, or gave it the task it was on then (the latest such call before the
    event, for a Codex agent given follow-up tasks). The pair is found by name, never by time: a Claude Code or
    opencode sub-agent's lineage names the call (`parent_tool_call_id`), a Codex one its path, which the call's
    record names (`step.agent`, `analysis.delegated_path`). An event of it that nothing but turn links reaches
    hangs under the delegation as a structural `follows` of origin `delegation`, and those turn links
    (`analysis.link_request_turns`) are set inactive. Run at every publish over the whole graph, since the
    parent's and the sub-agent's units are published in either order; returns how many links were added."""
    sources = {event["id"]: [s for s in (source_of(i) for i in event.get("evidence_ids", [])) if s]
               for event in graph["events"]}
    delegations: dict[tuple, list[tuple[str, dict]]] = {}
    for event in graph["events"]:
        if event.get("step") != "delegate":
            continue
        for source in sources[event["id"]]:
            if source.get("role") != "tool_call":
                continue
            agent, session = (source.get("step") or {}).get("agent"), source.get("session_id")
            key = (session, "path", agent) if agent else (session, "call", source.get("tool_call_id"))
            if key[2]:
                delegations.setdefault(key, []).append((source.get("recorded_at") or event.get("recorded_at") or "", event))

    def agent_of(event: dict) -> tuple | None:
        for source in sources[event["id"]]:
            lineage = source.get("lineage") or {}
            if lineage.get("kind") != "subagent" or not lineage.get("parent_session_id"):
                continue
            if lineage.get("parent_tool_call_id"):
                return lineage["parent_session_id"], "call", lineage["parent_tool_call_id"]
            if lineage.get("agent_path"):
                return lineage["parent_session_id"], "path", lineage["agent_path"]
        return None

    incoming: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        if edge["active"]:
            incoming.setdefault(edge["to_event_id"], []).append(edge)
    known = {edge["id"] for edge in graph["edges"]}
    added = 0
    for event in graph["events"]:
        event.pop(DELEGATED, None)
        key = agent_of(event)
        if key is None or key not in delegations:
            continue
        calls = sorted(delegations[key], key=lambda pair: pair[0])
        at = event.get("recorded_at") or ""
        delegation = ([e for when, e in calls if when <= at] or [calls[0][1]])[-1]
        if delegation["id"] == event["id"]:
            continue
        event[DELEGATED] = delegation["id"]
        hangs = incoming.get(event["id"], [])
        if any(edge.get("origin") != "dialog_turn" for edge in hangs):
            continue  # reached by a relation of its own (or already by the delegation)
        edge_id = ident("edge_", DELEGATION_ORIGIN, delegation["id"], event["id"])
        if edge_id in known:
            continue  # set inactive since: left as it is
        graph["edges"].append({"id": edge_id, "from_event_id": delegation["id"], "to_event_id": event["id"],
                               "relation": "follows", "basis": "structural",
                               "evidence_ids": list(delegation["evidence_ids"][:1]),
                               "rationale": DELEGATION_RATIONALE, "active": True, "origin": DELEGATION_ORIGIN})
        known.add(edge_id)
        added += 1
        for edge in hangs:
            edge["active"] = False
            edge["invalidation"] = {"reason": "the work hangs under the delegation that gave its agent the task",
                                    "evidence_ids": [], "replaced_by": edge_id}
    return added


FOLDED = "folded_into"


def fold_delegate_reports(graph: dict, source_of: Callable[[str], dict | None]) -> int:
    """A parent's result that rests only on a delegation's own call and result (the report a sub-agent or a
    called tool sent back) repeats what that agent's own events show. Such an outcome gets `folded_into`: the
    called agent's last outcome (else its last event), once the called session has events. Display only: the
    views draw the two as one box, the event and its relations stay in the graph. Recomputed at every publish;
    a Codex sub-agent's records name no call, so its parent's results are left as they are. Returns the count."""
    sources = {event["id"]: [s for s in (source_of(i) for i in event.get("evidence_ids", [])) if s]
               for event in graph["events"]}
    called: dict[tuple, list[dict]] = {}
    for event in graph["events"]:
        for source in sources[event["id"]]:
            lineage = source.get("lineage") or {}
            if lineage.get("kind") != "subagent" or not lineage.get("parent_session_id"):
                continue
            key = (lineage["parent_session_id"], lineage.get("parent_tool_call_id"), lineage.get("parent_result_record_id"))
            if key[1] or key[2]:
                called.setdefault(key, []).append(event)
                break

    def is_delegation(source: dict, key: tuple) -> bool:
        session, call, result = key
        return source.get("session_id") == session and (
            bool(call) and source.get("tool_call_id") == call or bool(result) and source.get("source_id") == result)

    folded = 0
    for event in graph["events"]:
        event.pop(FOLDED, None)
        own = sources[event["id"]]
        if event.get("kind") != "outcome" or not own or any(
                (source.get("lineage") or {}).get("kind") == "subagent" for source in own):
            continue
        for key, events in called.items():
            if all(is_delegation(source, key) for source in own):
                outcomes = [e for e in events if e.get("kind") == "outcome"] or events
                event[FOLDED] = max(outcomes, key=lambda e: e.get("recorded_at") or "")["id"]
                folded += 1
                break
    return folded


def folded_view(graph: dict) -> dict:
    """The graph as the views draw it: an event folded into a called agent's event (`fold_delegate_reports`) is
    left out, its relations move to that event (a loop or a repeat of a relation drawn already is dropped), and
    the event keeps it under `received_reports` for its detail. The stored graph is unchanged."""
    target = {e["id"]: e[FOLDED] for e in graph["events"] if e.get(FOLDED)}
    present = {e["id"] for e in graph["events"]}
    target = {key: value for key, value in target.items() if value in present and value not in target}
    if not target:
        return graph
    view = dict(graph)
    received: dict[str, list[dict]] = {}
    for event in graph["events"]:
        if event["id"] in target:
            received.setdefault(target[event["id"]], []).append(
                {"id": event["id"], "title": event["title"], "status": event.get("status")})
    view["events"] = [{**event, "received_reports": received[event["id"]]} if event["id"] in received else event
                      for event in graph["events"] if event["id"] not in target]
    edges, seen = [], set()
    for edge in graph["edges"]:
        moved = {**edge, "from_event_id": target.get(edge["from_event_id"], edge["from_event_id"]),
                 "to_event_id": target.get(edge["to_event_id"], edge["to_event_id"])}
        triple = (moved["from_event_id"], moved["to_event_id"], moved["relation"])
        if moved["from_event_id"] == moved["to_event_id"] or (edge["active"] and triple in seen):
            continue
        if edge["active"]:
            seen.add(triple)
        edges.append(moved if moved != edge else edge)
    view["edges"] = edges
    return view


def step_of(event: dict, sources: list[dict]) -> str | None:
    """What kind of step an event is, from the tool records it cites (`SourceRecord.step`), for the flow's lanes:
    `check` for a result resting only on test runs, `doc` for a change resting only on documentation edits,
    `delegate` for a change resting only on calls that handed work to another agent (`light.delegation_items`).
    None when the records do not say (a claim in words, a code change, a record read before `step` existed)."""
    steps = [source.get("step") or {} for source in sources if source.get("role") in ("tool_call", "tool_result")]
    if not steps or any(not step for step in steps):
        return None
    if event["kind"] == "outcome" and all(step.get("hint") == "test" for step in steps):
        return "check"
    if event["kind"] == "action" and all(step.get("hint") == "delegate" for step in steps):
        return "delegate"
    edits = [step for step in steps if step.get("hint") == "edit"]
    if event["kind"] in CHANGE_KINDS and edits and all(step.get("doc") for step in edits):
        return "doc"
    return None


def fill_steps(graph: dict, source_of: Callable[[str], dict | None]) -> None:
    """Set every event's `step` (`step_of`) from the records its evidence cites, when a graph is published."""
    for event in graph["events"]:
        found = step_of(event, [source for source in (source_of(i) for i in event.get("evidence_ids", [])) if source])
        if found:
            event["step"] = found
        else:
            event.pop("step", None)


def fill_authoring(graph: dict, source_of: Callable[[str], dict | None]) -> None:
    """Set every event's `authoring` from the records its evidence cites (`source_of(evidence_id)` gives the
    record's metadata, or None). Run when a graph is published, so it reads the records as stored then: an
    event of an older graph gets it once its records are read again with the field."""
    for event in graph["events"]:
        sources = [source for source in (source_of(i) for i in event.get("evidence_ids", [])) if source]
        agents = authoring_of(sources)
        if agents:
            event["authoring"] = agents
        else:
            event.pop("authoring", None)
