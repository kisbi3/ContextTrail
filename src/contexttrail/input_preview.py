"""Private, model-free explanation of a frozen eval fixture's planned inputs."""
from __future__ import annotations

import html
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .analysis import AnalysisConfig, Engine, IdAliases, _record_timestamp, build_task
from .evaluation import fixture_integrity, fixture_records, load_fixture, validate_expectations
from .git_context import Scope
from .i18n import language, tr
from .light import STORY_SCHEMA
from .model import Snapshot, SourceRecord
from .store import Store
from .util import FlowError, dumps, ident, private_dir


def _boundary(previous: SourceRecord | None, current: SourceRecord) -> str:
    if previous is None:
        return tr("표본 시작", "start of the sample")
    if (previous.provider, previous.session_id, previous.worktree_id) != (
            current.provider, current.session_id, current.worktree_id):
        return tr("출처·세션·worktree 변경", "source·session·worktree change")
    if current.lineage.get("kind") == "compaction":
        return tr("압축 요약 직전", "just before a compaction summary")
    before, after = _record_timestamp(previous.recorded_at), _record_timestamp(current.recorded_at)
    if before is not None and after is not None:
        if after - before >= 3 * 3600:
            return tr(f"대화 공백 {(after - before) / 3600:.1f}시간", f"conversation gap of {(after - before) / 3600:.1f} hours")
        if (after - before >= 3600 and datetime.fromtimestamp(before, timezone.utc).date()
                != datetime.fromtimestamp(after, timezone.utc).date()):
            return tr("UTC 날짜 변경과 1시간 이상 공백", "UTC date change with a gap of an hour or more")
    return tr("입력 크기·기록 수 한도 또는 사용자 차례 경계", "input size/record count limit or a user turn boundary")


def _panel(title: str, explanation: str, value: object) -> str:
    return (f"<details><summary>{html.escape(title)}</summary><p>{html.escape(explanation)}</p>"
            f"<pre>{html.escape(dumps(value, pretty=True))}</pre></details>")


def _page(units: list[dict], schema: dict, fixture: str, excluded: int) -> str:
    cards = []
    for unit in units:
        task, data = unit["task"], unit["task"]["data"]
        sections = [
            _panel(tr("① system 지시문", "① System instructions"),
                   tr("src/contexttrail/prompts/story.md에서 읽습니다(감사에서는 audit.md를 덧붙임). Codex CLI에는 별도의 읽기 전용 system 파일로 전달합니다.",
                      "Read from src/contexttrail/prompts/story.md (audit.md appended in an audit). The Codex CLI receives it as a separate read-only system file."),
                   task["system"]),
            _panel(tr("② 사람의 요청 user_requests · open_requests", "② The person's requests user_requests · open_requests"),
                   tr("이 단위의 사람 메시지(Q1…)와, 이 단위가 이어 가는 앞 요청(E…)입니다. 모델은 요청마다 답한 곳을 requests에 적습니다.",
                      "The person's messages of this unit (Q1…) and the earlier request whose turn it continues (E…). "
                      "The model says where each was answered in requests."),
                   {"user_requests": data["user_requests"], "open_requests": data["open_requests"]}),
            _panel(tr("③ 쓴 글 written", "③ What was written: written"),
                   tr("사람과 어시스턴트가 쓴 글을 코드가 블록(B1…)으로 나눈 것입니다. 도구 출력은 보내지 않습니다. 모델은 블록 ID만 인용합니다.",
                      "What the person and the assistant wrote, cut by code into blocks (B1…). No tool output is sent; "
                      "the model cites block IDs only."),
                   data["written"]),
            _panel(tr("④ 작업 개요 work", "④ Work outline: work"),
                   tr("코드가 도구 호출에서 찾은 수정·검사·커밋(K1…)과 다른 실행(R1…)입니다. 수정 직후의 실행은 명령과 마지막 줄을 함께 보입니다.",
                      "The edits, checks and commits code found in the tool calls (K1…) and the other runs (R1…). The runs right "
                      "after an edit show their command and last lines."),
                   data["work"]),
            _panel(tr("⑤ 기존 사건 선택 이유", "⑤ Why these existing events were selected"),
                   tr("파일 경로·명시적 사건 참조·설명 단어·세션·worktree를 비교해 이번 입력에 넣을 사건을 고릅니다. 선택은 관련성 단서이지 동일 사건이나 인과관계의 증명이 아닙니다.",
                      "Events for this input are chosen by comparing file paths, explicit event references, descriptive words, session and worktree. "
                      "The selection is a relevance hint, not proof of the same event or of causality."),
                   unit["context_selection"]),
            _panel(tr("⑥ 기존 사건과 관계", "⑥ Existing events and relations"),
                   tr("이전 WorkUnit이 저장한 그래프에서 고릅니다. 미리보기에서는 앞 단위를 아직 실행하지 않았으므로 후속 단위의 실제 값과 다를 수 있습니다.",
                      "Chosen from the graph stored by earlier WorkUnits. The preview has not run the earlier units, so later units may differ from the actual values."),
                   {"existing_events": data["existing_events"], "existing_edges": data["existing_edges"]}),
            _panel(tr("⑦ 응답 규칙 wire_contract", "⑦ Response rules wire_contract"),
                   tr("호스트가 JSON 계약을 덧붙입니다.", "The host appends the JSON contract."),
                   task["wire_contract"]),
            _panel(tr("⑧ Codex CLI 표준 입력 JSON", "⑧ Codex CLI standard input JSON"),
                   tr("실제 Codex CLI의 stdin으로 전달되는 값입니다. system은 별도 파일이므로 여기서 제외합니다.",
                      "The value passed to the Codex CLI's stdin. system is a separate file, so it is left out here."),
                   {key: value for key, value in task.items() if key != "system"}),
            _panel(tr("⑨ 전체 task JSON", "⑨ Full task JSON"),
                   tr("system 파일과 stdin JSON을 함께 표현한 호스트의 전체 요청입니다. 실제 Codex 호출 시 둘로 나뉩니다.",
                      "The host's full request, with the system file and the stdin JSON together. A real Codex call splits it in two."),
                   task),
            _panel(tr("⑩ 짧은 ID 대응표", "⑩ Short ID table"),
                   tr("모델에는 긴 사건 ID 대신 E1 같은 짧은 ID를 보냅니다. 응답은 검증 전에 이 표로 원래 ID로 되돌립니다.",
                      "The model receives short IDs such as E1 instead of the long event IDs. "
                      "The response is mapped back to the original IDs with this table before validation."),
                   unit["aliases"]),
        ]
        row = unit["summary"]
        counts = tr(f"새 기록 {row['new_records']}개 · 원문 {row['raw_chars']:,}자 · 글 블록 {row['blocks']}개 · 작업 항목 {row['work_items']}개 · "
                    f"전체 task {row['task_chars']:,}자 · stdin {row['stdin_chars']:,}자",
                    f"{row['new_records']} new records · {row['raw_chars']:,} source chars · {row['blocks']} written blocks · "
                    f"{row['work_items']} work items · {row['task_chars']:,} task chars · {row['stdin_chars']:,} stdin chars")
        parts = [f"<section><h2>WorkUnit {row['number']} · {html.escape(row['boundary_reason'])}</h2>",
                 f"<p>{html.escape(counts)}</p>"]
        if row["number"] > 1:
            parts.append("<p class='notice'>" + html.escape(tr(
                "이 단위의 지시문과 새 원문은 확정입니다. 기존 사건·근거는 앞 단위의 실제 응답에 따라 달라지므로 이 task JSON은 예상 입력입니다.",
                "This unit's instructions and new source are final. Its existing events and evidence depend on the earlier units' "
                "actual responses, so this task JSON is the expected input.")) + "</p>")
        parts += sections
        parts.append("</section>")
        cards.append("".join(parts))
    title = tr("ContextTrail 모델 입력 미리보기", "ContextTrail model input preview")
    steps = [tr("프로젝트 범위에서 선별해 고정한 fixture를 읽고 source ID·시각·역할을 검증합니다.",
                "Read the fixture frozen from the project scope and validate its source IDs, timestamps and roles."),
             tr("호스트가 세션·worktree, 압축, 시간 공백, 예산으로 WorkUnit을 계획합니다.",
                "The host plans WorkUnits by session/worktree, compaction, time gaps and budget."),
             tr(f"순수 실행 환경 기록은 의미 분석 대상에서 제외하고 metadata로 보존합니다. 이번 표본 제외: {excluded}개.",
                f"Pure execution-environment records are excluded from semantic analysis and kept as metadata. Excluded in this sample: {excluded}."),
             tr("코드가 도구 호출에서 작업(수정·검사·커밋·실행)을 찾고, 사람과 어시스턴트가 쓴 글을 블록으로 나누며, 기존 그래프에서 관련 사건을 고릅니다.",
                "Code finds the work (edits, checks, commits, runs) in the tool calls, cuts what the person and the assistant wrote into blocks, "
                "and picks related events from the existing graph."),
             tr("system 지시문, 입력 data, 응답 규칙, 출력 JSON Schema를 조립합니다. 단위마다 모델 호출은 하나입니다.",
                "The system instructions, the input data, the response rules and the output JSON Schema are assembled; one model call per unit."),
             tr("실제 실행 시 Codex는 system 파일과 stdin JSON을 받고 구조화 응답을 돌려줍니다. 코드가 그 답을 검사하고, 블록을 원문 줄로 바꿔 그래프에 넣습니다.",
                "In a real run Codex receives the system file and the stdin JSON and returns a structured response. Code checks the answer, "
                "turns the blocks into source lines and adds them to the graph.")]
    return (f"<!doctype html><html lang='{language()}'><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
            "<style>body{max-width:1120px;margin:2rem auto;padding:0 1rem;background:#f5f8fc;color:#15263c;font:16px/1.65 system-ui}"
            "section{background:white;padding:1.4rem;margin:1rem 0;border:1px solid #cbd7e5;border-radius:12px}"
            "details{border-top:1px solid #dce5ef;padding:.45rem 0}summary{cursor:pointer;font-weight:700}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf3f9;padding:1rem;border-radius:7px;font:13px/1.5 ui-monospace,monospace}"
            ".notice{background:#fff0ce;padding:.75rem;border-radius:6px}</style>"
            "<h1>" + html.escape(tr("모델 입력 미리보기", "Model input preview")) + "</h1><p>"
            + html.escape(tr(f"고정 fixture: {fixture}. 이 문서를 만드는 동안 AI 호출이나 LangSmith 전송은 없습니다. "
                             "개인 대화와 코드 원문이 들어 있으므로 로컬에서만 열어보세요.",
                             f"Frozen fixture: {fixture}. No AI call or LangSmith upload happens while this document is built. "
                             "It contains private conversations and source code, so open it locally only.")) + "</p>"
            "<section><h2>" + html.escape(tr("입력이 만들어지는 순서", "How the input is built")) + "</h2><ol>"
            + "".join(f"<li>{html.escape(step)}</li>" for step in steps) +
            "</ol></section>" + "".join(cards) +
            "<section><h2>" + html.escape(tr("출력 JSON Schema", "Output JSON Schema")) + "</h2><p>"
            + html.escape(tr("src/contexttrail/light.py의 STORY_SCHEMA입니다. CLI에 schema 파일로 전달되며 응답을 검증합니다.",
                             "STORY_SCHEMA from src/contexttrail/light.py. It is passed to the CLI as the schema file and validates the response.")) + "</p>"
            "<pre>" + html.escape(dumps(schema, pretty=True)) + "</pre></section></html>")


def preview_eval(fixture: str, output: Path, config: AnalysisConfig) -> dict:
    """Write an inspectable plan and task payloads without constructing any Runner."""
    data = load_fixture(fixture)
    records = fixture_records(data)
    validate_expectations(data.get("expectations"), {r.source_id for r in records})
    config.validate()
    if output.is_symlink():
        raise FlowError(tr("미리보기 output symlink는 허용하지 않습니다.", "The preview output must not be a symlink."))
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FlowError(tr("미리보기는 새 디렉터리 또는 빈 디렉터리에서만 시작합니다.", "A preview starts only in a new or empty directory."))
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
    language_name = engine.resolve_language(snapshot)
    for number, unit in enumerate(plans, 1):
        assigned = [pool[source_id] for source_id in unit["sources"]]
        prepared = engine._prepare_extraction(unit, pool, store.graph(), snapshot.id, "run_preview",
                                              SimpleNamespace(budget=None), threading.Event(), issues)
        request, parts = engine.story_input(prepared, unit, pool, snapshot.id)
        aliases = IdAliases()
        task = aliases.wire(build_task("story", request, language_name))
        summary = {"number": number, "boundary_reason": _boundary(previous, assigned[0]),
                   "source_ids": unit["sources"], "new_records": len(assigned),
                   "raw_chars": sum(len(r.content) for r in assigned),
                   "blocks": len(parts["blocks"]), "work_items": len(request["work"]),
                   "task_chars": len(dumps(task)),
                   "stdin_chars": len(dumps({k: v for k, v in task.items() if k != "system"})),
                   "existing_events": len(request["existing_events"]),
                   "task_file": f"unit-{number:02d}-task.json",
                   "certainty": "exact initial graph" if number == 1 else "provisional initial graph"}
        summaries.append(summary)
        units.append({"task": task, "summary": summary, "aliases": dict(aliases.full),
                      "context_selection": prepared.harness.selection_audit})
        path = output / summary["task_file"]
        path.write_text(dumps(task, pretty=True), encoding="utf-8")
        path.chmod(0o600)
        previous = assigned[-1]
    integrity = fixture_integrity(records)
    for filename, payload in (("plan.json", {"units": summaries, "issues": issues,
                                              "excluded_environment_context": excluded,
                                              "fixture_integrity": integrity}),
                              ("story-schema.json", STORY_SCHEMA)):
        path = output / filename
        path.write_text(dumps(payload, pretty=True), encoding="utf-8")
        path.chmod(0o600)
    page = output / "input-preview.html"
    page.write_text(_page(units, STORY_SCHEMA, fixture, excluded), encoding="utf-8")
    page.chmod(0o600)
    return {"preview": str(page), "units": summaries, "issues": issues,
            "excluded_environment_context": excluded, "fixture_integrity": integrity, "runner_calls": 0}
