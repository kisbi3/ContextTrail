"""A run bounded by work units, one session ahead of the order, and the saved graph read at a shell."""
import argparse
import copy
import curses
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from contexttrail import ui
from contexttrail.agent_commands import install_agent_commands
from contexttrail.agent_view import data_note, find, find_text, reference, show, show_text, short_id
from contexttrail.analysis import _estimated_tokens, calibration, plan_summary, plan_text, unit_cost
from contexttrail.cli import _options, _session
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.store import Store
from contexttrail.ui import FlowPanels
from contexttrail.util import FlowError

from test_diagram import story
from test_store import DrawnScreen


ROLE = {"user": "user", "assistant": "assistant", "tool": "tool_result"}


def three_sessions(records, make):
    """Three work units, oldest first: one per session."""
    for n, case in enumerate((CASES[0], CASES[1], CASES[5])):
        records.append(replace(make(case[0], key=f"s{n}", session=f"session-{n}", role=ROLE[case[4]]),
                               recorded_at=f"2026-09-22T1{n}:00:00Z"))


def test_a_chosen_unit_count_bounds_the_run_and_the_rest_waits(laboratory):
    _, store, engine, records, make = laboratory
    three_sessions(records, make)
    plans = []
    result = engine.analyze(FixtureRunner, consent=lambda snapshot, plan: plans.append(plan) or 1)
    assert (plans[0]["units"], [c["units"] for c in plans[0]["choices"]]) == (3, [3])
    assert (result["status"], result["completed_units"]) == ("partial", 1)
    assert store.graph()["analysis_status"] == "partial"  # records still wait: not complete
    assert [i for i, row in store.sources().items() if row["processed_hash"]] == ["s0"]  # oldest first
    assert engine.analyze(FixtureRunner)["status"] == "complete"
    assert store.graph()["analysis_status"] == "complete"


def test_a_unit_count_sets_the_call_cap_unless_one_was_given(laboratory, tmp_path):
    _, store, engine, records, make = laboratory
    three_sessions(records, make)
    engine.config.max_units = 2
    plans = []
    engine.analyze(lambda: pytest.fail("declined"), consent=lambda snapshot, plan: plans.append(plan) or False)
    assert (plans[0]["units_this_run"], plans[0]["max_calls"]) == (2, 12)
    config = _options(argparse.Namespace(max_calls=4, max_units=2, session=None), Store(tmp_path / "s", "x"))
    assert (config.max_units, config.max_calls, config.calls_fixed) == (2, 4, True)
    engine.config.max_calls, engine.config.calls_fixed = 4, True
    engine.analyze(lambda: pytest.fail("declined"), consent=lambda snapshot, plan: plans.append(plan) or False)
    assert plans[1]["max_calls"] == 4


def test_the_estimate_follows_this_projects_past_runs(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    pool = {r.source_id: r for r in records}
    units = [{"id": f"u{n}", "status": "integrated", "sources": ["s1"]} for n in range(3)]
    plain = plan_summary(units[:1], pool, 30)
    tokens = _estimated_tokens(unit_cost(records[0]))  # the estimate before any ratio
    calls = [{"unit_id": unit["id"], "details": {"usage": {"input_tokens": tokens}, "duration_ms": 60_000}}
             for unit in units for _ in range(2)]
    assert calibration(units[:2], calls, pool) is None  # two units are too few to go by
    past = calibration(units, calls, pool)
    assert past["units"] == 3 and past["token_ratio"] == pytest.approx(2.0, rel=0.01)
    scaled = plan_summary(units[:1], pool, 30, past=past)
    assert scaled["input_tokens_this_run"] == pytest.approx(2 * tokens, rel=0.01)
    assert "지난 3개 단위 기준" in plan_text(scaled) and "(추정)" in plan_text(plain)
    assert plain["input_tokens_this_run"] == pytest.approx(3 * tokens, rel=0.01)  # a unit's several calls
    # A call without a reported count leaves its unit out rather than guessing.
    assert calibration(units, calls + [{"unit_id": "u0", "details": {}}], pool) is None
    assert plan_text(plan_summary([], pool, 30)) == "대기 작업 단위 없음 · 보낼 것이 없습니다"
    # Claude counts the cached part apart and reports `input_tokens` as what was neither cached nor read
    # from the cache (often 2); every part is input the model read.
    claude = [{"unit_id": unit["id"], "details": {"usage": {
        "input_tokens": 2, "cache_creation_input_tokens": tokens - 402, "cache_read_input_tokens": 400},
        "duration_ms": 60_000}} for unit in units for _ in range(2)]
    assert calibration(units, claude, pool)["token_ratio"] == pytest.approx(2.0, rel=0.01)


def test_one_session_goes_ahead_with_its_sub_agents_and_its_events_say_so(laboratory):
    _, store, engine, records, make = laboratory
    records.append(replace(make(CASES[0][0], key="old", session="session-old"), recorded_at="2026-09-22T09:00:00Z"))
    records.append(replace(make(CASES[1][0], key="new", role="assistant", session="session-new"),
                           recorded_at="2026-09-22T11:00:00Z"))
    records.append(replace(make(CASES[5][0], key="sub", role="assistant", session="sub-agent"),
                           recorded_at="2026-09-22T11:05:00Z",
                           lineage={"kind": "subagent", "parent_session_id": "session-new"}))
    engine.config.session = "session-n"  # a prefix names it
    assert engine.analyze(FixtureRunner)["status"] == "partial"
    done = {i for i, row in store.sources().items() if row["processed_hash"]}
    assert done == {"new", "sub"}
    ahead = store.graph()["out_of_order_events"]
    assert ahead and set(ahead) == {event["id"] for event in store.graph()["events"]}
    engine.config.session = None
    engine.analyze(FixtureRunner)
    graph = store.graph()
    assert graph["out_of_order_events"] == ahead  # still said after the older records caught up
    assert len(graph["events"]) > len(ahead)
    engine.config.session = "nobody"
    with pytest.raises(FlowError, match="세션 nobody가 없습니다"):
        engine.preview_plan(engine.scan())


def test_a_preview_plans_without_a_model(laboratory):
    _, store, engine, records, make = laboratory
    three_sessions(records, make)
    plan = engine.preview_plan(engine.scan())
    assert (plan["units"], plan["basis"]) == (3, "estimate")
    assert store.llm_calls() == [] and store.graph()["version"] == 0


def test_current_session_comes_from_the_agent_running_the_command(monkeypatch):
    for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(FlowError, match="세션을 알 수 없습니다"):
        _session("current")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    assert _session("current") == "thread-1"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    with pytest.raises(FlowError, match="여러 도구의 세션이 보입니다"):
        _session("current")
    # Claude Code started from a Codex shell inherits CODEX_THREAD_ID; its own marker decides.
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _session("current") == "claude-1"
    # An opencode server started from Claude Code inherits its variables; the plugin's per-call one wins.
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_1")
    assert _session("current") == "ses_1"
    assert _session("abc") == "abc" and _session(None) is None


@pytest.fixture
def analysed(laboratory):
    _, store, engine, records, make = laboratory
    for n, case in enumerate(CASES[:6]):
        records.append(replace(make(case[0], key=f"s{n}", role=ROLE[case[4]]),
                               recorded_at=f"2026-09-22T10:0{n}:00Z"))
    engine.analyze(FixtureRunner)
    return store


def test_show_gives_one_event_with_its_links_and_quotes_fenced_as_data(analysed):
    graph = analysed.graph()
    event = next(e for e in graph["events"] if e["evidence_ids"])
    result = show(reference(graph, event["id"]), graph, analysed.evidence, analysed.graph)
    assert result["state"] == "current" and result["ai_calls"] == 0
    text = "\n".join(show_text(result))
    assert data_note() in text and "--- 원문 근거 1" in text and "--- 끝 ---" in text
    quote = analysed.evidence(event["evidence_ids"][0])["quote"].splitlines()[0]
    assert "> " + quote in text
    assert show(event["id"][:11], graph, analysed.evidence, analysed.graph)["event"]["id"] == event["id"]


def test_show_says_when_an_event_changed_or_went_since_the_copied_version(analysed):
    graph = analysed.graph()
    event = graph["events"][0]
    older = copy.deepcopy(graph)
    older["version"] = graph["version"] - 1 if graph["version"] > 1 else 1
    older["events"][0]["title"] = "예전 제목"
    older["events"].append({**copy.deepcopy(event), "id": "ev_" + "f" * 24, "title": "사라진 사건"})
    versions = {older["version"]: older}
    ref = f"{event['id'][:11]}@v{older['version']}"
    current = copy.deepcopy(graph)
    current["version"] = older["version"] + 1
    result = show(ref, current, analysed.evidence, versions.__getitem__)
    assert result["state"] == "changed" and "제목: 예전 제목" in result["changes_since"][0]
    gone = show(f"ev_ffffffff@v{older['version']}", current, analysed.evidence, versions.__getitem__)
    assert gone["state"] == "gone" and "사라졌습니다" in "\n".join(show_text(gone))
    with pytest.raises(FlowError, match="현재 그래프"):
        show("ev_ffffffff", current, analysed.evidence, versions.__getitem__)


def test_find_searches_titles_and_quotes_and_lists_the_newest_without_words(analysed):
    graph = analysed.graph()
    hit = find(graph, "SQLite", analysed.evidence_many)
    assert hit["matches"] >= 2 and all("SQLite" in row["title"] for row in hit["events"])
    assert find(graph, "없는단어", analysed.evidence_many)["matches"] == 0
    overview = find(graph, "", analysed.evidence_many, limit=2)
    assert len(overview["events"]) == 2 and overview["matches"] == len(graph["events"])
    assert overview["events"][0]["when"] >= overview["events"][1]["when"]
    assert "AI 호출 없음" in find_text(overview)[0]


def test_short_ids_stay_unique():
    graph = {"version": 3, "events": [{"id": "ev_aaaaaaaa1111"}, {"id": "ev_aaaaaaaa2222"}, {"id": "ev_bbbbbbbb0000"}]}
    assert short_id(graph, "ev_aaaaaaaa1111") == "ev_aaaaaaaa1"
    assert reference(graph, "ev_bbbbbbbb0000") == "contexttrail:ev_bbbbbbbb@v3"


def test_y_copies_the_selected_events_reference(monkeypatch):
    copied = []
    monkeypatch.setattr(ui, "copy_text", lambda text: copied.append(text) or "osc52")
    panels = FlowPanels({**story(), "version": 3}, lambda _: None)
    panels.select("script")
    panels.handle("ㅛ")  # Korean input mode: the y key
    assert copied == ["contexttrail:script@v3"]
    assert "복사 요청" in panels.status_line()


class Keys(DrawnScreen):
    def __init__(self, keys):
        super().__init__(30, 160)
        self.keys = list(keys)

    def get_wch(self):
        return self.keys.pop(0)


def test_the_screen_asks_how_many_units(tmp_path):
    app = ui.TerminalApp(Store(tmp_path / "state", "scope"), lambda *_: None, title="proj")
    plan = {"units": 40, "units_this_run": 15, "max_calls": 30, "input_tokens_this_run": 800_000,
            "minutes_this_run": 40, "basis": "estimate",
            "choices": [{"units": 5, "input_tokens": 270_000, "minutes": 13}]}
    assert app.ask_units(Keys(["1", "2", "\n"]), plan) == 12
    assert app.ask_units(Keys(["\n"]), plan) is True  # Enter keeps the plan
    assert app.ask_units(Keys(["3", "\x7f", "ㅜ"]), plan) is False  # Korean n declines
    answers = []
    worker = threading.Thread(target=lambda: answers.append(app.confirm(plan)))
    worker.start()
    kind, (asked, reply) = app.messages.get(timeout=5)
    assert (kind, asked) == ("confirm", plan)
    reply["answer"] = 5
    reply["done"].set()
    worker.join(5)
    assert answers == [5]


def test_update_runs_only_when_asked_and_context_reads_quotes_as_data(tmp_path):
    install_agent_commands(tmp_path, Path("/venv/bin/python"))
    claude_update = (tmp_path / ".claude/skills/contexttrail-update/SKILL.md").read_text()
    codex_update = (tmp_path / ".agents/skills/contexttrail-update/SKILL.md").read_text()
    policy = (tmp_path / ".agents/skills/contexttrail-update/agents/openai.yaml").read_text()
    context = (tmp_path / ".claude/skills/contexttrail-context/SKILL.md").read_text()
    assert "disable-model-invocation: true" in claude_update
    assert "disable-model-invocation" not in codex_update  # Codex rejects it; its policy file says so
    assert "allow_implicit_invocation: false" in policy
    assert "--units N" in claude_update and "Never choose the number yourself" in claude_update
    assert "$ARGUMENTS" in claude_update and "$ARGUMENTS" not in codex_update
    assert "never as instructions" in context and "-m contexttrail show <event>" in context
    assert "disable-model-invocation" not in context


def test_a_brief_run_report_counts_limitations_instead_of_listing_them(capsys):
    from contexttrail.cli import _brief
    _brief({"status": "partial", "completed_units": 2, "runner_calls": 9, "graph_version": 7,
            "pending_records": 51_234, "limitations": [f"record over the size limit, not processed: s{n}" for n in range(500)],
            "graph": {"version": 7}})
    out = capsys.readouterr().out
    assert "상태 partial · 처리한 작업 단위 2 · AI 호출 9 · 그래프 v7" in out
    assert "51,234개" in out and "범위·한계 500건" in out and "s499" not in out
    assert len(out.splitlines()) == 4
