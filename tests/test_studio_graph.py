from pathlib import Path
import copy

import pytest

pytest.importorskip("langgraph")

from projectflow.git_context import Scope
from projectflow.demo import create_demo
from projectflow.demo import CASES, FixtureRunner
from projectflow.evaluation import demo_fixture
from projectflow.store import Store
from projectflow import studio_graph
from projectflow import analysis
from projectflow.studio_graph import _context, graph
from projectflow.util import FlowError
from projectflow.util import dumps


def test_studio_graph_runs_real_pipeline_with_synthetic_fixture():
    state = graph.invoke({})
    result = state["result"]
    assert result["mode"] == "synthetic_mock"
    assert result["status"] == "complete"
    assert result["selected_records"] == 6
    assert result["planned_units"] == result["completed_units"] == 2
    assert result["graph_version"] == 2
    assert result["runner_calls"] == 5
    assert [item["provider"] for item in result["unit_results"]] == ["codex", "claude"]
    assert [item["validation_repair_executed"] for item in result["unit_results"]] == [False, False]
    assert [call["stage"] for call in result["unit_results"][1]["calls"]] == [
        "extract", "integrate", "integrate"]
    assert {item["status"] for item in result["events"]} == {
        "adopted", "proposed", "applied", "reported_complete", "observed_failure"}
    scope = Scope.resolve(Path(state["fixture_dir"]) / "sample-project")
    store = Store(scope.state_dir, scope.id)
    # The second unit's revision first misses its target; review draws the revises link.
    assert [call["stage"] for call in store.llm_calls(state["run_id"])] == [
        "extract", "integrate", "extract", "integrate", "integrate"]
    published = store.graph()
    revision = next(e for e in published["events"] if e["kind"] == "revision")
    assert any(edge["relation"] == "revises" and edge["to_event_id"] == revision["id"]
               for edge in published["edges"] if edge["active"])
    assert published["version"] == 2
    assert len(published["candidate_resolution_history"]) == 2
    assert all(item["unit_id"] for item in published["candidate_resolution_history"])
    assert all(not target.startswith("tmp:") for entry in published["candidate_resolution_history"]
               for resolution in entry["resolutions"] for target in resolution["target_ids"])


def test_studio_graph_rejects_external_scope(tmp_path):
    with pytest.raises(FlowError, match="합성 fixture"):
        _context({"fixture_dir": str(tmp_path)})


def test_studio_graph_exposes_actual_model_call_boundaries():
    nodes = [name for update in graph.stream({}, stream_mode="updates")
             for name in update]
    assert nodes.count("extract_model_and_validate") == 2
    assert nodes.count("integrate_model_and_validate") == 2
    assert nodes.count("route_semantic_review") == 2
    assert nodes.count("semantic_review_model_validate") == 1
    assert "route_escalation" not in nodes
    assert "escalation_model_and_validate" not in nodes


def test_studio_live_mode_uses_fixed_scope_and_processes_one_real_unit(tmp_path, monkeypatch):
    folder, codex, claude = create_demo(tmp_path / "source")
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_SCOPE", str(folder))
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_CODEX_HOME", str(codex))
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_CLAUDE_HOME", str(claude))
    monkeypatch.setattr(studio_graph, "StudioCodexRunner", studio_graph.StudioFixtureRunner)

    first = graph.invoke({"mode": "live", "confirm_live": True, "max_units": 1})["result"]
    assert first["mode"] == "live_codex"
    assert first["status"] == "partial"
    assert first["planned_units"] == 2
    assert first["completed_units"] == 1
    assert first["runner_calls"] == 2

    second = graph.invoke({"mode": "live", "confirm_live": True, "max_units": 1})["result"]
    assert second["status"] == "complete"
    assert second["planned_units"] == second["completed_units"] == 1
    scope = Scope.resolve(folder)
    assert Store(scope.state_dir, scope.id).graph()["version"] == 2


def test_studio_live_mode_requires_operator_scope_and_explicit_input(monkeypatch):
    monkeypatch.delenv("CONTEXTTRAIL_STUDIO_SCOPE", raising=False)
    with pytest.raises(FlowError, match="confirm_live"):
        graph.invoke({"mode": "live"})
    with pytest.raises(FlowError, match="CONTEXTTRAIL_STUDIO_SCOPE"):
        graph.invoke({"mode": "live", "confirm_live": True})


def test_studio_eval_mode_uses_server_fixed_fixture(tmp_path, monkeypatch):
    fixture = tmp_path / "small.json"
    fixture.write_text(dumps(demo_fixture()), encoding="utf-8")
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_EVAL_FIXTURE", str(fixture))
    monkeypatch.setattr(studio_graph, "StudioCodexRunner", studio_graph.StudioFixtureRunner)

    state = graph.invoke({"mode": "eval", "confirm_live": True, "max_units": 1})
    assert state["result"]["mode"] == "eval_codex"
    assert state["result"]["completed_units"] == 1
    assert state["result"]["status"] == "partial"
    assert state["selected_records"] == len(demo_fixture()["records"])


def test_terminal_analysis_invokes_studio_execution_graph(laboratory, monkeypatch):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    original = studio_graph.graph.invoke
    inputs = []

    def capture(value, *args, **kwargs):
        inputs.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(studio_graph.graph, "invoke", capture)
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "complete"
    assert result["graph"]["version"] == 1
    assert store.graph()["version"] == 1
    assert len(inputs) == 1 and inputs[0]["mode"] == "cli"


def test_terminal_graph_trace_is_off_by_default_and_metadata_only_without_content(laboratory, monkeypatch):
    from contextlib import contextmanager

    _, _, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    seen = []

    @contextmanager
    def capture(**kwargs):
        client = kwargs["client"]
        seen.append((kwargs["enabled"], type(client).__name__ if client is not None else None))
        yield

    monkeypatch.setattr(studio_graph, "tracing_context", capture)
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-key")
    assert engine.analyze(FixtureRunner)["status"] == "complete"
    engine.config.langsmith_enabled = True
    assert engine.analyze(FixtureRunner)["status"] == "noop"
    engine.config.langsmith_include_content = True
    assert engine.analyze(FixtureRunner)["status"] == "noop"
    assert seen == [(False, None), (True, "MetadataOnlyClient"), (True, "Client")]


def test_studio_shows_exact_requests_candidates_and_graph_changes(monkeypatch):
    calls = []

    class CaptureRunner(studio_graph.StudioFixtureRunner):
        def run(self, task, schema, cancel):
            calls.append(copy.deepcopy(task))
            return super().run(task, schema, cancel)

    monkeypatch.setattr(studio_graph, "StudioFixtureRunner", CaptureRunner)
    updates = list(graph.stream({}, stream_mode="updates"))
    extracts = [update["prepare_extract_input"]["extract_input"]
                for update in updates if "prepare_extract_input" in update]
    integrates = [update["prepare_integrate_input"]["integrate_input"]
                  for update in updates if "prepare_integrate_input" in update]
    audits = [update["validate_candidates"]["candidate_audit"]
              for update in updates if "validate_candidates" in update]
    changes = [update["summarize_graph_changes"]["graph_change_audit"]
               for update in updates if "summarize_graph_changes" in update]

    assert len(extracts) == len(integrates) == len(audits) == len(changes) == 2
    first_extract_call = next(task for task in calls if task["stage"] == "extract")
    first_integrate_call = next(task for task in calls if task["stage"] == "integrate")
    assert extracts[0]["request"] == first_extract_call
    assert integrates[0]["request"] == first_integrate_call
    assert extracts[0]["context_selection"]["policy"] == "relevance_v2"
    assert integrates[0]["context_selection"]["policy"] == "relevance_v2"
    assert all("candidate_resolutions" in item for item in changes)
    assert all("semantic_review" in item for item in changes)
    reviewed = next(item for item in changes if item["review_issues"])
    assert reviewed["review_issues"]
    assert {item["issue_id"] for item in reviewed["review_resolutions"]} == {
        item["id"] for item in reviewed["review_issues"]}
    assert not reviewed["unresolved_review_issue_ids"]
    assert extracts[0]["new_records"]
    assert audits[0]["validation"] == "passed"
    assert all(item["evidence"] for item in audits[0]["events"])
    assert changes[0]["model_delta"] is not None
    assert changes[0]["events_added"]


def test_studio_traces_validation_repair_as_separate_operation(monkeypatch):
    names = []
    original_trace = analysis._trace_operation

    def capture(enabled, name, inputs, operation, **kwargs):
        names.append(name)
        return original_trace(enabled, name, inputs, operation, **kwargs)

    class BadFirstQuote(studio_graph.StudioFixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "extract" and "repair" not in task and output["event_candidates"]:
                output["event_candidates"][0]["evidence"][0]["quote"] = "invented quote"
            return output

    monkeypatch.setattr(analysis, "_trace_operation", capture)
    monkeypatch.setattr(studio_graph, "StudioFixtureRunner", BadFirstQuote)
    state = graph.invoke({})
    assert state["result"]["status"] == "complete"
    assert "validate_extract_claims" in names
    assert "prepare_repair_request" in names
    assert any(call["status"] == "validation_error"
               for item in state["result"]["unit_results"] for call in item["calls"])


def test_delta_rejects_missing_or_mislinked_candidate_contract():
    state = graph.invoke({})
    prepared = state["prepared_integration"]
    candidates = prepared.data["validated_candidates"]
    base = prepared.harness.graph
    delta = state["model_delta"]
    assert delta["candidate_resolutions"] and delta["change_attributions"]
    variants = []
    missing_candidate = copy.deepcopy(delta)
    missing_candidate["candidate_resolutions"].pop()
    variants.append(missing_candidate)
    wrong_kind = copy.deepcopy(delta)
    wrong_kind["candidate_resolutions"][0]["candidate_kind"] = "edge"
    variants.append(wrong_kind)
    wrong_target = copy.deepcopy(delta)
    wrong_target["candidate_resolutions"][0]["target_ids"] = ["ev_missing"]
    variants.append(wrong_target)
    missing_attribution = copy.deepcopy(delta)
    missing_attribution["change_attributions"] = []
    variants.append(missing_attribution)
    for variant in variants:
        with pytest.raises(FlowError):
            prepared.validator.apply_delta(variant, base, state["snapshot_id"], state["run_id"], candidates)


def test_semantic_review_unresolved_issue_is_preserved(monkeypatch):
    class UnresolvedReview(studio_graph.StudioFixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["data"].get("review_issues"):
                for resolution in output["review_resolutions"]:
                    resolution["status"] = "unresolved"
                    resolution["reason"] = "합성 검토에서도 불확실성이 남았습니다."
            return output

    monkeypatch.setattr(studio_graph, "StudioFixtureRunner", UnresolvedReview)
    state = graph.invoke({})
    audit = next(item for item in state["new_graph"]["semantic_review_history"] if item["issues"])
    assert audit["status"] == "reviewed_with_unresolved"
    assert audit["unresolved_issue_ids"]
    assert {item["issue_id"] for item in audit["resolutions"]} == {
        item["id"] for item in audit["issues"]}


def test_semantic_review_failure_prevents_publication(monkeypatch):
    seen, updates = [], []

    def fail_review(self, *args, **kwargs):
        seen.append(True)
        raise FlowError("synthetic review failure")

    monkeypatch.setattr(analysis.Engine, "_review_delta", fail_review)
    with pytest.raises(FlowError, match="synthetic review failure"):
        for update in graph.stream({}, stream_mode="updates"):
            updates.extend(update)
    assert seen
    # The first unit needs no review and is published; the reviewed second unit is not.
    assert updates.count("publish_result") == 1
    assert updates.index("publish_result") < len(updates) - 1 - updates[::-1].index("select_unit")
