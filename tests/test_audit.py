"""`analyze --audit`: re-read the sessions an agent wrote notes in and add only what is missing (LIVE_JOURNAL §11)."""
import copy
import json
from pathlib import Path

import pytest

from contexttrail import journal
from contexttrail.analysis import AnalysisConfig, Engine, audit_guard, build_task, plan_text, prompt, uncited_edits
from contexttrail.cli import main
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.git_context import Scope
from contexttrail.store import Store
from contexttrail.util import FlowError, dumps

SESSION = "audit-sess"


def row(folder: Path, n: int, kind: str, content: list[dict], session: str = SESSION) -> dict:
    return {"type": kind, "uuid": f"{session}-u{n}", "sessionId": session, "cwd": str(folder),
            "timestamp": f"2026-10-07T10:{n:02d}:00Z", "message": {"content": content}}


def text(n_case: int) -> list[dict]:
    return [{"type": "text", "text": CASES[n_case][0]}]


def story(folder: Path) -> list[dict]:
    """The seven-step synthetic story as one Claude session: the notes will cover steps 0 and 3."""
    return [
        row(folder, 0, "user", text(0)),
        row(folder, 1, "assistant", text(1)),
        row(folder, 2, "assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]),
        row(folder, 3, "user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                 "content": "Error: Exit code 1\n" + CASES[3][0]}]),
        row(folder, 4, "user", text(4)),
        row(folder, 5, "assistant", text(5)),
        row(folder, 6, "assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}]),
        row(folder, 7, "user", [{"type": "tool_result", "tool_use_id": "t2", "content": CASES[6][0]}]),
    ]


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    path = claude / "projects" / "app" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(dumps(r) for r in story(folder)) + "\n", encoding="utf-8")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = dict(codex_home=tmp_path / "codex", claude_home=claude, opencode_home=tmp_path / "opencode")
    store.set_meta("options", {key: str(value) for key, value in homes.items()})
    journal.enable(store)
    return folder, scope, store, homes


def write_notes(scope, store, homes):
    """The agent noted the first decision and the failing run, and nothing else."""
    first = journal.write(scope, store, SESSION, kind="decision", title="JSON storage adopted", status="adopted",
                          actor="user", quotes=[CASES[0][0]], **homes)
    second = journal.write(scope, store, SESSION, kind="outcome", title="Concurrent write test failed",
                           status="observed_failure", quotes=[CASES[3][0]], **homes)
    return first["event"], second["event"]


def engine_for(scope, store, homes, **config) -> Engine:
    return Engine(scope, store, AnalysisConfig(**homes, audit=True, **config))


def test_an_audit_with_no_noted_session_has_nothing_to_read(project):
    _, scope, store, homes = project
    engine = engine_for(scope, store, homes)
    plan = engine.preview_plan(engine.scan())
    assert plan["units"] == 0
    assert plan_text(plan) == plan_text({"units": 0})
    with pytest.raises(FlowError, match="note가 없어"):
        engine_for(scope, store, homes, session=SESSION).preview_plan(engine.scan())


def test_an_audit_plans_the_noted_session_in_units_of_its_own(project):
    _, scope, store, homes = project
    write_notes(scope, store, homes)
    ordinary = Engine(scope, store, AnalysisConfig(**homes))
    assert ordinary.preview_plan(ordinary.scan())["units"] == 0  # the notes took the session out of analysis
    engine = engine_for(scope, store, homes)
    plan = engine.preview_plan(engine.scan())
    assert plan["units"] == 1 and plan["audit"] is True and plan["sessions"] == 1
    assert plan_text(plan).startswith("감사:")
    units, waiting, missing = engine._plan_units(engine.scan(), [])
    assert units[0]["id"].startswith("audit_") and missing == []
    assert len(units[0]["sources"]) == len(waiting) == 8


# What an audit of the story adds to the notes: the assistant's proposal and report (the story) and the passing run (the
# work code finds). The person's two messages have their events already (the first is the noted decision, the second
# the request the notes' publish made), and the failing run is noted.
AUDIT_ADDS = {CASES[1][2], CASES[5][2], "검사 통과: pytest -q"}


def test_an_audit_adds_only_what_the_notes_left_out(project):
    _, scope, store, homes = project
    first, second = write_notes(scope, store, homes)
    before = store.graph()
    noted = {event["id"]: copy.deepcopy(event) for event in before["events"]}
    runner = FixtureRunner()
    result = engine_for(scope, store, homes).analyze(lambda: runner)
    assert result["status"] == "complete", result
    graph = store.graph()
    # The story's other steps are new; the noted steps and the person's messages are not written a second time.
    added = [event for event in graph["events"] if event["id"] not in noted]
    assert {event["title"] for event in added} == AUDIT_ADDS
    assert all(event["origin"] == "audit" for event in added)
    assert not any(event["title"] == CASES[0][0] for event in added)  # the noted decision's message
    assert sum(e["title"] == "JSON storage adopted" for e in graph["events"]) == 1
    # Nothing that existed was changed, and the notes keep their origin.
    after = {event["id"]: event for event in graph["events"]}
    assert all(after[i] == event for i, event in noted.items())
    assert {e["id"] for e in graph["events"] if e.get("origin") == "note"} == {first["id"], second["id"]}
    assert all(edge.get("origin") for edge in graph["edges"] if edge["id"] not in {x["id"] for x in before["edges"]})
    # The story was shown the notes and what each of them cites, and the audit rules ride on the request only then.
    [task] = runner.tasks
    named = task["data"]["journal_audit"]["note_event_ids"]  # the request carries short IDs (E1, E2)
    assert len(named) == 2 and set(named) <= {e["id"] for e in task["data"]["existing_events"]}
    marked = [w.get("noted_by") for w in task["data"]["work"]] + [b.get("noted_by") for b in task["data"]["written"]]
    assert [m for m in marked if m] in ([[named[0]], [named[1]]], [[named[1]], [named[0]]])
    assert next(w for w in task["data"]["work"] if w.get("noted_by"))["status"] == "observed_failure"
    assert task["system"].endswith(prompt("audit"))
    # An audit says nothing about the records an ordinary analysis still has to read.
    assert graph.get("analysis_status") == before.get("analysis_status")
    assert graph.get("coverage") == before.get("coverage")
    assert "out_of_order_events" not in graph


def test_a_second_audit_reads_nothing_and_the_ordinary_plan_never_sees_audit_units(project):
    _, scope, store, homes = project
    write_notes(scope, store, homes)
    engine_for(scope, store, homes).analyze(FixtureRunner)
    version = store.graph()["version"]
    again = FixtureRunner()
    result = engine_for(scope, store, homes).analyze(lambda: again)
    assert result["status"] == "noop" and again.calls == 0 and store.graph()["version"] == version
    ordinary = Engine(scope, store, AnalysisConfig(**homes))
    assert ordinary.preview_plan(ordinary.scan())["units"] == 0
    # A note written after the audit changes the session; only the records the audit did not read are read.
    path = homes["claude_home"] / "projects" / "app" / f"{SESSION}.jsonl"
    with path.open("a") as handle:
        handle.write(dumps(row(scope.folder, 8, "user", [{"type": "text", "text": "Now add a retry."}])) + "\n")
    engine = engine_for(scope, store, homes)
    units, _, _ = engine._plan_units(engine.scan(), [])
    assert len(units) == 1 and len(units[0]["sources"]) == 1


def test_an_audit_unit_left_unfinished_is_not_taken_up_by_ordinary_analysis(project, monkeypatch):
    _, scope, store, homes = project
    write_notes(scope, store, homes)
    publish = Engine._integrate_unit

    def fails_once(self, *args, **kwargs):
        monkeypatch.setattr(Engine, "_integrate_unit", publish)
        raise FlowError("intentional publish failure")
    monkeypatch.setattr(Engine, "_integrate_unit", fails_once)
    assert engine_for(scope, store, homes).analyze(FixtureRunner)["status"] == "failed"
    stored = [u for u in store.units() if u["id"].startswith("audit_")]
    assert [u["status"] for u in stored] == ["extracted"]
    ordinary = Engine(scope, store, AnalysisConfig(**homes))
    runner = FixtureRunner()
    assert ordinary.analyze(lambda: runner)["status"] in {"noop", "complete"} and runner.calls == 0
    # The next audit reuses the paid story call and only publishes.
    again = FixtureRunner()
    result = engine_for(scope, store, homes).analyze(lambda: again)
    assert result["status"] == "complete" and result["reused_extractions"] == 1 and again.calls == 0


def test_a_draft_that_would_change_a_note_fails_the_unit_and_changes_nothing(project, monkeypatch):
    from contexttrail import analysis
    from contexttrail.schema import EVENT_FIELDS
    _, scope, store, homes = project
    first, _ = write_notes(scope, store, homes)
    version = store.graph()["version"]
    draft = analysis.draft_delta

    def rewrites_the_note(output, *args):
        delta = draft(output, *args)
        delta["events_to_update"] = [{"id": first["id"], "reason": "better", "evidence": [],
                                      "changes": {**{name: None for name in EVENT_FIELDS}, "title": "Rewritten"}}]
        return delta
    monkeypatch.setattr(analysis, "draft_delta", rewrites_the_note)
    result = engine_for(scope, store, homes).analyze(FixtureRunner)
    assert result["status"] == "failed"
    graph = store.graph()
    assert graph["version"] == version
    assert next(e for e in graph["events"] if e["id"] == first["id"]) == first
    assert not [u for u in store.units() if u["status"] == "integrated"]
    assert any("changes nothing that exists" in failure.get("message", "") for failure in store.unit_failures().values())
    # The unit failed on this input, so it is held back until asked for (or the input changes).
    monkeypatch.setattr(analysis, "draft_delta", draft)
    held = FixtureRunner()
    assert engine_for(scope, store, homes).analyze(lambda: held)["status"] == "noop" and held.calls == 0
    assert engine_for(scope, store, homes, retry_failed=True).analyze(FixtureRunner)["status"] == "complete"
    assert next(e for e in store.graph()["events"] if e["id"] == first["id"]) == first


def test_the_guard_allows_new_items_and_refuses_changes_to_existing_ones(project):
    _, scope, store, homes = project
    write_notes(scope, store, homes)
    graph = store.graph()
    empty = {"events_to_update": [], "edges_to_invalidate": [], "open_items_to_resolve": [], "open_items_to_upsert": []}
    audit_guard({**empty, "open_items_to_upsert": [{"id": "new"}]}, graph)
    for key in ("events_to_update", "edges_to_invalidate", "open_items_to_resolve"):
        with pytest.raises(FlowError, match="changes nothing that exists"):
            audit_guard({**empty, key: [{"id": "x"}]}, graph)
    graph["open_items"] = [{"id": "open_1"}]
    with pytest.raises(FlowError, match="only add new open items"):
        audit_guard({**empty, "open_items_to_upsert": [{"id": "open_1"}]}, graph)


def test_a_story_repeating_a_note_is_settled_as_its_duplicate(project):
    _, scope, store, homes = project
    first, second = write_notes(scope, store, homes)

    class IgnoresTheNotes(FixtureRunner):
        """A model that writes the noted decision again on the noted block and names the noted failing run anew."""
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            data = task["data"]
            block = next(b["block"] for b in data["written"] if b.get("noted_by"))
            output["story"] = [{"id": "N1", "kind": "decision", "status": "adopted", "title": CASES[0][2] + " (again)",
                                "why": None, "summary": "", "blocks": [block], "work": []}]
            output["requests"] = [{"id": r["id"], "answered_by": []} for r in data["user_requests"]]
            for entry, item in zip(output["work"], data["work"]):
                if item.get("noted_by"):
                    entry["title"] = CASES[3][2] + " (again)"
            return output
    runner = IgnoresTheNotes()
    result = engine_for(scope, store, homes).analyze(lambda: runner)
    assert result["status"] == "complete", result
    graph = store.graph()
    assert not any("(again)" in event["title"] for event in graph["events"])
    assert {e["id"] for e in graph["events"] if e.get("origin") == "note"} == {first["id"], second["id"]}
    added = [e for e in graph["events"] if e.get("origin") == "audit"]
    assert not any(e["kind"] == "decision" or e["status"] == "observed_failure" for e in added)


def test_an_edit_no_event_cites_is_still_required_and_one_a_note_cites_is_not():
    required = {"call_a": ("Edit a.py", "result_a"), "call_b": ("Edit b.py", "result_b")}
    saved = {"ev1": {"source_id": "result_a"}, "ev2": {"source_id": "other"}}
    assert uncited_edits(required, saved) == {"call_b": ("Edit b.py", "result_b")}


def test_the_audit_rules_are_sent_only_with_an_audit():
    data = {"unit_id": "u"}
    assert build_task("story", data)["system"] == prompt("story")
    audited = build_task("story", {**data, "journal_audit": {"note_event_ids": []}})
    assert audited["system"] == prompt("story") + "\n\n" + prompt("audit")


def test_every_note_of_the_units_records_is_shown_past_the_context_limit(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    path = claude / "projects" / "app" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [row(folder, n, "assistant", [{"type": "text", "text": f"Decision number {n}: keep module {n} as it is."}])
            for n in range(16)]
    path.write_text("\n".join(dumps(r) for r in rows) + "\n", encoding="utf-8")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = dict(codex_home=tmp_path / "codex", claude_home=claude, opencode_home=tmp_path / "opencode")
    store.set_meta("options", {key: str(value) for key, value in homes.items()})
    journal.enable(store)
    for n in range(16):
        journal.write(scope, store, SESSION, kind="decision", title=f"Keep module {n}", status="adopted",
                      quotes=[f"Decision number {n}: keep module {n} as it is."], **homes)
    engine = engine_for(scope, store, homes)
    snapshot = engine.scan()
    units, _, _ = engine._plan_units(snapshot, [])
    from contexttrail.routing import RunnerPool
    prepared = engine._prepare_extraction(units[0], {r.source_id: r for r in snapshot.records}, store.graph(),
                                          snapshot.id, "run_t", RunnerPool(FixtureRunner, engine.config),
                                          __import__("threading").Event(), [])
    shown = {event["id"] for event in prepared.data["existing_events"]}
    named = set(prepared.data["journal_audit"]["note_event_ids"])
    assert len(named) == 16 and named <= shown


def test_the_cli_previews_an_audit_without_a_model_and_refuses_it_for_a_hook(project, capsys):
    folder, scope, store, homes = project
    write_notes(scope, store, homes)
    capsys.readouterr()
    assert main(["scan", str(folder), "--audit"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["runner_calls"] == 0 and printed["plan"]["audit"] is True and printed["plan"]["units"] == 1
    assert printed["plan_text"].startswith("감사:")
    with pytest.raises(FlowError, match="감사"):
        AnalysisConfig(audit=True, trigger="hook").validate()


def edit_session(folder: Path) -> list[dict]:
    def edit(n, call, name, new):
        return [row(folder, n, "assistant", [{"type": "tool_use", "id": call, "name": "Edit", "input": {
                    "file_path": str(folder / name), "old_string": "pass", "new_string": new}}]),
                row(folder, n + 1, "user", [{"type": "tool_result", "tool_use_id": call,
                                              "content": f"The file {name} has been updated."}])]
    return [row(folder, 0, "user", [{"type": "text", "text": "Add add() and mul() helpers."}]),
            *edit(1, "t1", "add.py", "def add(a, b): return a + b"),
            *edit(3, "t2", "mul.py", "def mul(a, b): return a * b")]


def test_an_edit_the_notes_missed_must_still_be_cited_and_a_noted_one_need_not_be(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    path = claude / "projects" / "app" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(dumps(r) for r in edit_session(folder)) + "\n", encoding="utf-8")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = dict(codex_home=tmp_path / "codex", claude_home=claude, opencode_home=tmp_path / "opencode")
    store.set_meta("options", {key: str(value) for key, value in homes.items()})
    journal.enable(store)
    journal.write(scope, store, SESSION, kind="action", title="Add add()", quotes=["def add(a, b): return a + b"], **homes)

    engine = engine_for(scope, store, homes)
    snapshot = engine.scan()
    units, _, _ = engine._plan_units(snapshot, [])
    from contexttrail.routing import RunnerPool
    import threading
    prepared = engine._prepare_extraction(units[0], {r.source_id: r for r in snapshot.records}, store.graph(),
                                          snapshot.id, "run_t", RunnerPool(FixtureRunner, engine.config),
                                          threading.Event(), [])
    # Only the edit no event cites is demanded; the ordinary analysis would demand both.
    missed = next(step["call"] for step in prepared.data["tool_steps"] if step["target"].endswith("mul.py"))
    assert set(prepared.validator.required_citations) == {missed}
    # The code's work record holds both edits; after the audit an event it added cites the one the notes missed.
    result = engine_for(scope, store, homes).analyze(FixtureRunner)
    assert result["status"] == "complete", result
    added = [e for e in store.graph()["events"] if e.get("origin") == "audit"]
    cited = store.evidence_many([i for event in added for i in event["evidence_ids"]])
    assert missed in {item["source_id"] for item in cited.values()}


def test_the_cli_audits_one_noted_session_and_reports_it(project, capsys):
    folder, scope, store, homes = project
    write_notes(scope, store, homes)
    store.set_meta("demo", True)  # the mock runner stands in for a model, as for `contexttrail demo`
    capsys.readouterr()
    argv = ["analyze", str(folder), "--audit", "--session", SESSION[:6], "--no-tui", "--brief"]
    assert main(argv) == 0
    graph = store.graph()
    assert {e["title"] for e in graph["events"] if e.get("origin") == "audit"} == AUDIT_ADDS
    assert main(argv) == 0  # nothing left to read
    assert "noop" in capsys.readouterr().out
    # A session without notes is refused, not analyzed.
    other = homes["claude_home"] / "projects" / "app" / "plain.jsonl"
    other.write_text(dumps(row(folder, 0, "user", [{"type": "text", "text": "hello there"}], session="plain")) + "\n")
    assert main(["analyze", str(folder), "--audit", "--session", "plain", "--no-tui", "--brief"]) == 1


def test_status_and_find_say_how_many_noted_sessions_have_not_been_audited(project, capsys):
    from contexttrail.freshness import check, status_lines, summary
    folder, scope, store, homes = project
    assert check(scope, store, **homes)["unaudited"] == {"sessions": 0, "records": 0}
    write_notes(scope, store, homes)
    engine = engine_for(scope, store, homes)
    engine.scan()  # saves the scan index the status reads; the notes already stored their session's records
    result = check(scope, store, **homes)
    assert result["unaudited"] == {"sessions": 1, "records": 8}
    assert "감사" in summary(result) and "1" in summary(result)
    assert any("--audit" in line for line in status_lines(result, folder))
    capsys.readouterr()
    assert main(["status", str(folder), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unaudited"] == {"sessions": 1, "records": 8}
    # An audit covers them; a session that grows has only its new records left.
    engine.analyze(FixtureRunner)
    after = check(scope, store, **homes)
    assert after["unaudited"] == {"sessions": 0, "records": 0}
    assert "감사" not in summary(after) and not any("--audit" in line for line in status_lines(after, folder))
