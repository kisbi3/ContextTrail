from __future__ import annotations

import html
import re
from collections import Counter, deque
from datetime import datetime
from typing import Any, Callable

from .i18n import Labels, tr
from .util import FlowError, cell_slice, dumps, ellipsis, merge_focus, safe_text

# Screen labels follow the screen language at lookup time (`i18n.Labels`), so a module that
# imports them keeps working whichever language the process chooses later.
RELATION = Labels({"follows": "후속", "motivates": "동기", "produces": "결과", "revises": "수정",
                   "verifies": "검증", "answers": "답변"},
                  {"follows": "follows", "motivates": "motivates", "produces": "produces", "revises": "revises",
                   "verifies": "verifies", "answers": "answers"})
STATUS = Labels({"proposed": "제안", "adopted": "채택", "in_progress": "진행 중", "asked": "요청", "applied": "변경 적용",
                 "reported_complete": "완료 보고·미검증", "observed_success": "관측 성공", "observed_failure": "관측 실패",
                 "withdrawn": "철회", "unknown": "미확인"},
                {"proposed": "proposed", "adopted": "adopted", "in_progress": "in progress", "asked": "asked",
                 "applied": "applied", "reported_complete": "reported done·unverified",
                 "observed_success": "observed success", "observed_failure": "observed failure",
                 "withdrawn": "withdrawn", "unknown": "unknown"})
KIND = Labels({"goal": "목표", "question": "요청", "proposal": "제안", "decision": "결정", "action": "변경",
               "outcome": "결과", "revision": "수정"},
              {"goal": "goal", "question": "request", "proposal": "proposal", "decision": "decision",
               "action": "change", "outcome": "result", "revision": "fix"})
ACTOR = Labels({"user": "사용자", "assistant": "어시스턴트", "tool": "도구", "system": "시스템",
                "subagent": "하위 에이전트", "git": "Git"},
               {"user": "user", "assistant": "assistant", "tool": "tool", "system": "system",
                "subagent": "sub-agent", "git": "Git"})
ROLE = Labels({"user": "사용자 발화", "assistant": "어시스턴트 응답", "tool_call": "도구 호출",
               "tool_result": "도구 결과", "metadata": "세션 정보", "git": "Git 변경"},
              {"user": "user message", "assistant": "assistant reply", "tool_call": "tool call",
               "tool_result": "tool result", "metadata": "session info", "git": "Git change"})
PROVIDER = {"codex": "Codex", "claude": "Claude Code", "opencode": "opencode", "git": "Git"}
# One glyph per tone so a terminal list reads at a glance, with ASCII fallbacks.
MARK = {"ok": "✓", "warn": "!", "fail": "✗", "plain": "·"}
ASCII_MARK = {"ok": "v", "warn": "!", "fail": "x", "plain": "-"}
# Detour lanes past this count would overlap if wrapped, so extras are reported
# as a count instead of drawn on top of each other. Verified: wrapping made the
# 11th detour reuse lane 0.
MAX_DETOUR_LANES = 24
DETOUR_LANE_PITCH = 18


def verification_links(graph: dict) -> tuple[dict[str, list[dict]], set[str]]:
    """Outcomes that checked each change, and observed outcomes nothing leads to."""
    events = {event["id"]: event for event in graph["events"]}
    checks: dict[str, list[dict]] = {}
    reached = set()
    for edge in graph["edges"]:
        if not edge["active"]:
            continue
        reached.add(edge["to_event_id"])
        if edge["relation"] == "verifies" and edge["to_event_id"] in events:
            checks.setdefault(edge["from_event_id"], []).append(events[edge["to_event_id"]])
    unlinked = {event["id"] for event in graph["events"] if event["kind"] == "outcome" and
                event["status"] in ("observed_success", "observed_failure") and event["id"] not in reached}
    return checks, unlinked


def graph_summary(graph: dict) -> dict:
    """Counts only, so a metadata-only trace can show how the graph is shaped."""
    checks, unlinked = verification_links(graph)
    active = [edge for edge in graph["edges"] if edge["active"]]
    answered = {edge["from_event_id"] for edge in active if edge["relation"] == "answers"}
    changes = [event for event in graph["events"] if event["kind"] in ("action", "revision")
               and event["status"] in ("applied", "reported_complete")]
    questions = [event for event in graph["events"] if event["kind"] == "question"]
    revised = {edge["to_event_id"] for edge in active if edge["relation"] == "revises"}
    return {"events": len(graph["events"]), "relations": len(active),
            "events_by_kind": dict(Counter(event["kind"] for event in graph["events"])),
            "events_by_status": dict(Counter(event["status"] for event in graph["events"])),
            "relations_by_type": dict(Counter(edge["relation"] for edge in active)),
            "changes_verified": sum(bool(checks.get(event["id"])) for event in changes),
            "changes_unverified": sum(not checks.get(event["id"]) for event in changes),
            "observed_results_unlinked": len(unlinked),
            "revisions_unlinked": sum(event["kind"] == "revision" and event["id"] not in revised
                                      for event in graph["events"]),
            "questions_answered": sum(event["id"] in answered for event in questions),
            "questions_unanswered": sum(event["id"] not in answered for event in questions)}


def status_labels(graph: dict) -> dict[str, str]:
    """Each event's status plus what its relations add: checks of a change, an answer to a question."""
    checks, unlinked = verification_links(graph)
    answered = {edge["from_event_id"] for edge in graph["edges"]
                if edge["active"] and edge["relation"] == "answers"}
    labels = {}
    for event in graph["events"]:
        status = event["status"]
        if event["kind"] == "question" and status == "asked":
            labels[event["id"]] = tr("요청 · ", "request · ") + (tr("답변됨", "answered") if event["id"] in answered
                                                            else tr("답변 없음", "no answer"))
        elif event["kind"] in ("action", "revision") and status in ("applied", "reported_complete"):
            results = checks.get(event["id"], [])
            passed = sum(item["status"] == "observed_success" for item in results)
            failed = sum(item["status"] == "observed_failure" for item in results)
            base = tr("변경 적용", "applied") if status == "applied" else tr("완료 보고", "reported done")
            if len(results) == 1:
                # A lone check is named, so a syntax check does not read like a full run.
                verdict = tr("검증", "verified") if passed else tr("검증 실패", "check failed")
                title = safe_text(results[0]["title"], multiline=False)
                labels[event["id"]] = f"{base} · {verdict}: {ellipsis(title, 40)}"
            else:
                parts = (([tr(f"검증 통과 {passed}건", f"{passed} checks passed")] if passed else [])
                         + ([tr(f"검증 실패 {failed}건", f"{failed} checks failed")] if failed else []))
                labels[event["id"]] = base + " · " + (" · ".join(parts) or tr("미검증", "unverified"))
        elif event["id"] in unlinked:
            labels[event["id"]] = STATUS.get(status, status) + tr(" · 확인 대상 미연결", " · not linked to what it checked")
        else:
            labels[event["id"]] = STATUS.get(status, status)
    return labels


def status_tones(graph: dict) -> dict[str, str]:
    """ok / warn / fail / plain per event, from the same relations as the labels."""
    checks, unlinked = verification_links(graph)
    answered = {edge["from_event_id"] for edge in graph["edges"]
                if edge["active"] and edge["relation"] == "answers"}
    tones = {}
    for event in graph["events"]:
        kind, status = event["kind"], event["status"]
        if kind == "question" and status == "asked":
            tone = "ok" if event["id"] in answered else "warn"
        elif kind in ("action", "revision") and status in ("applied", "reported_complete"):
            results = {item["status"] for item in checks.get(event["id"], [])}
            tone = "fail" if "observed_failure" in results else "ok" if results else "warn"
        elif status == "observed_failure":
            tone = "fail"
        elif event["id"] in unlinked or status == "reported_complete":
            tone = "warn"
        elif status == "observed_success":
            tone = "ok"
        else:
            tone = "plain"
        tones[event["id"]] = tone
    return tones


def readable_quote(text: str) -> str:
    """Show a one-line tool record (escaped JSON arguments) with its line breaks restored.

    Display only: stored evidence keeps the exact source text.
    """
    # Two or more escapes: a code line with a single "\\n" in a string literal stays as written.
    if "\n" not in text and text.count("\\n") >= 2:
        text = (text.replace("\\r\\n", "\n").replace("\\n", "\n")
                .replace("\\t", "    ").replace('\\"', '"'))
    return text


def _when(value: str | None) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return moment.astimezone().strftime("%m-%d %H:%M")


def linked_events(graph: dict, event_id: str) -> list[str]:
    """Events linked to one event, in the order its detail lists them: checks first, then the rest."""
    events = {event["id"] for event in graph["events"]}
    active = [edge for edge in graph["edges"] if edge["active"] and
              event_id in (edge["from_event_id"], edge["to_event_id"])]
    ordered = sorted(active, key=lambda edge: edge["relation"] != "verifies")
    others = [edge["to_event_id"] if edge["from_event_id"] == event_id else edge["from_event_id"] for edge in ordered]
    return list(dict.fromkeys(key for key in others if key in events))


def event_detail(graph: dict, event_id: str, evidence: Callable[[str], dict | None], *,
                 ascii_only: bool = False, quote_lines: int = 8, keys: bool = False) -> list[tuple[str, str]]:
    """What a terminal shows for one event, as (text, style) lines.

    Styles: title, heading, ok, warn, fail, plain, dim. Text is not wrapped. With keys, the
    first nine linked events are numbered 1–9 (`linked_events` order) for a screen to jump to.
    """
    events = {event["id"]: event for event in graph["events"]}
    event = events.get(event_id)
    if not event:
        return [(tr("사건을 선택하면 설명과 원문 근거가 표시됩니다.",
                    "Select an event to see its description and source evidence."), "dim")]
    numbers = {item["id"]: f"[{n:02d}]" for n, item in enumerate(graph["events"], 1)}
    labels, tones = status_labels(graph), status_tones(graph)
    marks = ASCII_MARK if ascii_only else MARK
    clean = lambda value: safe_text(value, multiline=False)
    lines: list[tuple[str, str]] = [
        (f"{numbers[event_id]} {clean(event['title'])}", "title"),
        (f"{marks[tones[event_id]]} {clean(labels[event_id])}", tones[event_id]),
        (" · ".join(part for part in (KIND.get(event["kind"], event["kind"]),
                                      ACTOR.get(event.get("actor"), clean(event.get("actor") or "")),
                                      _when(event.get("occurred_at") or event.get("recorded_at"))) if part), "dim"),
        ("", ""), (tr("무슨 일이 있었나", "What happened"), "heading")]
    lines += [("  " + clean(part), "") for part in safe_text(event["summary"]).splitlines() if part.strip()]
    active = [edge for edge in graph["edges"] if edge["active"] and
              event_id in (edge["from_event_id"], edge["to_event_id"])]
    def other(edge: dict) -> dict | None:
        return events.get(edge["to_event_id"] if edge["from_event_id"] == event_id else edge["from_event_id"])
    key_of = {key: f"{n} " for n, key in enumerate(linked_events(graph, event_id)[:9], 1)} if keys else {}
    def key(item: dict) -> str:
        return key_of.get(item["id"], "  " if keys else "")
    checks = [edge for edge in active if edge["relation"] == "verifies"]
    if checks:
        # A change lists what checked it; a result lists what it checked.
        is_change = event["kind"] in ("action", "revision")
        heading = tr("검증한 결과", "Results that checked it") if is_change else tr("이 결과가 확인한 변경",
                                                                                 "Changes this result checked")
        lines += [("", ""), (heading, "heading")]
        for edge in checks:
            item = other(edge)
            if item:
                tone = tones[item["id"]] if is_change else "plain"
                lines.append((f"  {key(item)}{marks[tone]} {numbers[item['id']]} {clean(item['title'])}", tone))
    rest = [edge for edge in active if edge["relation"] != "verifies"]
    if rest:
        lines += [("", ""), (tr("연결된 사건", "Linked events"), "heading")]
        for edge in rest:
            item = other(edge)
            if item:
                arrow = ("->" if ascii_only else "→") if edge["from_event_id"] == event_id else (
                    "<-" if ascii_only else "←")
                # A turn link is the dialog's order, not a claim the model made.
                turn = tr(" (대화 순서)", " (dialog order)") if edge.get("origin") == "dialog_turn" else ""
                lines.append((f"  {key(item)}{arrow} {RELATION.get(edge['relation'], edge['relation'])}{turn}  "
                              f"{numbers[item['id']]} {clean(item['title'])}", "dim" if turn else ""))
    if event["evidence_ids"]:
        count = len(event["evidence_ids"])
        lines += [("", ""), (tr(f"원문 근거 {count}개", f"Source evidence: {count}"), "heading")]
        for index, evidence_id in enumerate(event["evidence_ids"], 1):
            item = evidence(evidence_id)
            if not item:
                lines.append((tr(f"  {index}) 보존된 근거 없음 · {evidence_id}", f"  {index}) no preserved evidence · {evidence_id}"), "dim"))
                continue
            source = item.get("source") or {}
            start, end = item.get("start_line", "?"), item.get("end_line", "?")
            where = tr(f"{start}번째 줄", f"line {start}") if start == end else tr(f"{start}–{end}번째 줄", f"lines {start}–{end}")
            head = " · ".join(part for part in (ROLE.get(source.get("role"), source.get("role") or ""),
                                                PROVIDER.get(source.get("provider"), source.get("provider") or ""),
                                                _when(source.get("recorded_at")), where) if part)
            lines.append((f"  {index}) {head}", "dim"))
            # Excerpt windows are joined by line breaks; each window may be one escaped line.
            quote = "\n".join(readable_quote(part) for part in
                              (evidence_excerpt(item) or item.get("quote", "")).split("\n"))
            shown = [part.rstrip() for part in safe_text(quote).splitlines()]
            while shown and not shown[-1].strip():
                shown.pop()
            for part in shown[:quote_lines]:
                lines.append(("     " + clean(part) if part.strip() else "", ""))
            if len(shown) > quote_lines:
                more = len(shown) - quote_lines
                lines.append((tr(f"     … {more}줄 더 있음", f"     … {more} more lines"), "dim"))
    open_items = [item for item in graph.get("open_items", []) if isinstance(item, dict) and
                  event_id in item.get("related_event_ids", [])]
    if open_items:
        lines += [("", ""), (tr("아직 확인되지 않은 일", "Open items"), "heading")]
        lines += [("  • " + clean(item.get("text", "")), "") for item in open_items]
    return lines


# A model may cite a few words inside a very long line (a whole patch or tool output).
# Show those cited parts with nearby context instead of the whole stored line.
EXCERPT_MIN_CHARS = 400
EXCERPT_CONTEXT = 120


def evidence_excerpt(item: dict) -> str | None:
    """Cited parts of a long evidence quote, or None when the whole quote should be shown."""
    quote = item.get("quote") or ""
    focus = [[s, e] for s, e in item.get("focus") or [] if 0 <= s < e <= len(quote)]
    if len(quote) <= EXCERPT_MIN_CHARS or not focus:
        return None
    windows = merge_focus([[max(0, s - EXCERPT_CONTEXT), min(len(quote), e + EXCERPT_CONTEXT)] for s, e in focus])
    return "\n".join(("…" if start > 0 else "") + quote[start:end] + ("…" if end < len(quote) else "")
                     for start, end in windows)


def _label(text: str) -> str:
    # Encode syntax characters so user content cannot introduce Mermaid directives.
    return "".join(c if c.isalnum() or c in " _-·" else f"#{ord(c)};" for c in safe_text(text, multiline=False))


def _decode(text: str) -> str:
    return re.sub(r"#(\d+);", lambda m: chr(int(m[1])), text)


def mermaid(graph: dict) -> str:
    ids = {event["id"]: f"n{n}" for n, event in enumerate(graph["events"])}
    lines = ["flowchart TB"]
    statuses = status_labels(graph)
    for n, event in enumerate(graph["events"]):
        label = _label(f"{n + 1:02d} {event['title']} / {statuses[event['id']]}")
        lines.append(f'  n{n}["{label}"]')
    for edge in graph["edges"]:
        if not edge["active"]:
            continue
        left, right = ids[edge["from_event_id"]], ids[edge["to_event_id"]]
        inferred = edge["basis"] == "inferred"
        arrow = "-.->" if inferred else "-->"
        label = _label(RELATION[edge["relation"]] + (tr("·추정", "·inferred") if inferred else ""))
        lines.append(f"  {left} {arrow}|{label}| {right}")
    return "\n".join(lines) + "\n"


def parse_safe_mermaid(text: str) -> tuple[dict[str, str], list[tuple[str, str, str, bool]]]:
    """Only parse our generated subset, not general Mermaid or untrusted instructions."""
    nodes, edges = {}, []
    lines = text.splitlines()
    if not lines or lines[0] != "flowchart TB":
        raise FlowError(tr("지원하지 않는 Mermaid subset", "Unsupported Mermaid subset"))
    for line in lines[1:]:
        node = re.fullmatch(r'\s*(n\d+)\["([^"\n]*)"\]', line)
        edge = re.fullmatch(r"\s*(n\d+) (-->|-\.->)\|([^|\n]*)\| (n\d+)", line)
        if node:
            nodes[node[1]] = _decode(node[2])
        elif edge:
            edges.append((edge[1], edge[4], _decode(edge[3]), edge[2] == "-.->"))
        elif line.strip():
            raise FlowError(tr("지원하지 않는 Mermaid 문법", "Unsupported Mermaid syntax"))
    if any(left not in nodes or right not in nodes for left, right, _, _ in edges):
        raise FlowError(tr("Mermaid의 노드 참조가 유효하지 않습니다.", "A Mermaid node reference is invalid."))
    return nodes, edges


def terminal_graph(graph: dict, *, ascii_only: bool = False,
                   marks: bool = False) -> list[tuple[str, str | None]]:
    """An actual branching graph. Revisited nodes are references (joins/cycles), not duplicates.

    With marks, each event starts with its tone glyph (✓ ! ✗ ·) so a list reads at a glance.
    """
    nodes, edges = parse_safe_mermaid(mermaid(graph))
    real_ids = {f"n{n}": event["id"] for n, event in enumerate(graph["events"])}
    glyphs = ASCII_MARK if ascii_only else MARK
    tones = status_tones(graph) if marks else {}
    outgoing = {n: [] for n in nodes}
    incoming = {n: 0 for n in nodes}
    for source, target, label, inferred in edges:
        outgoing[source].append((target, label, inferred))
        incoming[target] += 1
    roots = [n for n in nodes if incoming[n] == 0] + [n for n in nodes if incoming[n] != 0]
    seen, result = set(), []
    branch, last, vertical = ("+--", "`--", "|  ") if ascii_only else ("├──", "└──", "│  ")
    def walk(node: str, prefix: str, connector: str = "", relation: str = "", depth: int = 0) -> None:
        if node in seen:
            reference = "->" if ascii_only else "↗"
            number = f"{int(node[1:]) + 1:02d}"
            result.append((f"{prefix}{connector}{relation}{reference} [{number}] " + tr("(합류/되돌아감)", "(join/back)"),
                           real_ids[node]))
            return
        seen.add(node)
        label = nodes[node]
        number, _, description = label.partition(" ")
        mark = glyphs[tones[real_ids[node]]] + " " if marks else ""
        result.append((f"{prefix}{connector}{relation}[{number}] {mark}{description}", real_ids[node]))
        # Iterative call depth is bounded for pathological thousand-node histories.
        if depth >= 70 and outgoing[node]:
            for child, tag, inferred in outgoing[node]:
                result.append((f"{prefix}   -> [{int(child[1:]) + 1:02d}] {tag} " + tr("(깊은 분기 참조)", "(deep branch reference)"),
                               real_ids[child]))
            return
        children = outgoing[node]
        for n, (child, tag, inferred) in enumerate(children):
            is_last = n == len(children) - 1
            next_prefix = prefix + ("   " if connector == last else vertical if connector else "")
            arrow = "..>" if inferred else "->" if ascii_only else "→"
            walk(child, next_prefix, last if is_last else branch, f"{tag} {arrow} ", depth + 1)
    for node in roots:
        if node not in seen:
            if result:
                result.append(("", None))
            walk(node, "")
    if not result:
        result = [(tr("아직 저장된 사건이 없습니다.", "No events saved yet."), None)]
    return result


def _positions(graph: dict) -> tuple[dict[str, tuple[int, int]], int, int]:
    events = graph["events"]
    outgoing = {e["id"]: [] for e in events}
    degree = {e["id"]: 0 for e in events}
    for edge in graph["edges"]:
        if edge["active"]:
            outgoing[edge["from_event_id"]].append(edge["to_event_id"])
            degree[edge["to_event_id"]] += 1
    rank, queue = {key: 0 for key in degree}, deque(k for k in degree if degree[k] == 0)
    visited = set()
    while queue:
        node = queue.popleft()
        visited.add(node)
        for child in outgoing[node]:
            rank[child] = max(rank[child], rank[node] + 1)
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    # Cycles remain visible, with original edges. No DAG restriction is imposed on stored data.
    last = max(rank.values(), default=0)
    for event in events:
        if event["id"] not in visited:
            last += 1
            rank[event["id"]] = last
    layers: dict[int, list[str]] = {}
    for event in events:
        layers.setdefault(rank[event["id"]], []).append(event["id"])
    positions = {key: (32 + column * 330, 32 + layer * 164)
                 for layer, keys in layers.items() for column, key in enumerate(keys)}
    width = max((x + 310 for x, _ in positions.values()), default=600) + 32
    height = max((y + 108 for _, y in positions.values()), default=180) + 32
    return positions, width, height


def svg(graph: dict) -> str:
    # Same safe subset as the terminal path; intentionally not a general Mermaid engine.
    parse_safe_mermaid(mermaid(graph))
    positions, width, height = _positions(graph)
    detours = [e for e in graph["edges"] if e["active"] and
               positions[e["to_event_id"]][1] - positions[e["from_event_id"]][1] != 164]
    gutter_start = width + 12
    drawn_detours = detours[:MAX_DETOUR_LANES]
    if detours:
        width += 120 + len(drawn_detours) * DETOUR_LANE_PITCH
    detour_index = 0
    undrawn = 0
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{tr("프로젝트 흐름", "Project flow")}" font-family="Noto Sans CJK KR, Noto Sans KR, Malgun Gothic, Apple SD Gothic Neo, sans-serif">',
           '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="#778593"/></marker></defs>']
    for edge in graph["edges"]:
        if not edge["active"]:
            continue
        x1, y1 = positions[edge["from_event_id"]]
        x2, y2 = positions[edge["to_event_id"]]
        x1, y1, x2 = x1 + 145, y1 + 108, x2 + 145
        dashed = ' stroke-dasharray="5 5"' if edge["basis"] == "inferred" else ""
        if y2 - y1 == 56:
            midpoint = (y1 + y2) / 2
            path = f"M{x1} {y1} C{x1} {midpoint} {x2} {midpoint} {x2} {y2 - 5}"
            label_x, label_y = (x1 + x2) / 2 + 9, midpoint
        else:
            # Skipping an intermediate rank must NOT draw through an unrelated node.
            # Route long/backward edges outside all boxes, retaining the exact target.
            if detour_index >= MAX_DETOUR_LANES:
                # Lanes are finite. Overlapping paths read as one wrong line, which
                # is worse than a stated omission, so count the rest instead.
                undrawn += 1
                continue
            lane = gutter_start + detour_index * DETOUR_LANE_PITCH
            detour_index += 1
            top, bottom = y1 + 20, y2 - 20
            path = f"M{x1} {y1} V{top} H{lane} V{bottom} H{x2} V{y2 - 5}"
            label_x, label_y = lane + 5, (top + bottom) / 2
        label = RELATION[edge["relation"]] + (tr("·추정", "·inferred") if edge["basis"] == "inferred" else "")
        out += [f'<path d="{path}" fill="none" stroke="#778593" stroke-width="1.7"{dashed} marker-end="url(#arrow)"/>',
                f'<text x="{label_x}" y="{label_y}" fill="#667788" font-size="11">{html.escape(label)}</text>']
    statuses = status_labels(graph)
    for number, event in enumerate(graph["events"], 1):
        x, y = positions[event["id"]]
        status = statuses[event["id"]]
        title = safe_text(event["title"], multiline=False)
        first, second = cell_slice(title, 0, 31), cell_slice(title, 31, 31)
        if len(title) > len(first + second):
            second = ellipsis(second, 28) + "…"
        out.append(f'<g data-event-id="{html.escape(event["id"], quote=True)}" tabindex="0" role="button" aria-label="{html.escape(title, quote=True)}" transform="translate({x},{y})">')
        out.append('<rect width="290" height="108" rx="9" fill="#ffffff" stroke="#ced7df"/>')
        out.append(f'<text x="16" y="23" fill="#7b8995" font-size="11">{number:02d} · {html.escape(event["kind"])}</text>')
        out.append(f'<text x="16" y="47" fill="#203343" font-size="14" font-weight="600">{html.escape(first)}</text>')
        out.append(f'<text x="16" y="67" fill="#203343" font-size="14" font-weight="600">{html.escape(second)}</text>')
        out.append(f'<text x="16" y="92" fill="#617686" font-size="11">{html.escape(status)}</text></g>')
    if not graph["events"]:
        out.append(f'<text x="32" y="70" fill="#617686" font-size="18">{tr("저장된 사건이 없습니다.", "No saved events.")}</text>')
    if undrawn:
        note = tr(f"긴 연결 {undrawn}개는 선이 겹쳐 생략했습니다. 전체 목록은 text 출력이나 브라우저 보기를 사용하세요.",
                  f"{undrawn} long links were left out because their lines would overlap; "
                  f"use the text output or the browser view for the full list.")
        out.append(f'<text x="32" y="{height - 14}" fill="#8a97a4" font-size="11">{html.escape(note)}</text>')
    return "\n".join(out) + "</svg>"


def _md(text: Any) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(safe_text(text), quote=False))


def export_text(graph: dict, evidence: dict[str, dict], fmt: str) -> str:
    if fmt == "mmd":
        return mermaid(graph)
    if fmt == "json":
        return dumps({"format": "contexttrail.export.v1", "sensitive": True, "graph": graph, "evidence": evidence}, pretty=True) + "\n"
    if fmt != "md":
        raise FlowError(tr("내보내기 형식은 md, mmd, json입니다.", "Export formats are md, mmd and json."))
    analyzed = _md(graph.get("analyzed_at") or tr("없음", "none"))
    lines = ["# ContextTrail", "",
             tr("> 민감한 대화·코드가 포함될 수 있습니다. 공유 전에 확인하세요.",
                "> May contain sensitive conversation and code. Review before sharing."), "",
             tr(f"그래프 버전: {graph['version']} · 분석 기준: {analyzed}",
                f"Graph version: {graph['version']} · analyzed as of: {analyzed}"), "",
             "```mermaid", mermaid(graph).strip(), "```", "", tr("## 사건과 근거", "## Events and evidence")]
    statuses = status_labels(graph)
    for number, event in enumerate(graph["events"], 1):
        lines += ["", f"### {number:02d}. {_md(event['title'])}", "", _md(event["summary"]), "",
                  tr(f"상태: {_md(statuses[event['id']])} · 근거 수준: {_md(event['basis'])}",
                     f"Status: {_md(statuses[event['id']])} · basis: {_md(event['basis'])}"),
                  f"ID: `{event['id']}`", ""]
        for evidence_id in event["evidence_ids"]:
            item = evidence.get(evidence_id)
            if not item:
                continue
            lines += [tr(f"근거 `{evidence_id}` · `{item['source_id']}` · 줄 {item['start_line']}–{item['end_line']}",
                         f"Evidence `{evidence_id}` · `{item['source_id']}` · lines {item['start_line']}–{item['end_line']}"), ""]
            excerpt = evidence_excerpt(item)
            if excerpt:
                lines += [tr(f"원문 줄 {len(item['quote'])}자 중 인용 부분 발췌 · 전체는 근거 색인 참조",
                             f"Cited parts of a {len(item['quote'])}-character source line · see the evidence index for the whole"), ""]
            quote = safe_text(excerpt or item["quote"])
            fence = "~" * max(4, max((len(m) for m in re.findall(r"~+", quote)), default=0) + 1)
            lines += [fence + "text", quote, fence, ""]
    if graph["edges"]:
        titles = {event["id"]: event["title"] for event in graph["events"]}
        lines += ["", tr("## 관계와 연결 근거", "## Relations and their evidence"), ""]
        for edge in graph["edges"]:
            lines += [f"### {_md(titles[edge['from_event_id']])} → {_md(titles[edge['to_event_id']])}", "",
                      tr(f"관계: {_md(edge['relation'])} · {_md(edge['basis'])} · 활성: {edge['active']}",
                         f"Relation: {_md(edge['relation'])} · {_md(edge['basis'])} · active: {edge['active']}"),
                      _md(edge["rationale"]), tr("근거: ", "Evidence: ") + ", ".join(f"`{i}`" for i in edge["evidence_ids"]), ""]
    if graph["open_items"]:
        lines += [tr("## 미해결 사항", "## Open items"), ""]
        for item in graph["open_items"]:
            lines += [f"[{_md(item['status'])}] {_md(item['text'])}",
                      tr("근거: ", "Evidence: ") + ", ".join(f"`{i}`" for i in item["evidence_ids"]), ""]
    # Relation-only citations also remain inspectable in the standalone Markdown export.
    lines += [tr("## 전체 근거 색인", "## Full evidence index"), ""]
    for evidence_id, item in sorted(evidence.items()):
        lines += [f"### `{evidence_id}`", "",
                  tr(f"원문: `{item['source_id']}` · 줄 {item['start_line']}–{item['end_line']}",
                     f"Source: `{item['source_id']}` · lines {item['start_line']}–{item['end_line']}"),
                  _md(dumps(item["source"]["locator"])), ""]
        quote = safe_text(item["quote"])
        fence = "~" * max(4, max((len(m) for m in re.findall(r"~+", quote)), default=0) + 1)
        lines += [fence + "text", quote, fence, ""]
    lines += [tr("## 한계", "## Limitations"), ""] + [_md(message) for message in graph["limitations"] + graph.get("input_limitations", [])]
    return "\n".join(lines) + "\n"
