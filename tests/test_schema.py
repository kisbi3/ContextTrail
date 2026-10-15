

def test_delta_for_model_drops_only_evidence_copied_from_the_candidate():
    from contexttrail.schema import delta_for_model
    quote = [{"source_id": "src_1", "quote": "Use SQLite.", "start_line": 1, "end_line": 1}]
    other = [{"source_id": "src_2", "quote": "Tests pass.", "start_line": 3, "end_line": 3}]
    candidates = {"event_candidates": [{"id": "tmp_a", "evidence": quote}], "edge_candidates": [], "open_items": []}
    delta = {"candidate_resolutions": [{"candidate_id": "tmp_a", "disposition": "added", "evidence": quote},
                                       {"candidate_id": "tmp_zzz", "disposition": "excluded", "evidence": quote}],
             "change_attributions": [{"operation": "events_to_add", "item_id": "tmp_a", "candidate_ids": ["tmp_a"], "evidence": quote},
                                     {"operation": "events_to_update", "item_id": "ev_9", "candidate_ids": ["tmp_a"], "evidence": other},
                                     {"operation": "events_to_update", "item_id": "ev_8", "candidate_ids": [], "evidence": quote}],
             "events_to_add": [{"id": "tmp_a", "evidence": quote}]}
    view = delta_for_model(delta, candidates)
    assert view["events_to_add"] == [{"id": "tmp_a", "candidate": "unchanged"}]  # the candidate itself, named
    assert "evidence" not in view["candidate_resolutions"][0] and view["candidate_resolutions"][1]["evidence"] == quote
    assert "evidence" not in view["change_attributions"][0]
    assert view["change_attributions"][1]["evidence"] == other and view["change_attributions"][2]["evidence"] == quote
    from contexttrail.schema import expand_for_model_view
    assert expand_for_model_view(view, candidates)["events_to_add"] == delta["events_to_add"]
    changed = {**delta, "events_to_add": [{"id": "tmp_a", "evidence": quote, "title": "edited"}]}
    assert delta_for_model(changed, candidates)["events_to_add"] == changed["events_to_add"]  # an edited item stays whole
    assert delta["candidate_resolutions"][0]["evidence"] == quote  # the original is untouched


def test_a_citation_without_a_quote_gets_the_cited_lines_cut_by_code():
    import pytest
    from contexttrail.model import SourceRecord
    from contexttrail.schema import CUT_QUOTE_LINES, EvidenceValidator
    from contexttrail.util import FlowError
    text = "\n".join(f"line {n}" for n in range(1, 41))
    record = SourceRecord("src_a", "claude", "s", "assistant", text, {"kind": "jsonl", "path": "/l", "line": 1})
    checker = EvidenceValidator({"src_a": record}, {"src_a": [(1, 30)]})
    answer = {"event_candidates": [{"evidence": [{"source_id": "src_a", "start_line": 3, "end_line": 4}]}],
              "edge_candidates": [{"evidence": [{"source_id": "src_a", "start_line": 5, "end_line": 40}]}]}
    assert checker.fill_cut_quotes(answer) == 2
    first = answer["event_candidates"][0]["evidence"][0]
    assert first["quote"] == "line 3\nline 4" and (first["start_line"], first["end_line"]) == (3, 4)
    # A long range is narrowed to the shown lines and to the cut's size.
    second = answer["edge_candidates"][0]["evidence"][0]
    assert (second["start_line"], second["end_line"]) == (5, 5 + CUT_QUOTE_LINES - 1)
    assert second["quote"].startswith("line 5\n") and second["quote"].endswith(f"line {5 + CUT_QUOTE_LINES - 1}")
    assert [n["mode"] for n in checker.normalizations] == ["quote_cut_by_code"] * 2
    # The cut quote passes the usual checks; lines the model never saw are refused as before.
    assert len(checker.citations(answer["event_candidates"][0]["evidence"])) == 1
    with pytest.raises(FlowError):
        checker.citations([{"source_id": "src_a", "start_line": 35, "end_line": 36}])

