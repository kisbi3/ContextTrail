"""A unit that fails for its own reasons is skipped, held back while it is the same input, and counted apart;
the runner's errors, the call cap and unknown errors still stop the run. Failures are put in by a mock runner."""
import threading
from dataclasses import replace

import pytest

from contexttrail import journal
from contexttrail.analysis import AnalysisConfig, Engine, _incomplete_input, held_back_message, skipped_unit_message
from contexttrail.cli import main
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.freshness import check, status_lines, summary
from contexttrail.util import BrokenOutput, FlowError


def two_sessions(records, make):
    """Session `one` (unit A) is older than session `two` (unit B); each has one candidate for the fixture."""
    records.extend([replace(make(CASES[0][0], key="a", session="one"), recorded_at="2026-09-22T10:00:00Z"),
                    replace(make(CASES[4][0], key="b", session="two"), recorded_at="2026-09-23T10:00:00Z")])


def failing(session_text, *, stage="extract"):
    """A runner whose answer for the unit holding `session_text` quotes something the source never said."""
    class Invented(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            data = task["data"]
            texts = [line["text"] for record in data.get("new_records", []) for line in record["lines"]]
            if task["stage"] == stage and session_text in texts and output.get("event_candidates"):
                output["event_candidates"][0]["evidence"][0]["quote"] = "<invented quote>"
            return output
    return Invented


def failing_integration(session_text):
    """An integrator whose events quote something the source never said, for the unit holding `session_text`."""
    class Invented(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            candidates = (task["data"].get("validated_candidates") or {}).get("event_candidates", [])
            if task["stage"] == "integrate" and any(c["summary"] == session_text for c in candidates):
                for event in output.get("events_to_add", []):
                    event["evidence"][0]["quote"] = "<invented quote>"
            return output
    return Invented


def test_a_unit_failing_validation_is_skipped_and_the_next_one_runs(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    result = engine.analyze(failing(CASES[0][0]))
    assert result["status"] == "partial" and result["completed_units"] == 1 and result["skipped_units"] == 1
    assert [e["title"] for e in store.graph()["events"]] == [CASES[4][2]]
    (unit_id, entry), = store.unit_failures().items()
    assert entry["kind"] == "validation" and entry["sources"] == ["a"] and entry["attempts"] == 1
    assert any(message == skipped_unit_message(unit_id, "validation") for message in result["limitations"])
    # Nothing was published for the skipped unit, and its record still waits.
    assert store.sources()["a"]["processed_hash"] is None


def test_the_same_input_is_not_sent_again_and_a_rerun_has_nothing_to_do(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing(CASES[0][0]))
    runner = FixtureRunner()
    again = engine.analyze(lambda: runner)
    assert again["status"] == "noop" and runner.calls == 0 and again["pending_records"] == 0
    assert held_back_message(1) in again["limitations"]
    assert not _incomplete_input(again["limitations"])


def test_a_changed_setting_sends_the_unit_again(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing(CASES[0][0]))
    engine.config.extract_model = "other-model"  # part of the routing signature
    runner = FixtureRunner()
    assert engine.analyze(lambda: runner)["completed_units"] == 1 and runner.calls > 0
    assert store.unit_failures() == {}


def test_retry_failed_sends_a_held_unit_again(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing(CASES[0][0]))
    engine.config.retry_failed = True
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete" and result["completed_units"] == 1 and store.unit_failures() == {}


def test_a_failed_attempt_again_counts_up_and_a_changed_record_is_a_new_unit(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing(CASES[0][0]))
    engine.config.retry_failed = True
    engine.analyze(failing(CASES[0][0]))
    (entry,) = store.unit_failures().values()
    assert entry["attempts"] == 2
    # The record changes: another unit ID, so the old failure does not hold it.
    engine.config.retry_failed = False
    records[0] = replace(records[0], content=CASES[1][0])
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["completed_units"] == 1 and runner.calls > 0


def test_a_skipped_unit_sent_again_after_a_later_one_joins_the_graph_out_of_order(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing(CASES[0][0]))
    first = {e["id"] for e in store.graph()["events"]}
    engine.config.retry_failed = True
    engine.analyze(FixtureRunner)
    graph = store.graph()
    added = {e["id"] for e in graph["events"]} - first
    assert added and set(graph["out_of_order_events"]) == added


def test_a_unit_retried_with_nothing_integrated_since_is_in_order(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0], key="a", session="one"))
    engine.analyze(failing(CASES[0][0]))  # the only unit fails
    engine.config.retry_failed = True
    engine.analyze(FixtureRunner)
    assert "out_of_order_events" not in store.graph()


def test_an_input_over_the_budget_skips_the_unit(laboratory):
    _, store, engine, records, make = laboratory
    # Unit A's lines together outgrow the request budget, which the model's input cannot be trimmed below.
    engine.config.record_chars, engine.config.unit_chars, engine.config.task_chars = 38_000, 40_000, 45_000
    big = "\n".join("x" * 1400 for _ in range(25))
    records.extend([replace(make(big, key="a", session="one"), recorded_at="2026-09-22T10:00:00Z"),
                    replace(make(CASES[4][0], key="b", session="two"), recorded_at="2026-09-23T10:00:00Z")])
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "partial" and result["skipped_units"] == 1 and result["completed_units"] == 1
    (entry,) = store.unit_failures().values()
    assert entry["kind"] == "input_budget"


def test_a_runner_error_an_unknown_error_and_a_broken_answer_after_a_retry_stop_the_run(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    class Raises(FixtureRunner):
        def __init__(self, error):
            super().__init__()
            self.error = error
        def run(self, task, schema, cancel):
            raise self.error
    for error in (FlowError("codex run failed (exit 1): login"), RuntimeError("surprise"), BrokenOutput("not json")):
        result = engine.analyze(lambda error=error: Raises(error))
        assert result["status"] == "failed" and store.unit_failures() == {}, error
        assert store.graph()["version"] == 0
    # A runner error and an unknown error cost one call each; an unreadable answer is asked for once more.
    assert [call["status"] for call in store.llm_calls()].count("failed") == 4


def test_an_unreadable_answer_is_asked_for_once_more_as_a_call_of_its_own(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0], key="a", session="one"))
    class BrokenOnce(FixtureRunner):
        broken = True
        def run(self, task, schema, cancel):
            if task["stage"] == "extract" and BrokenOnce.broken:
                BrokenOnce.broken = False
                self.calls += 1
                raise BrokenOutput("Claude's final structured JSON could not be read.")
            return super().run(task, schema, cancel)
    runner = BrokenOnce()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete" and result["runner_calls"] == 3  # broken extract, extract, integrate
    statuses = [(call["stage"], call["status"]) for call in store.llm_calls()]
    assert statuses[:2] == [("extract", "failed"), ("extract", "complete")]
    # A cap smaller than the retry needs ends the run as the cap, not as a failure of the unit.
    assert store.unit_failures() == {}


def test_a_unit_of_a_session_that_has_notes_since_is_superseded_not_held(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing_integration(CASES[0][0]))  # A: extracted, then failed in integration
    (unit_id,) = store.unit_failures()
    assert store.unit(unit_id)["status"] == "extracted"
    store.note_session("one")
    store.acknowledge_journaled(records)
    result = engine.analyze(lambda: pytest.fail("the agent writes that session now"))
    assert result["status"] == "noop"
    assert store.unit(unit_id)["status"] == "superseded" and store.unit_failures() == {}


def test_status_counts_skipped_units_apart(laboratory, tmp_path):
    scope, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.analyze(failing(CASES[0][0]))
    result = check(scope, store, codex_home=tmp_path / "codex", claude_home=tmp_path / "claude")
    assert result["failed_units"] == 1
    assert result["pending"] == {"records": 0, "units": 0}  # its record is not "waiting" and it is not a pending unit
    result = {**result, "scanned_at": "2026-10-08T00:00:00Z", "since_scan": None}  # as after a scan
    assert "건너뛴" in summary(result)
    lines = "\n".join(status_lines(result, scope.folder))
    assert "--retry-failed" in lines and "contexttrail analyze" in lines
    # Fixed by a retry: no longer counted.
    engine.config.retry_failed = True
    engine.analyze(FixtureRunner)
    assert check(scope, store, codex_home=tmp_path / "codex", claude_home=tmp_path / "claude")["failed_units"] == 0


def test_with_too_few_calls_left_a_unit_is_not_started_and_nothing_is_a_failure(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    engine.config.max_calls = 1
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "call_limit" and runner.calls == 0 and result["completed_units"] == 0
    assert "Not enough AI calls" in result["stop_reason"] or "AI 호출" in result["stop_reason"]
    assert store.unit_failures() == {} and store.graph()["version"] == 0
    # Three calls carry the first unit (extract, integrate) and leave one, short of the second's two.
    engine.config.max_calls = 3
    runner = FixtureRunner()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "partial" and result["completed_units"] == 1 and runner.calls == 2


def test_a_stop_at_the_call_cap_exits_like_a_partial_run_which_the_hook_does_not_count(tmp_path, capsys):
    from contexttrail import auto_update
    from contexttrail.git_context import Scope
    from contexttrail.store import Store
    from contexttrail.util import dumps
    folder, claude = tmp_path / "app", tmp_path / "claude"
    folder.mkdir()
    path = claude / "projects" / "app" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(dumps({"type": "user", "uuid": "u1", "sessionId": "s1", "cwd": str(folder),
                           "timestamp": "2026-10-07T10:00:00Z",
                           "message": {"content": [{"type": "text", "text": CASES[0][0]}]}}) + "\n")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    store.set_meta("options", {"codex_home": str(tmp_path / "codex"), "claude_home": str(claude),
                               "opencode_home": str(tmp_path / "opencode")})
    store.set_meta("demo", True)  # the mock runner stands in for a model
    capsys.readouterr()
    code = main(["analyze", str(folder), "--no-tui", "--brief", "--max-calls", "1"])
    out = capsys.readouterr().out
    assert code == 2 and "call_limit" in out and "AI 호출" in out
    # The hook records exit 2 as a run that went fine; three failed ones in a row (exit 1) would stop it.
    for _ in range(auto_update.FAILURE_STOP):
        auto_update.record_start(store)
        auto_update.record_finish(store, code)
    assert not auto_update.failing(store.get_meta(auto_update.STATE_KEY))
    for _ in range(auto_update.FAILURE_STOP):
        auto_update.record_start(store)
        auto_update.record_finish(store, 1)
    assert auto_update.failing(store.get_meta(auto_update.STATE_KEY))


def test_only_unreadable_json_is_a_broken_output_not_a_cli_refusal():
    import json
    from contexttrail.runners.cli_runner import parse_claude_output, parse_codex_output
    from contexttrail.util import BrokenOutput
    with pytest.raises(BrokenOutput):
        parse_claude_output(json.dumps({"result": "this is not json"}))
    with pytest.raises(BrokenOutput):
        parse_claude_output(json.dumps({"structured_output": ["not", "an", "object"]}))
    with pytest.raises(FlowError) as refused:
        parse_claude_output(json.dumps({"is_error": True, "result": "login needed"}))
    assert not isinstance(refused.value, BrokenOutput)
    lines = "\n".join(json.dumps(item) for item in (
        {"type": "item.completed", "item": {"type": "agent_message", "text": "{broken"}},
        {"type": "turn.completed", "usage": {}}))
    with pytest.raises(BrokenOutput):
        parse_codex_output(lines)


def test_the_runner_is_checked_without_a_model_before_the_first_unit(laboratory):
    _, store, engine, records, make = laboratory
    two_sessions(records, make)
    class Broken(FixtureRunner):
        def preflight(self):
            raise FlowError("bubblewrap is missing")
    consented = []
    result = engine.analyze(Broken, consent=lambda snapshot, plan: consented.append(plan) or True)
    assert result["status"] == "failed" and "bubblewrap" in result["error"]
    assert result["runner_calls"] == 0 and len(consented) == 1  # asked first: no runner is built before consent
    # Declined, or cancelled before it starts: no runner is built at all.
    result = engine.analyze(lambda: pytest.fail("built"), consent=lambda snapshot, plan: False)
    assert result["status"] == "cancelled"


def test_a_unit_failing_in_a_parallel_batch_is_skipped_and_its_neighbour_completes(laboratory):
    _, store, engine, records, make = laboratory
    engine.config.extract_workers = 2
    two_sessions(records, make)
    result = engine.analyze(failing(CASES[0][0]))
    assert result["status"] == "partial" and result["completed_units"] == 1 and result["skipped_units"] == 1
    assert [e["title"] for e in store.graph()["events"]] == [CASES[4][2]]
