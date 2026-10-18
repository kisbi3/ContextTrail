"""Evidence blocks: each record cut by code into numbered pieces a model can cite by ID (`docs/plans/PIPELINE_V2.md` §1).

A model that cites a block ID writes no quote and no line number, so it cannot get either wrong; the block becomes
an ordinary citation (source and line range) and `EvidenceValidator.fill_cut_quotes` keeps the exact lines as its
quote. Blocks follow the record's own structure: paragraphs of what a person or an assistant wrote (where a
decision, a reason or a conclusion is stated), a tool call whole, and a tool result's head, tail and failure lines.
The cut is deterministic: the same record gives the same blocks.
"""
from __future__ import annotations

import re

from .model import SourceRecord

# At most this many lines in one block (within `schema.CUT_QUOTE_LINES`, so a block is kept whole as its quote).
BLOCK_LINES = 12
# A tool result is cited by its head, its tail and the lines that say it failed.
RESULT_HEAD_LINES, RESULT_TAIL_LINES = 6, 6
FAILURE_LINE = re.compile(r"Traceback|Error\b|error:|FAILED|\bfailed\b|Exit code: [1-9]|exited with code [1-9]|AssertionError")


def _spans(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """Lines start..end (1-based, inclusive) in pieces of at most BLOCK_LINES, skipping blank edges."""
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    return [(lo, min(lo + BLOCK_LINES - 1, end)) for lo in range(start, end + 1, BLOCK_LINES)] if start <= end else []


def blocks_of(record: SourceRecord) -> list[tuple[int, int]]:
    """The line ranges of one record's blocks, in order."""
    lines = record.content.splitlines()
    if not lines:
        return []
    if record.role == "tool_result":
        last = len(lines)
        found = _spans(lines, 1, min(RESULT_HEAD_LINES, last))
        failures = [n for n, line in enumerate(lines, 1) if RESULT_HEAD_LINES < n <= last - RESULT_TAIL_LINES
                    and FAILURE_LINE.search(line)]
        for n in failures[:4]:
            found += _spans(lines, max(RESULT_HEAD_LINES + 1, n - 1), min(n + 1, last - RESULT_TAIL_LINES))
        if last > RESULT_HEAD_LINES:
            found += _spans(lines, max(RESULT_HEAD_LINES + 1, last - RESULT_TAIL_LINES + 1), last)
        return sorted(dict.fromkeys(found))
    if record.role == "tool_call":
        return _spans(lines, 1, len(lines))
    # What someone wrote: its paragraphs.
    found, start = [], None
    for n, line in enumerate(lines + [""], 1):
        if line.strip() and start is None:
            start = n
        elif not line.strip() and start is not None:
            found += _spans(lines, start, n - 1)
            start = None
    return found


def number_blocks(records: list[SourceRecord]) -> list[dict]:
    """Every block of these records with its ID (`B1`, `B2`, … in record order) and its lines' text."""
    numbered = []
    for record in records:
        lines = record.content.splitlines()
        for start, end in blocks_of(record):
            numbered.append({"id": f"B{len(numbered) + 1}", "source_id": record.source_id, "start_line": start,
                             "end_line": end, "text": "\n".join(lines[start - 1:end])})
    return numbered


def citations(block_ids: list[str], blocks: list[dict]) -> list[dict]:
    """The citations the chosen blocks stand for (source and lines, no quote: code keeps the lines). Unknown IDs are
    left out; the caller decides whether an answer with none left is refused."""
    by_id = {block["id"]: block for block in blocks}
    return [{"source_id": by_id[i]["source_id"], "start_line": by_id[i]["start_line"], "end_line": by_id[i]["end_line"],
             "quote": None} for i in dict.fromkeys(block_ids) if i in by_id]
