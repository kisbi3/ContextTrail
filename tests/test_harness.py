import threading
from dataclasses import replace
import pytest
from projectflow.analysis import Harness, AnalysisConfig, _rehydrate
from projectflow.demo import CASES, FixtureRunner
from projectflow.model import SourceRecord
from projectflow.schema import EvidenceValidator
from projectflow.util import FlowError
from projectflow.util import ident


def harness(laboratory):
    _,store,engine,records,make=laboratory
    records.extend([make('one\ntwo\nthree'),make('other',key='s2')])
    h=Harness(FixtureRunner(),{r.source_id:r for r in records},store.graph(),store,AnalysisConfig(),threading.Event())
    h.context(records[:1])
    return h,records


def test_read_range_and_dependency(laboratory):
    h,records=harness(laboratory)
    result=h.read({'kind':'read_records','ids':['s1'],'start_line':2,'end_line':2,'query':None,'unit_id':None})
    assert result[0]['lines']==[{'line':2,'text':'two'}]
    assert h.dependencies['s1']==records[0].content_hash
    validator=EvidenceValidator(h.pool,h.provided)
    validator.citations([{'source_id':'s1','start_line':2,'end_line':2,'quote':'two'}])
    with pytest.raises(FlowError,match='제공하지 않은'):
        validator.citations([{'source_id':'s1','start_line':1,'end_line':1,'quote':'one'}])


def test_search_events_is_bounded_to_visible_event_manifest_and_unit(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make("source text"))
    events = [{"id": f"ev_{i}", "title": "SQLite storage decision", "summary": "retain evidence",
               "recorded_at": None, "evidence_ids": [], "session_ids": [], "worktree_ids": []}
              for i in range(210)]
    graph = {**store.graph(), "events": events}
    h = Harness(FixtureRunner(), {r.source_id: r for r in records}, graph, store,
                AnalysisConfig(), threading.Event(), unit_id="unit-test")
    h.context(records[:1])
    found = h.read({"kind": "search_events", "ids": [], "query": "SQLite storage",
                    "unit_id": "unit-test", "start_line": None, "end_line": None})
    assert len(found) == 8
    assert all("id" in item and "reason" in item and "description" in item for item in found)
    assert h.read({"kind": "search_events", "ids": [], "query": "SQLite",
                   "unit_id": "another-unit", "start_line": None, "end_line": None}) == [
                       {"denied": "search_events는 query·현재 unit_id만 사용하며 ids와 줄 범위는 비워야 합니다."}]
    malformed = {"kind": "search_events", "ids": ["ev_1"], "query": "SQLite",
                 "unit_id": "unit-test", "start_line": None, "end_line": None}
    assert "denied" in h.read(malformed)[0]
    malformed = {"kind": "read_records", "ids": ["s1"], "start_line": 1,
                 "end_line": 1, "query": "SQLite", "unit_id": "unit-test"}
    assert "denied" in h.read(malformed)[0]


def test_unique_exact_quote_can_narrow_the_claimed_line_range(laboratory):
    _, _, _, _, make = laboratory
    record = make("first\nsecond\nthird")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    ids = validator.citations([{'source_id':record.source_id,'start_line':1,'end_line':3,
                                'quote':'first\nsecond'}])
    assert validator.evidence[ids[0]]['start_line'] == 1
    assert validator.evidence[ids[0]]['end_line'] == 2
    assert validator.normalizations == [{'source_id':record.source_id,'requested_lines':[1,3],
        'actual_lines':[1,2],'mode':'exact_line_span_narrowed','requested_quote_chars':len('first\nsecond')}]


def test_unique_exact_substring_citation_expands_to_canonical_source_lines(laboratory):
    _, _, _, _, make = laboratory
    record = make("prefix: the exact cited phrase is here; trailing detail\nsecond prefix continues\n")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    quote = "exact cited phrase is here; trailing detail\nsecond prefix"
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1,
                                "end_line": 2, "quote": quote}])
    saved = validator.evidence[ids[0]]
    assert (saved["start_line"], saved["end_line"]) == (1, 2)
    assert saved["quote"] == "prefix: the exact cited phrase is here; trailing detail\nsecond prefix continues"
    assert validator.normalizations[0]["mode"] == "unique_exact_substring_expanded_to_lines"
    assert validator.normalizations[0]["requested_quote_hash"]


def test_canonicalized_extraction_survives_candidate_validation_and_integration_prep(laboratory):
    _, store, engine, _, make = laboratory
    record = make("prefix: unique decision phrase here; trailing context\nsecond line")
    unit_id, snapshot_id = "unit-substring", "snapshot-substring"
    citation = {"source_id": record.source_id, "start_line": 1, "end_line": 2,
                "quote": "unique decision phrase here"}
    output = {"status": "complete", "read_requests": [], "snapshot_id": snapshot_id,
        "unit_id": unit_id, "event_candidates": [{"id": "tmp:decision", "kind": "decision",
            "title": "Decision", "summary": "A decision", "actor": "user", "status": "adopted",
            "basis": "explicit_statement", "session_ids": [record.session_id],
            "worktree_ids": [record.worktree_id], "recorded_at": record.recorded_at,
            "occurred_at": None, "evidence": [citation]}],
        "edge_candidates": [], "existing_event_matches": [], "open_items": [],
        "limitations": [], "unprocessed_record_ids": []}
    initial = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]},
                                assigned_source_ids={record.source_id})
    initial.check_extraction(output, unit_id, snapshot_id, store.graph())
    assert citation["start_line"] == 1 and citation["end_line"] == 1
    assert citation["quote"] == "prefix: unique decision phrase here; trailing context"
    extracted = {"output": output, "evidence": initial.evidence, "dependencies": {}}
    unit = {"id": unit_id, "sources": [record.source_id]}
    from projectflow.routing import RunnerPool
    runners = RunnerPool(FixtureRunner, engine.config)
    prepared = engine._prepare_integration(unit, extracted, {record.source_id: record},
        store.graph(), snapshot_id, "run-substring", runners, threading.Event())
    assert prepared.data["validated_candidates"]["event_candidates"][0]["evidence"][0] == citation


def test_unique_substring_citation_rejects_duplicate_or_invented_text(laboratory):
    _, _, _, _, make = laboratory
    record = make("prefix exact phrase appears\nfiller one\nfiller two\nexact phrase appears suffix")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 4)]})
    with pytest.raises(FlowError, match="유일하게 일치.*1, 4번 줄 중 하나만 인용"):
        validator.citations([{"source_id": record.source_id, "start_line": 1,
                              "end_line": 4, "quote": "exact phrase appears"}])
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):
        validator.citations([{"source_id": record.source_id, "start_line": 1,
                              "end_line": 4, "quote": "invented phrase that is long enough"}])


def test_repeats_a_few_lines_apart_are_cited_as_the_lines_that_hold_them(laboratory):
    _, _, _, _, make = laboratory
    record = make("intro\nprefix exact phrase appears\nexact phrase appears suffix\nouter")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 4)]})
    [kept] = validator.citations([{"source_id": record.source_id, "start_line": 1,
                                   "end_line": 4, "quote": "exact phrase appears"}])
    saved = validator.evidence[kept]
    assert (saved["start_line"], saved["end_line"]) == (2, 3) and len(saved["focus"]) == 2
    assert validator.normalizations[0]["mode"] == "repeats_within_few_lines_expanded"
    # Only inside the lines the model named: a quote cited at the wrong line is not moved onto repeats.
    with pytest.raises(FlowError, match="인용문"):
        validator.citations([{"source_id": record.source_id, "start_line": 1,
                              "end_line": 1, "quote": "exact phrase appears"}])


def test_quote_normalization_rejects_ambiguous_or_partial_text(laboratory):
    _, _, _, _, make = laboratory
    record = make("same\nsame\nother")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    with pytest.raises(FlowError, match='인용문이'):
        validator.citations([{'source_id':record.source_id,'start_line':1,'end_line':2,'quote':'same'}])
    with pytest.raises(FlowError, match='인용문이'):
        validator.citations([{'source_id':record.source_id,'start_line':3,'end_line':3,'quote':'otter'}])
    # A short fragment found once is kept, as its whole line.
    [kept] = validator.citations([{'source_id':record.source_id,'start_line':3,'end_line':3,'quote':'othe'}])
    assert validator.evidence[kept]['quote'] == 'other'


def test_whitespace_the_model_reflowed_is_read_back_as_the_source_line(laboratory):
    _, _, _, _, make = laboratory
    record = make("header line here\nthe exact cited phrase   is here; trailing detail\ntail line here")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    [evidence_id] = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2,
        "quote": "the exact cited phrase is here; trailing detail"}])
    saved = validator.evidence[evidence_id]
    assert (saved["start_line"], saved["end_line"]) == (2, 2)
    assert saved["quote"] == "the exact cited phrase   is here; trailing detail"
    start, end = saved["focus"][0]
    assert saved["quote"][start:end] == saved["quote"]  # the span is raw source text
    assert validator.normalizations[0]["mode"] == "whitespace_normalized"


def test_whitespace_that_two_lines_share_is_not_read_back(laboratory):
    _, _, _, _, make = laboratory
    record = make("one aa  bb cc line here\ntwo aa  bb cc line here")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2, "quote": "aa bb cc"}])


def test_ellipsis_joined_quote_expands_to_the_lines_its_pieces_came_from(laboratory):
    _, _, _, _, make = laboratory
    record = make("first line of the request\nsecond line of the request\nthird line of the request")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    [evidence_id] = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 3,
        "quote": "first line of the request ... third line of the request"}])
    saved = validator.evidence[evidence_id]
    assert (saved["start_line"], saved["end_line"]) == (1, 3)  # the line between is stored whole
    assert [saved["quote"][a:b] for a, b in saved["focus"]] == ["first line of the request",
                                                                "third line of the request"]
    assert validator.normalizations[0]["mode"] == "ellipsis_pieces"


def test_ellipsis_pieces_out_of_order_or_under_eight_chars_are_not_read_back(laboratory):
    _, _, _, _, make = laboratory
    record = make("aaa second line here now\nbbb first line here now\nccc third line here now")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 3,
                              "quote": "first line here now ... second line here now"}])
    shorter = make("first line of the request\nsecond line\nthird line of the request", key="short")
    strict = EvidenceValidator({shorter.source_id: shorter}, {shorter.source_id: [(1, 3)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):
        strict.citations([{"source_id": shorter.source_id, "start_line": 1, "end_line": 3,
                           "quote": "first line of the request ... second"}])


def test_quote_that_ran_past_the_line_end_keeps_the_line_it_copied(laboratory):
    _, _, _, _, make = laboratory
    record = make("intro line\nthe distinctive result line is here\ntail line")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    [evidence_id] = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2,
        "quote": "the distinctive result line is here and"}])
    saved = validator.evidence[evidence_id]
    assert (saved["start_line"], saved["end_line"]) == (2, 2)
    assert [saved["quote"][a:b] for a, b in saved["focus"]] == ["the distinctive result line is here"]
    assert validator.normalizations[0]["mode"] == "tail_past_line_end"


def test_quote_past_the_line_end_is_rejected_under_eighty_percent_or_inside_a_line(laboratory):
    _, _, _, _, make = laboratory
    record = make("intro line\nthe distinctive result line is here\ntail line")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 3)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):  # too much past the line end
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2,
                              "quote": "the distinctive result line is here and the model kept going"}])
    longer = make("intro line\nthe distinctive result line is here and then some more text here", key="mid")
    strict = EvidenceValidator({longer.source_id: longer}, {longer.source_id: [(1, 2)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):  # the copy stops inside the line
        strict.citations([{"source_id": longer.source_id, "start_line": 1, "end_line": 2,
                           "quote": "the distinctive result line is here more text here"}])


def test_a_translated_quote_and_a_short_fragment_are_never_read_back(laboratory):
    _, _, _, _, make = laboratory
    record = make("header line\nthe user decided to keep the sqlite file in the state directory", key="ko")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2,
                              "quote": "사용자는 상태 디렉터리에 sqlite 파일을 두기로 결정했다"}])
    tabbed = make("same\twords here now", key="tab")
    strict = EvidenceValidator({tabbed.source_id: tabbed}, {tabbed.source_id: [(1, 1)]})
    with pytest.raises(FlowError, match="고정 원문과 다릅니다"):  # a fragment under 8 chars stays strict
        strict.citations([{"source_id": tabbed.source_id, "start_line": 1, "end_line": 1, "quote": "same w"}])


@pytest.mark.parametrize('text,start,end,quote,category', [
    ("intro line here\nrepeat phrase lives here\nrepeat phrase lives here", 1, 1, "repeat phrase lives here",
     "other_provided_lines_multiple"),
    ("same words here now\na\nb\nsame words here now", 1, 4, "same words here now", "multiple_in_cited_lines"),
    ("same\twords here now", 1, 1, "same w", "whitespace_only"),
    ("same words here now", 1, 1, "sane wd", "short_fragment"),
    ("same words here now", 1, 1, "nothing like this here", "not_found")])
def test_a_failed_quote_is_classified_by_where_it_went(laboratory, text, start, end, quote, category):
    _, _, _, _, make = laboratory
    record = make(text)
    last = len(text.splitlines())
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, last)]})
    with pytest.raises(FlowError, match="인용문"):
        validator.citations([{"source_id": record.source_id, "start_line": start, "end_line": end, "quote": quote}])
    [item] = validator.mismatch_audit()
    assert {k: v for k, v in item.items() if k != "shape"} == {
        "source_id": record.source_id, "lines": [start, end], "category": category, "quote_chars": len(quote)}
    assert ("shape" in item) == (category == "multiple_in_cited_lines")
    assert validator.mismatches[0]["quote"] == quote  # kept in memory for the repair round only


def test_a_short_quote_found_twice_is_counted_as_two_matches(laboratory):
    _, _, _, _, make = laboratory
    record = make("ab cd ab cd here")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 1)]})
    with pytest.raises(FlowError, match="고정 원문과 다릅니다"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 1, "quote": "ab cd"}])
    assert validator.mismatches[0]["matches"] == 2


def test_a_quote_written_for_another_record_is_classified_as_such(laboratory):
    _, _, _, _, make = laboratory
    here, other = make("intro line here", key="here"), make("an invented phrase that is long enough", key="other")
    validator = EvidenceValidator({r.source_id: r for r in (here, other)},
                                  {here.source_id: [(1, 1)], other.source_id: [(1, 1)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다"):
        validator.citations([{"source_id": here.source_id, "start_line": 1, "end_line": 1,
                              "quote": "an invented phrase that is long"}])
    assert [item["category"] for item in validator.mismatch_audit()] == ["other_record"]


def test_nearest_lines_come_from_provided_lines_only_and_clip_a_long_one(laboratory):
    _, store, _, records, make = laboratory
    records.extend([make("filler line one\nfiller line two\na distinctive marker lives here"),
                    make("context " * 80 + "a distinctive marker lives here" + " trailing" * 40, key="long")])
    h = Harness(FixtureRunner(), {r.source_id: r for r in records}, store.graph(), store, AnalysisConfig(),
                threading.Event())
    h.provided = {r.source_id: [(1, 1)] for r in records}
    assert [item["line"] for item in h.nearest_lines("s1", "a distinctive marker lives here")] == [1]
    assert h.nearest_lines("s1", "a distinctive marker lives here") == [
        {"source_id": "s1", "line": 1, "text": "filler line one", "clipped": False}]
    [clipped] = h.nearest_lines("long", "a distinctive marker lives here")
    assert clipped["clipped"] is True and len(clipped["text"]) == 400
    assert "a distinctive marker lives here" in clipped["text"]


def test_validation_lists_multiple_bad_citations(laboratory):
    _, _, _, _, make = laboratory
    record = make("first\nsecond")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    with pytest.raises(FlowError) as failure:
        validator._check_all_citations({'event_candidates': [
            {'evidence': [{'source_id':record.source_id,'start_line':1,'end_line':1,'quote':'wrong'}]},
            {'evidence': [{'source_id':record.source_id,'start_line':2,'end_line':2,'quote':'also wrong'}]},
        ]})
    assert failure.value.args[0].count('인용문이') == 2


@pytest.mark.parametrize('kind,value',[('read_records','/etc/passwd'),('read_records','../../outside'),('read_diff','s1'),('read_file_at_revision','HEAD:/etc/passwd'),('read_existing_event','unknown')])
def test_manifest_escape_denied(laboratory,kind,value):
    h,_=harness(laboratory)
    result=h.read({'kind':kind,'ids':[value],'start_line':1,'end_line':1,'query':None,'unit_id':None})
    assert 'denied' in result[0]


def test_preserved_excerpt_cannot_read_gap(laboratory):
    _,store,engine,records,make=laboratory
    record=make('one\ntwo\nthree');pool={};provided={}
    evidence={'e':{'id':'e','source_id':'s1','content_hash':record.content_hash,'start_line':2,'end_line':2,'quote':'two','source':record.metadata()}}
    _rehydrate(pool,provided,evidence)
    h=Harness(FixtureRunner(),pool,store.graph(),store,AnalysisConfig(),threading.Event())
    h.provided=provided
    assert h.provide('s1',2,2)['lines'][0]['text']=='two'
    with pytest.raises(FlowError,match='보존된 인용'):
        h.provide('s1',1,2)


def test_nearby_git_and_conversation_share_bounded_context(laboratory):
    scope, store, _, _, make = laboratory
    chat = make("Discuss the change", key="chat", provider="codex")
    tree = chat.worktree_id
    commit = SourceRecord("commit", "git", None, "git", "Commit abc\n+change",
                          {"kind": "commit_diff", "commit": "abc", "root": str(scope.folder)},
                          recorded_at="2026-09-22T18:58:00+09:00", cwd=str(scope.folder), worktree_id=tree)
    far = SourceRecord("far", "git", None, "git", "unrelated old commit",
                       {"kind": "commit_diff", "commit": "far", "root": str(scope.folder)},
                       recorded_at="2026-09-21T10:00:00Z", cwd=str(scope.folder), worktree_id=tree)
    other_tree = SourceRecord("other_tree", "git", None, "git", "different worktree",
                              {"kind": "commit_diff", "commit": "other", "root": str(scope.folder)},
                              recorded_at="2026-09-22T10:01:00Z", cwd=str(scope.folder),
                              worktree_id="wt_other")
    pool = {r.source_id: r for r in (far, other_tree, chat, commit)}
    h = Harness(FixtureRunner(), pool, store.graph(), store, AnalysisConfig(), threading.Event())
    context = h.context([chat])
    assert any(r["source_id"] == commit.source_id and
               r["context_reason"] == "same_worktree_nearby_time"
               for r in context["context_only"])
    assert commit.source_id in {r["id"] for r in context["manifest"]["records"]}
    assert far.source_id not in {r["source_id"] for r in context["context_only"]}
    assert other_tree.source_id not in {r["source_id"] for r in context["context_only"]}
    earlier = Harness(FixtureRunner(), pool, store.graph(), store, AnalysisConfig(), threading.Event())
    earlier_context = earlier.context([commit])
    assert chat.source_id not in {r["id"] for r in earlier_context["manifest"]["records"]}


def test_earlier_unit_cannot_discover_or_read_later_session_record(laboratory):
    _, store, _, _, make = laboratory
    before = replace(make("first", key="before", provider="codex"), recorded_at="2026-09-22T10:00:00Z")
    after = replace(make("future secret", key="after", provider="codex"), recorded_at="2026-09-22T14:00:00Z")
    graph = store.graph()
    graph["events"] = [dict(id="future_event", title="later", session_ids=[before.session_id],
                            worktree_ids=[before.worktree_id], evidence_ids=[],
                            recorded_at=after.recorded_at)]
    h = Harness(FixtureRunner(), {r.source_id: r for r in (before, after)}, graph, store,
                AnalysisConfig(), threading.Event())
    context = h.context([before])
    assert after.source_id not in {r["id"] for r in context["manifest"]["records"]}
    assert "future_event" not in {e["id"] for e in context["existing_events"]}
    assert "denied" in h.read({"kind": "read_records", "ids": [after.source_id],
                               "start_line": 1, "end_line": 1, "query": None, "unit_id": None})[0]


def test_unavailable_saved_evidence_with_unknown_time_is_hidden_everywhere(laboratory):
    _, store, _, _, make = laboratory
    assigned = make("current unit", key="current")
    source = make("preserved secret", key="missing", session="old-session")
    source.recorded_at = None
    evidence = {"orphan_evidence": {"id": "orphan_evidence", "source_id": source.source_id,
        "content_hash": source.content_hash, "start_line": 1, "end_line": 1,
        "quote": source.content, "source": source.metadata()}}
    graph = store.graph()
    graph["events"] = [{"id": "orphan_event", "title": "preserved secret decision",
        "summary": "Unavailable source", "recorded_at": None, "session_ids": [],
        "worktree_ids": [], "evidence_ids": ["orphan_evidence"]}]
    store.publish(graph, [], {}, evidence, expected_version=0)
    h = Harness(FixtureRunner(), {assigned.source_id: assigned}, store.graph(), store,
                AnalysisConfig(), threading.Event(), unit_id="unit-current")
    context = h.context([assigned])
    assert context["existing_events"] == []
    assert h.selection_audit["excluded_events"] == [
        {"event_id": "orphan_event", "reason": "unsafe_or_unavailable_evidence"}]
    assert h.selection_audit["delivered_chars"]["total"] > 0
    search = h.read({"kind": "search_events", "ids": [], "query": "preserved secret",
                     "unit_id": "unit-current", "start_line": None, "end_line": None})
    assert search == []
    read = h.read({"kind": "read_existing_event", "ids": ["orphan_event"],
                   "query": None, "unit_id": None, "start_line": None, "end_line": None})
    assert "denied" in read[0]


def test_extracted_file_clue_recovers_old_decision_among_recent_events(laboratory):
    _, store, _, _, make = laboratory
    assigned = make("Return to the earlier login approach", key="current")
    graph = store.graph()
    graph["events"] = [dict(id="old_decision", title="src/auth.py login decision",
                            summary="Use the earlier token storage approach", session_ids=["other"],
                            worktree_ids=["other"], evidence_ids=[], recorded_at="2026-09-01T10:00:00Z")]
    graph["events"] += [dict(id=f"recent_{i}", title=f"Unrelated incident {i}", summary="Other work",
                             session_ids=[assigned.session_id], worktree_ids=[assigned.worktree_id],
                             evidence_ids=[], recorded_at="2026-09-22T09:00:00Z") for i in range(30)]
    h = Harness(FixtureRunner(), {assigned.source_id: assigned}, graph, store,
                AnalysisConfig(context_events=2), threading.Event())
    context = h.context([assigned], clues="src/auth.py login decision")
    assert "old_decision" in {item["id"] for item in context["existing_events"]}
    old = next(item for item in h.selection_audit["selected"] if item["event_id"] == "old_decision")
    assert old["matched_paths"] == ["src/auth.py"]
    assert "same_file_path" in old["reasons"]


def test_future_edge_and_open_item_are_hidden_from_earlier_unit(laboratory):
    _, store, _, _, make = laboratory
    before = replace(make("first", key="before"), recorded_at="2026-09-22T10:00:00Z")
    after = replace(make("later", key="after"), recorded_at="2026-09-22T14:00:00Z")
    earlier_evidence = dict(id="earlier_evidence", source_id=before.source_id,
                            content_hash=before.content_hash, start_line=1, end_line=1,
                            quote=before.content, source=before.metadata())
    later_evidence = dict(id="later_evidence", source_id=after.source_id,
                          content_hash=after.content_hash, start_line=1, end_line=1,
                          quote=after.content, source=after.metadata())
    graph = store.graph()
    graph["events"] = [dict(id="old_event", title="first", summary="first", session_ids=[before.session_id],
                            worktree_ids=[before.worktree_id], evidence_ids=["earlier_evidence"],
                            recorded_at=before.recorded_at)]
    graph["edges"] = [dict(id="future_edge", from_event_id="old_event", to_event_id="old_event",
                           active=True, evidence_ids=["later_evidence"])]
    graph["open_items"] = [dict(id="future_open", related_event_ids=["old_event"],
                                evidence_ids=["later_evidence"], status="open")]
    store.publish(graph, [], {}, {"earlier_evidence": earlier_evidence,
                                  "later_evidence": later_evidence}, expected_version=0)
    h = Harness(FixtureRunner(), {r.source_id: r for r in (before, after)}, store.graph(), store,
                AnalysisConfig(), threading.Event())
    context = h.context([before])
    assert [event["id"] for event in context["existing_events"]] == ["old_event"]
    assert context["existing_edges"] == []
    assert context["existing_open_items"] == []


def test_future_git_revision_file_is_not_readable(laboratory):
    scope, store, _, _, make = laboratory
    chat = make("before", key="chat", provider="codex")
    later = SourceRecord("later", "git", None, "git", "Commit future\n+++ b/future.py\n+change",
                         {"kind": "commit_diff", "commit": "abcdef", "root": str(scope.folder)},
                         recorded_at="2026-09-22T11:00:00Z", worktree_id=chat.worktree_id)
    h = Harness(FixtureRunner(), {r.source_id: r for r in (chat, later)}, store.graph(), store,
                AnalysisConfig(), threading.Event())
    context = h.context([chat])
    file_id = ident("file_", "abcdef", "future.py")
    assert file_id not in {item["id"] for item in context["manifest"]["files_at_revision"]}
    assert "denied" in h.read({"kind": "read_file_at_revision", "ids": [file_id],
                               "start_line": 1, "end_line": 1, "query": None, "unit_id": None})[0]


def test_context_only_record_cannot_be_the_only_source_of_new_event(laboratory):
    _, store, _, _, make = laboratory
    assigned = make(CASES[0][0], key="assigned")
    context = make(CASES[1][0], key="context")
    pool = {r.source_id: r for r in (assigned, context)}
    h = Harness(FixtureRunner(), pool, store.graph(), store, AnalysisConfig(), threading.Event())
    h.context([assigned])
    assigned_input, context_input = h.provide("assigned"), h.provide("context")
    runner = FixtureRunner()
    def extract(record):
        return runner.run({"stage": "extract", "data": {"snapshot_id": "snap_test",
            "unit_id": "unit_test", "new_records": [record]}}, {}, threading.Event())
    validator = EvidenceValidator(h.pool, h.provided, assigned_source_ids={"assigned"})
    validator.check_extraction(extract(assigned_input), "unit_test", "snap_test", store.graph())
    with pytest.raises(FlowError, match="이번 작업 단위"):
        validator.check_extraction(extract(context_input), "unit_test", "snap_test", store.graph())


def test_needs_evidence_round_trip_then_complete(laboratory):
    _,store,engine,records,make=laboratory
    records.append(make(CASES[0][0]))
    class Reader(FixtureRunner):
        def run(self,task,schema,cancel):
            output=super().run(task,schema,cancel)
            if task['stage']=='extract' and 'evidence_rounds' not in task:
                output.update(status='needs_evidence',event_candidates=[],read_requests=[{'kind':'read_records','ids':['s1'],'start_line':1,'end_line':1,'query':None,'unit_id':None}])
            return output
    runner=Reader();result=engine.analyze(lambda:runner)
    assert result['status']=='complete' and runner.calls==3
    assert runner.tasks[1]['evidence_rounds'][0][0]['results'][0]['source_id']=='s1'


def test_read_rounds_are_bounded(laboratory):
    _,store,engine,records,make=laboratory;records.append(make(CASES[0][0]))
    class Endless(FixtureRunner):
        def run(self,task,schema,cancel):
            output=super().run(task,schema,cancel)
            output.update(status='needs_evidence',event_candidates=[],read_requests=[{'kind':'read_records','ids':['s1'],'start_line':1,'end_line':1,'query':None,'unit_id':None}])
            return output
    runner=Endless();result=engine.analyze(lambda:runner)
    assert result['status']=='failed' and runner.calls==4
    assert store.graph()['version']==0


ESCAPED_PATCH = ('text(await tools.apply_patch("*** Begin Patch\\n+if [ -e \\"$bin/contexttrail\\" ]; then\\n'
                 '+  \\"$venv/bin/contexttrail\\" install-commands\\n+fi\\n*** End Patch"))')


def test_escape_decoded_quote_is_stored_as_exact_raw_source_text(laboratory):
    _, _, _, _, make = laboratory
    record = make("Tool: exec\n" + ESCAPED_PATCH, role="tool_call")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    decoded = '+  "$venv/bin/contexttrail" install-commands'
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2, "quote": decoded}])
    saved = validator.evidence[ids[0]]
    assert (saved["start_line"], saved["end_line"]) == (2, 2)
    assert saved["quote"] == ESCAPED_PATCH
    start, end = saved["focus"][0]
    assert saved["quote"][start:end] == '+  \\"$venv/bin/contexttrail\\" install-commands'
    assert validator.normalizations[0]["mode"] == "escape_decoded_substring_expanded_to_lines"


def test_a_repeated_quote_records_where_its_repeats_sit_without_text(laboratory):
    _, _, _, _, make = laboratory
    record = make("intro\nsame words here now\nmiddle\nmore\nsame words here now\nouter")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 6)]})
    with pytest.raises(FlowError, match="일치 2건, 서로 다른 줄"):
        validator.citations([{"source_id": record.source_id, "start_line": 2, "end_line": 5,
                              "quote": "same words here now"}])
    [item] = validator.mismatch_audit()
    assert item["shape"] == {"matches": 2, "span_lines": 4, "on_start_line": 1, "on_end_line": 1,
                             "whole_block": False}


def test_escape_decoded_quote_may_span_an_escaped_newline(laboratory):
    _, _, _, _, make = laboratory
    record = make(ESCAPED_PATCH, role="tool_call")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 1)]})
    quote = 'then\n+  "$venv/bin/contexttrail"'
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 1, "quote": quote}])
    start, end = validator.evidence[ids[0]]["focus"][0]
    assert ESCAPED_PATCH[start:end] == 'then\\n+  \\"$venv/bin/contexttrail\\"'


def test_escape_decoded_quote_rejects_ambiguous_or_invented_text(laboratory):
    _, _, _, _, make = laboratory
    record = make('say(\\"same phrase\\")\nx\ny\nsay(\\"same phrase\\") again', role="tool_call")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 4)]})
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다.*일치 2건, 서로 다른 줄"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 4,
                              "quote": 'say("same phrase")'}])
    with pytest.raises(FlowError, match="유일하게 일치하지 않습니다.*일치 0건"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 4,
                              "quote": 'say("other phrase")'}])


def test_over_escaped_quote_matches_plain_source_text_once(laboratory):
    _, _, _, _, make = laboratory
    line = 'meta {"stage": "extract", "status": "complete", "content_included": false}'
    record = make("header\n" + line, role="tool_result")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    # Seen live from gpt-6-sol: the model escaped a double quote the source holds plainly.
    ids = validator.citations([{"source_id": record.source_id, "start_line": 2, "end_line": 2,
                                "quote": 'content_included\\": false'}])
    saved = validator.evidence[ids[0]]
    assert saved["quote"] == line and (saved["start_line"], saved["end_line"]) == (2, 2)
    start, end = saved["focus"][0]
    assert line[start:end] == 'content_included": false'
    assert validator.normalizations[0]["mode"] == "quote_unescaped_substring_expanded_to_lines"
    with pytest.raises(FlowError, match="일치 0건"):  # unescaping never licenses different words
        validator.citations([{"source_id": record.source_id, "start_line": 2, "end_line": 2,
                              "quote": 'content_included\\": true'}])


def test_quote_repeated_within_one_patch_line_keeps_the_same_evidence(laboratory):
    _, _, _, _, make = laboratory
    patch = ('text(await tools.apply_patch("*** Begin Patch\\n-    printf \'Existing command was preserved\'\\n'
             '+    printf \'Existing command was preserved\'\\n*** End Patch"))')
    record = make("Tool: exec\n" + patch, role="tool_call")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 2,
                                "quote": "Existing command was preserved"}])
    saved = validator.evidence[ids[0]]
    unique = validator.citations([{"source_id": record.source_id, "start_line": 2, "end_line": 2, "quote": patch}])
    assert ids == unique and saved["quote"] == patch
    assert [patch[a:b] for a, b in saved["focus"]] == ["Existing command was preserved"] * 2
    assert validator.normalizations[0]["mode"] == "repeated_substring_within_same_lines"
    assert validator.normalizations[0]["matches"] == 2


def test_many_repeats_within_one_line_are_accepted_without_focus(laboratory):
    _, _, _, _, make = laboratory
    record = make(" ".join(["_MANAGED_MARKER"] * 6))
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 1)]})
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 1,
                                "quote": "_MANAGED_MARKER"}])
    assert "focus" not in validator.evidence[ids[0]]


GIT_DIFF = """diff --git a/install.sh b/install.sh
@@ -10,5 +10,5 @@
 if [ -e "$bin" ]; then
-    printf 'Existing command was preserved: %s' "$bin" >&2
+    printf 'Existing command was preserved: %s\\n' "$bin" >&2
     exit 1
@@ -40,2 +40,2 @@
-echo 'Existing command was preserved: later'
+echo done"""


def test_same_text_on_both_sides_of_one_hunk_covers_both_lines(laboratory):
    _, _, _, _, make = laboratory
    record = make(GIT_DIFF, role="git", provider="git")
    lines = GIT_DIFF.splitlines()
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 6)]})
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 6,
                                "quote": "Existing command was preserved: %s"}])
    saved = validator.evidence[ids[0]]
    assert (saved["start_line"], saved["end_line"]) == (4, 5)
    assert saved["quote"] == "\n".join(lines[3:5])
    assert len(saved["focus"]) == 2
    assert validator.normalizations[0]["mode"] == "diff_hunk_substring_expanded_to_lines"


def test_diff_matches_across_hunks_or_plain_lines_are_still_ambiguous(laboratory):
    _, _, _, _, make = laboratory
    record = make(GIT_DIFF, role="git", provider="git")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 9)]})
    with pytest.raises(FlowError, match="일치 3건, 서로 다른 줄"):
        validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 9,
                              "quote": "Existing command was preserved"}])
    far = make("-shared phrase here\n" + "\n".join(f" context {i}" for i in range(25)) + "\n+shared phrase here",
               key="far", role="git", provider="git")
    validator = EvidenceValidator({far.source_id: far}, {far.source_id: [(1, 27)]})
    with pytest.raises(FlowError, match="일치 2건, 서로 다른 줄"):
        validator.citations([{"source_id": far.source_id, "start_line": 1, "end_line": 27,
                              "quote": "shared phrase here"}])


def test_whole_line_citation_records_no_focus(laboratory):
    _, _, _, _, make = laboratory
    record = make("first line\nsecond line")
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]})
    ids = validator.citations([{"source_id": record.source_id, "start_line": 1, "end_line": 1, "quote": "first line"}])
    assert "focus" not in validator.evidence[ids[0]]


def test_cited_focus_survives_canonicalization_and_publish(laboratory):
    _, store, engine, _, make = laboratory
    record = make("prefix: unique decision phrase here; trailing context\nsecond line")
    unit_id, snapshot_id = "unit-focus", "snapshot-focus"
    output = {"status": "complete", "read_requests": [], "snapshot_id": snapshot_id,
        "unit_id": unit_id, "event_candidates": [{"id": "tmp:decision", "kind": "decision",
            "title": "Decision", "summary": "A decision", "actor": "user", "status": "adopted",
            "basis": "explicit_statement", "session_ids": [record.session_id],
            "worktree_ids": [record.worktree_id], "recorded_at": record.recorded_at,
            "occurred_at": None, "evidence": [{"source_id": record.source_id, "start_line": 1,
                                               "end_line": 1, "quote": "unique decision phrase here"}]}],
        "edge_candidates": [], "existing_event_matches": [], "open_items": [],
        "limitations": [], "unprocessed_record_ids": []}
    initial = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 2)]},
                                assigned_source_ids={record.source_id})
    initial.check_extraction(output, unit_id, snapshot_id, store.graph())
    [(evidence_id, item)] = initial.evidence.items()
    assert item["focus"] == [[8, 35]]
    extracted = {"output": output, "evidence": initial.evidence, "dependencies": {}}
    from projectflow.routing import RunnerPool
    prepared = engine._prepare_integration({"id": unit_id, "sources": [record.source_id]}, extracted,
        {record.source_id: record}, store.graph(), snapshot_id, "run-focus",
        RunnerPool(FixtureRunner, engine.config), threading.Event())
    assert prepared.validator.evidence[evidence_id]["focus"] == [[8, 35]]
    store.publish(store.graph(), [], {}, {evidence_id: item}, expected_version=0)
    store.publish(store.graph(), [], {}, {evidence_id: {**item, "focus": [[0, 6]]}}, expected_version=1)
    assert store.evidence(evidence_id)["focus"] == [[0, 6], [8, 35]]


def _lean_fixture(laboratory):
    _, store, _, _, make = laboratory
    earlier = [replace(make("\n".join(f"line {n} of record {i} " + "x" * 50 for n in range(1, 41)), key=f"old{i}"),
                       recorded_at="2026-09-22T09:00:00Z") for i in range(12)]
    assigned = replace(make("next step", key="now"), recorded_at="2026-09-22T12:00:00Z")
    events, evidence = [], {}
    for i, record in enumerate(earlier):
        evidence[f"ev{i}"] = dict(id=f"ev{i}", source_id=record.source_id, content_hash=record.content_hash,
                                  start_line=20, end_line=20, quote=record.content.splitlines()[19],
                                  source=record.metadata())
        events.append(dict(id=f"event{i}", title=f"step {i}", summary="did", session_ids=[record.session_id],
                           worktree_ids=[record.worktree_id], evidence_ids=[f"ev{i}"], recorded_at=record.recorded_at))
    graph = store.graph()
    graph["events"] = events
    store.publish(graph, [], {}, evidence, expected_version=0)
    return store, [*earlier, assigned], assigned


def test_lean_context_sends_cited_lines_not_whole_records(laboratory):
    store, records, assigned = _lean_fixture(laboratory)
    def build(mode):
        h = Harness(FixtureRunner(), {r.source_id: r for r in records}, store.graph(), store,
                    AnalysisConfig(context_mode=mode), threading.Event())
        return h, h.context([assigned])
    full_h, full = build("full")
    lean_h, lean = build("lean")
    assert lean_h.selection_audit["delivered_chars"]["total"] < full_h.selection_audit["delivered_chars"]["total"] * 0.6
    cited = [item for item in lean["context_only"] if item.get("context_reason") == "cited_lines"]
    assert cited and all(item["lines"][0]["line"] == 15 and item["lines"][-1]["line"] == 25 for item in cited)
    assert not any(len(item["lines"]) == 40 for item in lean["context_only"]
                   if item["source_id"] not in {"old10", "old11"})  # the two preceding records stay whole
    assert all(item.get("quote_in_context_only") or "quote" in item for item in lean["existing_evidence"].values())
    assert len(lean["existing_events"]) <= 12


def test_lean_context_is_the_default_and_full_changes_the_routing_signature(laboratory):
    _, store, engine, _, _ = laboratory
    assert engine.config.context_mode == "lean"
    lean = engine._routing_signature()
    engine.config.context_mode = "full"
    assert engine._routing_signature() != lean
    with pytest.raises(FlowError, match="context_mode"):
        AnalysisConfig(context_mode="tiny").validate()


def test_integrate_evidence_is_full_by_default_and_only_full_or_reuse(laboratory):
    _, _, engine, _, _ = laboratory
    assert engine.config.integrate_evidence == "full"
    # It changes only the integration request, so a finished extraction is never resent.
    signature = engine._routing_signature()
    engine.config.integrate_evidence = "reuse"
    assert engine._routing_signature() == signature
    with pytest.raises(FlowError, match="integrate_evidence"):
        AnalysisConfig(integrate_evidence="partial").validate()


def _reuse_case(laboratory):
    """One event candidate and the GraphDelta a reuse-mode model returns with no quotes at all."""
    _, store, _, records, make = laboratory
    record = make("동시 쓰기 때문에 SQLite로 바꾸기로 했습니다.")
    records.append(record)
    citation = {"source_id": record.source_id, "start_line": 1, "end_line": 1, "quote": record.content}
    candidate = {"id": "tmp:sqlite", "kind": "revision", "title": "SQLite로 전환", "summary": "동시 쓰기 실패 때문",
                 "actor": "user", "status": "adopted", "basis": "explicit_statement",
                 "session_ids": [record.session_id], "worktree_ids": [], "recorded_at": None,
                 "occurred_at": None, "evidence": [citation]}
    candidates = {"unit_id": "unit", "event_candidates": [candidate], "edge_candidates": [],
                  "existing_event_matches": [], "open_items": [], "limitations": [], "unprocessed_record_ids": []}
    delta = {"status": "complete", "read_requests": [], "snapshot_id": "snap", "base_graph_version": 0,
             "events_to_add": [{**{k: v for k, v in candidate.items() if k != "id"},
                                "id": "tmp:switch", "evidence": []}],
             "events_to_update": [], "edges_to_add": [], "edges_to_invalidate": [],
             "open_items_to_upsert": [], "open_items_to_resolve": [],
             "candidate_resolutions": [{"candidate_id": "tmp:sqlite", "candidate_kind": "event",
                 "disposition": "added", "target_ids": ["tmp:switch"], "reason": "결정을 뒤집었습니다.",
                 "evidence": []}],
             "change_attributions": [{"operation": "events_to_add", "item_id": "tmp:switch",
                 "candidate_ids": ["tmp:sqlite"], "reason": "결정을 뒤집었습니다.", "evidence": []}],
             "review_issues": [], "review_resolutions": [], "limitations": []}
    validator = EvidenceValidator({record.source_id: record}, {record.source_id: [(1, 1)]})
    return validator, delta, candidates, store.graph(), citation


def test_reuse_fills_the_same_citation_dicts_the_candidate_already_carried(laboratory):
    validator, delta, candidates, graph, citation = _reuse_case(laboratory)
    result = validator.apply_delta(delta, graph, "snap", "run", candidates, evidence_reuse=True)
    assert delta["events_to_add"][0]["evidence"] == [citation]
    assert delta["candidate_resolutions"][0]["evidence"] == [citation]
    assert delta["change_attributions"][0]["evidence"] == [citation]
    assert [item for item in validator.normalizations
            if item["mode"] == "evidence_reused_from_candidates"] == [
        {"mode": "evidence_reused_from_candidates", "items": 3}]
    [event] = result["events"]
    assert event["evidence_ids"] == validator.citations([citation])
    # A second apply of the same delta fills nothing and records nothing more.
    validator.normalizations.clear()
    validator.apply_delta(delta, graph, "snap", "run", candidates, evidence_reuse=True)
    assert not [item for item in validator.normalizations
                if item["mode"] == "evidence_reused_from_candidates"]


def test_reuse_rejects_an_item_no_candidate_supports(laboratory):
    validator, delta, candidates, graph, _ = _reuse_case(laboratory)
    delta["candidate_resolutions"][0].update(disposition="excluded", target_ids=[])
    with pytest.raises(FlowError, match="events_to_add tmp:switch"):
        validator.apply_delta(delta, graph, "snap", "run", candidates, evidence_reuse=True)


def test_reuse_still_verifies_the_quote_the_model_did_write(laboratory):
    validator, delta, candidates, graph, _ = _reuse_case(laboratory)
    delta["events_to_add"][0]["evidence"] = [{"source_id": "s1", "start_line": 1, "end_line": 1,
                                              "quote": "원문에 없는 다른 문장입니다"}]
    with pytest.raises(FlowError, match="인용문"):
        validator.apply_delta(delta, graph, "snap", "run", candidates, evidence_reuse=True)


def _tool_restore_case(make, candidate_cites_result=True):
    said = make("I ran the unit tests and they all passed now", key="said", role="assistant")
    ran = make("3 passed in 0.12s", key="ran", role="tool_result")
    cite = lambda record: {"source_id": record.source_id, "start_line": 1, "end_line": 1, "quote": record.content}
    validator = EvidenceValidator({r.source_id: r for r in (said, ran)}, {said.source_id: [(1, 1)], ran.source_id: [(1, 1)]})
    candidates = {"event_candidates": [{"id": "tmp:c1", "evidence": [cite(said), *([cite(ran)] if candidate_cites_result else [])]}],
                  "edge_candidates": [], "open_items": []}
    event = {"id": "tmp:e1", "basis": "tool_record", "status": "observed_success", "evidence": [cite(said)]}
    output = {"candidate_resolutions": [{"candidate_id": "tmp:c1", "candidate_kind": "event", "target_ids": ["tmp:e1"]}],
              "events_to_add": [event]}
    return validator, candidates, output, event, ran


def test_integration_restores_the_tool_citation_its_candidate_carried(laboratory):
    _, _, _, _, make = laboratory
    validator, candidates, output, event, ran = _tool_restore_case(make)
    assert validator.restore_tool_evidence(output, candidates) == 1
    assert [c["source_id"] for c in event["evidence"]] == ["said", ran.source_id]
    assert validator.normalizations == [{"mode": "tool_evidence_restored_from_candidates", "events": 1}]


def test_integration_does_not_invent_a_tool_citation_the_candidate_lacked(laboratory):
    _, _, _, _, make = laboratory
    validator, candidates, output, event, _ = _tool_restore_case(make, candidate_cites_result=False)
    assert validator.restore_tool_evidence(output, candidates) == 0
    assert [c["source_id"] for c in event["evidence"]] == ["said"]
    output["candidate_resolutions"][0]["target_ids"] = ["tmp:other"]
    assert validator.restore_tool_evidence(output, _tool_restore_case(make)[1]) == 0


def _doc_verifies_case(make, patch_file="docs/DECISIONS.md", command="pytest -q tests"):
    from dataclasses import replace
    edit = make(f"Tool: apply_patch\n*** Begin Patch\n*** Update File: {patch_file}\n@@\n-old\n+new\n*** End Patch",
                key="edit", role="tool_call")
    run = replace(make(f'Tool: Bash\n{{"command": "{command}"}}', key="run", role="tool_call"), tool_call_id="t1")
    result = replace(make("12 passed in 0.4s", key="result", role="tool_result"), tool_call_id="t1")
    records = {r.source_id: r for r in (edit, run, result)}
    validator = EvidenceValidator(records, {r.source_id: [(1, 99)] for r in records.values()})
    cite = lambda r: {"source_id": r.source_id, "start_line": 1, "end_line": 1, "quote": r.content.splitlines()[0]}
    output = {"events_to_add": [{"id": "tmp:change", "evidence": [cite(edit)]}, {"id": "tmp:ran", "evidence": [cite(result)]}],
              "edges_to_add": [{"id": "tmp:v", "relation": "verifies", "from_event_id": "tmp:change", "to_event_id": "tmp:ran"}],
              "change_attributions": [{"operation": "edges_to_add", "item_id": "tmp:v", "candidate_ids": ["tmp:c"]}],
              "candidate_resolutions": [{"candidate_id": "tmp:c", "candidate_kind": "edge", "disposition": "added",
                                         "target_ids": ["tmp:v"], "reason": "r", "evidence": []}],
              "review_issues": [], "limitations": []}
    return validator, output


def test_a_test_run_does_not_verify_a_documentation_only_change(laboratory):
    _, _, _, _, make = laboratory
    validator, output = _doc_verifies_case(make)
    assert validator.drop_unchecked_doc_verifies(output, {"events": []}) == 1
    assert output["edges_to_add"] == [] and output["change_attributions"] == []
    row = output["candidate_resolutions"][0]
    assert (row["disposition"], row["target_ids"]) == ("excluded", [])
    assert output["limitations"] and validator.normalizations == [{"mode": "doc_verifies_dropped", "edges": 1}]


def test_a_run_that_reads_the_document_or_a_code_change_keeps_its_verifies(laboratory):
    _, _, _, _, make = laboratory
    named, output = _doc_verifies_case(make, command="python scripts/check_links.py docs/DECISIONS.md")
    assert named.drop_unchecked_doc_verifies(output, {"events": []}) == 0 and output["edges_to_add"]
    code, output = _doc_verifies_case(make, patch_file="src/app.py")
    assert code.drop_unchecked_doc_verifies(output, {"events": []}) == 0 and output["edges_to_add"]


def test_documentation_only_edits_are_flagged_in_tool_steps(laboratory):
    from dataclasses import replace
    from projectflow.analysis import tool_steps
    _, _, _, _, make = laboratory
    doc = replace(make("Tool: apply_patch\n*** Begin Patch\n*** Update File: README.md\n@@\n-a\n+b\n*** End Patch",
                       key="doc", role="tool_call"), tool_call_id="d")
    code = replace(make('Tool: Edit\n{"file_path": "/x/src/app.py", "old_string": "a"}', key="code", role="tool_call"),
                   tool_call_id="c")
    steps = {s["call"]: s for s in tool_steps([doc, code])}
    assert steps[doc.source_id]["doc"] is True and "doc" not in steps[code.source_id]
