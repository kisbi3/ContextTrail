import json
from pathlib import Path

from projectflow.analysis import AnalysisConfig
from projectflow.cli import main
from projectflow.demo import FixtureRunner
from projectflow.evaluation import run_eval


def test_eval_writes_private_model_review_with_candidates(tmp_path):
    output = tmp_path / "eval"
    report = run_eval("demo", output, "mock", AnalysisConfig())
    assert report["first_run"]["status"] == "complete"
    captures = sorted((output / "call-review").glob("*.json"))
    assert captures
    assert all(path.stat().st_mode & 0o077 == 0 for path in captures)
    assert any(json.loads(path.read_text())["response"].get("event_candidates") for path in captures)
    page = (output / "review.html").read_text()
    assert "모델 인용" in page and "실제 원문" in page
    assert "모델의 전체 구조화 응답" in page
    assert "정확한 줄 인용" in page
    assert (output / "review.html").stat().st_mode & 0o077 == 0


def test_rejected_model_response_is_visible_and_old_eval_can_be_reviewed(tmp_path, monkeypatch, capsys):
    import projectflow.evaluation as evaluation

    class WrongQuote(FixtureRunner):
        def run(self, task, schema, cancel):
            response = super().run(task, schema, cancel)
            if task["stage"] == "extract":
                for candidate in response["event_candidates"]:  # none left to keep
                    candidate["evidence"][0]["quote"] = "<invented-quote>"
            return response

    monkeypatch.setattr(evaluation, "FixtureRunner", WrongQuote)
    output = tmp_path / "failed"
    report = run_eval("demo", output, "mock", AnalysisConfig())
    assert report["first_run"]["status"] == "failed"
    captures = [json.loads(path.read_text()) for path in (output / "call-review").glob("*.json")]
    assert len(captures) == 2
    assert all(item["response"]["event_candidates"][0]["evidence"][0]["quote"] == "<invented-quote>"
               for item in captures)
    page = (output / "review.html").read_text()
    assert "&lt;invented-quote&gt;" in page
    assert "문자열 불일치" in page
    assert "실제 원문" in page

    for path in (output / "call-review").glob("*.json"):
        path.unlink()
    assert main(["review", str(output)]) == 0
    assert "평가 검토 HTML:" in capsys.readouterr().out
    assert "모델 응답 원문을 저장하지 않았습니다" in (output / "review.html").read_text()


def test_eval_reports_progress_messages_while_it_runs(tmp_path):
    messages = []
    run_eval("demo", tmp_path / "eval", "mock", AnalysisConfig(), progress=messages.append)
    assert any("단위 완료" in message for message in messages)
