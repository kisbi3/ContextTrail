"""Studio execution graph for synthetic walkthroughs and bounded live Codex runs.

Both modes use the production scan, planning, extraction, integration and store
methods. A live scope is fixed by the server operator, never by Studio input.
"""
from __future__ import annotations

import copy
import contextvars
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict
from concurrent.futures import ThreadPoolExecutor

from langgraph.graph import END, START, StateGraph
from langsmith import traceable, tracing_context

from contexttrail.analysis import (AnalysisConfig, Engine, IdAliases, PreparedExtraction,
                                  PreparedIntegration, _incomplete_input, _rehydrate,
                                  build_task, review_signal_items, REVIEW_SIGNALS)
from contexttrail.analysis import link_request_turns as analysis_link_request_turns
from contexttrail.analysis import calibration, call_cap, plan_summary
from contexttrail.analysis import classify_steps as classify_records
from contexttrail.demo import FixtureRunner, create_demo
from contexttrail.evaluation import fixture_records, load_fixture
from contexttrail.git_context import Scope
from contexttrail.i18n import tr
from contexttrail.model import Snapshot
from contexttrail.render import graph_summary
from contexttrail.runners.cli_runner import CLIRunner
from contexttrail.routing import RunnerPool
from contexttrail.schema import EXTRACT_SCHEMA, EvidenceValidator, delta_schema
from contexttrail.store import Store
from contexttrail.util import Cancelled, FlowError, ident, private_dir


ROOT = Path(tempfile.gettempdir()) / "contexttrail-studio-fixtures"


@traceable(name="Synthetic model response (Mock, no LLM)", run_type="chain")
def _mock_model_response(task: dict, schema: dict) -> dict:
    return FixtureRunner().run(task, schema, threading.Event())


class StudioFixtureRunner(FixtureRunner):
    def run(self, task: dict, schema: dict, cancel: threading.Event) -> dict:
        return _mock_model_response(task, schema)


class StudioCodexRunner(CLIRunner):
    def __init__(self):
        super().__init__("codex")

    def run(self, task: dict, schema: dict, cancel: threading.Event) -> dict:
        # A child of the running Studio node, with the exact request and response.
        @traceable(name="Codex structured response", run_type="llm")
        def call(request: dict, response_schema: dict) -> dict:
            return super(StudioCodexRunner, self).run(request, response_schema, cancel)

        return call(task, schema)


@dataclass
class _LiveSession:
    engine: Engine
    store: Store
    snapshot: Snapshot
    runners: RunnerPool
    lock: object


_live_sessions: dict[str, _LiveSession] = {}
_live_sessions_lock = threading.Lock()


@dataclass
class _CliInvocation:
    engine: Engine
    runners: RunnerPool
    cancel: threading.Event
    update: Callable[[str], None]
    consent: Callable[[Snapshot, dict], bool] | None
    snapshot: Snapshot | None = None
    completed: int = 0
    reused: int = 0
    issues: list[str] | None = None
    batch_outcomes: dict[int, dict | BaseException] | None = None
    # One worker extracts the next unit while this one is integrated (extract_workers == 1).
    prefetch: tuple[int, Any] | None = None
    prefetch_cancel: threading.Event | None = None
    prefetch_pool: ThreadPoolExecutor | None = None

    def stop_prefetch(self) -> None:
        """Stop and wait for a next-unit extraction still running when the run ends."""
        if self.prefetch_cancel is not None:
            self.prefetch_cancel.set()
        if self.prefetch is not None:
            try:
                self.prefetch[1].result()
            except BaseException:
                pass
            self.prefetch = None
        if self.prefetch_pool is not None:
            self.prefetch_pool.shutdown(wait=True)
            self.prefetch_pool = None


_cli_invocations: dict[str, _CliInvocation] = {}
_cli_invocations_lock = threading.Lock()
_prepared_extractions: dict[tuple[str, int], PreparedExtraction] = {}
_prepared_integrations: dict[tuple[str, int], PreparedIntegration] = {}
_synthetic_runners: dict[str, RunnerPool] = {}
_prepared_lock = threading.Lock()


def _clear_prepared(run_id: str) -> None:
    with _prepared_lock:
        for items in (_prepared_extractions, _prepared_integrations):
            for key in [key for key in items if key[0] == run_id]:
                items.pop(key, None)
        _synthetic_runners.pop(run_id, None)


def _cli_session(state: StudioState) -> _CliInvocation:
    with _cli_invocations_lock:
        session = _cli_invocations.get(state["run_id"])
    if session is None:
        raise FlowError(tr("CLI 분석 실행 상태가 사라졌습니다.", "The CLI analysis run state is gone."))
    return session


class StudioState(TypedDict, total=False):
    mode: Literal["synthetic", "live", "eval", "cli"]
    confirm_live: bool
    max_units: int
    max_calls: int
    scope_folder: str
    eval_fixture_path: str
    output_dir: str
    fixture_dir: str
    run_id: str
    snapshot_id: str
    selected_records: int
    limitations: list[str]
    step_classes: dict
    planned_units: list[dict]
    total_planned_units: int
    records_waiting: int
    missing_sources: int
    unit_index: int
    unit_id: str
    source_ids: list[str]
    provider: str
    extract_input: dict
    extracted: dict
    candidate_audit: dict
    candidate_count: int
    validation_repair_reasons: list[str]
    validation_repair_pending: bool
    validation_repair_executed: bool
    current_calls: list[dict]
    integrate_input: dict
    model_delta: dict | None
    prepared_integration: PreparedIntegration
    semantic_review_pending: bool
    semantic_review_executed: bool
    semantic_review_issues: list[dict]
    new_graph: dict
    graph_change_audit: dict
    evidence: dict
    dependencies: dict
    completed_units: int
    reused_extractions: int
    pending_records: int
    unit_results: list[dict]
    graph_summary: dict
    result: dict


def _context(state: StudioState) -> tuple[Engine, Store, Snapshot]:
    if state.get("mode") == "cli":
        session = _cli_session(state)
        if session.snapshot is None:
            raise FlowError(tr("CLI 입력 snapshot이 아직 준비되지 않았습니다.", "The CLI input snapshot is not ready yet."))
        return session.engine, session.engine.store, session.snapshot
    if state.get("mode") in {"live", "eval"}:
        with _live_sessions_lock:
            session = _live_sessions.get(state["run_id"])
        if session is None:
            raise FlowError(tr("실제 Studio 실행 상태가 사라졌습니다. 새 실행을 시작하세요.",
                               "The live Studio run state is gone. Start a new run."))
        return session.engine, session.store, session.snapshot
    private_dir(ROOT)
    directory = Path(state["fixture_dir"]).resolve()
    if not directory.is_relative_to(ROOT.resolve()) or not (directory / "DEMO_ONLY.txt").is_file():
        raise FlowError(tr("Studio는 자체 합성 fixture만 읽습니다.", "Studio reads only its own synthetic fixtures."))
    scope = Scope.resolve(directory / "sample-project")
    store = Store(scope.state_dir, scope.id)
    config = AnalysisConfig(codex_home=directory / "fixture-codex",
                            claude_home=directory / "fixture-claude",
                            escalation_model="fixture-review")
    engine = Engine(scope, store, config)
    engine.detailed_trace = True
    return engine, store, engine.scan()


def _runners(state: StudioState, engine: Engine) -> RunnerPool:
    if state.get("mode") == "cli":
        return _cli_session(state).runners
    if state.get("mode") in {"live", "eval"}:
        with _live_sessions_lock:
            return _live_sessions[state["run_id"]].runners
    with _prepared_lock:
        return _synthetic_runners.setdefault(
            state["run_id"], RunnerPool(StudioFixtureRunner, engine.config))


def _release_live(run_id: str) -> None:
    with _live_sessions_lock:
        session = _live_sessions.pop(run_id, None)
    if session is not None:
        session.lock.__exit__(None, None, None)


def prepare_run(state: StudioState) -> StudioState:
    mode = state.get("mode", "synthetic")
    if mode == "cli":
        session = _cli_session(state)
        session.engine.store.mark_interrupted_runs()
        session.engine.store.start_run(state["run_id"], {"scope_id": session.engine.scope.id})
        return {"unit_index": 0, "completed_units": 0, "reused_extractions": 0,
                "unit_results": []}
    if mode not in {"synthetic", "live", "eval"}:
        raise FlowError(tr("Studio mode는 synthetic, live 또는 eval이어야 합니다.",
                           "Studio mode must be synthetic, live or eval."))
    if mode in {"live", "eval"}:
        if state.get("confirm_live") is not True:
            raise FlowError(tr("실제 Studio 실행에는 confirm_live=true가 필요합니다.",
                               "A live Studio run requires confirm_live=true."))
        units, calls = state.get("max_units", 1), state.get("max_calls", 10)
        if type(units) is not int or not 1 <= units <= 10 or type(calls) is not int or not 1 <= calls <= 50:
            raise FlowError(tr("Studio 실제 실행은 max_units=1~10, max_calls=1~50만 허용합니다.",
                               "A live Studio run allows only max_units=1~10 and max_calls=1~50."))
        common = {"mode": mode, "max_units": units, "max_calls": calls,
                  "unit_index": 0, "completed_units": 0, "unit_results": []}
        if mode == "eval":
            raw_fixture = os.environ.get("CONTEXTTRAIL_STUDIO_EVAL_FIXTURE")
            if not raw_fixture or not Path(raw_fixture).is_absolute():
                raise FlowError(tr("서버 환경변수 CONTEXTTRAIL_STUDIO_EVAL_FIXTURE에 절대 fixture 경로를 고정하세요.",
                                   "Pin an absolute fixture path in the server environment variable CONTEXTTRAIL_STUDIO_EVAL_FIXTURE."))
            return {**common, "eval_fixture_path": str(Path(raw_fixture).resolve(strict=True))}
        raw_scope = os.environ.get("CONTEXTTRAIL_STUDIO_SCOPE")
        if not raw_scope or not Path(raw_scope).is_absolute():
            raise FlowError(tr("서버 환경변수 CONTEXTTRAIL_STUDIO_SCOPE에 절대 프로젝트 경로를 고정하세요.",
                               "Pin an absolute project path in the server environment variable CONTEXTTRAIL_STUDIO_SCOPE."))
        folder = Path(raw_scope).resolve(strict=True)
        if not folder.is_dir():
            raise FlowError(tr("Studio의 실제 프로젝트 경로가 디렉터리가 아닙니다.", "Studio's live project path is not a directory."))
        return {**common, "scope_folder": str(folder)}
    private_dir(ROOT)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=ROOT))
    create_demo(directory)
    return {"mode": "synthetic", "fixture_dir": str(directory), "unit_index": 0,
            "completed_units": 0, "unit_results": []}


def scan_sources(state: StudioState) -> StudioState:
    if state.get("mode") == "cli":
        session = _cli_session(state)
        session.update(tr("입력 변화 확인 중", "Checking for input changes"))
        snapshot = session.engine.scan()
        session.snapshot = snapshot
        session.engine.store.ingest(snapshot.records)
        session.engine.store.acknowledge_environment_context(snapshot.records)
        return {"snapshot_id": snapshot.id, "selected_records": len(snapshot.records),
                "limitations": snapshot.limitations}
    if state.get("mode") in {"live", "eval"}:
        if state["mode"] == "live":
            fixed_scope = os.environ.get("CONTEXTTRAIL_STUDIO_SCOPE")
            if not fixed_scope or Path(fixed_scope).resolve() != Path(state["scope_folder"]).resolve():
                raise FlowError(tr("Studio 프로젝트 경로가 서버에 고정된 범위와 다릅니다.",
                                   "The Studio project path differs from the scope pinned on the server."))
            scope = Scope.resolve(Path(state["scope_folder"]))
            frozen_snapshot = None
        else:
            fixed_fixture = os.environ.get("CONTEXTTRAIL_STUDIO_EVAL_FIXTURE")
            if not fixed_fixture or Path(fixed_fixture).resolve() != Path(state["eval_fixture_path"]).resolve():
                raise FlowError(tr("Studio 평가 fixture가 서버에 고정된 파일과 다릅니다.",
                                   "The Studio eval fixture differs from the file pinned on the server."))
            frozen_snapshot = Snapshot(fixture_records(load_fixture(state["eval_fixture_path"])))
            private_dir(ROOT)
            output = Path(tempfile.mkdtemp(prefix="eval-", dir=ROOT))
            scope = Scope(output, None, None, "", [output], output / "state",
                          ident("eval_", frozen_snapshot.id))
        store = Store(scope.state_dir, scope.id)
        lock = store.analyze_lock()
        lock.__enter__()
        try:
            store.mark_interrupted_runs()
            config = AnalysisConfig(
                codex_home=Path(os.environ["CONTEXTTRAIL_STUDIO_CODEX_HOME"]) if os.environ.get("CONTEXTTRAIL_STUDIO_CODEX_HOME") else None,
                claude_home=Path(os.environ["CONTEXTTRAIL_STUDIO_CLAUDE_HOME"]) if os.environ.get("CONTEXTTRAIL_STUDIO_CLAUDE_HOME") else None,
                runner_name="codex", max_calls=state["max_calls"], extract_workers=1)
            engine = Engine(scope, store, config)
            engine.detailed_trace = True
            if frozen_snapshot is not None:
                engine.scan = lambda: frozen_snapshot
            snapshot = engine.scan()
            store.ingest(snapshot.records)
            store.acknowledge_environment_context(snapshot.records)
            run_id = "run_" + uuid.uuid4().hex
            store.start_run(run_id, {"scope_id": scope.id, "mode": "studio_" + state["mode"] + "_codex"})
            with _live_sessions_lock:
                _live_sessions[run_id] = _LiveSession(
                    engine, store, snapshot, RunnerPool(StudioCodexRunner, config), lock)
        except BaseException:
            lock.__exit__(None, None, None)
            raise
        return {"run_id": run_id, "snapshot_id": snapshot.id, "output_dir": str(scope.folder),
                "selected_records": len(snapshot.records), "limitations": snapshot.limitations}
    engine, store, snapshot = _context(state)
    store.ingest(snapshot.records)
    run_id = "run_" + uuid.uuid4().hex
    store.start_run(run_id, {"scope_id": engine.scope.id, "mode": "studio_synthetic"})
    return {"run_id": run_id, "snapshot_id": snapshot.id,
            "selected_records": len(snapshot.records), "limitations": snapshot.limitations}


def classify_steps(state: StudioState) -> StudioState:
    """Code-only pass over the tool calls (edit/read/commit/test/vcs/run) ahead of planning.

    The cut rules and the extract input read the same hints; this makes them visible per run."""
    _, _, snapshot = _context(state)
    return {"step_classes": classify_records(snapshot.records)}


def plan_work_units(state: StudioState) -> StudioState:
    engine, _, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("Studio fixture snapshot이 변경됐습니다.", "The Studio fixture snapshot has changed."))
    issues = list(state["limitations"])
    language = engine.resolve_language(snapshot)
    units, pending, missing = engine._plan_units(snapshot, issues, repair=True)
    if state.get("mode") == "cli":
        session = _cli_session(state)
        session.issues = issues
        pool = {r.source_id: r for r in snapshot.records}
        limit = engine.config.max_units
        past = calibration(engine.store.units(), engine.store.llm_calls(), pool)
        plan = {**plan_summary(units, pool, call_cap(engine.config, limit), limit=limit, past=past),
                "output_language": language}
        answer = session.consent(snapshot, plan) if units and session.consent else True
        if not answer:
            raise Cancelled(tr("외부 전송에 동의하지 않아 분석하지 않았습니다.",
                            "Not analyzed: consent to send data externally was not given."))
        # A consent may answer with a unit count (the screen's choice); True keeps the plan as it is.
        if answer is not True:
            limit = int(answer)
        if limit:
            units = units[:limit]
            session.runners.budget.maximum = call_cap(engine.config, limit)
    total = len(units)
    if state.get("mode") in {"live", "eval"}:
        units = units[:state["max_units"]]
    pool = {r.source_id: r for r in snapshot.records}
    chosen = {i for unit in units for i in unit["sources"]}
    return {"planned_units": [{**unit, "provider": pool[unit["sources"][0]].provider}
                              for unit in units], "total_planned_units": total,
            "records_waiting": len(pending - chosen),
            "missing_sources": len(missing), "pending_records": len(pending),
            "limitations": issues}


def initial_route(state: StudioState) -> Literal["select_unit", "finish_run"]:
    return "select_unit" if state["planned_units"] else "finish_run"


def select_unit(state: StudioState) -> StudioState:
    unit = state["planned_units"][state["unit_index"]]
    return {"unit_id": unit["id"], "source_ids": unit["sources"],
            "provider": unit["provider"]}


def prepare_extract_input(state: StudioState) -> StudioState:
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("추출 입력의 snapshot이 변경됐습니다.", "The extract input's snapshot has changed."))
    unit = state["planned_units"][state["unit_index"]]
    if state.get("mode") == "cli" and engine.config.extract_workers > 1:
        return {"extract_input": {"unit_id": unit["id"], "mode": "parallel_batch",
                                  "note": tr("배치의 정확한 요청은 각 worker의 Build extract request trace에서 확인합니다.",
                                             "The exact request of a batch is in each worker's Build extract request trace.")}}
    pool = {r.source_id: r for r in snapshot.records}
    cancel = _cli_session(state).cancel if state.get("mode") == "cli" else threading.Event()
    prepared = engine._prepare_extraction(unit, pool, store.graph(), snapshot.id,
                                          state["run_id"], _runners(state, engine),
                                          cancel, state["limitations"])
    with _prepared_lock:
        _prepared_extractions[(state["run_id"], state["unit_index"])] = prepared
    cached = unit.get("result") or {}
    reused = unit["status"] in {"extracted", "draft"} and (
        cached.get("routing_signature") == engine._routing_signature())
    data = prepared.data
    return {"extract_input": {
        "unit_id": unit["id"], "model_call_expected": not reused,
        "context_selection": copy.deepcopy(prepared.harness.selection_audit),
        "new_records": [{"source_id": item["source_id"], "provider": item["provider"],
                         "role": item["role"], "line_count": len(item["lines"])}
                        for item in data["new_records"]],
        "context_only_source_ids": [item["source_id"] for item in data["context_only"]],
        "existing_event_ids": [item["id"] for item in data["existing_events"]],
        # Exactly what the Runner receives on the first attempt, short IDs included.
        "request": None if reused else IdAliases().wire(build_task("extract", data, engine.config.output_language)),
        "response_schema": None if reused else EXTRACT_SCHEMA,
        "reuse_reason": tr("저장된 추출을 먼저 검증합니다. 근거가 바뀌었으면 새 요청을 만듭니다.",
                           "The saved extraction is checked first; a new request is built if the evidence has changed.")
                        if reused else None}}


def _current_calls(store: Store, state: StudioState) -> list[dict]:
    return [{"stage": call["stage"], "status": call["status"],
             "attempt": call["attempt"],
             "routing_role": call["metadata"].get("routing_role"),
             "read_rounds_used": call["details"].get("read_rounds_used", 0),
             "repair_rounds_used": call["details"].get("repair_rounds_used", 0)}
            for call in store.llm_calls(state["run_id"])
            if call["unit_id"] == state["unit_id"]]


# A prefetch is only started while the call budget still has room for this unit's integration
# and review and the next unit's extraction, so it never costs the current unit its calls.
PREFETCH_MIN_CALLS = 8


def _prefetch_next(state: StudioState, engine: Engine, store: Store, snapshot_id: str,
                   pool: dict) -> None:
    """Start extracting the next planned unit in the background, against the graph as it is now.

    Like a batch of workers, the next unit is extracted before this one is published; the
    integration that follows sees the published graph. Runs only with one extract worker.
    """
    session = _cli_session(state)
    following = state["unit_index"] + 1
    budget = session.runners.budget
    if (engine.config.extract_workers != 1 or following >= len(state["planned_units"])
            or session.cancel.is_set() or session.prefetch is not None
            or budget.maximum - budget.started < PREFETCH_MIN_CALLS or not session.runners.parallel_safe()):
        return
    if session.prefetch_pool is None:
        session.prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pf-prefetch")
        session.prefetch_cancel = threading.Event()
    base = copy.deepcopy(store.graph())
    unit = state["planned_units"][following]
    future = session.prefetch_pool.submit(
        contextvars.copy_context().run, engine._extract_unit, unit, pool, base, snapshot_id,
        state["run_id"], session.runners, session.prefetch_cancel, state["limitations"],
        defer_escalation=True)
    session.prefetch = (following, future)


def extract_model_and_validate(state: StudioState) -> StudioState:
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("Studio fixture snapshot이 변경됐습니다.", "The Studio fixture snapshot has changed."))
    pool = {r.source_id: r for r in snapshot.records}
    unit = state["planned_units"][state["unit_index"]]
    with _prepared_lock:
        prepared = _prepared_extractions.pop((state["run_id"], state["unit_index"]), None)
    if state.get("mode") == "cli":
        session = _cli_session(state)
        if session.prefetch is not None and session.prefetch[0] == state["unit_index"]:
            future = session.prefetch[1]
            session.prefetch = None
            session.update(tr(f"구간 추출 결과 대기 · {state['unit_index'] + 1}/{len(state['planned_units'])}",
                              f"Waiting for unit extraction · {state['unit_index'] + 1}/{len(state['planned_units'])}"))
            try:
                session.batch_outcomes = {state["unit_index"]: future.result()}
            except Exception as exc:
                session.batch_outcomes = {state["unit_index"]: exc}
        if session.batch_outcomes is None or state["unit_index"] not in session.batch_outcomes:
            width = engine.config.extract_workers
            offset = state["unit_index"]
            batch = state["planned_units"][offset:offset + width]
            base = copy.deepcopy(store.graph())
            session.update(tr(f"구간 추출 중 · {offset + 1}~{offset + len(batch)}/{len(state['planned_units'])} · workers={width}",
                              f"Extracting units · {offset + 1}~{offset + len(batch)}/{len(state['planned_units'])} · workers={width}"))
            if width == 1:
                outcomes: list[dict | BaseException] = [engine._extract_unit(
                    batch[0], pool, base, snapshot.id, state["run_id"], session.runners,
                    session.cancel, state["limitations"], defer_escalation=True,
                    prepared=prepared)]
            else:
                with ThreadPoolExecutor(max_workers=width, thread_name_prefix="pf-extract") as executor:
                    futures = [executor.submit(contextvars.copy_context().run, engine._extract_unit,
                                               item, pool, base, snapshot.id, state["run_id"],
                                               session.runners, session.cancel, state["limitations"])
                               for item in batch]
                    outcomes = []
                    try:
                        for future in futures:
                            try:
                                outcomes.append(future.result())
                            except Exception as exc:
                                outcomes.append(exc)
                    except BaseException:
                        session.cancel.set()
                        for future in futures:
                            future.cancel()
                        raise
            session.batch_outcomes = dict(zip(range(offset, offset + len(batch)), outcomes))
        extracted = session.batch_outcomes.pop(state["unit_index"])
        if isinstance(extracted, BaseException):
            raise extracted
        if session.cancel.is_set():
            raise Cancelled(tr("분석 중단", "Analysis stopped"))
        session.reused += int(extracted["reused"])
        _prefetch_next(state, engine, store, snapshot.id, pool)
    else:
        extracted = engine._extract_unit(unit, pool, engine.store.graph(), snapshot.id,
                                         state["run_id"], _runners(state, engine),
                                         threading.Event(), snapshot.limitations,
                                         defer_escalation=True, prepared=prepared)
    output = extracted["output"]
    cached = extracted["cached"]
    return {"extracted": extracted,
            "candidate_count": len(output["event_candidates"]) + len(output["edge_candidates"]),
            "validation_repair_reasons": cached["routing_reasons"],
            "validation_repair_pending": extracted.get("escalation_pending", False),
            "validation_repair_executed": cached.get("escalated", False),
            "current_calls": _current_calls(store, state)}


def validate_candidates(state: StudioState) -> StudioState:
    _, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("후보 검증의 snapshot이 변경됐습니다.", "The candidate validation's snapshot has changed."))
    pool = {r.source_id: r for r in snapshot.records}
    evidence = state["extracted"]["evidence"]
    provided: dict[str, list[tuple[int, int]]] = {}
    _rehydrate(pool, provided, evidence)
    for item in evidence.values():
        provided.setdefault(item["source_id"], []).append((item["start_line"], item["end_line"]))
    validator = EvidenceValidator(pool, provided, assigned_source_ids=set(state["source_ids"]))
    validator.inherit_focus(evidence)
    output = state["extracted"]["output"]
    validator.check_extraction(output, state["unit_id"], snapshot.id, store.graph())
    assigned = set(state["source_ids"])
    return {"candidate_audit": {
        "validation": "passed", "unit_id": state["unit_id"],
        "validation_repair_reasons": state["validation_repair_reasons"],
        "validation_repair_executed": state["validation_repair_executed"],
        "events": [{"candidate_id": item["id"], "title": item["title"],
                    "kind": item["kind"], "status": item["status"],
                    "actor": item["actor"], "basis": item["basis"],
                    "summary": item["summary"],
                    "evidence": [{**citation,
                                  "from_current_unit": citation["source_id"] in assigned}
                                 for citation in item["evidence"]]}
                   for item in output["event_candidates"]],
        "edges": [{"from": item["from_event_id"], "to": item["to_event_id"],
                   "relation": item["relation"], "basis": item["basis"],
                   "evidence": item["evidence"]} for item in output["edge_candidates"]],
        "context_selection": copy.deepcopy(state["extracted"]["cached"].get("context_selection", {})),
        "existing_event_matches": output["existing_event_matches"],
        "open_items": output["open_items"],
        "limitations": output["limitations"]}}


def prepare_integrate_input(state: StudioState) -> StudioState:
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("통합 입력의 snapshot이 변경됐습니다.", "The integrate input's snapshot has changed."))
    pool = {r.source_id: r for r in snapshot.records}
    unit = state["planned_units"][state["unit_index"]]
    cancel = _cli_session(state).cancel if state.get("mode") == "cli" else threading.Event()
    prepared = engine._prepare_integration(unit, state["extracted"], pool, store.graph(),
                                           snapshot.id, state["run_id"],
                                           _runners(state, engine), cancel)
    with _prepared_lock:
        _prepared_integrations[(state["run_id"], state["unit_index"])] = prepared
    if prepared.data is None:
        return {"integrate_input": {"unit_id": unit["id"], "model_call_expected": False,
                                    "reason": tr("통합할 사건·관계 후보가 없습니다.",
                                                 "No event or relation candidates to integrate."),
                                    "base_graph_version": prepared.graph_version}}
    return {"integrate_input": {
        "unit_id": unit["id"], "model_call_expected": True,
        "context_selection": copy.deepcopy(prepared.harness.selection_audit),
        "base_graph_version": prepared.graph_version,
        "validated_candidate_count": len(prepared.data["validated_candidates"]["event_candidates"]),
        "request": IdAliases().wire(build_task("integrate", prepared.data, engine.config.output_language)),
        "response_schema": delta_schema(engine.config.integrate_evidence == "reuse")}}


def integrate_model_and_validate(state: StudioState) -> StudioState:
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("Studio fixture snapshot이 변경됐습니다.", "The Studio fixture snapshot has changed."))
    pool = {r.source_id: r for r in snapshot.records}
    unit = state["planned_units"][state["unit_index"]]
    cancel = _cli_session(state).cancel if state.get("mode") == "cli" else threading.Event()
    if state.get("mode") == "cli":
        _cli_session(state).update(tr(f"근거 확인·흐름 통합 중 · {state['unit_index'] + 1}/{len(state['planned_units'])}",
                                      f"Checking evidence and integrating the flow · {state['unit_index'] + 1}/{len(state['planned_units'])}"))
    with _prepared_lock:
        prepared = _prepared_integrations.pop((state["run_id"], state["unit_index"]), None)
    new_graph, evidence, dependencies = engine._integrate_unit(
        unit, state["extracted"], pool, engine.store.graph(), snapshot.id,
        state["run_id"], _runners(state, engine), cancel, prepared=prepared)
    return {"new_graph": new_graph, "evidence": evidence, "dependencies": dependencies,
            "model_delta": prepared.delta if prepared is not None else None,
            "prepared_integration": prepared,
            "current_calls": _current_calls(store, state)}


def route_semantic_review(state: StudioState) -> StudioState:
    delta = state.get("model_delta")
    signals = review_signal_items(delta)
    reasons = list(signals)
    engine, store, _ = _context(state)
    prepared = state.get("prepared_integration")
    issues = list(delta.get("review_issues", [])) if delta else []
    candidate_set = (prepared.data or {}).get("validated_candidates", {}) if prepared else {}
    candidates = {item["id"]: (kind, item) for kind, key in (
        ("event_candidate", "event_candidates"), ("edge_candidate", "edge_candidates"),
        ("open_item_candidate", "open_items")) for item in candidate_set.get(key, [])}
    if delta:
        for operation, signal, question in REVIEW_SIGNALS:
            for item in signals.get(signal, []):
                attribution = next((row for row in delta["change_attributions"]
                                    if row["operation"] == operation and row["item_id"] == item["id"]), None)
                if not attribution:
                    continue
                candidate_id = next((cid for cid in attribution["candidate_ids"] if cid in candidates), None)
                if candidate_id is None:
                    continue
                kind = candidates[candidate_id][0]
                issues.append({"id": ident("review_", state["unit_id"], operation, signal, item["id"]),
                    "origin": "code_signal", "stage": "integrate",
                    "target_kind": kind, "target_id": candidate_id,
                    "signal": signal, "question": question,
                    "evidence": item.get("evidence", [])})
    if prepared:
        prepared.review_issue_inputs = issues
    audit_issues = [{**issue, "evidence_ids": prepared.validator.citations(issue["evidence"]),
                     "evidence": None} for issue in issues] if prepared else issues
    prepared.review_audit = {"triggered": reasons, "issues": audit_issues}
    pending = bool(issues and engine.config.semantic_review)
    if not pending:
        graph = copy.deepcopy(state["new_graph"])
        audit = {"triggered": reasons, "issues": audit_issues, "executed": False,
            "status": "skipped_disabled" if issues else "not_needed",
            "resolutions": [], "unresolved_issue_ids": [item["id"] for item in issues]}
        graph["semantic_review_audit"] = audit
        graph["semantic_review_history"] = [*graph.get("semantic_review_history", []),
            {"unit_id": state["unit_id"], **audit}]
        if prepared:
            prepared.review_audit = audit
        return {"new_graph": graph, "semantic_review_pending": False,
                "semantic_review_executed": False, "semantic_review_reasons": reasons,
                "semantic_review_issues": audit_issues,
                "evidence": {**state["evidence"], **prepared.validator.evidence} if prepared else state["evidence"],
                "current_calls": _current_calls(store, state)}
    return {"semantic_review_pending": pending,
            "semantic_review_executed": False,
            "semantic_review_reasons": reasons,
            "semantic_review_issues": audit_issues,
            "evidence": {**state["evidence"], **prepared.validator.evidence} if prepared else state["evidence"],
            "current_calls": _current_calls(store, state)}


def semantic_review_route(state: StudioState) -> Literal["semantic_review_model_validate", "link_request_turns"]:
    return "semantic_review_model_validate" if state.get("semantic_review_pending") else "link_request_turns"


def semantic_review_model_validate(state: StudioState) -> StudioState:
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("의미 재검토 snapshot이 변경됐습니다.", "The semantic review's snapshot has changed."))
    runners = _runners(state, engine)
    prepared = state["prepared_integration"]
    graph = engine._review_delta(prepared, store.graph(), snapshot.id, state["run_id"], runners)
    return {"new_graph": graph,
            "evidence": {**state["evidence"], **prepared.validator.evidence},
            "semantic_review_executed": prepared.review_audit.get("executed", False),
            "current_calls": _current_calls(store, state)}


def link_request_turns(state: StudioState) -> StudioState:
    """Code, not a model: a node for every user message, and each new event tied to its turn."""
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("Studio fixture snapshot이 변경됐습니다.", "The Studio fixture snapshot has changed."))
    pool = {r.source_id: r for r in snapshot.records}
    graph, cited = analysis_link_request_turns(state["new_graph"], store.graph(), pool, state["source_ids"],
                                               state["evidence"], store.evidence_many, state["run_id"])
    return {"new_graph": graph, "evidence": {**state["evidence"], **cited}}


def summarize_graph_changes(state: StudioState) -> StudioState:
    _, store, _ = _context(state)
    before, after = store.graph(), state["new_graph"]
    old_events = {item["id"]: item for item in before["events"]}
    old_edges = {item["id"]: item for item in before["edges"]}
    return {"graph_change_audit": {
        "base_graph_version": before["version"],
        "model_delta": state["model_delta"],
        "events_added": [{"id": item["id"], "title": item["title"],
                          "status": item["status"], "evidence_ids": item["evidence_ids"]}
                         for item in after["events"] if item["id"] not in old_events],
        "events_updated": [{"id": item["id"], "title_before": old_events[item["id"]]["title"],
                            "title_after": item["title"],
                            "status_before": old_events[item["id"]]["status"],
                            "status_after": item["status"]}
                           for item in after["events"] if item["id"] in old_events
                           and item != old_events[item["id"]]],
        "edges_added": [{"id": item["id"], "from": item["from_event_id"],
                         "to": item["to_event_id"], "relation": item["relation"],
                         "evidence_ids": item["evidence_ids"]}
                        for item in after["edges"] if item["id"] not in old_edges],
        "edges_invalidated": [item["id"] for item in after["edges"]
                              if item["id"] in old_edges and old_edges[item["id"]]["active"]
                              and not item["active"]],
        "candidate_resolutions": after.get("last_candidate_resolutions", []),
        "semantic_review": after.get("semantic_review_audit", {"status": "not_run"}),
        "review_issues": after.get("semantic_review_audit", {}).get("issues", []),
        "review_resolutions": after.get("semantic_review_audit", {}).get("resolutions", []),
        "unresolved_review_issue_ids": after.get("semantic_review_audit", {}).get("unresolved_issue_ids", [])}}


def publish_result(state: StudioState) -> StudioState:
    engine, store, snapshot = _context(state)
    if snapshot.id != state["snapshot_id"]:
        raise FlowError(tr("Studio fixture snapshot이 변경됐습니다.", "The Studio fixture snapshot has changed."))
    graph = state["new_graph"]
    completed = state["completed_units"] + 1
    all_done = (completed == state["total_planned_units"] and not state["missing_sources"]
                and not state.get("records_waiting"))
    graph["analysis_status"] = "complete" if all_done and not _incomplete_input(state["limitations"]) else "partial"
    graph["input_limitations"] = state["limitations"]
    runners = _runners(state, engine)
    graph["analysis_mode"] = "synthetic_mock" if runners.is_mock else "cli_ai"
    graph["coverage"] = {"selected_records": state["selected_records"],
                         "completed_units_this_run": completed,
                         "planned_units_this_run": state["total_planned_units"]}
    base = store.graph()
    # Events analysed ahead of older records still waiting (`--session`): said so wherever shown.
    ahead = set(base.get("out_of_order_events", []))
    if engine.config.session:
        ahead |= {event["id"] for event in graph["events"]} - {event["id"] for event in base["events"]}
    ahead &= {event["id"] for event in graph["events"]}
    if ahead:
        graph["out_of_order_events"] = sorted(ahead)
    else:
        graph.pop("out_of_order_events", None)
    cached = {**state["extracted"]["cached"], "evidence": state["evidence"]}
    # This call site used to compute a cache_key from a different formula than
    # Engine._extract_unit did. Neither was ever read back, so both were removed
    # rather than reconciled; reuse keys off routing_signature and context_digest
    # inside `cached`.
    store.save_unit(state["unit_id"], state["source_ids"], state["dependencies"],
                    "extracted", cached)
    pool = {r.source_id: r for r in snapshot.records}
    published = store.publish(graph, [state["unit_id"]],
                              {i: pool[i].content_hash for i in state["source_ids"]},
                              state["evidence"], expected_version=base["version"])
    if state.get("mode") == "cli":
        session = _cli_session(state)
        session.completed = completed
        session.update(tr(f"단위 완료 {completed}/{state['total_planned_units']} · 그래프 v{published['version']}",
                          f"Units done {completed}/{state['total_planned_units']} · graph v{published['version']}"))
    unit_result = {"provider": state["provider"], "source_count": len(state["source_ids"]),
                   "candidate_count": state["candidate_count"],
                   "validation_repair_executed": state["validation_repair_executed"],
                   "validation_repair_reasons": state["validation_repair_reasons"],
                   "calls": _current_calls(store, state),
                   "graph_version": published["version"]}
    return {"completed_units": completed,
            "unit_results": [*state["unit_results"], unit_result],
            "graph_summary": graph_summary(published)}


def advance_unit(state: StudioState) -> StudioState:
    return {"unit_index": state["unit_index"] + 1}


def next_unit_route(state: StudioState) -> Literal["select_unit", "finish_run"]:
    return "select_unit" if state["unit_index"] < len(state["planned_units"]) else "finish_run"


def finish_run(state: StudioState) -> StudioState:
    try:
        _, store, _ = _context(state)
        graph = store.graph()
        calls = store.llm_calls(state["run_id"])
        if state.get("mode") == "cli":
            session = _cli_session(state)
            pending_count = sum(row["available"] and row["content_hash"] != row["processed_hash"]
                                for row in store.sources().values())
            status = ("partial" if pending_count or state["missing_sources"] or
                      _incomplete_input(state["limitations"]) else
                      "complete" if state["planned_units"] else
                      "noop" if graph["version"] else "no_data")
            details = {"run_id": state["run_id"], "runner_calls": session.runners.budget.started,
                       "completed_units": session.completed, "reused_extractions": session.reused,
                       "snapshot_id": state["snapshot_id"], "limitations": state["limitations"],
                       "pending_records": pending_count, "graph_version": graph["version"]}
            store.finish_run(state["run_id"], status, details)
            return {"result": {"status": status, "graph": graph, **details}}
        status = (graph["analysis_status"] if state["planned_units"] else
                  ("partial" if state["missing_sources"] or _incomplete_input(state["limitations"])
                   else "noop" if graph["version"] else "no_data"))
        mode = state.get("mode", "synthetic") + "_codex" if state.get("mode") in {"live", "eval"} else "synthetic_mock"
        store.finish_run(state["run_id"], status,
                         {"runner_calls": len(calls), "completed_units": state["completed_units"],
                          "graph_version": graph["version"], "mode": mode})
        return {"result": {"mode": mode, "status": status,
                           "run_id": state["run_id"],
                           "output_dir": state.get("output_dir"),
                           "selected_records": state["selected_records"],
                           "planned_units": state["total_planned_units"],
                           "completed_units": state["completed_units"],
                           "unit_results": state["unit_results"],
                           "graph_version": graph["version"],
                           "events": [{"title": e["title"], "status": e["status"]}
                                      for e in graph["events"]],
                           "runner_calls": len(calls)}}
    finally:
        _clear_prepared(state["run_id"])
        if state.get("mode") in {"live", "eval"}:
            _release_live(state["run_id"])


def _record_failure(store: Store, run_id: str, exc: BaseException, *,
                    completed: int, reused: int = 0, runner_calls: int = 0,
                    limitations: list[str] | None = None) -> tuple[str, dict]:
    status = "cancelled" if isinstance(exc, (Cancelled, KeyboardInterrupt)) else (
        "partial" if completed else "failed")
    message = str(exc) if isinstance(exc, FlowError) else tr(f"내부 오류: {type(exc).__name__}",
                                                             f"Internal error: {type(exc).__name__}")
    details = {"run_id": run_id, "runner_calls": runner_calls,
               "completed_units": completed, "reused_extractions": reused,
               "error": message, "limitations": limitations or [],
               "graph_version": store.graph()["version"]}
    store.finish_run(run_id, status, details)
    return status, details


def _guard(node):
    def run(state: StudioState) -> StudioState:
        try:
            return node(state)
        except BaseException as exc:
            if state.get("run_id"):
                _clear_prepared(state["run_id"])
            if state.get("mode") in {"live", "eval"} and state.get("run_id"):
                with _live_sessions_lock:
                    session = _live_sessions.get(state["run_id"])
                if session is not None:
                    _record_failure(session.store, state["run_id"], exc,
                                    completed=state.get("completed_units", 0),
                                    runner_calls=session.runners.budget.started,
                                    limitations=state.get("limitations", []))
                    _release_live(state["run_id"])
            raise
    return run


workflow = StateGraph(StudioState)
for name, node in (("prepare_run", prepare_run), ("scan_sources", scan_sources),
                   ("classify_steps", classify_steps), ("plan_work_units", plan_work_units), ("select_unit", select_unit),
                   ("prepare_extract_input", prepare_extract_input),
                   ("extract_model_and_validate", extract_model_and_validate),
                   ("validate_candidates", validate_candidates),
                   ("prepare_integrate_input", prepare_integrate_input),
                   ("integrate_model_and_validate", integrate_model_and_validate),
                   ("route_semantic_review", route_semantic_review),
                   ("semantic_review_model_validate", semantic_review_model_validate),
                   ("link_request_turns", link_request_turns),
                   ("summarize_graph_changes", summarize_graph_changes),
                   ("publish_result", publish_result), ("advance_unit", advance_unit),
                   ("finish_run", finish_run)):
    workflow.add_node(name, _guard(node))
workflow.add_edge(START, "prepare_run")
workflow.add_edge("prepare_run", "scan_sources")
workflow.add_edge("scan_sources", "classify_steps")
workflow.add_edge("classify_steps", "plan_work_units")
workflow.add_conditional_edges("plan_work_units", initial_route)
workflow.add_edge("select_unit", "prepare_extract_input")
workflow.add_edge("prepare_extract_input", "extract_model_and_validate")
workflow.add_edge("extract_model_and_validate", "validate_candidates")
workflow.add_edge("validate_candidates", "prepare_integrate_input")
workflow.add_edge("prepare_integrate_input", "integrate_model_and_validate")
workflow.add_edge("integrate_model_and_validate", "route_semantic_review")
workflow.add_conditional_edges("route_semantic_review", semantic_review_route)
workflow.add_edge("semantic_review_model_validate", "link_request_turns")
workflow.add_edge("link_request_turns", "summarize_graph_changes")
workflow.add_edge("summarize_graph_changes", "publish_result")
workflow.add_edge("publish_result", "advance_unit")
workflow.add_conditional_edges("advance_unit", next_unit_route)
workflow.add_edge("finish_run", END)
graph = workflow.compile()


def run_engine(engine: Engine, runner_factory: Callable[[], Any], *,
               cancel: threading.Event | None = None,
               update: Callable[[str], None] | None = None,
               consent: Callable[[Snapshot, dict], bool] | None = None) -> dict:
    """Run the same graph used by Studio for a terminal or eval analysis."""
    event = cancel or threading.Event()
    run_id = "run_" + uuid.uuid4().hex
    session = _CliInvocation(engine, RunnerPool(runner_factory, engine.config), event,
                             update or (lambda _: None), consent)
    tracer = getattr(engine, "tracer", None)
    # Graph state contains raw candidate text. Without content tracing the node and
    # step spans go through a metadata-only client; without --langsmith nothing is sent.
    make_client = getattr(tracer, "graph_client", None)
    trace_client = make_client() if make_client is not None else None
    with engine.store.analyze_lock():
        with _cli_invocations_lock:
            _cli_invocations[run_id] = session
        try:
            # Never fall back to the SDK's default client: only an explicit client traces.
            with tracing_context(enabled=trace_client is not None, client=trace_client,
                                 project_name=tracer.project if trace_client is not None else None):
                state = graph.invoke({"mode": "cli", "run_id": run_id}, config={
                    "recursion_limit": 1_000_000, "run_name": "ContextTrail analysis",
                    "tags": ["contexttrail", "content" if engine.config.langsmith_include_content else "metadata_only"],
                    "metadata": {"contexttrail_run_id": run_id,
                                 "runner": engine.config.runner_name or "unknown",
                                 "content_included": engine.config.langsmith_include_content}})
            return state["result"]
        except BaseException as exc:
            status, details = _record_failure(engine.store, run_id, exc,
                                              completed=session.completed, reused=session.reused,
                                              runner_calls=session.runners.budget.started,
                                              limitations=session.issues)
            if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                raise
            return {"status": status, "graph": engine.store.graph(), **details}
        finally:
            session.stop_prefetch()
            with _cli_invocations_lock:
                _cli_invocations.pop(run_id, None)
            if trace_client is not None:
                try:
                    trace_client.flush(timeout=10)
                except Exception:
                    pass  # Tracing never changes the analysis result.
