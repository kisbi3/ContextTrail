from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .agent_commands import install_agent_commands
from .agent_view import find, find_text, show, show_text
from .analysis import AnalysisConfig, Engine, _evidence_ids, classify_steps, plan_choices_text, plan_text
from .demo import FixtureRunner, create_demo
from .diagram import flow_diagram
from .evaluation import call_timeline, run_eval, summarize_calls
from .eval_review import render_eval_review
from .input_preview import preview_eval
from .git_context import Scope
from . import i18n
from .i18n import tr
from .render import event_detail, export_text, terminal_graph
from .runners import CLIRunner
from .runners.cli_runner import EFFORTS
from .store import Store
from .ui import GraphApp, TerminalApp, legend, token_usage_label
from .util import FlowError, dumps, safe_text, within
from .webview import LocalViewer


def _progress_line(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def route_options(sub) -> None:
    sub.add_argument("--extract-model", help=tr("작은/빠른 추출 모델. 설치된 CLI에서 접근 가능한 식별자를 지정",
                                               "Small/fast extraction model; an identifier the installed CLI can reach"))
    sub.add_argument("--integrate-model", help=tr("흐름 통합 모델", "Flow integration model"))
    escalation = sub.add_mutually_exclusive_group()
    escalation.add_argument("--escalation-model", help=tr("구조 검증 실패 복구와 변경안 재검토에 사용할 모델",
                                                  "Model for repairing failed structural checks and reviewing deltas"))
    escalation.add_argument("--no-escalation", action="store_true", help=tr("저장된 보조 검토 모델 설정 해제", "Drop the saved escalation model"))
    for role, default in (("extract", "medium"), ("integrate", "medium"), ("escalation", "medium")):
        sub.add_argument(f"--{role}-effort", choices=EFFORTS, help=tr(f"이 단계의 추론 수준; 기본 {default}", f"Reasoning effort for this stage; default {default}"))
    sub.add_argument("--no-review", dest="semantic_review", action="store_false",
                     help=tr("이번 실행에서 변경안 재검토(누락된 검증·수정 관계 확인)를 건너뜀",
                             "Skip the delta review (missing verifies/revises relations) for this run"))
    sub.add_argument("--workers", dest="extract_workers", type=int, help=tr("동시 추출 worker 수 1~8; 기본 1", "Concurrent extraction workers, 1~8; default 1"))
    sub.add_argument("--language", dest="output_language",
                     help=tr("사건 제목·요약을 쓸 언어(예: Korean, English); 화면 언어도 같이 정함. 기본은 사용자 발화에서 "
                             "한 번 정해 저장; auto는 다시 정함",
                             "Language for event titles and summaries (e.g. Korean, English); it also sets the screen "
                             "language. Default: decided once from the person's messages and saved; auto decides again"))
    sub.add_argument("--max-calls", type=int, help=tr("이번 실행의 AI 호출 상한(저장하지 않음); 분석 기본 30(--units를 주면 단위당 6), eval 기본 20",
                             "Cap on AI calls for this run (not saved); analysis default 30 (6 per unit with --units), "
                             "eval default 20"))


def langsmith_options(sub) -> None:
    # Developer-only tracing for designing the pipeline (docs/guides/LLM_OPS.md); not a user feature.
    sub.add_argument("--langsmith", dest="langsmith_enabled", action="store_true", help=argparse.SUPPRESS)
    sub.add_argument("--langsmith-content", dest="langsmith_include_content", action="store_true",
                     help=argparse.SUPPRESS)
    sub.add_argument("--langsmith-project", help=argparse.SUPPRESS)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="project",
                                   description=tr("Codex·Claude Code 기록의 근거 기반 프로젝트 흐름",
                                                  "Evidence-linked project flow from Codex and Claude Code transcripts"),
                                   epilog=tr("명령 없이 실행하면 현재 폴더(또는 첫 인자로 준 폴더)의 저장 결과를 엽니다(view). "
                                             "AI 호출은 화면에서 R을 누를 때만 합니다.",
                                             "With no command, opens the saved result of the current folder (or of the folder "
                                             "given first) (view). AI is called only when R is pressed on the screen."))
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for command, help_text in [("analyze", tr("변경분을 분석하고 TUI 열기", "Analyze new records and open the TUI")),
                                ("view", tr("AI 호출 없이 저장 결과 열기", "Open the saved result without AI calls")),
                                ("scan", tr("AI 호출 없이 입력 범위 진단", "Diagnose the input scope without AI calls")),
                                ("serve", tr("저장 결과의 로컬 상세 뷰어", "Local detail viewer of the saved result"))]:
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("folder", nargs="?", default=".")
        sub.add_argument("--runner", choices=["codex", "claude"])
        sub.add_argument("--model", help=tr("Runner 기본 모델 대신 쓸 모델; 기본 Codex gpt-6-sol, Claude sonnet",
                                           "Model instead of the runner's default: Codex gpt-6-sol, Claude sonnet"))
        route_options(sub)
        sub.add_argument("--codex-home", type=Path)
        sub.add_argument("--claude-home", type=Path)
        sub.add_argument("--history-limit", type=int)
        sub.add_argument("--timeout", type=float, help=tr("모델 호출 하나의 제한 시간(초); 기본 600", "Time limit per model call in seconds; default 600"))
        sub.add_argument("--record-chars", type=int)
        sub.add_argument("--unit-chars", type=int)
        sub.add_argument("--context-mode", choices=["full", "lean"], help=argparse.SUPPRESS)
        sub.add_argument("--integrate-evidence", choices=["full", "reuse"], help=argparse.SUPPRESS)
        sub.add_argument("--review-output", choices=["full", "patch"], help=argparse.SUPPRESS)
        sub.add_argument("--integrate-output", choices=["full", "patch", "draft"], help=argparse.SUPPRESS)
        sub.add_argument("--no-tui", action="store_true")
        sub.add_argument("--no-mouse", action="store_true", help=tr("터미널 화면에서 마우스를 쓰지 않음(터미널의 글자 선택 사용)",
                                                                   "No mouse on the terminal screen (use the terminal's own text selection)"))
        sub.add_argument("--ascii", action="store_true")
        sub.add_argument("--no-color", action="store_true", help=tr("상태 색 없이 표시(✓ ! ✗ 표식은 유지). NO_COLOR 환경 변수도 따름",
                                                                    "Show without status colors (the ✓ ! ✗ marks stay); NO_COLOR is honored too"))
        sub.add_argument("--yes", action="store_true", help=tr("선택 범위의 클라우드 전송에 동의하고 해당 scope/Runner에 저장",
                                                               "Consent to sending the selected scope to the cloud and save that for this scope/runner"))
        sub.add_argument("--port", type=int, default=8765)
        sub.add_argument("--brief", action="store_true",
                         help=tr("--no-tui 결과를 흐름 없이 몇 줄로(에이전트용): 상태·처리 단위·남은 기록·그래프 버전",
                                 "The --no-tui result in a few lines without the flow (for agents): status, units, remaining records, graph version"))
        sub.add_argument("--units", dest="max_units", type=int,
                         help=tr("이번 실행에서 처리할 작업 단위 수(저장하지 않음). 오래된 기록부터",
                                 "Number of work units to process in this run (not saved), oldest records first"))
        sub.add_argument("--session", help=tr("이 세션과 그 하위 에이전트만 먼저 분석(순서 밖 분석으로 표시). "
                                              "current는 지금 대화 중인 Codex·Claude Code 세션",
                                              "Analyze only this session and its sub-agents first (marked out of order); "
                                              "current is the Codex/Claude Code session running now"))
        if command in {"analyze", "view", "serve"}:
            langsmith_options(sub)
    sub = commands.add_parser("eval", help=tr("별도 상태에서 고정 fixture 평가. 기본 mock, live는 --yes 필요",
                                              "Evaluate a fixed fixture in a separate state; mock by default, live needs --yes"))
    sub.add_argument("--fixture", default="demo", help=tr("demo 또는 projectflow-eval-v1 JSON 경로", "demo or the path of a projectflow-eval-v1 JSON"))
    sub.add_argument("--output", type=Path, required=True, help=tr("새/빈 평가 디렉터리", "New or empty evaluation directory"))
    sub.add_argument("--runner", choices=["mock", "codex", "claude"], default="mock")
    sub.add_argument("--model")
    sub.add_argument("--timeout", type=float, default=600,
                     help=tr("모델 호출 하나의 제한 시간(초); 기본 600", "Time limit per model call in seconds; default 600"))
    sub.add_argument("--yes", action="store_true")
    sub.add_argument("--preview", action="store_true", help=tr("AI 호출 없이 WorkUnit·실제 추출 입력 HTML 작성",
                                                             "Write the WorkUnits and the exact extract inputs as HTML without AI calls"))
    sub.add_argument("--context-mode", choices=["full", "lean"], help=argparse.SUPPRESS)
    sub.add_argument("--integrate-evidence", choices=["full", "reuse"], help=argparse.SUPPRESS)
    sub.add_argument("--review-output", choices=["full", "patch"], help=argparse.SUPPRESS)
    sub.add_argument("--integrate-output", choices=["full", "patch", "draft"], help=argparse.SUPPRESS)
    # Fewer records per unit splits a fixture so later units integrate against an existing graph.
    sub.add_argument("--unit-records", type=int, help=argparse.SUPPRESS)
    langsmith_options(sub)
    route_options(sub)
    sub = commands.add_parser("review", help=tr("저장된 eval의 모델 후보·인용·검증 결과를 HTML로 열람. AI 호출 없음",
                                                "Browse a saved eval's model candidates, quotes and checks as HTML; no AI calls"))
    sub.add_argument("folder", help=tr("eval 출력 디렉터리", "eval output directory"))
    sub = commands.add_parser("ops", help=tr("로컬 호출 ledger 요약·시간순 상세. AI 호출/외부 추적 전송 없음",
                                             "Summary and timeline of the local call ledger; no AI calls, nothing sent to external tracing"))
    sub.add_argument("folder", nargs="?", default=".")
    sub.add_argument("--run-id")
    sub.add_argument("--details", action="store_true", help=tr("원문 없이 단계별 호출 순서·상태·사용량 표시", "Show each call's stage, order, status and usage, without content"))
    sub = commands.add_parser("graph", help=tr("저장된 프로젝트 또는 eval 그래프를 터미널에서 열기. AI 호출 없음",
                                               "Open a saved project or eval graph in the terminal; no AI calls"))
    sub.add_argument("folder", nargs="?", default=".", help=tr("프로젝트 경로 또는 eval 출력 디렉터리", "Project path or eval output directory"))
    sub.add_argument("--no-mouse", action="store_true", help=tr("터미널 화면에서 마우스를 쓰지 않음(터미널의 글자 선택 사용)",
                                                               "No mouse on the terminal screen (use the terminal's own text selection)"))
    sub = commands.add_parser("find", help=tr("저장된 사건 검색(검색어 없으면 최근 사건과 열린 항목). AI 호출 없음",
                                              "Search saved events (recent events and open items without a query); no AI calls"))
    sub.add_argument("query", nargs="?", default="", help=tr("제목·설명·원문 근거에 모두 들어 있어야 할 단어들", "Words that must all appear in the title, summary or quoted evidence"))
    sub.add_argument("folder", nargs="?", default=".")
    sub.add_argument("--limit", type=int, default=20)
    sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("show", help=tr("사건 하나의 설명·연결·원문 근거. AI 호출 없음",
                                              "One event's summary, links and quoted evidence; no AI calls"))
    sub.add_argument("ref", help=tr("사건 ID 앞부분 또는 복사한 참조(contexttrail:ev_…@v12)", "Event ID prefix or a copied reference (contexttrail:ev_…@v12)"))
    sub.add_argument("folder", nargs="?", default=".")
    sub.add_argument("--quote-lines", type=int, default=40, help=tr("근거 하나에 보일 최대 줄 수; 기본 40", "Maximum lines shown per quote; default 40"))
    sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("install-commands", help=tr("Codex·Claude Code에 ContextTrail 명령 설치",
                                                          "Install the ContextTrail commands into Codex and Claude Code"))
    sub.add_argument("--force", action="store_true", help=tr("이미 설치된 ContextTrail 명령 갱신", "Refresh ContextTrail commands already installed"))
    sub = commands.add_parser("export", help=tr("저장 결과 내보내기 · AI 호출 없음", "Export the saved result · no AI calls"))
    sub.add_argument("folder", nargs="?", default=".")
    sub.add_argument("--format", choices=["md", "mmd", "json"], required=True)
    sub.add_argument("--output", type=Path, required=True)
    sub.add_argument("--force", action="store_true")
    sub = commands.add_parser("doctor", help=tr("로컬 CLI·격리 capability 진단; 기본 AI 호출 없음",
                                                "Check the local CLIs and isolation capabilities; no AI calls by default"))
    sub.add_argument("--runner", choices=["codex", "claude"])
    sub.add_argument("--smoke", action="store_true", help=tr("명시적으로 실제 CLI에 합성 JSON 분석 요청", "Explicitly send a synthetic JSON task to the real CLI"))
    sub.add_argument("--yes", action="store_true")
    sub.add_argument("--model")
    sub = commands.add_parser("demo", help=tr("합성 데이터와 Mock Runner로 로컬 동작 확인; AI 호출 없음",
                                              "Try it locally with synthetic data and a mock runner; no AI calls"))
    sub.add_argument("--path", type=Path, required=True)
    sub.add_argument("--no-tui", action="store_true")
    sub.add_argument("--no-mouse", action="store_true", help=tr("터미널 화면에서 마우스를 쓰지 않음", "No mouse on the terminal screen"))
    sub.add_argument("--ascii", action="store_true")
    return root


def plain(store: Store, *, ascii_only: bool = False) -> None:
    graph, check = store.graph(), store.get_meta("last_check", {})
    print(f"Project Flow | v{graph['version']} | {check.get('status', graph['analysis_status'])}")
    if graph.get("analysis_mode") == "synthetic_mock":
        print(tr("[합성 데이터 / Mock 분석 — 실제 AI 결과가 아닙니다]", "[Synthetic data / mock analysis — not a real AI result]"))
    print(tr("분석 기준:", "Analyzed at:"), graph.get("analyzed_at") or tr("없음", "none"), tr("| 마지막 확인:", "| Last check:"),
          check.get("at", tr("없음", "none")))
    _print_flow(graph, ascii_only=ascii_only)
    if graph["events"]:
        print(tr("사건별 설명과 원문 근거: project graph", "Per-event summary and quoted evidence: project graph"),
              tr("(대화형 터미널에서는 view 화면의 오른쪽 칸)", "(the right pane of the view screen in an interactive terminal)"))
    print()
    if "runner_calls" in check:
        print(tr(f"이번 실행의 Runner 호출: {check['runner_calls']} | 재사용 추출: {check.get('reused_extractions',0)}",
                 f"Runner calls this run: {check['runner_calls']} | Reused extractions: {check.get('reused_extractions',0)}"))
    if check.get("error"):
        print(tr("오류:", "Error:"), safe_text(check["error"]))
    for limitation in check.get("limitations", []):
        print(tr("범위/한계:", "Scope/limits:"), safe_text(limitation))
    print(token_usage_label(*store.llm_token_totals()))


def _graph(args) -> int:
    folder = Path(args.folder).expanduser().resolve()
    flow_file, report_file = folder / "flow.json", folder / "report.json"
    if flow_file.exists() or report_file.exists():
        if not flow_file.is_file() or not report_file.is_file() or flow_file.is_symlink() or report_file.is_symlink():
            raise FlowError(tr("eval 결과에는 일반 파일 flow.json과 report.json이 모두 필요합니다.",
                               "An eval result needs both flow.json and report.json as regular files."))
        if flow_file.stat().st_size > 64_000_000:
            raise FlowError(tr("eval flow.json이 64 MB를 초과합니다.", "The eval flow.json exceeds 64 MB."))
        try:
            result = json.loads(flow_file.read_text(encoding="utf-8"))
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise FlowError(tr("eval 결과 JSON을 읽을 수 없습니다.", "Cannot read the eval result JSON.")) from exc
        if not isinstance(result, dict) or not isinstance(result.get("graph"), dict) or not isinstance(result.get("evidence"), dict) or not isinstance(report, dict) or report.get("format") != "projectflow-eval-report-v1":
            raise FlowError(tr("지원하지 않는 eval 결과 형식입니다.", "Unsupported eval result format."))
        graph, evidence = result["graph"], result["evidence"]
        title = report.get("name") or folder.name
        lookup = evidence.get
    else:
        scope = Scope.resolve(folder)
        store = Store(scope.state_dir, scope.id)
        _screen_language(None, store)
        graph = store.graph()
        title = scope.folder.name
        lookup = store.evidence
    if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb":
        GraphApp(graph, lookup, title=safe_text(title, multiline=False), mouse=not args.no_mouse).run()
        return 0
    events, edges = graph["events"], [edge for edge in graph["edges"] if edge["active"]]
    print(f"ContextTrail · {safe_text(title, multiline=False)} · v{graph['version']} · "
          f"{safe_text(graph['analysis_status'], multiline=False)}")
    print(tr(f"사건 {len(events)}개 · 관계 {len(edges)}개 · 저장된 결과 · AI 호출 없음",
             f"{len(events)} events · {len(edges)} relations · saved result · no AI calls"))
    _print_flow(graph)
    if events:
        print(tr("\n사건 상세", "\nEvent details"))
        rule = "─" * 60
        for event in events:
            print(rule)
            for text, _ in event_detail(graph, event["id"], lookup, quote_lines=6):
                print(text)
        print(rule)
    return 0


def _print_flow(graph: dict, *, ascii_only: bool = False, width: int = 104) -> None:
    print(tr("표시:", "Legend:"), legend(ascii_only),
          tr(" ·  화살표 이름은 관계(검증·답변·수정·동기·후속·결과)",
             " ·  arrow names are relations (verifies, answers, revises, motivates, follows, produces)"))
    print(tr("\n흐름", "\nFlow"))
    diagram = flow_diagram(graph, width, ascii_only=ascii_only)
    lines = diagram.lines() if diagram else [line for line, _ in terminal_graph(graph, ascii_only=ascii_only, marks=True)]
    for line in lines:
        print(safe_text(line))


def _options(args, store: Store) -> AnalysisConfig:
    options = store.get_meta("options", {})
    for key in ("runner", "model", "codex_home", "claude_home", "history_limit", "timeout", "record_chars", "unit_chars",
                "extract_model", "integrate_model", "escalation_model", "extract_effort", "integrate_effort",
                "escalation_effort", "extract_workers", "output_language", "context_mode", "integrate_evidence",
                "review_output", "integrate_output"):
        value = getattr(args, key, None)
        if value is not None:
            options[key] = str(value.expanduser().resolve()) if isinstance(value, Path) else value
    if getattr(args, "no_escalation", False):
        options.pop("escalation_model", None)
    if options.get("output_language") == "auto":
        # Detected again at the next plan from the person's messages.
        options.pop("output_language")
        store.set_meta("output_language", None)
    # The call cap is for this run only; one saved by an older version is dropped, not reused.
    options.pop("max_calls", None)
    store.set_meta("options", options)
    config_keys = {field.name for field in dataclasses.fields(AnalysisConfig)}
    values = {key: value for key, value in options.items() if key in config_keys}
    for key in ("codex_home", "claude_home"):
        if key in values:
            values[key] = Path(values[key])
    if getattr(args, "max_calls", None) is not None:
        values["max_calls"], values["calls_fixed"] = args.max_calls, True
    values["max_units"] = getattr(args, "max_units", None)
    values["session"] = _session(getattr(args, "session", None))
    values["runner_name"], values["base_model"] = options.get("runner"), options.get("model")
    values["semantic_review"] = getattr(args, "semantic_review", True)
    values["langsmith_enabled"] = getattr(args, "langsmith_enabled", False)
    values["langsmith_include_content"] = getattr(args, "langsmith_include_content", False)
    values["langsmith_project"] = getattr(args, "langsmith_project", None)
    return AnalysisConfig(**values)


def _session(value: str | None) -> str | None:
    """`current` is the Codex or Claude Code session running this command, from its environment."""
    if value != "current":
        return value
    found = {name: os.environ[name] for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID") if os.environ.get(name)}
    if len(found) != 1:
        raise FlowError(tr("지금 대화 중인 세션을 알 수 없습니다", "Cannot tell which session is running this command")
                        + (tr(" (Codex와 Claude Code 세션이 모두 보입니다)", " (both a Codex and a Claude Code session are visible)")
                           if found else "")
                        + tr(". --session에 세션 ID를 지정하세요.", ". Give --session a session ID."))
    return next(iter(found.values()))


def _choose_units(plan: dict) -> bool | int:
    """Ask at a terminal how many units to run; Enter keeps the plan, n declines."""
    print(tr("선택지: ", "Choices: ") + plan_choices_text(plan), file=sys.stderr)
    answer = input(tr(f"처리할 작업 단위 수 [Enter={plan['units_this_run']}개 · n 취소]: ",
                      f"Work units to process [Enter={plan['units_this_run']} · n cancels]: ")).strip().lower()
    if not answer:
        return True
    if answer.isdigit() and int(answer) > 0:
        return int(answer)
    return False


def _brief(result: dict) -> None:
    """A run's outcome in a few lines, for an agent: no flow, limitations counted rather than listed."""
    limitations = result.get("limitations") or []
    version = result.get('graph_version', result['graph']['version'])
    print(tr(f"상태 {result['status']} · 처리한 작업 단위 {result.get('completed_units', 0)} · "
             f"AI 호출 {result.get('runner_calls', 0)} · 그래프 v{version}",
             f"Status {result['status']} · units processed {result.get('completed_units', 0)} · "
             f"AI calls {result.get('runner_calls', 0)} · graph v{version}"))
    if result.get("pending_records"):
        print(tr(f"아직 분석하지 않은 기록 {result['pending_records']:,}개 (다음 실행에서 이어서)",
                 f"{result['pending_records']:,} records not yet analyzed (the next run continues)"))
    if result.get("error"):
        print(tr("오류:", "Error:"), safe_text(result["error"]))
    if limitations:
        print(tr(f"범위·한계 {len(limitations)}건 (전체: contexttrail analyze --no-tui 또는 view)",
                 f"{len(limitations)} scope/limit notes (all of them: contexttrail analyze --no-tui or view)"))
    print(tr("새 사건 보기: contexttrail find", "See the new events: contexttrail find"))


def _find(args) -> int:
    scope = Scope.resolve(args.folder)
    store = Store(scope.state_dir, scope.id)
    _screen_language(None, store)
    result = find(store.graph(), args.query, store.evidence_many, limit=max(1, args.limit))
    print(dumps(result, pretty=True) if args.json else "\n".join(find_text(result)))
    return 0


def _show(args) -> int:
    scope = Scope.resolve(args.folder)
    store = Store(scope.state_dir, scope.id)
    _screen_language(None, store)
    graph = store.graph()
    result = show(args.ref, graph, store.evidence, store.graph, quote_lines=max(1, args.quote_lines))
    print(dumps(result, pretty=True) if args.json else "\n".join(show_text(result)))
    return 0


def _export(args, scope: Scope, store: Store) -> int:
    if args.output.is_symlink():
        raise FlowError(tr("Export symlink 대상은 허용하지 않습니다.", "An export target that is a symlink is not allowed."))
    output = args.output.expanduser().resolve()
    safe_exports = store.directory / "exports"
    if (scope.common_dir and within(output, scope.common_dir) and not within(output, safe_exports)) or (
            within(output, store.directory) and not within(output, safe_exports)):
        raise FlowError(tr("Git 메타데이터나 프로그램 상태 파일을 덮어쓸 수 없습니다.", "Cannot overwrite Git metadata or the program's state files."))
    if any(row["metadata"]["locator"].get("path") == str(output) for row in store.sources().values()):
        raise FlowError(tr("분석 원본 로그를 Export로 덮어쓸 수 없습니다.", "Cannot overwrite an analyzed source log with an export."))
    if output.exists() and not args.force:
        raise FlowError(tr("출력 파일이 이미 있습니다. 다른 경로 또는 명시적 --force가 필요합니다.",
                           "The output file already exists; use another path or an explicit --force."))
    if not output.parent.exists():
        if within(output, safe_exports):
            output.parent.mkdir(parents=True, mode=0o700)
        else:
            raise FlowError(tr("출력 상위 디렉터리가 없습니다.", "The output's parent directory does not exist."))
    graph = store.graph()
    text = export_text(graph, store.evidence_many(_evidence_ids(graph)), args.format)
    store.set_meta("exports", list(dict.fromkeys(store.get_meta("exports", []) + [str(output)])))
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | (os.O_TRUNC if args.force else os.O_EXCL)
    fd = os.open(output, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    print(tr("저장:", "Saved:"), safe_text(output),
          tr("\n민감한 대화·코드가 포함될 수 있습니다. 공유 전에 확인하세요.", "\nIt may contain sensitive conversation and code. Check before sharing."))
    return 0


def _doctor(args) -> int:
    results, failed = [], False
    for name in [args.runner] if args.runner else ["codex", "claude"]:
        runner = CLIRunner(name, model=args.model)
        report = {"runner": name, "installed": bool(shutil.which(name)),
                  "live_model_test": "not_run", "auth": "not_verified"}
        if sys.platform == "linux":
            report["bubblewrap"] = bool(shutil.which("bwrap"))
        elif sys.platform == "darwin":
            report["sandbox_exec"] = bool(shutil.which("sandbox-exec"))
        try:
            report.update(runner.preflight())
            if args.smoke:
                if not args.yes:
                    raise FlowError(tr("실제 계정 호출에는 doctor --smoke --yes가 필요합니다. 합성 문장만 전송합니다.",
                                       "A call on the real account needs doctor --smoke --yes; only a synthetic sentence is sent."))
                schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                          "required": ["status"], "additionalProperties": False}
                value = runner.run({"system": "Return only the JSON object required by the schema. Do not use tools.",
                                    "stage": "smoke", "data": "Return status ok. This is synthetic test data."},
                                   schema, threading.Event())
                if value != {"status": "ok"}:
                    raise FlowError(tr("실제 CLI smoke 출력이 계약과 다릅니다.", "The real CLI's smoke output does not match the contract."))
                report["live_model_test"] = "passed_for_this_invocation"
                report["auth"] = "model_call_succeeded"
        except FlowError as exc:
            report["error"] = str(exc)
            failed = True
        results.append(report)
    print(dumps(results, pretty=True))
    return 1 if failed else 0


def _default_command(argv: list[str]) -> list[str]:
    """No command means `view`: of the current folder, or of a folder given first.

    `view` never calls a model, so a bare `contexttrail` is safe to type anywhere.
    """
    root = parser()
    commands = next(action.choices for action in root._actions if isinstance(action, argparse._SubParsersAction))
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        return ["view", *argv]
    if argv[0] not in commands and Path(argv[0]).expanduser().is_dir():
        return ["view", *argv]
    return argv


def _language_option(argv: list[str]) -> str | None:
    """The `--language` value in argv, read before the parser exists so its help text can follow it."""
    for n, item in enumerate(argv):
        if item == "--language" and n + 1 < len(argv):
            return argv[n + 1]
        if item.startswith("--language="):
            return item.split("=", 1)[1]
    return None


def _screen_language(explicit: str | None, store: Store | None = None) -> None:
    """Set the screen language: an explicit `--language`, the environment, the project's saved language, the locale.

    `auto` asks for the output language to be detected again, so it is no choice for the screen.
    """
    explicit = None if explicit == "auto" else explicit
    i18n.set_language(i18n.resolve(explicit=explicit, saved=store.get_meta("output_language") if store else None))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    _screen_language(_language_option(argv))
    args = parser().parse_args(_default_command(argv))
    try:
        if args.command == "review":
            print(tr("평가 검토 HTML:", "Eval review HTML:"), render_eval_review(Path(args.folder)))
            return 0
        if args.command == "eval":
            keys = {field.name for field in dataclasses.fields(AnalysisConfig)}
            options = {key: value for key, value in vars(args).items() if key in keys and value is not None}
            options.setdefault("max_calls", 20)
            if options.get("output_language") == "auto":
                options.pop("output_language")
            if args.preview:
                result = preview_eval(args.fixture, args.output, AnalysisConfig(**options))
                print(dumps(result, pretty=True))
                return 0
            report = run_eval(args.fixture, args.output, args.runner, AnalysisConfig(**options),
                              yes=args.yes, model=args.model, timeout=args.timeout,
                              progress=None if args.runner == "mock" else _progress_line)
            print(dumps({"report": str(args.output / "report.json"), "mode": report["mode"],
                         "review": str(args.output / "review.html"),
                         "status": report["first_run"]["status"], "ops": report["ops"],
                         "expectations": {k:v for k,v in report["expectations"].items() if k != "checks"},
                         "style": report["style"]}, pretty=True))
            return 0 if report["first_run"]["status"] == "complete" and report["expectations"].get("failed", 0) in (0, None) else 1
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "install-commands":
            paths = install_agent_commands(Path.home(), force=args.force)
            print(tr("설치한 에이전트 명령:", "Installed agent commands:"))
            for path in paths:
                print(" ", safe_text(path))
            print("Codex: $contexttrail-update / $contexttrail-context")
            print(tr("Codex CLI·IDE 슬래시: /prompts:contexttrail-update / /prompts:contexttrail-context",
                     "Codex CLI/IDE slash: /prompts:contexttrail-update / /prompts:contexttrail-context"))
            print("Claude Code: /contexttrail-update / /contexttrail-context")
            print(tr("분석(update)은 직접 부를 때만 실행되고, 처리할 작업 단위 수를 먼저 묻습니다.",
                     "Analysis (update) runs only when called directly and asks for the number of work units first."))
            print(tr("context는 저장된 결과만 읽습니다. 다만 Claude Code에서 쓰면 인용된 Codex·Claude 기록이 "
                     "그 대화의 모델(Anthropic)로 전달됩니다.",
                     "context reads the saved result only; used from Claude Code, though, the quoted Codex/Claude "
                     "records go to that conversation's model (Anthropic)."))
            return 0
        if args.command == "graph":
            return _graph(args)
        if args.command == "find":
            return _find(args)
        if args.command == "show":
            return _show(args)
        if args.command == "demo":
            folder, codex, claude = create_demo(args.path)
            scope = Scope.resolve(folder)
            store = Store(scope.state_dir, scope.id)
            store.set_meta("demo", True)
            store.set_meta("options", {"codex_home": str(codex), "claude_home": str(claude)})
            engine = Engine(scope, store, AnalysisConfig(codex_home=codex, claude_home=claude))
            result = engine.analyze(FixtureRunner)
            if args.no_tui or not sys.stdout.isatty():
                plain(store, ascii_only=args.ascii)
            else:
                TerminalApp(store, lambda cancel, update: engine.analyze(FixtureRunner, cancel=cancel, update=update),
                            title=tr("합성 fixture / Mock", "Synthetic fixture / mock"), ascii_only=args.ascii, mouse=not args.no_mouse).run()
            return 0 if result["status"] in {"complete", "noop"} else 1
        scope = Scope.resolve(args.folder)
        store = Store(scope.state_dir, scope.id)
        _screen_language(getattr(args, "output_language", None), store)
        if args.command == "ops":
            calls = store.llm_calls(args.run_id)
            summary = summarize_calls(calls)
            print(dumps({"summary": summary, "calls": call_timeline(calls)} if args.details else summary,
                        pretty=True))
            return 0
        if args.command == "export":
            return _export(args, scope, store)
        config = _options(args, store)
        engine = Engine(scope, store, config)
        is_demo = store.get_meta("demo", False)
        def choose_runner() -> str:
            options = store.get_meta("options", {})
            name = options.get("runner")
            if not name:
                if not sys.stdin.isatty():
                    raise FlowError(tr("분석 Runner를 지정하세요: --runner codex 또는 --runner claude",
                                       "Choose the analysis runner: --runner codex or --runner claude"))
                answer = input(tr("분석 Runner [1 Codex / 2 Claude / q 취소]: ", "Analysis runner [1 Codex / 2 Claude / q cancels]: ")).strip()
                name = {"1": "codex", "2": "claude"}.get(answer)
                if not name:
                    raise FlowError(tr("Runner를 선택하지 않았습니다.", "No runner was chosen."))
                options["runner"] = name
                store.set_meta("options", options)
            return name
        def factory():
            if is_demo:
                return FixtureRunner()
            options = store.get_meta("options", {})
            name = options.get("runner")
            if not name:
                raise FlowError(tr("Runner 선택이 필요합니다. project analyze . --runner codex 또는 claude를 사용하세요.",
                                   "A runner must be chosen: use project analyze . --runner codex or claude."))
            return CLIRunner(name, model=options.get("model"), timeout=options.get("timeout", 600))
        def consent(snapshot, plan):
            if is_demo:
                return True
            name = choose_runner()
            key = "consent:" + name
            if args.yes or store.get_meta(key, False):
                store.set_meta(key, True)
                print(plan_text(plan), file=sys.stderr)
                if args.yes or not sys.stdin.isatty():
                    return True
                return _choose_units(plan)
            if not sys.stdin.isatty():
                raise FlowError(tr("클라우드 전송 동의가 필요합니다. 범위를 확인한 후 --yes를 사용하세요.",
                                   "Consent to sending to the cloud is needed; check the scope, then use --yes."))
            print(tr(f"범위: {safe_text(scope.folder)}\n연결 worktree: {len(scope.roots)} | 선택 원문: {len(snapshot.records)}개",
                     f"Scope: {safe_text(scope.folder)}\nLinked worktrees: {len(scope.roots)} | selected records: {len(snapshot.records)}"))
            print(tr(f"Codex·Claude 기록과 Git 근거 중 분석 입력이 {name} CLI의 클라우드 모델에 전송될 수 있습니다.",
                     f"Analysis input from the Codex/Claude transcripts and Git evidence may be sent to the cloud model of the {name} CLI."))
            print(plan_text(plan))
            accepted = input(tr("이 scope와 Runner의 분석에 동의합니까? [y/N]: ",
                                "Consent to analyzing this scope with this runner? [y/N]: ")).strip().lower() == "y"
            if not accepted:
                return False
            store.set_meta(key, True)
            return _choose_units(plan)
        def authorize_ui(ask):
            if is_demo:
                return True
            options = store.get_meta("options", {})
            name = options.get("runner")
            if not name:
                choice = ask(tr("분석기 선택 [c Codex / a Claude / q 취소]", "Choose the analyzer [c Codex / a Claude / q cancels]"), "caq")
                name = {"c": "codex", "a": "claude"}.get(choice)
                if not name:
                    return False
                options["runner"] = name
                store.set_meta("options", options)
            key = "consent:" + name
            if args.yes or store.get_meta(key, False):
                store.set_meta(key, True)
                return True
            if ask(tr(f"현재 범위의 기록·코드를 {name}에 전송? [y/n]", f"Send this scope's records and code to {name}? [y/n]"), "yn") == "y":
                store.set_meta(key, True)
                return True
            return False
        def analyze_callback(cancel, update, confirm=None):
            # The screen asks again once the run knows how much it would send (demo excepted).
            ask = None if is_demo or confirm is None or args.yes else (lambda snapshot, plan: confirm(plan))
            return engine.analyze(factory, cancel=cancel, update=update, consent=ask)
        if args.command == "scan":
            snapshot = engine.scan()
            try:
                plan, plan_error = engine.preview_plan(snapshot), None
            except FlowError as exc:
                plan, plan_error = None, str(exc)
            counts = {provider: sum(r.provider == provider for r in snapshot.records) for provider in ("codex", "claude", "git")}
            print(dumps({"scope": str(scope.folder), "worktrees": [str(p) for p in scope.roots],
                         "state_dir": str(scope.state_dir), "snapshot_id": snapshot.id,
                         "records": counts, "steps": classify_steps(snapshot.records),
                         "limitations": snapshot.limitations, "runner_calls": 0,
                         "plan": plan, "plan_text": plan_text(plan) if plan else plan_error,
                         "plan_choices": plan_choices_text(plan) if plan else None,
                         "codex_selection": {
                             "files_examined": sum("selection" in f for f in snapshot.files),
                             "files_with_selected_records": sum(f.get("selection", {}).get("normalized_records", 0) > 0 for f in snapshot.files),
                             "files_without_selected_records": sum(f.get("selection", {}).get("normalized_records", 0) == 0 for f in snapshot.files if "selection" in f),
                             "unattributed_records": sum(f.get("selection", {}).get("scope_decisions", {}).get("unattributed", 0) for f in snapshot.files),
                             "selection_basis": "record cwd / turn_context cwd / explicit tool workdir / verified worktree roots, not calendar folder"
                         }}, pretty=True))
            return 0
        if args.command == "serve":
            cancel = threading.Event()
            viewer = LocalViewer(store, refresh=lambda: analyze_callback(cancel, lambda _: None), port=args.port).start()
            print(tr("PC에서 포트 포워딩:", "Port forwarding from your PC:"))
            print(f"ssh -L 127.0.0.1:{viewer.port}:127.0.0.1:{viewer.port} user@server")
            print(tr("접근 주소 (외부 공유 금지):", "Access URL (do not share):"), viewer.url())
            print(tr("종료: Ctrl+C. 페이지 조회는 AI를 호출하지 않습니다.", "Quit: Ctrl+C. Viewing pages never calls AI."))
            try:
                while True:
                    time.sleep(0.2)
            except KeyboardInterrupt:
                cancel.set()
            finally:
                viewer.close()
                if viewer.refresh_thread:
                    viewer.refresh_thread.join(timeout=5)
            return 0
        use_tui = not args.no_tui and sys.stdout.isatty() and sys.stdin.isatty() and os.environ.get("TERM", "dumb") != "dumb"
        if use_tui:
            TerminalApp(store, analyze_callback, title=scope.folder.name, initial_analyze=args.command == "analyze",
                        ascii_only=args.ascii, authorize=authorize_ui, color=not args.no_color,
                        mouse=not args.no_mouse).run()
            return 0
        if args.command == "analyze":
            result = engine.analyze(factory, consent=consent, update=lambda s: print(s, file=sys.stderr))
            if args.brief:
                _brief(result)
            else:
                plain(store, ascii_only=args.ascii)
            return {"failed": 1, "partial": 2, "cancelled": 130}.get(result["status"], 0)
        plain(store, ascii_only=args.ascii)
        return 0
    except KeyboardInterrupt:
        print(tr("\n중단했습니다. 저장된 결과는 유지됩니다.", "\nInterrupted. The saved result is kept."), file=sys.stderr)
        return 130
    except (FlowError, OSError) as exc:
        print(tr("오류:", "Error:"), safe_text(str(exc)), file=sys.stderr)
        return 1
