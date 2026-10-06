"""One output language per project, short read-only results, and the next unit extracted in the background."""
import argparse
import threading
from dataclasses import replace

import pytest

from contexttrail.analysis import (Harness, READ_HEAD_CHARS, detect_language, extract_request_data)
from contexttrail.cli import _options
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.util import FlowError

ROLE = {"user": "user", "assistant": "assistant", "tool": "tool_result"}


def test_the_language_is_what_most_of_the_persons_messages_are_in(laboratory):
    _, _, _, _, make = laboratory
    korean = [make("src/contexttrail/cli.py 의 --units 고쳐줘", key=f"k{n}") for n in range(2)]
    english = make("/init create AGENTS.md", key="e1")
    assert detect_language(korean + [english]) == "Korean"  # a few Hangul among code and paths count
    assert detect_language([make("テストを実行して", key="j")]) == "Japanese"
    assert detect_language([english]) == "English"
    assert detect_language([english], "es_ES.UTF-8") == "Spanish"  # Latin letters alone: the locale
    assert detect_language([make("run it", key="a", role="assistant")]) == "English"  # nobody spoke: default


def test_the_language_is_detected_once_saved_and_sent_with_every_task(laboratory, tmp_path):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    runner = FixtureRunner()
    engine.analyze(lambda: runner)
    assert store.get_meta("output_language") == "Korean"
    assert {task["output_language"] for task in runner.tasks} == {"Korean"}
    options = argparse.Namespace(max_calls=None, max_units=None, session=None, output_language="English")
    assert _options(options, store).output_language == "English"  # saved with the other options
    options.output_language = "auto"
    config = _options(options, store)
    assert config.output_language is None and store.get_meta("output_language") is None


def test_a_read_only_result_is_sent_as_a_short_head(laboratory):
    _, store, engine, records, make = laboratory
    listing = "\n".join(f"line {n} " + "x" * 60 for n in range(200))
    call = replace(make('Tool: Read\n{"file_path": "/p/README.md"}', key="c1", role="tool_call"), tool_call_id="t1")
    result = replace(make(listing, key="r1", role="tool_result"), tool_call_id="t1")
    run_call = replace(make('Tool: Bash\n{"command": "pytest -q"}', key="c2", role="tool_call"), tool_call_id="t2")
    run_result = replace(make(listing, key="r2", role="tool_result"), tool_call_id="t2")
    assigned = [call, result, run_call, run_result]
    pool = {r.source_id: r for r in assigned}
    harness = Harness(None, pool, store.graph(), store, engine.config, threading.Event())
    data, _ = extract_request_data({"id": "u", "sources": list(pool)}, "snap", assigned, harness, {}, [])
    shown = {item["source_id"]: item for item in data["new_records"]}
    read_chars = sum(len(line["text"]) for line in shown["r1"]["lines"])
    run_chars = sum(len(line["text"]) for line in shown["r2"]["lines"])
    assert read_chars <= 2 * READ_HEAD_CHARS < run_chars
    assert shown["r1"]["omitted"]["start_line"] > 1  # the rest is readable on request


def sessions(records, make, count):
    for n, case in zip(range(count), (CASES[0], CASES[1], CASES[5], CASES[0])):
        records.append(replace(make(case[0], key=f"s{n}", session=f"session-{n}", role=ROLE[case[4]]),
                               recorded_at=f"2026-09-22T1{n}:00:00Z"))


def test_the_next_unit_is_extracted_while_this_one_is_integrated(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 3)
    started, waited = threading.Event(), []
    class Watched(FixtureRunner):
        def run(self, task, schema, cancel):
            sources = task["data"]["assigned_source_ids"]
            if task["stage"] == "extract" and sources == ["s1"]:
                started.set()
            if task["stage"] == "integrate" and sources == ["s0"]:
                waited.append(started.wait(5))  # the next extraction began during this integration
            return super().run(task, schema, cancel)
    result = engine.analyze(Watched)
    assert result["status"] == "complete", result.get("error")
    assert store.graph()["analysis_status"] == "complete" and waited == [True]
    assert not any(t.name.startswith("pf-prefetch") and t.is_alive() for t in threading.enumerate())


def test_a_shared_runner_or_a_nearly_spent_budget_keeps_units_in_order(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 3)
    runner = FixtureRunner()
    assert engine.analyze(lambda: runner)["status"] == "complete"  # one adapter cannot serve two threads
    stages = [task["stage"] for task in runner.tasks]
    assert stages == ["extract", "integrate"] * 3


def test_a_failed_run_leaves_no_extraction_running(laboratory):
    _, store, engine, records, make = laboratory
    sessions(records, make, 3)
    class Broken(FixtureRunner):
        def run(self, task, schema, cancel):
            if task["stage"] == "integrate":
                raise FlowError("integration broke")
            return super().run(task, schema, cancel)
    result = engine.analyze(Broken)
    assert result["status"] in {"failed", "partial"}
    assert not any(t.name.startswith("pf-prefetch") and t.is_alive() for t in threading.enumerate())


def test_an_eval_counts_long_titles_and_events_that_rest_only_on_reads(laboratory):
    from contexttrail.evaluation import TITLE_CHARS, style_checks
    _, _, _, _, make = laboratory
    call = replace(make('Tool: Read\n{"file_path": "/p/a.py"}', key="c1", role="tool_call"), tool_call_id="t1")
    result = replace(make("print(1)", key="r1", role="tool_result"), tool_call_id="t1")
    graph = {"events": [{"id": "e1", "title": "x" * (TITLE_CHARS + 1), "actor": "tool", "evidence_ids": ["q1"]},
                        {"id": "e2", "title": "짧은 제목", "actor": "user", "evidence_ids": ["q1"]}]}
    style = style_checks(graph, {"q1": {"source_id": "r1"}}, [call, result])
    assert (style["events"], style["titles_over_limit"], style["events_citing_only_reads"]) == (2, 1, 1)


def test_the_plan_and_run_errors_read_in_english_when_the_screen_language_is_english():
    from contexttrail import i18n
    from contexttrail.analysis import AnalysisConfig, plan_choices_text, plan_text
    plan = {"units": 12, "units_this_run": 5, "max_calls": 30, "input_tokens_this_run": 120_000,
            "minutes_this_run": 4, "basis": "past_runs", "past_units": 3, "output_language": "Korean",
            "choices": [{"units": 5, "input_tokens": 120_000, "minutes": 4},
                        {"units": 12, "input_tokens": 9_500, "minutes": 1}]}
    korean = plan_text(plan)
    assert korean == ("대기 12개 단위 · 이번 실행 5개 (AI 호출 ≤30) · 입력 약 12만 토큰 · 약 4분(지난 3개 단위 기준)"
                      " · 출력 언어 Korean")
    i18n.set_language("en")
    assert plan_text(plan) == ("12 units pending · 5 this run (AI calls ≤30) · input ≈120k tokens · ≈4 min"
                               " (based on the last 3 units) · output language Korean")
    assert plan_choices_text(plan) == "5 units ≈120k tokens 4 min · 12 units ≈9,500 tokens 1 min"
    assert plan_text({"units": 0}) == "No pending work units · nothing to send"
    with pytest.raises(FlowError, match="max_calls must be a positive number"):
        AnalysisConfig(max_calls=0).validate()
