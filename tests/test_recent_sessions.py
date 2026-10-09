"""`--recent N` / `--since WHEN`: the latest sessions first, ahead of the oldest-first order
(docs/plans/RECENT_SESSIONS_FIRST.md). Synthetic records and the mock runner only."""
import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from contexttrail import journal
from contexttrail.analysis import AnalysisConfig, Engine, plan_text, since_cutoff
from contexttrail.cli import _options, main
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.store import Store
from contexttrail.util import FlowError, dumps

from test_unit_failures import failing


PICK = (0, 1, 4, 5)  # the cases a person or an assistant says (the rest are tool results)
ROLE = {"user": "user", "assistant": "assistant"}


def case(n):
    return CASES[PICK[n % len(PICK)]]


def sessions(records, make, count=4, *, parents=None):
    """`count` sessions, one record each, session n on day n+1 of October (oldest first)."""
    for n in range(count):
        record = replace(make(case(n)[0], key=f"r{n}", session=f"s{n}", role=ROLE[case(n)[4]]),
                         recorded_at=f"2026-10-{n + 1:02d}T10:00:00Z")
        if parents and f"s{n}" in parents:
            record = replace(record, lineage={"kind": "subagent", "parent_session_id": parents[f"s{n}"]})
        records.append(record)


def processed(store):
    return {i for i, row in store.sources().items() if row["processed_hash"]}


def test_the_latest_sessions_are_analysed_and_the_rest_wait(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    engine.config.recent = 2
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "partial" and result["pending_records"] == 2
    assert processed(store) == {"r2", "r3"}
    graph = store.graph()
    assert graph["analysis_status"] == "partial"
    # What it added is marked: it was analysed ahead of the two older sessions.
    assert graph["out_of_order_events"] and set(graph["out_of_order_events"]) == {e["id"] for e in graph["events"]}
    # A later ordinary run fills in the rest in the usual order and keeps the mark on what came first.
    engine.config.recent = None
    assert engine.analyze(FixtureRunner)["status"] == "complete"
    later = store.graph()
    assert processed(store) == {"r0", "r1", "r2", "r3"}
    assert later["out_of_order_events"] == graph["out_of_order_events"] and len(later["events"]) > len(graph["events"])


def test_the_chosen_sessions_are_analysed_oldest_first(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    engine.config.recent = 3
    runner = FixtureRunner()
    engine.analyze(lambda: runner)
    order = [task["data"]["assigned_source_ids"] for task in runner.tasks if task["stage"] == "extract"]
    assert order == [["r1"], ["r2"], ["r3"]]


def test_a_unit_count_stops_inside_the_chosen_sessions(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    engine.config.recent, engine.config.max_units = 3, 1
    assert engine.analyze(FixtureRunner)["completed_units"] == 1
    assert processed(store) == {"r1"}  # the oldest of the three; the same selection continues with the next run
    engine.config.max_units = None
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r1", "r2", "r3"}


def test_a_sub_agent_follows_its_parent_in_age_and_in_the_run(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 4, parents={"s0": "s3"})  # s0 is s3's sub-agent although it is the oldest record
    engine.config.recent = 1
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r0", "r3"}


def test_a_session_is_as_recent_as_its_latest_sub_agent(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 3)
    records.append(replace(make(CASES[1][0], key="late", session="child", role="assistant"), recorded_at="2026-10-09T10:00:00Z",
                           lineage={"kind": "subagent", "parent_session_id": "s0"}))
    engine.config.recent = 1
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r0", "late"}


def test_a_session_whose_parent_is_not_in_the_project_counts_on_its_own(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 3, parents={"s2": "elsewhere"})
    engine.config.recent = 1
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r2"}


def test_a_claude_session_with_a_sub_agent_in_its_own_session_is_still_a_session(laboratory):
    # Claude Code keeps a sub-agent's records under its parent's session ID, naming that session as parent.
    _, store, engine, records, make = laboratory
    sessions(records, make, 3)
    records.append(replace(make(CASES[1][0], key="task", session="s2", role="assistant"), recorded_at="2026-10-03T11:00:00Z",
                           lineage={"kind": "subagent", "parent_session_id": "s2"}))
    engine.config.recent = 1
    plan = engine.preview_plan(engine.scan())
    assert [s["id"] for s in plan["selection"]["sessions"]] == ["s2"] and plan["selection"]["candidates"] == 3
    store.note_session("s2")
    plan = engine.preview_plan(engine.scan())
    assert [s["id"] for s in plan["selection"]["sessions"]] == ["s1"] and plan["selection"]["noted"] == 1


def test_since_takes_the_sessions_that_ended_after_it_and_recent_the_latest_of_them(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 5)
    engine.config.since = "2026-10-03T00:00:00Z"
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r2", "r3", "r4"}
    engine.config.recent = 1  # the latest of those that ended since: already done, so nothing new
    plan = engine.preview_plan(engine.scan())
    assert [s["id"] for s in plan["selection"]["sessions"]] == ["s4"] and plan["units"] == 0


def test_sessions_with_notes_are_left_out_and_do_not_use_up_the_count(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    store.note_session("s3")  # the newest session is written by its agent
    engine.config.recent = 2
    plan = engine.preview_plan(engine.scan())
    assert [s["id"] for s in plan["selection"]["sessions"]] == ["s1", "s2"]
    assert (plan["selection"]["candidates"], plan["selection"]["noted"]) == (3, 1)
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r1", "r2", "r3"}  # s3 was marked processed by the note, not analysed


def test_nothing_to_take_is_an_error_not_an_empty_run(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 2)
    engine.config.since = "2026-12-01T00:00:00Z"
    with pytest.raises(FlowError, match="No recent session to take|고를 최근 세션이 없습니다"):
        engine.preview_plan(engine.scan())
    store.note_session("s0")
    store.note_session("s1")
    engine.config.since = None
    engine.config.recent = 1
    result = engine.analyze(FixtureRunner)  # an error of the plan ends the run as failed, nothing sent
    assert result["status"] == "failed" and "제외한 note 세션 2개" in result["error"]
    assert result["runner_calls"] == 0 and store.graph()["version"] == 0


def test_the_plan_names_the_sessions_taken_first(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    engine.config.recent = 2
    plan = engine.preview_plan(engine.scan())
    assert plan["units"] == 2
    assert plan["selection"]["sessions"] == [{"id": "s2", "last": "2026-10-03T10:00:00Z", "units": 1},
                                             {"id": "s3", "last": "2026-10-04T10:00:00Z", "units": 1}]
    text = plan_text(plan)
    assert text.startswith("최근 세션 먼저: 2개 세션(2026-10-03 ~ 2026-10-04; s2, s3)")
    assert "나머지 세션은 나중의 일반 실행" in text
    # An ordinary plan says nothing of the kind, and the consent callback sees the same figures.
    engine.config.recent = None
    assert "selection" not in engine.preview_plan(engine.scan())
    seen = []
    engine.config.recent = 2
    engine.analyze(FixtureRunner, consent=lambda snapshot, plan: seen.append(plan) or True)
    assert seen[0]["selection"]["sessions"][0]["id"] == "s2"


def test_events_are_not_marked_out_of_order_when_the_choice_covers_everything_that_waits(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 2)
    engine.config.recent = 5  # more than there are: the oldest-first order is what happens anyway
    assert engine.analyze(FixtureRunner)["status"] == "complete"
    assert "out_of_order_events" not in store.graph()
    # `--session` on the last record of a project with nothing else waiting is in order too.
    records.append(replace(make(CASES[4][0], key="new", session="s9", role="user"), recorded_at="2026-10-09T10:00:00Z"))
    engine.config.recent, engine.config.session = None, "s9"
    engine.analyze(FixtureRunner)
    assert "out_of_order_events" not in store.graph()


def test_a_failing_unit_in_the_chosen_sessions_is_skipped_and_held_back(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 4)
    engine.config.recent = 3
    result = engine.analyze(failing(case(2)[0]))  # session s2
    assert result["status"] == "partial" and result["completed_units"] == 2 and result["skipped_units"] == 1
    assert processed(store) == {"r1", "r3"}
    (unit_id, entry), = store.unit_failures().items()
    assert entry["sources"] == ["r2"]
    # The same selection does not send it again; the failed unit does not count as waiting for this run.
    runner = FixtureRunner()
    again = engine.analyze(lambda: runner)
    assert again["status"] == "noop" or again["runner_calls"] == 0
    # An ordinary run later retries nothing either, until the input changes or --retry-failed.
    engine.config.recent = None
    engine.config.retry_failed = True
    engine.analyze(FixtureRunner)
    assert processed(store) == {"r0", "r1", "r2", "r3"} and store.unit_failures() == {}


def test_the_call_cap_still_stops_a_run_inside_the_chosen_sessions(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    engine.config.recent, engine.config.max_calls = 3, 4
    result = engine.analyze(FixtureRunner)
    assert result["status"] == "partial" and result["completed_units"] < 3
    assert processed(store) and processed(store) <= {"r1", "r2", "r3"}  # r0 is not in the chosen sessions


def test_a_stored_unit_of_another_session_is_left_alone(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make)
    snapshot = engine.scan()
    store.ingest(snapshot.records)
    store.save_unit("unit_old", ["r0"], {}, "parsed")
    engine.config.recent = 1
    plans, _, _ = engine._plan_units(snapshot, [], repair=True)
    assert [u["sources"] for u in plans] == [["r3"]]
    assert {u["id"]: u["status"] for u in store.units()}["unit_old"] == "parsed"


def test_the_options_do_not_mix_with_session_audit_or_the_hook():
    for kwargs in ({"session": "abc", "recent": 2}, {"session": "abc", "since": "2026-10-01T00:00:00Z"},
                   {"audit": True, "recent": 2}, {"audit": True, "since": "2026-10-01T00:00:00Z"},
                   {"trigger": "hook", "recent": 2}, {"trigger": "hook", "since": "2026-10-01T00:00:00Z"}):
        with pytest.raises(FlowError):
            Engine(None, None, AnalysisConfig(**kwargs))
    for kwargs in ({"recent": 0}, {"since": "not a time"}):
        with pytest.raises(FlowError):
            AnalysisConfig(**kwargs).validate()
    AnalysisConfig(recent=3, since="2026-10-01T00:00:00Z").validate()


def test_since_reads_an_age_or_a_date():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert since_cutoff("7d", now) == "2026-10-02T12:00:00Z"
    assert since_cutoff("36h", now) == "2026-10-08T00:00:00Z"
    assert since_cutoff("2w", now) == "2026-09-25T12:00:00Z"
    assert since_cutoff("2026-10-01", now) == "2026-10-01T00:00:00Z"
    assert since_cutoff("2026-10-01T09:00:00+09:00", now) == "2026-10-01T00:00:00Z"
    for bad in ("yesterday", "7", "-1d", ""):
        with pytest.raises(FlowError):
            since_cutoff(bad, now)


def test_the_command_line_options_reach_the_config(tmp_path):
    store = Store(tmp_path / "s", "x")
    args = argparse.Namespace(max_calls=None, max_units=None, session=None, recent=3, since="2026-10-01")
    config = _options(args, store)
    assert (config.recent, config.since) == (3, "2026-10-01T00:00:00Z")
    config = _options(argparse.Namespace(max_calls=None, max_units=None, session=None), store)
    assert (config.recent, config.since) == (None, None)  # per run, never saved
    assert "recent" not in (store.get_meta("options") or {})


def test_scan_previews_the_recent_sessions_without_a_model(tmp_path, capsys):
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    (claude / "projects" / "app").mkdir(parents=True)
    for n in range(3):
        rows = [{"type": "user", "uuid": f"s{n}-u0", "sessionId": f"sess{n}", "cwd": str(folder),
                 "timestamp": f"2026-10-0{n + 1}T10:00:00Z", "message": {"content": [{"type": "text", "text": case(n)[0]}]}}]
        (claude / "projects" / "app" / f"sess{n}.jsonl").write_text("\n".join(dumps(r) for r in rows) + "\n", encoding="utf-8")
    common = ["--claude-home", str(claude), "--codex-home", str(tmp_path / "codex"), "--opencode-home", str(tmp_path / "oc")]
    assert main(["scan", str(folder), *common, "--recent", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["runner_calls"] == 0
    assert [s["id"] for s in out["plan"]["selection"]["sessions"]] == ["sess1", "sess2"]
    assert out["plan_text"].startswith("최근 세션 먼저: 2개 세션")
    # Combined with --session it is refused rather than guessed at.
    assert main(["scan", str(folder), *common, "--recent", "2", "--session", "sess1"]) == 1
    assert "함께 쓸 수 없습니다" in capsys.readouterr().err
