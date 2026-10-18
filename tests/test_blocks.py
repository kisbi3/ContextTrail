from contexttrail.blocks import BLOCK_LINES, blocks_of, citations, number_blocks
from contexttrail.model import SourceRecord
from contexttrail.schema import EvidenceValidator


def record(key, role, text):
    return SourceRecord(key, "codex", "s", role, text, {}, recorded_at="2026-10-12T04:39:15Z")


REPORT = ("완료된 7일 분석 결과입니다.\n\nBootstrap도 실제로 실행했습니다. 7일 블록은 표본이 바뀌지 않아\n불확실성을 평가할 수 없어 한 달로 확대 중입니다.\n\n"
          "BTC 현물은 탐색 범위의 경계에서 최대여서, 내부 최댓값 가설을 그대로 지지하지 않습니다.")


def test_what_someone_wrote_is_cut_into_its_paragraphs_so_a_conclusion_has_its_own_block():
    blocks = number_blocks([record("say", "assistant", REPORT)])
    assert [(b["start_line"], b["end_line"]) for b in blocks] == [(1, 1), (3, 4), (6, 6)]
    assert blocks[2]["text"].startswith("BTC 현물은") and [b["id"] for b in blocks] == ["B1", "B2", "B3"]
    long = record("long", "assistant", "\n".join(f"line {n}" for n in range(1, 30)))
    assert all(end - start < BLOCK_LINES for start, end in blocks_of(long)) and blocks_of(long)[-1][1] == 29


def test_a_tool_result_is_cited_by_its_head_its_tail_and_its_failure_lines():
    lines = [f"collected {n}" for n in range(1, 41)]
    lines[19] = "E   AssertionError: expected 3"
    spans = blocks_of(record("out", "tool_result", "\n".join(lines)))
    assert spans[0] == (1, 6) and spans[-1] == (35, 40)
    assert any(start <= 20 <= end for start, end in spans[1:-1])  # the failure line has a block of its own
    assert blocks_of(record("call", "tool_call", "Tool: Bash\n{\"command\": \"pytest -q\"}")) == [(1, 2)]


def test_a_cited_block_becomes_the_exact_lines_by_code_and_the_cut_is_the_same_each_time():
    say = record("say", "assistant", REPORT)
    blocks = number_blocks([say])
    assert number_blocks([say]) == blocks
    found = citations(["B3", "B3", "B9"], blocks)  # repeats and unknown IDs are dropped
    assert found == [{"source_id": "say", "start_line": 6, "end_line": 6, "quote": None}]
    validator = EvidenceValidator({"say": say}, {"say": [(1, 6)]})
    answer = {"evidence": found}
    assert validator.fill_cut_quotes(answer) == 1
    assert answer["evidence"][0]["quote"].startswith("BTC 현물은 탐색 범위의 경계에서 최대여서")
