from __future__ import annotations

import html
import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from .i18n import Labels, tr
from .schema import retired
from .util import FlowError, dumps, ellipsis, merge_focus, safe_text

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


def kind_label(event: dict) -> str:
    """An event's kind as the views name it: a change that only handed work to another agent reads `위임`."""
    if event.get("step") == "delegate":
        return tr("위임", "delegation")
    return KIND.get(event["kind"], event["kind"])


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
# A run's end state in the screen language (`last_check.status`, from `analysis_runs`), in the TUI and the browser.
RUN_STATE = Labels({"complete": "완료", "partial": "일부 완료", "failed": "실패", "noop": "새 기록 없음",
                    "no_data": "기록 없음", "cancelled": "취소됨", "call_limit": "호출 상한", "running": "실행 중"},
                   {"complete": "complete", "partial": "partial", "failed": "failed", "noop": "nothing new",
                    "no_data": "no records", "cancelled": "cancelled", "call_limit": "call cap reached",
                    "running": "running"})
# What an event rests on, and how a relation was found, as the browser's detail names them.
BASIS = Labels({"explicit_statement": "말로 밝힘", "tool_record": "도구 기록", "git_artifact": "Git 기록",
                "inference": "추정"},
               {"explicit_statement": "stated", "tool_record": "tool record", "git_artifact": "Git record",
                "inference": "inferred"})
LINK_BASIS = Labels({"explicit": "명시", "inferred": "추정", "structural": "구조"},
                    {"explicit": "stated", "inferred": "inferred", "structural": "structure"})


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
               and event["status"] in ("applied", "reported_complete") and event.get("step") != "delegate"]
    questions = [event for event in graph["events"] if event["kind"] == "question" and not retired(event)]
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


def request_links(graph: dict) -> tuple[set[str], set[str]]:
    """The requests with an answer linked, and those with any other relation leaving them (work that followed)."""
    answered, followed = set(), set()
    for edge in graph["edges"]:
        if edge["active"]:
            (answered if edge["relation"] == "answers" else followed).add(edge["from_event_id"])
    return answered, followed - answered


# A change and the statuses it is drawn with: applied or reported, and the observed success older graphs gave a
# change itself, which reads as applied (never as a success badge). A change stored as an observed failure keeps
# reading as a failure.
CHANGE_KINDS = ("action", "revision")
CHANGE_STATUSES = ("applied", "reported_complete", "observed_success")
# The heads code puts on a check's title (`light.code_title`, `light.skeleton`).
CHECK_TITLE_HEADS = ("검사 실패: ", "검사 통과: ", "Check failed: ", "Check passed: ")


def status_labels(graph: dict) -> dict[str, str]:
    """Each event's status plus what its relations add: checks of a change, an answer to a question, the work a
    delegation started."""
    checks, unlinked = verification_links(graph)
    answered, followed = request_links(graph)
    handed = Counter(event["delegated_by"] for event in graph["events"] if event.get("delegated_by"))
    labels = {}
    for event in graph["events"]:
        status = event["status"]
        if retired(event):
            # Set aside by `refresh`: under today's rules its message is no request of the person's (harness text, a
            # control command, a sub-agent's brief, Claude Code's note after the agent reads an image file).
            labels[event["id"]] = tr("요청 아님 · 지금 규칙으로는 사람의 요청이 아님",
                                     "not a request · not the person's request under the current rules")
        elif event.get("step") == "delegate":
            # Handing work over is no change to check: it reads as what the other agent did with it.
            count = handed.get(event["id"], 0)
            labels[event["id"]] = tr("위임함 · ", "delegated · ") + (
                tr(f"하위 작업 {count}건", f"{count} sub-agent events") if count
                else tr("하위 작업 연결 없음", "no sub-agent work linked"))
        elif event["kind"] == "question" and status == "asked":
            # No answer linked is not the same as no answer: work that followed the request in its turn says the
            # request was taken up, even when nothing is linked as its answer (`docs/plans/REQUEST_ANSWERS.md` §6).
            # With nothing linked at all the graph knows only that: the turn may never have been analysed (a noted
            # session's turns are not) or left no event, so it does not claim the request went unanswered.
            labels[event["id"]] = tr("요청 · ", "request · ") + (
                tr("답변됨", "answered") if event["id"] in answered
                else tr("답변 연결 없음", "no answer linked") if event["id"] in followed
                else tr("이어진 사건 연결 없음", "nothing linked"))
        elif event["kind"] in CHANGE_KINDS and status in CHANGE_STATUSES:
            # A change is verified only through a check of it; an observed status on the change itself (graphs
            # written before that rule) reads as applied, never as a success badge.
            results = checks.get(event["id"], [])
            passed = sum(item["status"] == "observed_success" for item in results)
            failed = sum(item["status"] == "observed_failure" for item in results)
            base = tr("완료 보고", "reported done") if status == "reported_complete" else tr("변경 적용", "applied")
            if len(results) == 1:
                # A lone check is named, so a syntax check does not read like a full run.
                verdict = tr("검증", "verified") if passed else tr("검증 실패", "check failed")
                # The verdict says how the check went; the head code gave the check's title would say it again.
                title = safe_text(results[0]["title"], multiline=False)
                title = next((title[len(head):] for head in CHECK_TITLE_HEADS if title.startswith(head)), title)
                labels[event["id"]] = f"{base} · {verdict}: {ellipsis(title, 40)}"
            else:
                parts = (([tr(f"검증 통과 {passed}건", f"{passed} checks passed")] if passed else [])
                         + ([tr(f"검증 실패 {failed}건", f"{failed} checks failed")] if failed else []))
                labels[event["id"]] = base + " · " + (" · ".join(parts) or tr("미검증", "unverified"))
        elif event["id"] in unlinked:
            labels[event["id"]] = STATUS.get(status, status) + tr(" · 무엇을 검사했는지 연결 없음", " · not linked to what it checked")
        else:
            labels[event["id"]] = STATUS.get(status, status)
    return labels


def status_tones(graph: dict) -> dict[str, str]:
    """ok / warn / fail / plain per event, from the same relations as the labels."""
    checks, unlinked = verification_links(graph)
    answered, _ = request_links(graph)
    tones = {}
    for event in graph["events"]:
        kind, status = event["kind"], event["status"]
        if event.get("step") == "delegate":
            tone = "plain"
        elif kind == "question" and status == "asked":
            # Unanswered is never claimed (`status_labels`): a request with no answer linked is plain, not a warning.
            tone = "ok" if event["id"] in answered else "plain"
        elif kind in CHANGE_KINDS and status in CHANGE_STATUSES:
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


def agent_name(agent: dict) -> str:
    """The tool that wrote a record, with the sub-agent it ran as: `claude › Explore`, or the called tool's own
    name when it is one (`codex exec`)."""
    tool, named = agent.get("tool", ""), agent.get("agent", "")
    if not named:
        return tool
    # A called tool is named as a command (`codex exec`, `claude -p`); a sub-agent type is one word (`Explore`,
    # `claude-code-guide`), shown under its tool.
    return named if named.startswith(f"{tool} ") else f"{tool} › {named}"


def agents_label(event: dict) -> str:
    """Who did the work an event rests on, from its `authoring` (read off the cited records at publish): the
    tool (with the sub-agent it ran as), then the model and effort its log named, as `claude · opus-5-5 · xhigh`
    (a model's name drops the tool's own prefix); a run with no person says so; a person's message names only the
    tool. Several agents are separated by commas; empty when no agent record is cited."""
    parts: list[str] = []
    for agent in event.get("authoring") or []:
        tool = agent.get("tool", "")
        pieces = [agent_name(agent)]
        if agent.get("role") == "unattended":
            pieces.append(tr("사람 없음", "no person"))
        if event.get("actor") != "user":
            pieces += [agent.get("model", "").removeprefix(f"{tool}-"), agent.get("effort", "")]
        label = " · ".join(piece for piece in pieces if piece)
        if label and label not in parts:
            parts.append(label)
    return ", ".join(parts)


def delegate_label(event: dict) -> str:
    """For a box header: whose work this is when it is not the session's own agent, `하위 Explore` for a
    sub-agent, the called tool's name (`codex exec`) for a run another call started; empty otherwise."""
    for agent in event.get("authoring") or []:
        if agent.get("role") not in ("subagent", "unattended"):
            continue
        named = agent.get("agent", "")
        if named and named.startswith(f"{agent.get('tool', '')} "):
            return named
        return tr("하위 ", "sub-agent ") + (named or agent.get("tool", ""))
    return ""


def analysis_label(event: dict) -> str:
    """The analysis model that wrote an event (`analysis`, stamped when its unit was published): `codex gpt-6.1-sol medium`."""
    analysis = event.get("analysis") or {}
    return " ".join(str(analysis[key]) for key in ("runner", "model", "effort") if analysis.get(key))


def linked_events(graph: dict, event_id: str) -> list[str]:
    """Events linked to one event, in the order its detail lists them: checks first, then the rest."""
    events = {event["id"] for event in graph["events"]}
    active = [edge for edge in graph["edges"] if edge["active"] and
              event_id in (edge["from_event_id"], edge["to_event_id"])]
    ordered = sorted(active, key=lambda edge: edge["relation"] != "verifies")
    others = [edge["to_event_id"] if edge["from_event_id"] == event_id else edge["from_event_id"] for edge in ordered]
    return list(dict.fromkeys(key for key in others if key in events))


def detail_context(graph: dict) -> dict:
    """What `event_detail` reads of the whole graph, computed once for a caller that shows many events (the plain
    `graph` output showed 4,784 in 136 s computing it per event)."""
    edges: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        if edge["active"]:
            for key in {edge["from_event_id"], edge["to_event_id"]}:
                edges.setdefault(key, []).append(edge)
    return {"events": {event["id"]: event for event in graph["events"]},
            "numbers": {item["id"]: f"[{n:02d}]" for n, item in enumerate(graph["events"], 1)},
            "labels": status_labels(graph), "tones": status_tones(graph), "edges": edges}


def event_detail(graph: dict, event_id: str, evidence: Callable[[str], dict | None], *,
                 ascii_only: bool = False, quote_lines: int = 8, keys: bool = False,
                 context: dict | None = None) -> list[tuple[str, str]]:
    """What a terminal shows for one event, as (text, style) lines.

    Styles: title, heading, ok, warn, fail, plain, dim. Text is not wrapped. With keys, the
    first nine linked events are numbered 1–9 (`linked_events` order) for a screen to jump to.
    `context` is `detail_context(graph)` when the caller shows many events.
    """
    context = context or detail_context(graph)
    events = context["events"]
    event = events.get(event_id)
    if not event:
        return [(tr("사건을 선택하면 설명과 원문 근거가 표시됩니다.",
                    "Select an event to see its description and source evidence."), "dim")]
    numbers, labels, tones = context["numbers"], context["labels"], context["tones"]
    marks = ASCII_MARK if ascii_only else MARK
    clean = lambda value: safe_text(value, multiline=False)
    lines: list[tuple[str, str]] = [
        (f"{numbers[event_id]} {clean(event['title'])}", "title"),
        (f"{marks[tones[event_id]]} {clean(labels[event_id])}", tones[event_id]),
        (" · ".join(part for part in (kind_label(event),
                                      ACTOR.get(event.get("actor"), clean(event.get("actor") or "")),
                                      _when(event.get("occurred_at") or event.get("recorded_at"))) if part), "dim")]
    if agents_label(event):
        lines.append((tr("에이전트: ", "agent: ") + agents_label(event), "dim"))
    if analysis_label(event):
        lines.append((tr("분석: ", "analysed by: ") + analysis_label(event), "dim"))
    for report in event.get("received_reports") or []:
        # The caller's own result for the same report, drawn as this box (`schema.folded_view`).
        lines.append((tr("호출한 쪽이 받은 보고: ", "report as its caller received it: ") + clean(report["title"]), "dim"))
    said = [("  " + clean(part), "") for part in safe_text(event["summary"]).splitlines() if part.strip()]
    lines += [("", ""), (tr("무슨 일이 있었나", "What happened"), "heading"), *said] if said else []
    active = context["edges"].get(event_id, [])
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
                turn = (tr(" (대화 순서)", " (dialog order)") if edge.get("origin") == "dialog_turn" else
                        tr(" (갈라진 세션)", " (forked session)") if edge.get("origin") == "fork" else
                        tr(" (위임)", " (delegation)") if edge.get("origin") == "delegation" else "")
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


def _md(text: Any) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(safe_text(text), quote=False))


def graph_changes(old: dict, new: dict) -> dict[str, Any]:
    """What changed from one graph version to a later one, by ID: events, relations and open items added, updated,
    invalidated (an event whose status became `withdrawn`, a relation made inactive; an open item resolved) or gone,
    each item whole as the later version holds it. For `export --since-version`."""
    def compare(before: list[dict], after: list[dict], invalidated) -> dict[str, list]:
        earlier = {item["id"]: item for item in before}
        result: dict[str, list] = {"added": [], "updated": [], "invalidated": [], "removed": []}
        for item in after:
            previous = earlier.pop(item["id"], None)
            if previous is None:
                result["added"].append(item)
            elif previous != item:
                result["invalidated" if invalidated(previous, item) else "updated"].append(item)
        result["removed"] = list(earlier)
        return result
    events = compare(old.get("events", []), new.get("events", []),
                     lambda a, b: b.get("status") == "withdrawn" and a.get("status") != "withdrawn")
    edges = compare(old.get("edges", []), new.get("edges", []), lambda a, b: a.get("active") and not b.get("active"))
    items = compare(old.get("open_items", []), new.get("open_items", []),
                    lambda a, b: a.get("status") != b.get("status") and b.get("status") == "resolved")
    changes: dict[str, Any] = {}
    for name, found in (("events", events), ("edges", edges), ("open_items", items)):
        for kind, values in found.items():
            changes[f"{name}_{'resolved' if name == 'open_items' and kind == 'invalidated' else kind}"] = values
    return changes


def export_delta(old: dict, new: dict, evidence_for) -> str:
    """`export --since-version N --format json`: the changes from version N to this one with the evidence they cite
    (`evidence_for(ids)`), keyed on the `graph.version` a previous export carried."""
    changes = graph_changes(old, new)
    cited = [i for key, values in changes.items() if not key.endswith("_removed") for item in values
             for i in _cited(item)]
    return dumps({"format": "contexttrail.export-delta.v1", "sensitive": True, "scope_id": new.get("scope_id"),
                  "from_version": old.get("version", 0), "to_version": new.get("version", 0),
                  "schema_version": new.get("schema_version"), "analysis_status": new.get("analysis_status"),
                  "analyzed_at": new.get("analyzed_at"), **changes,
                  "evidence": evidence_for(list(dict.fromkeys(cited)))}, pretty=True) + "\n"


def _cited(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                found.extend(i for i in item if isinstance(i, str))
            else:
                found.extend(_cited(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_cited(item))
    return found


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
