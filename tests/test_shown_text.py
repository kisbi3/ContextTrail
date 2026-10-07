"""What the model is shown of long lines and long quotes: cut, never whole, and still exact source text."""
import threading

from contexttrail.analysis import (LINE_CHARS, SHOWN_QUOTE_CHARS, AnalysisConfig, Harness, candidates_for_model,
                                   shown_line, shown_quote, unit_cost)
from contexttrail.model import SourceRecord
from contexttrail.store import Store


def record(text: str, role: str = "tool_result") -> SourceRecord:
    return SourceRecord("src_long", "codex", "s1", role, text, {"kind": "test"}, recorded_at="2026-10-07T00:00:00Z")


def test_a_long_line_is_cut_with_a_note_and_the_cost_follows():
    line = "x" * 10_000
    assert shown_line("short") == "short"
    cut = shown_line(line)
    assert cut.startswith("x" * LINE_CHARS) and "8500 more characters" in cut and len(cut) < LINE_CHARS + 120
    one_line = record(line)
    assert unit_cost(one_line) < LINE_CHARS + 200  # not 10,000
    assert unit_cost(record("a\nb\nc")) == 6


def test_provide_shows_the_cut_line_and_a_quote_from_it_is_source_text(tmp_path):
    rec = record("first\n" + "y" * 5_000 + "\nlast")
    store = Store(tmp_path / "state", "scope")
    harness = Harness(None, {rec.source_id: rec}, {"version": 0, "events": [], "edges": [], "open_items": []},
                      store, AnalysisConfig(), threading.Event())
    shown = harness.provide(rec.source_id)
    texts = [line["text"] for line in shown["lines"]]
    assert texts[0] == "first" and texts[2] == "last" and "[cut:" in texts[1]
    assert rec.content.splitlines()[1].startswith(texts[1].split(" [cut:")[0])


def test_a_quote_is_shown_as_its_cited_part_or_its_head():
    short = "Use SQLite."
    assert shown_quote(short, None) == short
    lines = "a" * 200 + "DECISIVE PART" + "b" * 400
    assert shown_quote(lines, [[200, 213]]) == "DECISIVE PART"
    assert shown_quote(lines, [[0, 3], [200, 213]]) == "DECISIVE PART"  # the longest cited piece
    assert shown_quote(lines, None) == lines[:SHOWN_QUOTE_CHARS]
    assert shown_quote(lines, [[0, 5_000]]) == lines[:SHOWN_QUOTE_CHARS]  # a span past the end is ignored, head shown
    candidates = {"event_candidates": [{"id": "tmp_a", "evidence": [{"source_id": "src_long", "start_line": 1, "end_line": 1, "quote": lines}]}],
                  "edge_candidates": [], "open_items": [], "existing_event_matches": []}
    evidence = {"evi_1": {"source_id": "src_long", "start_line": 1, "end_line": 1, "quote": lines, "focus": [[200, 213]]}}
    view = candidates_for_model(candidates, evidence)
    assert view["event_candidates"][0]["evidence"][0]["quote"] == "DECISIVE PART"
    assert candidates["event_candidates"][0]["evidence"][0]["quote"] == lines  # the code keeps the canonical quote


def test_context_records_are_shown_head_and_tail_whatever_their_role():
    from contexttrail.analysis import CONTEXT_HEAD_CHARS, CONTEXT_TAIL_CHARS, _view
    long_assistant = record("\n".join(f"line {n} " + "z" * 80 for n in range(400)), role="assistant")
    assert _view(long_assistant) is None  # the unit's own assistant text goes whole
    head, tail = _view(long_assistant, CONTEXT_HEAD_CHARS, CONTEXT_TAIL_CHARS, force=True)
    assert head < 25 and tail > 390  # as context it is head and tail


def test_a_request_over_the_budget_loses_background_then_quotes_then_cited_lines_then_the_index():
    from contexttrail.analysis import TRIM_STEPS, trim_step
    data = {"context_only": [{"source_id": "a", "context_reason": "same_worktree_nearby_time", "lines": []},
                             {"source_id": "b", "context_reason": "related_context", "lines": []},
                             {"source_id": "c", "context_reason": "cited_lines", "lines": []}],
            "existing_evidence": {"evi_1": {"id": "evi_1", "quote": "Use SQLite."}, "evi_2": {"id": "evi_2", "quote_in_context_only": True}},
            "manifest": {"records": [{"id": f"src_{n}"} for n in range(50)], "events": [{"id": f"ev_{n}"} for n in range(50)]}}
    done = []
    steps = []
    while (step := trim_step(data, done)) is not None:
        done.append(step)
        steps.append(step)
        if step == "context_background":
            assert [c["source_id"] for c in data["context_only"]] == ["c"]
        if step == "existing_evidence_quotes":
            assert "quote" not in data["existing_evidence"]["evi_1"] and data["existing_evidence"]["evi_1"]["quote_omitted_for_budget"]
            assert data["existing_evidence"]["evi_2"] == {"id": "evi_2", "quote_in_context_only": True}
    assert steps == list(TRIM_STEPS)
    assert data["context_only"] == [] and len(data["manifest"]["records"]) == 20 and data["manifest"]["index_cut_for_budget"]
    assert trim_step(data, done) is None  # nothing left: the unit fails
