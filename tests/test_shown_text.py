"""What the model is shown of long lines and long quotes: cut, never whole, and still exact source text."""
import threading

from contexttrail.analysis import (LINE_CHARS, SHOWN_QUOTE_CHARS, AnalysisConfig, Harness, shown_line, shown_quote,
                                   unit_cost)
from contexttrail.model import SourceRecord
from contexttrail.store import Store


def record(text: str, role: str = "tool_result") -> SourceRecord:
    return SourceRecord("src_long", "codex", "s1", role, text, {"kind": "test"}, recorded_at="2026-10-07T00:00:00Z")


def test_a_long_line_is_cut_with_a_note_and_the_cost_follows():
    line = "x" * 10_000
    assert shown_line("short") == "short"
    cut = shown_line(line)
    assert cut.startswith("x" * 1_000) and cut.endswith("x" * 400) and "8500 characters here" in cut
    assert len(cut) < LINE_CHARS + 120
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


def test_context_records_are_shown_head_and_tail_whatever_their_role():
    from contexttrail.analysis import CONTEXT_HEAD_CHARS, CONTEXT_TAIL_CHARS, _view
    long_assistant = record("\n".join(f"line {n} " + "z" * 80 for n in range(400)), role="assistant")
    assert _view(long_assistant) is None  # the unit's own assistant text goes whole
    head, tail = _view(long_assistant, CONTEXT_HEAD_CHARS, CONTEXT_TAIL_CHARS, force=True)
    assert head < 25 and tail > 390  # as context it is head and tail


def test_a_quote_cut_at_a_line_break_still_resolves_to_its_lines():
    from contexttrail.model import SourceRecord
    from contexttrail.schema import EvidenceValidator
    text = "\n".join(["Script completed", "Output:", "x" * (SHOWN_QUOTE_CHARS - 26), "a much longer line " * 40, "end"])
    record = SourceRecord("r", "codex", "s", "tool_result", text, {})
    quote = "\n".join(text.splitlines()[:4])
    shown = shown_quote(quote, None)
    assert not shown.endswith("\n")  # the cut fell right after a line break
    validator = EvidenceValidator({"r": record}, {"r": [(1, 5)]})
    for asked in (shown, "Script completed\nOutput:\n"):  # a quote ending on a line break, as the shown part or as written
        validator.citations([{"source_id": "r", "start_line": 1, "end_line": 4, "quote": asked}])
