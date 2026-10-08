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
        row(folder, 3, "user", [{"type": "tool_result", "tool_use_id": "t1", "content": CASES[3][0]}]),
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
    config.setdefault("integrate_output", "draft")  # the default, which the suite's fixture otherwise overrides
    return Engine(scope, store, AnalysisConfig(**homes, audit=True, **config))


def titles(graph):
    return {event["title"] for event in graph["events"]}


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


def test_an_audit_adds_only_what_the_notes_left_out(project):
    _, scope, store, homes = project
    first, second = write_notes(scope, store, homes)
    before = store.graph()
    noted = {event["id"]: copy.deepcopy(event) for event in before["events"]}
    runner = FixtureRunner()
    result = engine_for(scope, store, homes).analyze(lambda: runner)
    assert result["status"] == "complete", result
    graph = store.graph()
    # The story's other steps are new; the two noted steps are not written a second time.
    added = [event for event in graph["events"] if event["id"] not in noted]
    assert {event["title"] for event in added} == {CASES[n][2] for n in (1, 4, 5, 6)}
    assert all(event["origin"] == "audit" for event in added)
    assert CASES[0][2] not in titles(graph) and sum(e["title"] == "JSON storage adopted" for e in graph["events"]) == 1
    # Nothing that existed was changed, and the notes keep their origin.
    after = {event["id"]: event for event in graph["events"]}
    assert all(after[i] == event for i, event in noted.items())
    assert {e["id"] for e in graph["events"] if e.get("origin") == "note"} == {first["id"], second["id"]}
    assert all(edge.get("origin") for edge in graph["edges"] if edge["id"] not in {x["id"] for x in before["edges"]})
    # The extraction was shown the notes, and the audit rules ride on the request only then.
    extract = next(task for task in runner.tasks if task["stage"] == "extract")
    named = extract["data"]["journal_audit"]["note_event_ids"]  # the request carries short IDs (E1, E2)
    assert len(named) == 2 and set(named) <= {e["id"] for e in extract["data"]["existing_events"]}
    assert extract["instructions"].endswith(prompt("audit"))
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


def test_an_audit_unit_left_unfinished_is_not_taken_up_by_ordinary_analysis(project):
    _, scope, store, homes = project
    write_notes(scope, store, homes)

    class FailIntegration(FixtureRunner):
        def run(self, task, schema, cancel):
            if task["stage"] == "integrate":
                self.calls += 1
                raise FlowError("intentional integration failure")
            return super().run(task, schema, cancel)
    assert engine_for(scope, store, homes).analyze(FailIntegration)["status"] == "failed"
    stored = [u for u in store.units() if u["id"].startswith("audit_")]
    assert [u["status"] for u in stored] == ["extracted"]
    ordinary = Engine(scope, store, AnalysisConfig(**homes))
    runner = FixtureRunner()
    assert ordinary.analyze(lambda: runner)["status"] in {"noop", "complete"} and runner.calls == 0
    # The next audit reuses the paid extraction and only integrates.
    again = FixtureRunner()
    result = engine_for(scope, store, homes).analyze(lambda: again)
    assert result["status"] == "complete" and result["reused_extractions"] == 1
    assert [task["stage"] for task in again.tasks] == ["integrate"]


def rewriting_runner(note_id: str, *, once: bool):
    from contexttrail.schema import EVENT_FIELDS

    class RewritesTheNote(FixtureRunner):
        """An integrator that updates the agent's note, on its first answer only or on every one."""
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "integrate" and not (once and task.get("repair")):
                citation = next(iter(task["data"]["candidate_evidence"].values()))
                update = {"id": note_id, "reason": "better",
                          "evidence": [{"source_id": citation["source_id"], "start_line": citation["start_line"],
                                        "end_line": citation["end_line"], "quote": "x" * 8}],
                          "changes": {**{name: None for name in EVENT_FIELDS}, "title": "Rewritten"}}
                # Asked for a patch it answers one, else the whole delta.
                (output["patch"] if "patch" in schema["properties"] else output)["events_to_update"] = [update]
            self.last = task
            return output
    return RewritesTheNote


@pytest.mark.parametrize("mode", ["draft", "full"])
def test_an_audit_cannot_change_what_exists_and_a_rewrite_is_sent_back_once(project, mode):
    _, scope, store, homes = project
    first, _ = write_notes(scope, store, homes)
    runner = rewriting_runner(first["id"], once=True)()
    result = engine_for(scope, store, homes, integrate_output=mode).analyze(lambda: runner)
    assert result["status"] == "complete", result
    assert runner.last.get("repair") and "changes nothing that exists" in runner.last["repair"]["instruction"]
    assert next(e for e in store.graph()["events"] if e["id"] == first["id"]) == first


@pytest.mark.parametrize("mode", ["draft", "full"])
def test_a_stubborn_rewrite_of_a_note_fails_the_unit_and_changes_nothing(project, mode):
    _, scope, store, homes = project
    first, _ = write_notes(scope, store, homes)
    version = store.graph()["version"]
    result = engine_for(scope, store, homes, integrate_output=mode).analyze(rewriting_runner(first["id"], once=False))
    assert result["status"] == "failed"
    graph = store.graph()
    assert graph["version"] == version
    assert next(e for e in graph["events"] if e["id"] == first["id"]) == first
    assert not [u for u in store.units() if u["status"] == "integrated"]


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


def test_a_candidate_repeating_a_note_exactly_is_settled_as_its_duplicate(project):
    _, scope, store, homes = project
    first, second = write_notes(scope, store, homes)

    class IgnoresTheNotes(FixtureRunner):
        """A model that writes the noted failing run and decision again, on the very same lines."""
        def run(self, task, schema, cancel):
            if task["stage"] != "extract":
                return super().run(task, schema, cancel)
            data = task["data"]
            self.calls += 1
            self.tasks.append(copy.deepcopy(task))
            candidates = []
            for record in data["new_records"]:
                line = "\n".join(item["text"] for item in record["lines"])
                for n in (0, 3):
                    if line == CASES[n][0]:
                        _, kind, title, status, actor, basis = CASES[n]
                        candidates.append({"id": f"tmp:{n}", "kind": kind, "title": title + " (again)", "summary": line,
                                           "status": status, "actor": actor, "basis": basis,
                                           "session_ids": [record["session_id"]], "worktree_ids": [],
                                           "recorded_at": record["recorded_at"], "occurred_at": None,
                                           "evidence": [{"source_id": record["source_id"], "start_line": 1,
                                                         "end_line": len(record["lines"]), "quote": line}]})
            return {"status": "complete", "read_requests": [], "snapshot_id": data["snapshot_id"],
                    "unit_id": data["unit_id"], "event_candidates": candidates, "edge_candidates": [],
                    "existing_event_matches": [], "open_items": [], "limitations": [], "unprocessed_record_ids": []}
    runner = IgnoresTheNotes()
    result = engine_for(scope, store, homes).analyze(lambda: runner)
    assert result["status"] == "complete", result
    graph = store.graph()
    assert not any("(again)" in event["title"] for event in graph["events"])
    assert {e["id"] for e in graph["events"] if e.get("origin") == "note"} == {first["id"], second["id"]}
    assert not any(e.get("origin") == "audit" for e in graph["events"])


def test_an_edit_no_event_cites_is_still_required_and_one_a_note_cites_is_not():
    required = {"call_a": ("Edit a.py", "result_a"), "call_b": ("Edit b.py", "result_b")}
    saved = {"ev1": {"source_id": "result_a"}, "ev2": {"source_id": "other"}}
    assert uncited_edits(required, saved) == {"call_b": ("Edit b.py", "result_b")}


def test_the_audit_rules_are_sent_only_with_an_audit():
    data = {"unit_id": "u"}
    assert build_task("extract", data)["instructions"] == prompt("extract")
    assert build_task("integrate", data)["instructions"] == prompt("integrate")
    audited = build_task("extract", {**data, "journal_audit": {"note_event_ids": []}})
    assert audited["instructions"] == prompt("extract") + "\n\n" + prompt("audit")


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

    class FindsTheMissedEdit(FixtureRunner):
        def run(self, task, schema, cancel):
            if task["stage"] != "extract":
                return super().run(task, schema, cancel)
            self.calls += 1
            data = task["data"]
            call = next(step["call"] for step in data["tool_steps"] if step["target"].endswith("mul.py"))
            record = next(r for r in data["new_records"] if r["source_id"] == call)
            candidate = {"id": "tmp:mul", "kind": "action", "title": "Add mul()", "summary": "mul() was added",
                         "status": "applied", "actor": "assistant", "basis": "tool_record", "session_ids": [SESSION],
                         "worktree_ids": [], "recorded_at": record["recorded_at"], "occurred_at": None,
                         "evidence": [{"source_id": call, "start_line": 1, "end_line": len(record["lines"]),
                                       "quote": "def mul(a, b): return a * b"}]}
            return {"status": "complete", "read_requests": [], "snapshot_id": data["snapshot_id"],
                    "unit_id": data["unit_id"], "event_candidates": [candidate], "edge_candidates": [],
                    "existing_event_matches": [], "open_items": [], "limitations": [], "unprocessed_record_ids": []}

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
    # An audit whose extraction leaves the missed edit out does not pass.
    empty = FixtureRunner()
    assert engine_for(scope, store, homes).analyze(lambda: empty)["status"] == "failed"
    result = engine_for(scope, store, homes).analyze(FindsTheMissedEdit)
    assert result["status"] == "complete", result
    added = [e for e in store.graph()["events"] if e.get("origin") == "audit"]
    assert [e["title"] for e in added] == ["Add mul()"]


def test_the_cli_audits_one_noted_session_and_reports_it(project, capsys):
    folder, scope, store, homes = project
    write_notes(scope, store, homes)
    store.set_meta("demo", True)  # the mock runner stands in for a model, as for `contexttrail demo`
    capsys.readouterr()
    argv = ["analyze", str(folder), "--audit", "--session", SESSION[:6], "--no-tui", "--brief"]
    assert main(argv) == 0
    graph = store.graph()
    assert sum(e.get("origin") == "audit" for e in graph["events"]) == 4
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
