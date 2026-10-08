"""Private, static review of model proposals and their cited source lines."""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

from .i18n import language, tr
from .schema import resolve_quote
from .util import FlowError, dumps, input_tokens, private_dir


CALL_ID = re.compile(r"llm_[0-9a-f]{32}\Z")
SOURCE_SPAN = re.compile(r"(src_[0-9a-f]+):(\d+)-(\d+)")


def _read_json(path: Path, *, limit: int = 12_000_000) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise FlowError(tr(f"평가 파일을 안전하게 읽을 수 없습니다: {path.name}", f"The eval file cannot be read safely: {path.name}"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise FlowError(tr(f"평가 JSON이 잘못되었습니다: {path.name}", f"The eval JSON is invalid: {path.name}")) from exc


class EvalCallRecorder:
    """Keep exact pre-validation model responses, including rejected proposals."""

    def __init__(self, output: Path):
        self.directory = output / "call-review"
        private_dir(self.directory)

    def __call__(self, item: dict) -> None:
        call_id = item["call_id"]
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise FlowError(tr("평가 호출 ID가 잘못되었습니다.", "The eval call ID is invalid."))
        path = self.directory / f"{call_id}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(dumps(item, pretty=True))


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _source_lines(record: dict, start: int, end: int) -> str:
    lines = record.get("content", "").splitlines()
    if not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= len(lines):
        return tr("줄 범위가 원문 밖입니다.", "The line range lies outside the source.")
    return "\n".join(f"{number:>3} │ {lines[number - 1]}" for number in range(start, end + 1))


def _citation_match(citation: dict, records: dict[str, dict]) -> str:
    record = records.get(citation.get("source_id"))
    start, end, quote = citation.get("start_line"), citation.get("end_line"), citation.get("quote")
    lines = record.get("content", "").splitlines() if record else []
    if not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= len(lines):
        return "unavailable"
    if not isinstance(quote, str):
        return "mismatch"
    region = "\n".join(lines[start - 1:end])
    if quote == region:
        return "exact"
    if len(quote) < 8:
        return "mismatch"
    resolved = resolve_quote(region, quote)
    if resolved[0] is None:
        return "mismatch"
    mode = resolved[3]
    if "repeated" in mode:
        return "repeated"
    if "diff_hunk" in mode:
        return "diff_hunk"
    return "escape_decoded" if mode.startswith(("escape_decoded", "quote_unescaped")) else "substring"


def _citation(citation: dict, records: dict[str, dict]) -> str:
    source_id = citation.get("source_id")
    start, end = citation.get("start_line"), citation.get("end_line")
    quote = citation.get("quote", "")
    record = records.get(source_id)
    actual = _source_lines(record, start, end) if record else tr("이 fixture에 원문이 없습니다.", "This fixture has no source for it.")
    state = _citation_match(citation, records)
    verdict, style = {"unavailable": (tr("원문/줄 범위를 확인할 수 없음", "source/line range cannot be checked"), "uncertain"),
                      "exact": (tr("정확한 줄 인용", "exact line quote"), "good"),
                      "substring": (tr("원문 안의 유일한 부분 인용", "unique partial quote within the source"), "good"),
                      "escape_decoded": (tr("이스케이프 한 겹 차이로 유일하게 일치 · 저장은 원문 표기",
                                            "unique match up to one layer of escaping · stored as written in the source"), "good"),
                      "repeated": (tr("같은 줄 안에서 반복 · 저장 근거는 동일", "repeated within the same lines · stored evidence is the same"), "good"),
                      "diff_hunk": (tr("diff 한 hunk의 여러 줄에서 일치 · 해당 줄 전체 저장",
                                       "matches across several lines of one diff hunk · those whole lines are stored"), "good"),
                      "mismatch": (tr("문자열 불일치 · 의미 오류 판정 아님", "string mismatch · not a judgement of meaning"), "bad")}[state]
    hint = ("<p>" + tr("이 원문은 도구 호출 문자열입니다. 코드가 이스케이프되어 기록됐을 수 있으니 아래 두 텍스트를 비교하세요.",
                       "This source is a tool call string. The code may have been recorded escaped, so compare the two texts below.")
            + "</p>" if state == "mismatch" and record and record.get("role") == "tool_call" else "")
    return (f"<div class='citation {style}'><b>{_escape(verdict)}</b> · "
            f"<code>{_escape(source_id)}:{_escape(start)}-{_escape(end)}</code>"
            f"<div class='columns'><div><small>{tr('모델 인용', 'Model quote')}</small><pre>{_escape(quote)}</pre></div>"
            f"<div><small>{tr('실제 원문', 'Actual source')}</small><pre>{_escape(actual)}</pre></div></div>{hint}</div>")


def _all_citations(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("evidence"), list):
            yield from (item for item in value["evidence"] if isinstance(item, dict))
        for child in value.values():
            yield from _all_citations(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_citations(child)


def _candidate_groups(response: dict) -> list[tuple[str, list[dict]]]:
    # A review answer in patch mode carries the same sections one level down, under `patch`.
    body = {**response, **response["patch"]} if isinstance(response.get("patch"), dict) else response
    names = (("event_candidates", tr("사건 후보", "Event candidates")), ("edge_candidates", tr("관계 후보", "Relation candidates")),
             ("existing_event_matches", tr("기존 사건 연결", "Links to existing events")), ("open_items", tr("미해결 항목", "Open items")),
             ("events_to_add", tr("추가 사건", "Events to add")), ("events_to_update", tr("수정 사건", "Events to update")),
             ("edges_to_add", tr("추가 관계", "Relations to add")), ("edges_to_invalidate", tr("무효화 관계", "Relations to invalidate")),
             ("candidate_resolutions", tr("후보 처리", "Candidate resolutions")), ("change_attributions", tr("변경 근거", "Change attributions")),
             ("review_issues", tr("검토 쟁점", "Review issues")), ("review_resolutions", tr("검토 결론", "Review resolutions")))
    return [(title, body[key]) for key, title in names if isinstance(body.get(key), list) and body[key]]


def _candidate(item: dict, records: dict[str, dict]) -> str:
    title = item.get("title") or item.get("text") or item.get("id") or item.get("candidate_id") or tr("후보", "candidate")
    fields = [f"{key}: {item[key]}" for key in ("kind", "status", "actor", "basis", "relation", "disposition")
              if key in item]
    details = item.get("summary") or item.get("reason") or item.get("rationale") or ""
    citations = "".join(_citation(c, records) for c in item.get("evidence", []) if isinstance(c, dict))
    return (f"<article><h4>{_escape(title)}</h4><p class='muted'>{_escape(' · '.join(fields))}</p>"
            f"<p>{_escape(details)}</p>{citations}</article>")


def _call_card(call: dict, capture: dict | None, records: dict[str, dict]) -> str:
    details = call.get("details", {})
    status = call.get("status", "unknown")
    header = f"{call.get('stage', '?')} #{call.get('attempt', '?')} · {status}"
    usage = details.get("usage") or {}
    read = input_tokens(usage)
    tokens = (tr(f"입력 {read:,} · 출력 {usage['output_tokens']:,} 토큰",
                 f"{read:,} input · {usage['output_tokens']:,} output tokens")
              if read is not None and isinstance(usage.get("output_tokens"), int)
              else tr("토큰 정보 없음", "no token figures"))
    error = details.get("error") or ""
    normalized = details.get("citation_normalizations", 0)
    normalizations = tr(f"자동 정규화 {normalized}건", f"{normalized} automatic normalizations")
    parts = [f"<section><h2>{_escape(header)}</h2>",
             f"<p class='muted'>{_escape(tokens)} · {_escape(normalizations)}</p>"]
    if error:
        parts.append(f"<p class='error'><b>{tr('검증 오류:', 'Validation error:')}</b> {_escape(error)}</p>")
    if capture is None:
        parts.append("<p class='notice'>" + tr(
            "이 실행은 모델 응답 원문을 저장하지 않았습니다. 아래 원문은 오류 메시지가 가리킨 줄이며, 모델이 쓴 정확한 인용문은 복원할 수 없습니다.",
            "This run did not keep the model's raw responses. The source below is the lines the error message pointed at; "
            "the exact quote the model wrote cannot be recovered.") + "</p>")
        for source_id, left, right in dict.fromkeys(SOURCE_SPAN.findall(error)):
            record = records.get(source_id)
            actual = _source_lines(record, int(left), int(right)) if record else tr("원문 없음", "no source")
            parts.append(f"<article><b>{_escape(source_id)}:{left}-{right}</b><pre>{_escape(actual)}</pre></article>")
    else:
        response = capture.get("response")
        if isinstance(response, dict):
            citation_states = [_citation_match(c, records) for c in _all_citations(response)]
            checked = sum(s in {"exact", "substring", "escape_decoded", "repeated", "diff_hunk"} for s in citation_states)
            mismatched = citation_states.count("mismatch")
            parts.append("<p><b>" + tr("인용 문자열 대조:", "Quote string check:") + "</b> "
                         + tr(f"일치 {checked}건 · 불일치 {mismatched}건 (원문 내용이 주장을 뒷받침하는지는 별도 판단)",
                              f"{checked} matched · {mismatched} mismatched (whether the source supports the claim is a separate judgement)")
                         + "</p>")
            groups = _candidate_groups(response)
            if groups:
                for title, items in groups:
                    heading = tr(f"{title} {len(items)}개", f"{title} ({len(items)})")
                    parts.append(f"<h3>{_escape(heading)}</h3>")
                    parts.extend(_candidate(item, records) for item in items if isinstance(item, dict))
            else:
                parts.append("<p>" + tr("이 호출에는 사건·관계 후보가 없습니다.", "This call carries no event or relation candidates.") + "</p>")
        else:
            parts.append("<p class='notice'>" + tr("모델 응답을 받기 전에 호출이 실패했습니다.", "The call failed before a model response arrived.") + "</p>")
        for title, value in ((tr("모델에 보낸 정확한 요청", "Exact request sent to the model"), capture.get("task")),
                             (tr("모델의 전체 구조화 응답", "Full structured model response"), response)):
            if value is not None:
                parts.append(f"<details><summary>{_escape(title)}</summary><pre>{_escape(dumps(value, pretty=True))}</pre></details>")
    parts.append("</section>")
    return "".join(parts)


def render_eval_review(output: Path) -> Path:
    if output.is_symlink() or not output.is_dir():
        raise FlowError(tr("평가 결과 디렉터리가 없습니다.", "The eval output directory does not exist."))
    output = output.expanduser().resolve()
    report = _read_json(output / "report.json")
    calls = _read_json(output / "calls.json")
    fixture = _read_json(output / "fixture.json")
    if not isinstance(report, dict) or not isinstance(calls, list) or not isinstance(fixture, dict):
        raise FlowError(tr("평가 결과 형식이 잘못되었습니다.", "The eval output format is invalid."))
    records = {r["source_id"]: r for r in fixture.get("records", []) if isinstance(r, dict) and
               isinstance(r.get("source_id"), str)}
    cards = []
    for call in calls:
        call_id = call.get("id")
        trace_path = output / "call-review" / f"{call_id}.json"
        capture = (_read_json(trace_path) if isinstance(call_id, str) and CALL_ID.fullmatch(call_id)
                   and trace_path.exists() else None)
        cards.append(_call_card(call, capture, records))
    first = report.get("first_run", {})
    status = first.get("status", "unknown")
    version = first.get("graph_version", "?")
    summary = tr(f"상태 {status} · 저장된 그래프 v{version} · 모델 호출 {len(calls)}회 · "
                 f"완료 WorkUnit {first.get('completed_units', 0)}개",
                 f"status {status} · stored graph v{version} · {len(calls)} model calls · "
                 f"{first.get('completed_units', 0)} WorkUnits completed")
    title = tr("ContextTrail 평가 검토", "ContextTrail eval review")
    page = (f"<!doctype html><html lang='{language()}'><head><meta charset='utf-8'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            "<meta name='referrer' content='no-referrer'><title>" + title + "</title>"
            "<style>body{max-width:1180px;margin:2rem auto;padding:0 1rem;background:#f4f7fb;color:#17253b;font:16px/1.55 system-ui}"
            "section,article{background:#fff;border:1px solid #ccd7e6;border-radius:12px;padding:1rem 1.25rem;margin:1rem 0}"
            "article{background:#fbfdff}h1,h2,h3,h4{line-height:1.25}h4{margin:.2rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef3f9;padding:.8rem;border-radius:6px;font:13px/1.5 ui-monospace,monospace}"
            ".columns{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.columns>*{min-width:0}.muted,small{color:#536579}"
            ".citation{border-left:4px solid #8595a9;padding:.5rem;margin:.7rem 0}.citation.good{border-color:#198267}.citation.bad,.error{border-color:#b72d47;color:#8d1930}"
            ".notice{padding:.8rem;background:#fff1cd;border-radius:8px}details{margin:.6rem 0}summary{cursor:pointer;font-weight:700}"
            "@media(max-width:760px){.columns{grid-template-columns:1fr}}</style></head><body>"
            "<h1>" + title + "</h1><p>" + _escape(summary) + "</p><p>"
            + tr("각 호출에서 모델이 제안한 후보와 인용 원문을 비교합니다. 문자열 일치는 의미적 정답을 보증하지 않습니다. "
                 "이 파일에는 비공개 대화와 코드가 들어갈 수 있으므로 로컬에서만 열어보세요.",
                 "Each call's proposed candidates are compared with the source they cite. A string match does not "
                 "guarantee semantic correctness. This file may contain private conversations and code, so open it locally only.")
            + "</p>"
            + (f"<p class='error'><b>{tr('실행 오류:', 'Run error:')}</b> {_escape(first['error'])}</p>" if first.get("error") else "")
            + "".join(cards) + "</body></html>")
    target = output / "review.html"
    if target.is_symlink():
        raise FlowError(tr("평가 검토 HTML symlink는 허용하지 않습니다.", "The eval review HTML must not be a symlink."))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(page)
    return target
