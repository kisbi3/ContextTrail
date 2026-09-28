"""Slips that used to cost a repair call or a whole unit: an answered user goal, a short quote, one bad candidate."""
from dataclasses import replace

import pytest

from projectflow.demo import CASES, FixtureRunner
from projectflow.schema import SHORT_QUOTE_CHARS
from projectflow.util import FlowError
from test_relations import cite, edge, event, extraction, records, validator


def test_a_persons_answered_goal_is_read_as_their_question(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    goal = {**event("tmp:q", pool["question"], "goal", "proposed"), "actor": "user"}
    answer = event("tmp:a", pool["answer"], "outcome", "reported_complete")
    output = extraction([goal, answer], [edge("tmp:e", "tmp:q", "tmp:a", "answers", pool["answer"])])
    checker = validator(pool)
    checker.check_extraction(output, "unit", "snap", store.graph())
    assert (output["event_candidates"][0]["kind"], output["event_candidates"][0]["status"]) == ("question", "asked")
    assert "user_goal_as_answered_question" in [n["mode"] for n in checker.normalizations]
    # Only a goal resting on the person's own words has one reading.
    said = {**event("tmp:q", pool["answer"], "goal", "proposed"), "actor": "user"}
    with pytest.raises(FlowError, match="answers 관계는"):
        validator(pool).check_extraction(extraction(
            [said, answer], [edge("tmp:e", "tmp:q", "tmp:a", "answers", pool["answer"])]), "unit", "snap", store.graph())
    with pytest.raises(FlowError, match="answers 관계는"):
        validator(pool).check_extraction(extraction(
            [event("tmp:q", pool["question"], "goal", "proposed"), answer],
            [edge("tmp:e", "tmp:q", "tmp:a", "answers", pool["answer"])]), "unit", "snap", store.graph())


def test_a_short_fragment_counts_where_it_appears_once(laboratory):
    _, _, _, _, make = laboratory
    probe = make('{"sandbox": "denied", "network": "on", "proxy": "on"}', key="probe", role="tool_result")
    pool = {"probe": probe}
    unique = [{"source_id": "probe", "start_line": 1, "end_line": 1, "quote": "denied"}]
    checker = validator(pool)
    [evidence_id] = checker.citations(unique)
    assert checker.evidence[evidence_id]["quote"] == probe.content  # stored as the whole line
    assert checker.evidence[evidence_id]["focus"] == [[13, 19]]
    assert len('"on"') < SHORT_QUOTE_CHARS
    with pytest.raises(FlowError, match="고정 원문과 다릅니다: probe:1-1 .*한 번 나올 때만"):
        validator(pool).citations([{**unique[0], "quote": '"on"'}])


def test_candidates_that_pass_are_kept_when_the_repair_also_fails(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    good = event("tmp:a", pool["answer"], "outcome", "reported_complete")
    bad = {**event("tmp:b", pool["patch"], "action", "applied", "tool_record"),
           "evidence": [{**cite(pool["patch"])[0], "quote": "not in the source at all"}]}
    link = edge("tmp:e", "tmp:b", "tmp:a", "follows", pool["answer"])
    checker = validator(pool)
    checker.required_citations = {"patch": ("apply_patch", None)}
    output = extraction([good, bad], [link])
    with pytest.raises(FlowError):
        checker.check_extraction(output, "unit", "snap", store.graph())
    kept, waived, notes = checker.salvage_extraction(output, "unit", "snap", store.graph())
    assert [item["id"] for item in kept["event_candidates"]] == ["tmp:a"] and kept["edge_candidates"] == []
    assert waived == ["patch"]  # the edit was described, only by the event that failed
    assert notes == ["근거 검증을 통과하지 못해 제외한 사건 후보: tmp:b", "근거 검증을 통과하지 못해 제외한 관계 후보 1개"]
    assert kept["limitations"] == notes
    # An edit that no event mentioned still fails the unit.
    silent = validator(pool)
    silent.required_citations = {"run": ("Bash", None)}
    with pytest.raises(FlowError):
        silent.salvage_extraction(output, "unit", "snap", store.graph())


def test_a_unit_is_saved_without_the_candidate_that_failed_twice(laboratory):
    _, store, engine, records_, make = laboratory
    for n, case in enumerate((CASES[0], CASES[1])):
        records_.append(replace(make(case[0], key=f"s{n}", role=case[4]), recorded_at=f"2026-09-22T10:0{n}:00Z"))
    class Stubborn(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "extract":
                for item in output["event_candidates"]:
                    if item["actor"] == "assistant":
                        item["evidence"][0]["quote"] = "a sentence nobody wrote"
            return output
    runner = Stubborn()
    result = engine.analyze(lambda: runner)
    assert result["status"] == "complete", result.get("error")
    graph = store.graph()
    assert [e["title"] for e in graph["events"]] == [CASES[0][2]]
    assert any(CASES[1][2] in line for line in graph["limitations"])
    assert [task["stage"] for task in runner.tasks] == ["extract", "extract", "integrate"]
