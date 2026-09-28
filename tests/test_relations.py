import pytest

from projectflow.render import mermaid, status_labels
from projectflow.schema import EVENT_FIELDS, EvidenceValidator
from projectflow.util import FlowError


def records(make):
    return {"question": make("GitHub에서 설치하면 자동으로 설치되는 것 맞아?", key="question"),
            "patch": make("apply_patch: Add File install.sh", key="patch", role="tool_call"),
            "run": make("./install.sh exit_code 0", key="run", role="tool_result"),
            "answer": make("자동 설치되지 않습니다. install.sh를 추가했습니다.", key="answer", role="assistant")}


def cite(record):
    return [{"source_id": record.source_id, "start_line": 1, "end_line": 1, "quote": record.content}]


def event(temp_id, record, kind, status, basis="explicit_statement"):
    return {"id": temp_id, "kind": kind, "title": temp_id, "summary": temp_id, "actor": "assistant",
            "status": status, "basis": basis, "session_ids": [record.session_id],
            "worktree_ids": [record.worktree_id], "recorded_at": record.recorded_at, "occurred_at": None,
            "evidence": cite(record)}


def edge(temp_id, left, right, relation, record, basis="structural"):
    return {"id": temp_id, "from_event_id": left, "to_event_id": right, "relation": relation,
            "basis": basis, "evidence": cite(record), "rationale": "test", "active": True}


def validator(pool):
    return EvidenceValidator({r.source_id: r for r in pool.values()},
                             {r.source_id: [(1, 1)] for r in pool.values()})


def extraction(events, edges=()):
    return {"status": "complete", "read_requests": [], "snapshot_id": "snap", "unit_id": "unit",
            "event_candidates": list(events), "edge_candidates": list(edges), "existing_event_matches": [],
            "open_items": [], "limitations": [], "unprocessed_record_ids": []}


def delta(graph, events=(), edges=(), updates=()):
    return {"status": "complete", "read_requests": [], "snapshot_id": "snap",
            "base_graph_version": graph["version"], "events_to_add": list(events),
            "events_to_update": list(updates), "edges_to_add": list(edges), "edges_to_invalidate": [],
            "open_items_to_upsert": [], "open_items_to_resolve": [], "candidate_resolutions": [],
            "change_attributions": [], "review_issues": [], "review_resolutions": [], "limitations": []}


def test_observed_status_belongs_only_to_the_run_outcome(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    with pytest.raises(FlowError, match="kind=outcome"):
        validator(pool).event(event("tmp:change", pool["run"], "action", "observed_success", "tool_record"))
    validator(pool).event(event("tmp:run", pool["run"], "outcome", "observed_success", "tool_record"))


def test_applied_status_needs_a_recorded_change(laboratory):
    _, _, _, _, make = laboratory
    pool = records(make)
    with pytest.raises(FlowError, match="applied"):
        validator(pool).event(event("tmp:said", pool["answer"], "action", "applied"))
    with pytest.raises(FlowError, match="applied"):
        validator(pool).event(event("tmp:outcome", pool["patch"], "outcome", "applied", "tool_record"))
    validator(pool).event(event("tmp:change", pool["patch"], "action", "applied", "tool_record"))


def test_user_question_is_asked_not_proposed(laboratory):
    _, _, _, _, make = laboratory
    pool = records(make)
    with pytest.raises(FlowError, match="question 사건의 상태"):
        validator(pool).event(event("tmp:q", pool["question"], "question", "proposed"))
    with pytest.raises(FlowError, match="asked 상태는"):
        validator(pool).event(event("tmp:q", pool["question"], "goal", "asked"))
    validator(pool).event(event("tmp:q", pool["question"], "question", "asked"))


def test_verifies_joins_a_change_to_the_run_that_exercised_it(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    events = [event("tmp:change", pool["patch"], "action", "applied", "tool_record"),
              event("tmp:run", pool["run"], "outcome", "observed_success", "tool_record"),
              event("tmp:report", pool["answer"], "outcome", "reported_complete")]
    check = lambda edges: validator(pool).check_extraction(extraction(events, edges), "unit", "snap", store.graph())
    check([edge("tmp:e", "tmp:change", "tmp:run", "verifies", pool["run"])])
    # "The run verifies the change" written as run → change: the kinds allow one meaning, so it is turned.
    turned = extraction(events, [edge("tmp:e", "tmp:run", "tmp:change", "verifies", pool["run"])])
    checker = validator(pool)
    checker.check_extraction(turned, "unit", "snap", store.graph())
    assert (turned["edge_candidates"][0]["from_event_id"], turned["edge_candidates"][0]["to_event_id"]) == (
        "tmp:change", "tmp:run")
    assert [n["mode"] for n in checker.normalizations].count("relation_direction_corrected") == 1
    with pytest.raises(FlowError, match="verifies 관계는 변경 사건.*tmp:e"):
        check([edge("tmp:e", "tmp:change", "tmp:report", "verifies", pool["answer"])])
    with pytest.raises(FlowError, match="verifies 관계는 변경 사건.*tmp:e"):
        check([edge("tmp:e", "tmp:report", "tmp:change", "verifies", pool["answer"])])
    with pytest.raises(FlowError, match="추정"):
        check([edge("tmp:e", "tmp:change", "tmp:run", "verifies", pool["run"], basis="inferred")])


def test_answers_starts_at_the_question(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    events = [event("tmp:q", pool["question"], "question", "asked"),
              event("tmp:a", pool["answer"], "outcome", "reported_complete")]
    check = lambda edges: validator(pool).check_extraction(extraction(events, edges), "unit", "snap", store.graph())
    check([edge("tmp:e", "tmp:q", "tmp:a", "answers", pool["answer"])])
    turned = extraction(events, [edge("tmp:e", "tmp:a", "tmp:q", "answers", pool["answer"])])
    validator(pool).check_extraction(turned, "unit", "snap", store.graph())
    assert turned["edge_candidates"][0]["from_event_id"] == "tmp:q"
    with pytest.raises(FlowError, match="answers 관계는"):
        check([edge("tmp:e", "tmp:a", "tmp:a", "answers", pool["answer"])])


def test_later_update_cannot_leave_a_verification_pointing_at_an_unobserved_result(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    first = validator(pool).apply_delta(delta(store.graph(),
        [event("tmp:change", pool["patch"], "action", "applied", "tool_record"),
         event("tmp:run", pool["run"], "outcome", "observed_success", "tool_record")],
        [edge("tmp:e", "tmp:change", "tmp:run", "verifies", pool["run"])]), store.graph(), "snap", "run-1")
    assert first["schema_version"] == 2
    run_id = next(e["id"] for e in first["events"] if e["title"] == "tmp:run")
    changes = {key: None for key in EVENT_FIELDS} | {"status": "reported_complete"}
    downgrade = {"id": run_id, "reason": "test", "evidence": cite(pool["run"]), "changes": changes}
    with pytest.raises(FlowError, match="verifies 관계는"):
        validator(pool).apply_delta(delta(first, updates=[downgrade]), first, "snap", "run-2")


def test_labels_show_what_checked_a_change_and_whether_a_question_was_answered():
    def node(key, kind, status, title=None):
        return {"id": key, "kind": kind, "status": status, "title": title or key}
    def link(left, right, relation, active=True):
        return {"id": f"{left}-{right}", "from_event_id": left, "to_event_id": right,
                "relation": relation, "basis": "structural", "active": active}
    graph = {"events": [node("q", "question", "asked"), node("q2", "question", "asked"),
                        node("c1", "action", "applied"), node("c2", "revision", "applied"),
                        node("c3", "action", "reported_complete"), node("ok", "outcome", "observed_success"),
                        node("bad", "outcome", "observed_failure"), node("a", "outcome", "reported_complete"),
                        node("c4", "revision", "applied"), node("syntax", "outcome", "observed_success", "수정본 구문 검사 통과"),
                        node("loose", "outcome", "observed_success")],
             "edges": [link("q", "a", "answers"), link("c1", "ok", "verifies"), link("c1", "bad", "verifies"),
                       link("c2", "ok", "verifies", active=False), link("c4", "syntax", "verifies")]}
    labels = status_labels(graph)
    assert labels["q"] == "요청 · 답변됨" and labels["q2"] == "요청 · 답변 없음"
    assert labels["c1"] == "변경 적용 · 검증 통과 1건 · 검증 실패 1건"
    assert labels["c2"] == "변경 적용 · 미검증"
    assert labels["c3"] == "완료 보고 · 미검증"
    assert labels["ok"] == "관측 성공" and labels["a"] == "완료 보고·미검증"
    # A single check is named so its strength is visible; a result nothing leads to is flagged.
    assert labels["c4"] == "변경 적용 · 검증: 수정본 구문 검사 통과"
    assert labels["loose"] == "관측 성공 · 확인 대상 미연결"
    assert "|검증|" in mermaid(graph) and "|답변|" in mermaid(graph)


def test_every_claim_error_is_reported_in_one_round(laboratory):
    _, store, _, _, make = laboratory
    pool = records(make)
    events = [event("tmp:change", pool["run"], "action", "observed_success", "tool_record"),
              event("tmp:fix", pool["run"], "revision", "observed_success", "tool_record"),
              event("tmp:q", pool["question"], "goal", "asked")]
    edges = [edge("tmp:e", "tmp:q", "tmp:change", "answers", pool["question"])]
    for check in (lambda: validator(pool).check_extraction(extraction(events, edges), "unit", "snap", store.graph()),
                  lambda: validator(pool).apply_delta(delta(store.graph(), events, edges), store.graph(), "snap", "run")):
        with pytest.raises(FlowError) as raised:
            check()
        message = str(raised.value)
        assert all(name in message for name in ("tmp:change", "tmp:fix", "asked 상태는", "answers 관계는"))


def test_new_observed_result_without_a_relation_is_signalled_for_review():
    from projectflow.analysis import unlinked_observed_outcomes
    def item(key, kind, status):
        return {"id": key, "kind": kind, "status": status}
    delta = {"events_to_add": [item("tmp:change", "action", "applied"),
                               item("tmp:checked", "outcome", "observed_success"),
                               item("tmp:loose", "outcome", "observed_success"),
                               item("tmp:report", "outcome", "reported_complete")],
             "edges_to_add": [{"from_event_id": "tmp:change", "to_event_id": "tmp:checked"}]}
    assert [x["id"] for x in unlinked_observed_outcomes(delta)] == ["tmp:loose"]
    assert unlinked_observed_outcomes(None) == []


def test_revision_without_revises_is_the_review_signal_not_every_observed_result():
    from projectflow.analysis import review_signal_items, unlinked_revisions
    def item(key, kind, status):
        return {"id": key, "kind": kind, "status": status}
    delta = {"events_to_add": [item("tmp:fix", "revision", "applied"),
                               item("tmp:linked", "revision", "applied"),
                               item("tmp:ok", "outcome", "observed_success")],
             "edges_to_add": [{"from_event_id": "ev_old", "to_event_id": "tmp:linked", "relation": "revises"},
                              {"from_event_id": "ev_old", "to_event_id": "tmp:fix", "relation": "follows"},
                              {"from_event_id": "tmp:linked", "to_event_id": "tmp:ok", "relation": "verifies"}],
             "events_to_update": [], "edges_to_invalidate": []}
    assert [x["id"] for x in unlinked_revisions(delta)] == ["tmp:fix"]
    assert list(review_signal_items(delta)) == ["unlinked_revision"]
    assert review_signal_items(None) == {}


def test_graph_summary_counts_revisions_without_a_revised_event():
    from projectflow.render import graph_summary
    graph = {"events": [{"id": "a", "kind": "action", "status": "applied"},
                        {"id": "b", "kind": "revision", "status": "applied"},
                        {"id": "c", "kind": "revision", "status": "applied"}],
             "edges": [{"from_event_id": "a", "to_event_id": "b", "relation": "revises", "active": True},
                       {"from_event_id": "a", "to_event_id": "c", "relation": "revises", "active": False}]}
    assert graph_summary(graph)["revisions_unlinked"] == 1


def test_terminal_marks_and_detail_read_without_a_browser():
    from projectflow.render import event_detail, readable_quote, status_tones, terminal_graph
    evidence = {"evi_patch": {"source_id": "s1", "start_line": 2, "end_line": 2,
                              "quote": '{"cmd": "apply", "patch": "@@\\n-old\\n+new\\n"}',
                              "source": {"role": "tool_call", "provider": "codex", "recorded_at": "2026-09-25T06:00:00Z"}}}
    def event(key, kind, status, title, evidence_ids=()):
        return {"id": key, "kind": kind, "status": status, "title": title, "summary": title + " 요약",
                "actor": "assistant", "evidence_ids": list(evidence_ids), "recorded_at": None}
    graph = {"events": [event("a", "action", "applied", "변경", ["evi_patch"]),
                        event("b", "outcome", "observed_success", "테스트 통과"),
                        event("c", "action", "applied", "문서"),
                        event("d", "outcome", "observed_failure", "실패")],
             "edges": [{"id": "e", "from_event_id": "a", "to_event_id": "b", "relation": "verifies",
                        "active": True, "basis": "explicit"}],
             "open_items": []}
    assert status_tones(graph) == {"a": "ok", "b": "ok", "c": "warn", "d": "fail"}
    lines = [line for line, _ in terminal_graph(graph, marks=True)]
    assert lines[0].startswith("[01] ✓ 변경") and any(line.startswith("[04] ✗ 실패") for line in lines)
    assert terminal_graph(graph, ascii_only=True, marks=True)[0][0].startswith("[01] v ")
    detail = event_detail(graph, "a", evidence.get)
    texts = [text for text, _ in detail]
    assert ("  ✓ [02] 테스트 통과", "ok") in detail and "검증한 결과" in texts
    assert any("도구 호출 · Codex" in text and "2번째 줄" in text for text in texts)
    assert "     -old" in texts and "     +new" in texts  # escaped line breaks restored for display
    assert readable_quote('print("a\\nb")') == 'print("a\\nb")'
    assert event_detail(graph, "missing", evidence.get)[0][1] == "dim"


def test_locator_slips_are_corrected_but_unseen_or_ambiguous_quotes_are_not(laboratory):
    _, _, _, _, make = laboratory
    log = make("line one\nthe distinctive result line\nline three\nrepeat phrase\nrepeat phrase", key="log",
               role="tool_result")
    checker = EvidenceValidator({log.source_id: log}, {log.source_id: [(1, 4)]})
    def cite_at(start, end, quote):
        return [{"source_id": log.source_id, "start_line": start, "end_line": end, "quote": quote}]
    # Wrong lines (and a reversed range) for a quote that is shown exactly once: relocated to line 2.
    for start, end in ((3, 3), (4, 1)):
        evidence_id, = checker.citations(cite_at(start, end, "distinctive result"))
        assert (checker.evidence[evidence_id]["start_line"], checker.evidence[evidence_id]["end_line"]) == (2, 2)
    assert [n["mode"] for n in checker.normalizations].count("relocated_within_provided_lines") == 2
    with pytest.raises(FlowError):  # line 5 was never shown to the model
        EvidenceValidator({log.source_id: log}, {log.source_id: [(1, 3)]}).citations(cite_at(1, 1, "repeat phrase"))
    with pytest.raises(FlowError, match="일치"):  # shown twice on different lines
        EvidenceValidator({log.source_id: log}, {log.source_id: [(1, 5)]}).citations(cite_at(1, 1, "repeat phrase"))
    with pytest.raises(FlowError):  # quote not in the record at all
        checker.citations(cite_at(1, 1, "not written anywhere"))


def test_recording_time_and_sessions_come_from_the_citations(laboratory):
    _, _, _, _, make = laboratory
    pool = records(make)
    checker = validator(pool)
    proposed = event("tmp:run", pool["run"], "outcome", "observed_success", "tool_record")
    proposed.update(recorded_at="2001-01-01T00:00:00Z", session_ids=["made-up"])
    result = checker.event(proposed)
    assert result["recorded_at"] == pool["run"].recorded_at == proposed["recorded_at"]
    assert result["session_ids"] == [pool["run"].session_id]
    assert checker.normalizations[-1] == {"mode": "event_provenance_from_evidence",
                                          "fields": ["recorded_at", "session_ids", "worktree_ids"]}
