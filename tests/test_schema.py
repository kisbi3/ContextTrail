

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

