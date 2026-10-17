import importlib.util
import sqlite3
from pathlib import Path

import pytest

from contexttrail.demo import CASES, FixtureRunner

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_models.py"
spec = importlib.util.spec_from_file_location("compare_models", SCRIPT)
compare_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_models)


def test_each_model_gets_a_copy_and_the_comparison_reads_what_the_runs_did(laboratory, tmp_path):
    scope, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    own = Path(scope.state_dir) / "state.sqlite"
    assert engine.analyze(FixtureRunner)["status"] == "complete"
    before = own.read_bytes()
    copy = tmp_path / "out" / "haiku" / "state" / "state.sqlite"
    compare_models.copy_state(own, copy)
    assert own.read_bytes() == before  # the project's file is only read
    assert sqlite3.connect(copy).execute("SELECT count(*) FROM graph_versions").fetchone()[0] >= 1
    summary = compare_models.summarize(copy, "2000-01-01")
    assert summary["paths"] == {"first": 1} and summary["calls"] >= 1 and summary["events"] >= 1
    assert all(e["unit"] in summary["units"] for e in summary["event_titles"])
    out = tmp_path / "out"
    text = compare_models.report({"claude:haiku": {**summary, "status": "complete"},
                                  "codex:gpt-luna": {**summary, "status": "complete"}}, out)
    assert "| units published | 1/1 | 1/1 |" in text and (out / "report.json").is_file()


def test_the_comparison_needs_consent_and_names_runner_and_model(tmp_path):
    with pytest.raises(SystemExit):
        compare_models.main([str(tmp_path), "--models", "claude:haiku", "--output", str(tmp_path / "o")])
    with pytest.raises(SystemExit):
        compare_models.parse_spec("haiku")
    assert compare_models.parse_spec("codex:gpt-luna") == ("codex", "gpt-luna")
