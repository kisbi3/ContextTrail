import json
from pathlib import Path

import pytest

from contexttrail.analysis import AnalysisConfig
from contexttrail.cli import main
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.eval_review import render_eval_review
from contexttrail.evaluation import run_eval


def test_eval_writes_private_model_review_with_candidates(tmp_path):
    output = tmp_path / "eval"
    report = run_eval("demo", output, "mock", AnalysisConfig())
    assert report["first_run"]["status"] == "complete"
    captures = sorted((output / "call-review").glob("*.json"))
    assert captures
    assert all(path.stat().st_mode & 0o077 == 0 for path in captures)
    assert any(json.loads(path.read_text())["response"].get("story") for path in captures)
    page = (output / "review.html").read_text()
    # The story's events, the work it named and the requests it answered, each under its section.
    assert "이야기 사건" in page and CASES[1][2] in page
    assert "작업 이름" in page and "요청과 답" in page
    assert "모델에 보낸 정확한 요청" in page and "모델의 전체 구조화 응답" in page
    assert (output / "review.html").stat().st_mode & 0o077 == 0


def test_rejected_model_response_is_visible_and_old_eval_can_be_reviewed(tmp_path, monkeypatch, capsys):
    import contexttrail.evaluation as evaluation

    class WrongStatus(FixtureRunner):
        def run(self, task, schema, cancel):
            response = super().run(task, schema, cancel)
            for event in response["story"]:  # refused again after the repair
                event["status"] = "<invented-status>"
            return response

    monkeypatch.setattr(evaluation, "FixtureRunner", WrongStatus)
    output = tmp_path / "failed"
    report = run_eval("demo", output, "mock", AnalysisConfig())
    # Each unit keeps the work code found, without the story.
    assert report["first_run"]["status"] == "complete"
    captures = [json.loads(path.read_text()) for path in (output / "call-review").glob("*.json")]
    assert len(captures) == 4  # per unit: the story call and its repair
    assert all(item["status"] == "validation_error" for item in captures)
    assert all(event["status"] == "<invented-status>" for item in captures for event in item["response"]["story"])
    page = (output / "review.html").read_text()
    assert "&lt;invented-status&gt;" in page
    assert "검증 오류:" in page

    for path in (output / "call-review").glob("*.json"):
        path.unlink()
    assert main(["review", str(output)]) == 0
    assert "평가 검토 HTML:" in capsys.readouterr().out
    assert "모델 응답 원문을 저장하지 않았습니다" in (output / "review.html").read_text()


@pytest.mark.parametrize("screen,words", [
    ("ko", ("모델 인용", "실제 원문", "정확한 줄 인용", "문자열 불일치")),
    ("en", ("Model quote", "Actual source", "exact line quote", "string mismatch"))])
def test_an_older_eval_shows_each_quote_against_the_source(tmp_path, screen, words):
    from contexttrail import i18n

    i18n.set_language(screen)
    output = tmp_path / "eval"
    run_eval("demo", output, "mock", AnalysisConfig())
    # An eval saved when the model quoted its evidence (an extraction answer): one exact quote and one invented.
    call = json.loads((output / "calls.json").read_text())[0]
    capture = output / "call-review" / f"{call['id']}.json"
    saved = json.loads(capture.read_text())
    saved["response"] = {"event_candidates": [{
        "id": "tmp:e1", "title": CASES[0][2], "kind": "decision", "status": "adopted",
        "evidence": [{"source_id": "eval-s0", "start_line": 1, "end_line": 1, "quote": CASES[0][0]},
                     {"source_id": "eval-s0", "start_line": 1, "end_line": 1, "quote": "<invented-quote>"}]}]}
    capture.write_text(json.dumps(saved, ensure_ascii=False))
    render_eval_review(output)
    page = (output / "review.html").read_text()
    assert all(word in page for word in words)
    assert "&lt;invented-quote&gt;" in page


def test_eval_reports_progress_messages_while_it_runs(tmp_path):
    messages = []
    run_eval("demo", tmp_path / "eval", "mock", AnalysisConfig(), progress=messages.append)
    assert any("단위 완료" in message for message in messages)


def test_eval_review_and_preview_read_english_when_the_screen_language_is_english(tmp_path):
    from contexttrail import i18n
    from contexttrail.input_preview import preview_eval

    i18n.set_language("en")
    output = tmp_path / "eval"
    run_eval("demo", output, "mock", AnalysisConfig())
    page = (output / "review.html").read_text()
    assert "<html lang='en'>" in page and "<h1>ContextTrail eval review</h1>" in page
    assert "Story events" in page and "Exact request sent to the model" in page
    assert "Full structured model response" in page
    assert "모델의 전체 구조화 응답" not in page
    preview = preview_eval("demo", tmp_path / "preview", AnalysisConfig())
    page = Path(preview["preview"]).read_text()
    assert "<html lang='en'>" in page and "How the input is built" in page
    assert preview["units"][0]["boundary_reason"] == "start of the sample"
