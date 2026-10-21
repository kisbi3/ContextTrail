from pathlib import Path
import copy

import pytest

pytest.importorskip("langgraph")

from contexttrail.git_context import Scope
from contexttrail.demo import create_demo
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.evaluation import demo_fixture
from contexttrail.store import Store
from contexttrail import studio_graph
from contexttrail import analysis
from contexttrail.schema import EvidenceValidator, draft_delta
from contexttrail.light import STORY_SCHEMA
from contexttrail.studio_graph import _context, graph
from contexttrail.util import FlowError
from contexttrail.util import dumps


def test_studio_graph_runs_real_pipeline_with_synthetic_fixture():
    state = graph.invoke({})
    result = state["result"]
    assert result["mode"] == "synthetic_mock"
    assert result["status"] == "complete"
    assert result["selected_records"] == 12
    assert result["planned_units"] == result["completed_units"] == 2
    assert result["graph_version"] == 2
    assert result["runner_calls"] == 2
    assert [item["provider"] for item in result["unit_results"]] == ["codex", "claude"]
    assert [call["stage"] for call in result["unit_results"][1]["calls"]] == ["story"]
    assert {item["status"] for item in result["events"]} == {
        "asked", "proposed", "applied", "observed_failure", "observed_success", "reported_complete"}
    scope = Scope.resolve(Path(state["fixture_dir"]) / "sample-project")
    store = Store(scope.state_dir, scope.id)
    # One story call per unit; the edits and checks are code's, and each edit is verified by the check after it.
    assert [call["stage"] for call in store.llm_calls(state["run_id"])] == ["story", "story"]
    published = store.graph()
    events = {e["id"]: e for e in published["events"]}
    verified = {(events[edge["from_event_id"]]["status"], events[edge["to_event_id"]]["status"])
                for edge in published["edges"] if edge["active"] and edge["relation"] == "verifies"}
    assert verified == {("applied", "observed_failure"), ("applied", "observed_success")}
    # Every node rebuilds the engine; the output language saved at planning still names the code's work.
    assert {e["title"] for e in published["events"] if e["kind"] == "action"} == {"수정: store.py"}
    report = next(e for e in published["events"] if e["status"] == "reported_complete")
    assert any(edge["relation"] == "answers" and edge["to_event_id"] == report["id"]
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
    assert nodes.index("scan_sources") + 1 == nodes.index("classify_steps") == nodes.index("plan_work_units") - 1
    # One model call per unit (the story); the unit's delta is published by code.
    assert nodes.count("extract_model_and_validate") == nodes.count("publish_draft") == 2
    assert nodes.index("extract_model_and_validate") < nodes.index("publish_draft") < nodes.index("publish_result")
    assert not {"integrate_model_and_validate", "route_semantic_review", "semantic_review_model_validate",
                "route_escalation", "escalation_model_and_validate"} & set(nodes)


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
    assert first["runner_calls"] == 1

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
    audits = [update["validate_candidates"]["candidate_audit"]
              for update in updates if "validate_candidates" in update]
    changes = [update["summarize_graph_changes"]["graph_change_audit"]
               for update in updates if "summarize_graph_changes" in update]

    assert len(extracts) == len(audits) == len(changes) == 2
    assert [task["stage"] for task in calls] == ["story", "story"]
    assert [item["request"] for item in extracts] == calls
    assert extracts[0]["response_schema"] == STORY_SCHEMA
    assert extracts[0]["context_selection"]["policy"] == "relevance_v2"
    assert all("candidate_resolutions" in item for item in changes)
    assert extracts[0]["new_records"]
    assert audits[0]["validation"] == "passed"
    assert all(item["evidence"] for item in audits[0]["events"])
    assert changes[0]["events_added"] and changes[0]["candidate_resolutions"]


def test_studio_traces_validation_repair_as_separate_operation(monkeypatch):
    names = []
    original_trace = analysis._trace_operation

    def capture(enabled, name, inputs, operation, **kwargs):
        names.append(name)
        return original_trace(enabled, name, inputs, operation, **kwargs)

    class BadFirstStory(studio_graph.StudioFixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "story" and "repair" not in task:
                output["story"] = "not a list"
            return output

    monkeypatch.setattr(analysis, "_trace_operation", capture)
    monkeypatch.setattr(studio_graph, "StudioFixtureRunner", BadFirstStory)
    state = graph.invoke({})
    assert state["result"]["status"] == "complete"
    assert "validate_story_contract" in names and "validate_story_claims" in names
    assert "prepare_repair_request" in names
    assert any(call["status"] == "validation_error"
               for item in state["result"]["unit_results"] for call in item["calls"])


def last_draft():
    """The demo's second unit as `publish_draft` publishes it: the code-built delta, its validator, the candidates
    and the graph it was published against."""
    state = graph.invoke({})
    _, store, snapshot = _context(state)
    pool = {r.source_id: r for r in snapshot.records}
    extracted, provided = state["extracted"], {}
    analysis._rehydrate(pool, provided, extracted["evidence"])
    for item in extracted["evidence"].values():
        provided.setdefault(item["source_id"], []).append((item["start_line"], item["end_line"]))
    validator = EvidenceValidator(pool, provided, assigned_source_ids=set(state["source_ids"]))
    validator.inherit_focus(extracted["evidence"])
    base = store.graph(1)
    return state, validator, base, extracted["output"], draft_delta(extracted["output"], base["version"],
                                                                     state["snapshot_id"])


def test_delta_rejects_missing_or_mislinked_candidate_contract():
    state, validator, base, candidates, delta = last_draft()
    assert delta["candidate_resolutions"] and delta["change_attributions"]
    validator.apply_delta(copy.deepcopy(delta), base, state["snapshot_id"], state["run_id"], candidates)
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
            validator.apply_delta(variant, base, state["snapshot_id"], state["run_id"], candidates)


def test_delta_attributes_a_change_to_the_candidate_that_resolves_to_it():
    state, validator, base, candidates, delta = last_draft()
    resolutions = delta["candidate_resolutions"]
    swapped = None
    for item in delta["change_attributions"]:
        owners = [row["candidate_id"] for row in resolutions if item["item_id"] in row["target_ids"]]
        others = [row["candidate_id"] for row in resolutions
                  if row["candidate_id"] not in owners and item["item_id"] not in row["target_ids"]
                  and row["disposition"] != "excluded"]
        if item["operation"] == "events_to_add" and owners and others:
            item["candidate_ids"] = others[:1]
            swapped = (item, owners)
            break
    if swapped is None:
        pytest.skip("the fixture has no attribution to swap")
    validator.apply_delta(delta, base, state["snapshot_id"], state["run_id"], candidates)
    assert swapped[0]["candidate_ids"] == sorted(swapped[1])
    assert any(n["mode"] == "attribution_candidates_from_resolutions" for n in validator.normalizations)


def test_studio_live_mode_passes_the_runner_conversation_to_the_codex_cli(tmp_path, monkeypatch):
    # The real Studio runner, with only the CLI call itself answered by the fixture: the harness hands every call
    # the task's conversation (`thread`), which the Studio runner must accept and pass on.
    folder, codex, claude = create_demo(tmp_path / "source")
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_SCOPE", str(folder))
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_CODEX_HOME", str(codex))
    monkeypatch.setenv("CONTEXTTRAIL_STUDIO_CLAUDE_HOME", str(claude))
    threads = []

    class Conversation:
        def close(self):
            pass

    def answer(self, task, schema, cancel, thread=None):
        threads.append(thread)
        return FixtureRunner().run(task, schema, cancel)

    monkeypatch.setattr(studio_graph.CLIRunner, "thread", lambda self: Conversation())
    monkeypatch.setattr(studio_graph.CLIRunner, "run", answer)
    monkeypatch.setattr(studio_graph.CLIRunner, "preflight", lambda self: None, raising=False)
    result = graph.invoke({"mode": "live", "confirm_live": True, "max_units": 1})["result"]
    assert result["completed_units"] == 1 and result["runner_calls"] == 1
    assert threads and all(isinstance(thread, Conversation) for thread in threads)
