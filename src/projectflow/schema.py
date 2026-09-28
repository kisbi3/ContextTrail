from __future__ import annotations

import bisect
import copy
from typing import Any

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
STR = {"type": "string"}
NULLSTR = {"type": ["string", "null"]}
STRS = {"type": "array", "items": STR}


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def arr(schema: dict, minimum: int = 0) -> dict:
    return {"type": "array", "items": schema, "minItems": minimum}


def enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


CITATION = obj({"source_id": STR, "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1}, "quote": {"type": "string", "minLength": 1}})
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


def _diff_body(line: str) -> bool:
    return bool(line) and line[0] in "+- " and not line.startswith(("+++ ", "--- "))


def resolve_quote(region: str, quote: str) -> tuple[list[tuple[int, int]], int, int, str] | tuple[None, int]:
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
    else:
        return None, len(spans)
    return spans, first, last, mode


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
    """A person's message written as a goal (or proposal) that an `answers` relation leaves.

    A typed message is a question (asked) or a decision (adopted); only a question can be
    answered, so the relation leaves one reading. Only events citing nothing but the
    person's own messages are changed. Returns the changed IDs.
    """
    answered = {edge["from_event_id"] for edge in edges if edge["relation"] == "answers"}
    changed = []
    for event in events:
        if event["id"] not in answered or event["actor"] != "user" or event["kind"] not in {"goal", "proposal"}:
            continue
        sources = [records.get(citation["source_id"]) for citation in event["evidence"]]
        if sources and all(record is not None and is_user_prompt(record) for record in sources):
            event["kind"], event["status"] = "question", "asked"
            changed.append(event["id"])
    return changed


def check_relation(edge: dict, events: dict[str, dict]) -> None:
    """Endpoint rules for relations whose meaning depends on what they connect."""
    source, target = events[edge["from_event_id"]], events[edge["to_event_id"]]
    if edge["relation"] == "verifies":
        if source["kind"] not in CHANGE_KINDS or target["kind"] != "outcome" or target["status"] not in OBSERVED:
            raise FlowError("verifies 관계는 변경 사건(action/revision)에서 그 변경을 실제로 실행·시험한 "
                            f"관측 결과(kind=outcome, observed 상태)로 이어야 합니다: {edge['id']}")
        if edge["basis"] == "inferred":
            raise FlowError("verifies 관계는 추정(inferred)으로 만들 수 없습니다. 실행 대상이 그 변경을 포함한다는 "
                            f"근거가 없으면 연결하지 않습니다: {edge['id']}")
    elif edge["relation"] == "answers" and (source["kind"] != "question" or target["kind"] == "question"):
        raise FlowError(f"answers 관계는 question 사건에서 그 질문에 답한 사건으로 이어야 합니다: {edge['id']}")


def validate_shape(value: dict, schema: dict) -> None:
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        messages = ["/".join(map(str, e.absolute_path)) + ": " + e.message[:220] for e in errors[:8]]
        raise FlowError("JSON schema 오류: " + "; ".join(messages))


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

    def citations(self, citations: list[dict]) -> list[str]:
        ids = []
        for citation in citations:
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
            if not record or len(citation["quote"]) < SHORT_QUOTE_CHARS:
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
                        {**citation, "start_line": lo, "end_line": hi}, track=False)
                except FlowError:
                    continue
                found.setdefault((start, end, quote), focus)
            if len(found) != 1:
                raise error
            (start, end, quote), focus = found.popitem()
            if track:
                self.normalizations.append({"source_id": citation["source_id"], "mode": "relocated_within_provided_lines",
                    "requested_lines": [citation["start_line"], citation["end_line"]], "actual_lines": [start, end],
                    "requested_quote_chars": len(citation["quote"])})
            return start, end, quote, focus

    def _citation_span_at(self, citation: dict, *, track: bool) -> tuple[int, int, str, list[list[int]] | None]:
        source_id, start, end = citation["source_id"], citation["start_line"], citation["end_line"]
        record = self.records.get(source_id)
        if not record:
            raise FlowError(f"존재하지 않는 source ID: {source_id}")
        lines = record.content.splitlines()
        if not (1 <= start <= end <= len(lines)):
            raise FlowError(f"인용 범위 불일치: {source_id}:{start}-{end}")
        if not any(lo <= start and end <= hi for lo, hi in self.provided.get(source_id, [])):
            raise FlowError(f"모델에 제공하지 않은 원문 인용: {source_id}")
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
                resolved = resolve_quote(region, requested_quote)
                # A fragment under SHORT_QUOTE_CHARS says little alone: it is kept only where it
                # appears once, exactly as written, in the cited lines.
                if len(requested_quote) < SHORT_QUOTE_CHARS and (
                        not requested_quote.strip() or resolved[0] is None or
                        resolved[3] != "unique_exact_substring_expanded_to_lines"):
                    raise FlowError(f"인용문이 고정 원문과 다릅니다: {source_id}:{start}-{end} "
                                    f"({SHORT_QUOTE_CHARS}자 미만 조각은 인용한 줄 범위에 정확히 한 번 나올 때만 "
                                    "쓸 수 있습니다. 그 조각을 포함한 더 긴 부분을 원문 그대로 인용하세요)")
                if len(requested_quote) < SHORT_QUOTE_CHARS:
                    resolved = (*resolved[:3], "short_unique_substring_expanded_to_lines")
                if resolved[0] is None:
                    count = resolved[1]
                    raise FlowError(f"인용문이 제공된 원문 범위에서 유일하게 일치하지 않습니다: "
                                    f"{source_id}:{start}-{end} (일치 {count}건"
                                    f"{', 서로 다른 줄' if count > 1 else ''})")
                spans, first, last, mode = resolved
                corrected_start, corrected_end = start + first, start + last
                quote = "\n".join(lines[corrected_start - 1:corrected_end])
                base = len("\n".join(lines[start - 1:corrected_start - 1])) + (1 if first else 0)
                relative = [[s - base, e - base] for s, e in spans]
                if any(quote[a:b] != region[s:e] for (a, b), (s, e) in zip(relative, spans)):
                    raise FlowError(f"인용문 정규화가 원문 부분 문자열을 보존하지 않습니다: {source_id}")
                focus = relative if len(relative) <= FOCUS_MAX_SPANS else None
                if track:
                    self.normalizations.append({"source_id": source_id, "requested_lines": [start, end],
                        "actual_lines": [corrected_start, corrected_end], "mode": mode,
                        "matches": len(spans), "requested_quote_hash": digest(requested_quote),
                        "requested_quote_chars": len(requested_quote), "canonical_quote_chars": len(quote)})
                start, end = corrected_start, corrected_end
        return start, end, quote, focus

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
            raise FlowError("observed 상태에는 원래 tool_result 근거가 필요합니다.")
        if status in OBSERVED and kind != "outcome":
            raise FlowError("observed 상태는 실행 결과 사건(kind=outcome)에만 씁니다. 변경의 확인 결과는 별도 "
                            f"outcome 사건으로 나누고 verifies 관계로 잇습니다: {label}")
        if status == "applied" and (kind not in CHANGE_KINDS or not any(
                s["role"] in {"tool_call", "tool_result"} or s["provider"] == "git" for s in sources)):
            raise FlowError(f"applied 상태는 패치·diff 등 변경 기록을 인용한 action/revision 사건에만 씁니다: {label}")
        if kind == "question" and status not in QUESTION_STATUSES:
            raise FlowError(f"question 사건의 상태는 asked·withdrawn·unknown 중 하나입니다: {label}")
        if status == "asked" and kind != "question":
            raise FlowError(f"asked 상태는 question 사건에만 씁니다: {label}")
        if result["basis"] == "tool_record" and not any(s["role"] in {"tool_call", "tool_result"} for s in sources):
            raise FlowError("tool_record basis에 실행 기록이 없습니다.")
        if result["basis"] == "git_artifact" and not any(s["provider"] == "git" for s in sources):
            raise FlowError("git_artifact basis에 Git 근거가 없습니다.")
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
        validate_shape(output, EXTRACT_SCHEMA)
        if output["unit_id"] != unit_id or output["snapshot_id"] != snapshot_id:
            raise FlowError("추출 작업의 unit/snapshot 식별자가 다릅니다.")
        if output["status"] != "complete" or output["read_requests"]:
            raise FlowError("완료하지 않은 근거 요청 응답을 추출 결과로 사용할 수 없습니다.")
        if output["unprocessed_record_ids"]:
            raise FlowError("미처리 record가 있어 이 단위의 게시를 보류했습니다.")
        for event_id in retype_answered_user_goals(output["event_candidates"], output["edge_candidates"], self.records):
            self.normalizations.append({"mode": "user_goal_as_answered_question", "event": event_id})
        # Quote, status and relation errors are reported together: there is only one repair round.
        try:
            self._check_all_citations(output)
            quoted = ""
        except FlowError as exc:
            quoted = str(exc)
        candidates = set()
        claim_errors: list[str] = [quoted] if quoted else []
        for item in output["event_candidates"]:
            if not item["id"].startswith("tmp:") or item["id"] in candidates:
                raise FlowError("사건 후보 ID는 고유한 tmp: ID여야 합니다.")
            if self.assigned_source_ids is not None and not any(
                    citation["source_id"] in self.assigned_source_ids for citation in item["evidence"]):
                raise FlowError("새 사건 후보에는 이번 작업 단위의 원문 근거가 필요합니다.")
            candidates.add(item["id"])
            try:
                self.event(item)
            except FlowError as exc:
                if not quoted or str(exc) not in quoted:  # a bad quote is already listed
                    claim_errors.append(str(exc))
        existing = {e["id"] for e in graph["events"]}
        endpoints = {e["id"]: e for e in graph["events"]} | {e["id"]: e for e in output["event_candidates"]}
        for edge in output["edge_candidates"]:
            if edge["from_event_id"] not in candidates | existing or edge["to_event_id"] not in candidates | existing:
                raise FlowError("추출 관계가 없는 사건을 참조합니다.")
            if orient_relation(edge, endpoints):
                self.normalizations.append({"mode": "relation_direction_corrected", "relation": edge["relation"]})
            try:
                check_relation(edge, endpoints)
            except FlowError as exc:
                claim_errors.append(str(exc))
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
            claim_errors.append("파일을 바꾼 도구 호출이 어떤 사건의 근거에도 없습니다: " + ", ".join(uncovered[:6]) +
                                ". 변경 사건(action/revision)을 만들거나 알맞은 기존 후보의 근거에 넣습니다.")
        if claim_errors:
            raise FlowError("; ".join(claim_errors[:8]))
        for match in output["existing_event_matches"]:
            if match["candidate_id"] not in candidates or match["existing_event_id"] not in existing:
                raise FlowError("기존 사건 대응의 ID가 없습니다.")
            self.citations(match["evidence"])
        for item in output["open_items"]:
            self.citations(item["evidence"])
            if not set(item["related_event_ids"]) <= candidates | existing:
                raise FlowError("미해결 사항이 없는 사건을 참조합니다.")

    def salvage_extraction(self, output: dict, unit_id: str, snapshot_id: str,
                           graph: dict) -> tuple[dict, list[str], list[str]]:
        """What stays of an extraction that still failed after its repair round.

        Each candidate is checked on its own and dropped if it fails; the dropped ones are
        named in limitations and the rest must pass the whole check. A file edit only a
        dropped event cited is waived; an edit no event mentioned still fails the unit.
        Returns the trimmed output, the waived tool calls and the notes on what was dropped.
        """
        output = copy.deepcopy(output)
        validate_shape(output, EXTRACT_SCHEMA)
        if (output["unit_id"] != unit_id or output["snapshot_id"] != snapshot_id or output["status"] != "complete"
                or output["read_requests"] or output["unprocessed_record_ids"]):
            raise FlowError("살릴 수 있는 추출 결과가 아닙니다.")
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
            raise FlowError("살릴 후보가 없습니다.")
        matches = [match for match in output["existing_event_matches"]
                   if match["candidate_id"] in kept and match["existing_event_id"] in existing and valid(match)]
        items = [item for item in output["open_items"]
                 if set(item["related_event_ids"]) <= set(endpoints) and valid(item)]
        cited = {citation["source_id"] for item in [*kept.values(), *matches] for citation in item["evidence"]}
        described = {citation["source_id"] for item in dropped for citation in item["evidence"]}
        waived = sorted(call for call, (_, result) in self.required_citations.items()
                        if call not in cited and result not in cited and ({call, result} & described))
        notes = [*(f"근거 검증을 통과하지 못해 제외한 사건 후보: {item['title']}" for item in dropped),
                 *([f"근거 검증을 통과하지 못해 제외한 관계 후보 {dropped_edges}개"] if dropped_edges else [])]
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

    def apply_delta(self, output: dict, graph: dict, snapshot_id: str, run_id: str,
                    candidates: dict | None = None,
                    expected_review_issues: list[dict] | None = None) -> dict:
        validate_shape(output, DELTA_SCHEMA)
        if output["status"] != "complete" or output["read_requests"]:
            raise FlowError("완료되지 않은 GraphDelta입니다.")
        if output["base_graph_version"] != graph["version"] or output["snapshot_id"] != snapshot_id:
            raise FlowError("GraphDelta의 기준 graph version 또는 snapshot이 다릅니다.")
        self._check_all_citations(output)
        review_issues = output["review_issues"]
        issue_ids = [item["id"] for item in review_issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise FlowError("review_issues ID는 고유해야 합니다.")
        candidate_kind = {"event_candidate": "event", "edge_candidate": "edge",
                          "open_item_candidate": "open_item"}
        for issue in review_issues:
            target = issue["target_id"]
            if issue["target_kind"] in candidate_kind:
                if candidates is None or target not in {x["id"] for x in candidates[
                        {"event": "event_candidates", "edge": "edge_candidates",
                         "open_item": "open_items"}[candidate_kind[issue["target_kind"]]]]}:
                    raise FlowError("review issue가 입력에 없는 후보를 대상으로 합니다.")
            else:
                key = {"event": "events", "edge": "edges", "open_item": "open_items"}[issue["target_kind"]]
                delta_key = {"event": "events_to_add", "edge": "edges_to_add",
                             "open_item": "open_items_to_upsert"}[issue["target_kind"]]
                if target not in ({x["id"] for x in graph[key]} | {x["id"] for x in output[delta_key]}):
                    raise FlowError("review issue가 기존 그래프에 없는 항목을 대상으로 합니다.")
            self.citations(issue["evidence"])
        review_resolutions = output["review_resolutions"]
        if expected_review_issues is None:
            if review_resolutions:
                raise FlowError("검토 호출이 아닌데 review_resolutions를 반환했습니다.")
        else:
            expected_issue_ids = {item["id"] for item in expected_review_issues}
            returned_issue_ids = [item["issue_id"] for item in review_resolutions]
            if len(returned_issue_ids) != len(set(returned_issue_ids)) or set(returned_issue_ids) != expected_issue_ids:
                raise FlowError("review_resolutions는 전달된 모든 이슈를 정확히 한 번 처리해야 합니다.")
            for item in review_resolutions:
                self.citations(item["evidence"])
        if candidates is not None:
            expected = {(item["id"], "event") for item in candidates["event_candidates"]}
            expected |= {(item["id"], "edge") for item in candidates["edge_candidates"]}
            expected |= {(item["id"], "open_item") for item in candidates["open_items"]}
            resolutions = output["candidate_resolutions"]
            actual = [(item["candidate_id"], item["candidate_kind"]) for item in resolutions]
            if len(actual) != len(set(actual)) or set(actual) != expected:
                raise FlowError("candidate_resolutions가 후보를 빠짐없이 정확히 한 번 처리해야 합니다.")
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
                        raise FlowError("excluded 후보에는 대상 ID를 지정할 수 없습니다.")
                elif not item["target_ids"] or not set(item["target_ids"]) <= targets_by_kind[item["candidate_kind"]]:
                    raise FlowError("candidate resolution 대상이 GraphDelta 또는 기존 그래프에 없습니다.")
                if item["disposition"] == "added" and not set(item["target_ids"]) <= added_by_kind[item["candidate_kind"]]:
                    raise FlowError("added 후보 처리는 같은 종류의 새 GraphDelta 항목을 가리켜야 합니다.")
                if item["disposition"] in {"duplicate", "updated"} and any(i.startswith("tmp:") for i in item["target_ids"]):
                    raise FlowError("기존 항목 처리는 기존 그래프 ID를 가리켜야 합니다.")
                if item["disposition"] == "duplicate" and not set(item["target_ids"]) <= existing_by_kind[item["candidate_kind"]]:
                    raise FlowError("duplicate 처리는 실제 기존 그래프 항목을 가리켜야 합니다.")
                if item["disposition"] == "updated" and not set(item["target_ids"]) <= updated_by_kind[item["candidate_kind"]]:
                    raise FlowError("updated 처리는 같은 종류의 GraphDelta 갱신을 가리켜야 합니다.")
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
                raise FlowError("change_attributions가 모든 GraphDelta 변경을 정확히 한 번 귀속해야 합니다.")
            for item in attributions:
                if not item["candidate_ids"] or not set(item["candidate_ids"]) <= valid_candidate_ids:
                    raise FlowError("변경 귀속이 추출 후보에 없는 ID를 참조합니다.")
                resolution_by_id = {row["candidate_id"]: row for row in resolutions}
                attributed = [resolution_by_id[cid] for cid in item["candidate_ids"]]
                linked = any(item["item_id"] in row["target_ids"] for row in attributed)
                if item["operation"] == "edges_to_add" and not linked:
                    edge = next(x for x in output["edges_to_add"] if x["id"] == item["item_id"])
                    linked = any(row["candidate_kind"] == "event" and
                                 set(row["target_ids"]) & {edge["from_event_id"], edge["to_event_id"]}
                                 for row in attributed)
                if item["operation"] == "edges_to_invalidate" and not linked:
                    edge = next(x for x in graph["edges"] if x["id"] == item["item_id"])
                    linked = any(row["candidate_kind"] == "event" and
                                 set(row["target_ids"]) & {edge["from_event_id"], edge["to_event_id"]}
                                 for row in attributed)
                if item["operation"] == "open_items_to_upsert" and not linked:
                    open_item = next(x for x in output["open_items_to_upsert"] if x["id"] == item["item_id"])
                    linked = any(row["candidate_kind"] == "event" and
                                 set(row["target_ids"]) & set(open_item["related_event_ids"])
                                 for row in attributed)
                if item["operation"] == "open_items_to_resolve" and not linked:
                    open_item = next(x for x in graph["open_items"] if x["id"] == item["item_id"])
                    linked = any(row["candidate_kind"] == "event" and
                                 set(row["target_ids"]) & set(open_item.get("related_event_ids", []))
                                 for row in attributed)
                if not linked:
                    raise FlowError("change attribution 후보의 처리 대상이 귀속 GraphDelta 변경과 연결되지 않습니다.")
                if item["operation"] == "events_to_add" and self.assigned_source_ids is not None and not any(
                        quote["source_id"] in self.assigned_source_ids for quote in item["evidence"]):
                    raise FlowError("새 사건 귀속에는 이번 WorkUnit의 인용이 필요합니다.")
                self.citations(item["evidence"])
        else:
            attributions = output["change_attributions"]
            resolutions = output["candidate_resolutions"]
        result = copy.deepcopy(graph)
        event_map = {event["id"]: event for event in result["events"]}
        local_ids: dict[str, str] = {}
        claim_errors: list[str] = []
        for event_id in retype_answered_user_goals(output["events_to_add"], output["edges_to_add"], self.records):
            self.normalizations.append({"mode": "user_goal_as_answered_question", "event": event_id})
        for item in output["events_to_add"]:
            temp_id = item["id"]
            if not temp_id.startswith("tmp:") or temp_id in local_ids:
                raise FlowError("새 사건에는 고유한 tmp: ID를 사용해야 합니다.")
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
        updated = set()
        for update in output["events_to_update"]:
            event_id = update["id"]
            if event_id not in event_map or event_id in updated:
                raise FlowError("수정 대상이 없거나 중복 수정입니다.")
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
                raise FlowError("관계가 존재하지 않는 사건을 참조합니다: " + value)
            return target
        edge_map = {e["id"]: e for e in result["edges"]}
        edge_tmp = set()
        edge_local_ids: dict[str, str] = {}
        for original in output["edges_to_add"]:
            edge = copy.deepcopy(original)
            if not edge["id"].startswith("tmp:") or edge["id"] in edge_tmp:
                raise FlowError("새 관계에는 고유한 tmp: ID가 필요합니다.")
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
                raise FlowError("무효화할 관계가 없습니다.")
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
                raise FlowError("기존 미해결 사항 ID가 없습니다.")
            items[item["id"]] = item
        for resolution in output["open_items_to_resolve"]:
            item = items.get(resolution["id"])
            if not item:
                raise FlowError("해결할 미해결 사항이 없습니다.")
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
