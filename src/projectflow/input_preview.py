"""Private, model-free explanation of a frozen eval fixture's planned inputs."""
from __future__ import annotations

import html
import threading
from datetime import datetime, timezone
from pathlib import Path

from .analysis import AnalysisConfig, Engine, Harness, IdAliases, _record_timestamp, build_task, extract_request_data
from .evaluation import fixture_integrity, fixture_records, load_fixture, validate_expectations
from .git_context import Scope
from .model import Snapshot, SourceRecord
from .schema import EXTRACT_SCHEMA
from .store import Store
from .util import FlowError, dumps, ident, private_dir


def _boundary(previous: SourceRecord | None, current: SourceRecord) -> str:
    if previous is None:
        return "표본 시작"
    if (previous.provider, previous.session_id, previous.worktree_id) != (
            current.provider, current.session_id, current.worktree_id):
        return "출처·세션·worktree 변경"
    if current.lineage.get("kind") == "compaction":
        return "압축 요약 직전"
    before, after = _record_timestamp(previous.recorded_at), _record_timestamp(current.recorded_at)
    if before is not None and after is not None:
        if after - before >= 3 * 3600:
            return f"대화 공백 {(after - before) / 3600:.1f}시간"
        if (after - before >= 3600 and datetime.fromtimestamp(before, timezone.utc).date()
                != datetime.fromtimestamp(after, timezone.utc).date()):
            return "UTC 날짜 변경과 1시간 이상 공백"
    return "입력 크기·기록 수 한도 또는 사용자 차례 경계"


def _panel(title: str, explanation: str, value: object) -> str:
    return (f"<details><summary>{html.escape(title)}</summary><p>{html.escape(explanation)}</p>"
            f"<pre>{html.escape(dumps(value, pretty=True))}</pre></details>")


def _page(units: list[dict], schema: dict, fixture: str, excluded: int) -> str:
    cards = []
    for unit in units:
        task, data = unit["task"], unit["task"]["data"]
        new = data["new_records"]
        related = data["context_only"]
        sections = [
            _panel("① 공통 system 지시문", "src/projectflow/prompts/common.md에서 읽습니다. Codex CLI에는 별도의 읽기 전용 system 파일로 전달합니다.", task["system"]),
            _panel("② extract 단계 지시문", "src/projectflow/prompts/extract.md에서 읽습니다. WorkUnit별 사건 후보와 근거 인용 규칙입니다.", task["instructions"]),
            _panel("③ 새 기록 new_records", "호스트가 이 WorkUnit에 배정한 원문입니다. Harness.provide가 출처 metadata와 줄 번호가 붙은 전체 내용을 만듭니다.", new),
            _panel("④ 이전 맥락 context_only", "같은 세션의 앞 기록과 시각이 가까운 과거 Git·대화 기록 중 읽기 예산 안의 원문입니다. 새 사건의 유일한 근거가 될 수 없습니다.", related),
            _panel("⑤ 기존 사건 선택 이유", "파일 경로·명시적 사건 참조·설명 단어·세션·worktree를 비교해 이번 입력에 넣을 사건을 고릅니다. 선택은 관련성 단서이지 동일 사건이나 인과관계의 증명이 아닙니다.", unit["context_selection"]),
            _panel("⑥ 기존 사건과 근거", "이전 WorkUnit이 저장한 그래프에서 고릅니다. 미리보기에서는 앞 단위를 아직 실행하지 않았으므로 후속 단위의 실제 값과 다를 수 있습니다.", {
                "existing_events": data["existing_events"], "existing_edges": data["existing_edges"],
                "existing_evidence": data["existing_evidence"], "existing_open_items": data["existing_open_items"]}),
            _panel("⑦ 추가 읽기 목록 manifest", "모델이 필요하면 ReadRequest로 요청할 수 있는 ID 목록입니다. 뒤 WorkUnit의 기록은 포함하지 않습니다.", data["manifest"]),
            _panel("⑧ 응답 규칙 wire_contract", "호스트가 단계 공통 JSON 계약을 덧붙입니다.", task["wire_contract"]),
            _panel("⑨ Codex CLI 표준 입력 JSON", "실제 Codex CLI의 stdin으로 전달되는 값입니다. system은 별도 파일이므로 여기서 제외합니다.",
                   {key: value for key, value in task.items() if key != "system"}),
            _panel("⑩ 전체 task JSON", "system 파일과 stdin JSON을 함께 표현한 호스트의 전체 요청입니다. 실제 Codex 호출 시 둘로 나뉩니다.", task),
            _panel("⑪ 짧은 ID 대응표", "모델에는 긴 원문·사건 ID 대신 S1·E1 같은 짧은 ID를 보냅니다. 응답은 검증 전에 이 표로 원래 ID로 되돌립니다.", unit["aliases"]),
        ]
        row = unit["summary"]
        parts = [f"<section><h2>WorkUnit {row['number']} · {html.escape(row['boundary_reason'])}</h2>",
                 f"<p>새 기록 {row['new_records']}개 · 원문 {row['raw_chars']:,}자 · 주변 기록 {row['context_only']}개 · "
                 f"전체 task {row['task_chars']:,}자 · stdin {row['stdin_chars']:,}자</p>"]
        if row["number"] > 1:
            parts.append("<p class='notice'>이 단위의 지시문과 새 원문은 확정입니다. 기존 사건·근거는 앞 단위의 실제 응답에 따라 달라지므로 이 task JSON은 예상 입력입니다.</p>")
        parts += sections
        parts.append("</section>")
        cards.append("".join(parts))
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'><title>ContextTrail 모델 입력 미리보기</title>"
            "<style>body{max-width:1120px;margin:2rem auto;padding:0 1rem;background:#f5f8fc;color:#15263c;font:16px/1.65 system-ui}"
            "section{background:white;padding:1.4rem;margin:1rem 0;border:1px solid #cbd7e5;border-radius:12px}"
            "details{border-top:1px solid #dce5ef;padding:.45rem 0}summary{cursor:pointer;font-weight:700}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf3f9;padding:1rem;border-radius:7px;font:13px/1.5 ui-monospace,monospace}"
            ".notice{background:#fff0ce;padding:.75rem;border-radius:6px}</style>"
            "<h1>모델 입력 미리보기</h1><p>고정 fixture: " + html.escape(fixture) +
            ". 이 문서를 만드는 동안 AI 호출이나 LangSmith 전송은 없습니다. 개인 대화와 코드 원문이 들어 있으므로 로컬에서만 열어보세요.</p>"
            "<section><h2>입력이 만들어지는 순서</h2><ol>"
            "<li>프로젝트 범위에서 선별해 고정한 fixture를 읽고 source ID·시각·역할을 검증합니다.</li>"
            "<li>호스트가 세션·worktree, 압축, 시간 공백, 예산으로 WorkUnit을 계획합니다.</li>"
            "<li>순수 실행 환경 기록은 의미 분석 대상에서 제외하고 metadata로 보존합니다. 이번 표본 제외: " + str(excluded) + "개.</li>"
            "<li>WorkUnit 원문에는 줄 번호를 붙이고, 앞선 맥락과 기존 그래프의 근거를 예산 안에서 고릅니다.</li>"
            "<li>공통 system 지시문, extract 지시문, 입력 data, 응답 규칙, 출력 JSON Schema를 조립합니다.</li>"
            "<li>실제 실행 시 Codex는 system 파일과 stdin JSON을 받고 구조화 응답을 돌려줍니다. 검증 후 통합 단계 입력이 만들어집니다.</li>"
            "</ol></section>" + "".join(cards) +
            "<section><h2>출력 JSON Schema</h2><p>src/projectflow/schema.py의 EXTRACT_SCHEMA입니다. CLI에 schema 파일로 전달되며 응답을 검증합니다.</p>"
            "<pre>" + html.escape(dumps(schema, pretty=True)) + "</pre></section></html>")


def preview_eval(fixture: str, output: Path, config: AnalysisConfig) -> dict:
    """Write an inspectable plan and task payloads without constructing any Runner."""
    data = load_fixture(fixture)
    records = fixture_records(data)
    validate_expectations(data.get("expectations"), {r.source_id for r in records})
    config.validate()
    if output.is_symlink():
        raise FlowError("미리보기 output symlink는 허용하지 않습니다.")
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FlowError("미리보기는 새 디렉터리 또는 빈 디렉터리에서만 시작합니다.")
    private_dir(output)
    snapshot = Snapshot(records)
    scope = Scope(output, None, None, "", [output], output / "state", ident("preview_", snapshot.id))
    store = Store(scope.state_dir, scope.id)
    engine = Engine(scope, store, config)
    store.ingest(records)
    excluded = store.acknowledge_environment_context(records)
    issues: list[str] = []
    plans, _, _ = engine._plan_units(snapshot, issues)
    pool = {r.source_id: r for r in records}
    units, summaries = [], []
    previous: SourceRecord | None = None
    for number, unit in enumerate(plans, 1):
        assigned = [pool[source_id] for source_id in unit["sources"]]
        h = Harness(None, pool, store.graph(), store, config, threading.Event(), unit_id=unit["id"])
        context = h.context(assigned)
        data, _ = extract_request_data(unit, snapshot.id, assigned, h, context, issues)
        aliases = IdAliases()
        task = aliases.wire(build_task("extract", data, engine.resolve_language(snapshot)))
        summary = {"number": number, "boundary_reason": _boundary(previous, assigned[0]),
                   "source_ids": unit["sources"], "new_records": len(assigned),
                   "raw_chars": sum(len(r.content) for r in assigned),
                   "context_only": len(context["context_only"]), "task_chars": len(dumps(task)),
                   "stdin_chars": len(dumps({k: v for k, v in task.items() if k != "system"})),
                   "existing_events": len(context["existing_events"]),
                   "task_file": f"unit-{number:02d}-task.json",
                   "certainty": "exact initial graph" if number == 1 else "provisional initial graph"}
        summaries.append(summary)
        units.append({"task": task, "summary": summary, "aliases": dict(aliases.full),
                      "context_selection": h.selection_audit})
        path = output / summary["task_file"]
        path.write_text(dumps(task, pretty=True), encoding="utf-8")
        path.chmod(0o600)
        previous = assigned[-1]
    integrity = fixture_integrity(records)
    for filename, payload in (("plan.json", {"units": summaries, "issues": issues,
                                              "excluded_environment_context": excluded,
                                              "fixture_integrity": integrity}),
                              ("extract-schema.json", EXTRACT_SCHEMA)):
        path = output / filename
        path.write_text(dumps(payload, pretty=True), encoding="utf-8")
        path.chmod(0o600)
    page = output / "input-preview.html"
    page.write_text(_page(units, EXTRACT_SCHEMA, fixture, excluded), encoding="utf-8")
    page.chmod(0o600)
    return {"preview": str(page), "units": summaries, "issues": issues,
            "excluded_environment_context": excluded, "fixture_integrity": integrity, "runner_calls": 0}
