"""The freshness check: what the graph does not hold yet, counted from the store and from changed files only."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contexttrail import freshness, i18n
from contexttrail.analysis import AnalysisConfig, Engine
from contexttrail.cli import main
from contexttrail.demo import FixtureRunner
from contexttrail.freshness import INDEX_KEY, ago, build_index, check, summary
from contexttrail.git_context import Scope
from contexttrail.store import Store
from contexttrail.util import dumps

from test_opencode_source import Builder, conversation


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dumps(r) for r in rows) + "\n", encoding="utf-8")


def claude_rows(folder: Path, session: str, texts: list[str], *, start: int = 0) -> list[dict]:
    return [{"type": "user" if n % 2 == 0 else "assistant", "uuid": f"{session}-{n}", "sessionId": session, "cwd": str(folder),
             "timestamp": f"2026-10-06T0{start + n}:00:00Z", "message": {"content": [{"type": "text", "text": text}]}}
            for n, text in enumerate(texts)]


def laboratory(tmp_path: Path):
    folder = tmp_path / "app"
    folder.mkdir()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    engine = Engine(scope, store, AnalysisConfig(**homes))
    return folder, scope, store, engine, homes


def test_never_scanned_then_pending_then_up_to_date(tmp_path):
    folder, scope, store, engine, homes = laboratory(tmp_path)
    write(homes["claude_home"] / "projects" / "p" / "s1.jsonl", claude_rows(folder, "s1", ["Use SQLite.", "Done."]))
    assert summary(check(scope, store, **homes)) == "기록을 아직 scan하지 않음"
    snapshot = engine.scan()
    engine.preview_plan(snapshot)  # ingests, as `scan` does
    result = check(scope, store, **homes)
    assert result["scanned_at"] and result["pending"] == {"records": 2, "units": 0}
    assert result["since_scan"]["parsed"] and result["since_scan"]["records"] == 0 and result["since_scan"]["files_changed"] == 0
    assert summary(result) == "마지막 scan 기준 미분석 기록 2개"
    i18n.set_language("en")
    assert summary(result) == "2 records not analyzed as of the last scan"
    engine.analyze(FixtureRunner)
    result = check(scope, store, **homes)
    assert result["pending"] == {"records": 0, "units": 0}
    assert summary(result) == "up to date"


def test_only_changed_files_are_parsed_and_their_new_records_counted(tmp_path, monkeypatch):
    folder, scope, store, engine, homes = laboratory(tmp_path)
    old = homes["claude_home"] / "projects" / "p" / "old.jsonl"
    write(old, claude_rows(folder, "old", ["First.", "Reply."]))
    engine.preview_plan(engine.scan())
    engine.analyze(FixtureRunner)
    # one file grows, one appears, one is untouched; a Codex file elsewhere is out of scope
    write(old, claude_rows(folder, "old", ["First.", "Reply.", "More.", "Sure."]))
    write(homes["claude_home"] / "projects" / "p" / "new.jsonl", claude_rows(folder, "new", ["Hello."], start=5))
    other = tmp_path / "other"
    other.mkdir()
    write(homes["codex_home"] / "sessions" / "x.jsonl", [{"type": "session_meta", "payload": {"id": "x", "cwd": str(other)}},
          {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "elsewhere"}]}}])
    parsed = []
    original = freshness.parse_claude
    monkeypatch.setattr(freshness, "parse_claude", lambda path, scope: parsed.append(path.name) or original(path, scope))
    result = check(scope, store, **homes)
    since = result["since_scan"]
    assert sorted(parsed) == ["new.jsonl", "old.jsonl"]
    assert since["files_changed"] == 3 and since["files_new"] == 2 and since["parsed"]
    assert since["records"] == 3 and since["sessions"] == 2 and since["by_source"] == {"claude": 3}
    assert since["newest_at"] == "2026-10-06T05:00:00Z"
    assert result["pending"]["records"] == 0
    assert "scan 이후 세션 2개·기록 3개" in summary(result)
    # The budget only counts
    result = check(scope, store, budget_bytes=10, **homes)
    assert result["since_scan"]["parsed"] is False and result["since_scan"]["files_changed"] == 3
    assert summary(result).startswith("scan 이후 바뀐 기록 파일 3개 (파싱 안 함")


def test_opencode_sessions_count_by_their_update_time(tmp_path):
    folder, scope, store, engine, homes = laboratory(tmp_path)
    homes["opencode_home"].mkdir()
    builder = Builder(homes["opencode_home"] / "opencode.db")
    session = builder.session(folder)
    conversation(builder, session, folder)
    builder.done()
    engine.preview_plan(engine.scan())
    index = store.get_meta(INDEX_KEY)
    assert list(index["opencode"].values())[0]["sessions"] == {session: builder.n and index["opencode"][str(builder.path)]["sessions"][session]}
    result = check(scope, store, **homes)
    assert result["since_scan"]["databases_changed"] == 0 and result["since_scan"]["records"] == 0
    import sqlite3
    db = sqlite3.connect(builder.path)
    db.execute("UPDATE session SET time_updated = time_updated + 5000 WHERE id = ?", (session,))
    db.execute("INSERT INTO message VALUES ('msg_9999', ?, 1790000099000, 1790000099000, ?)",
               (session, json.dumps({"role": "user", "time": {"created": 1790000099000}, "agent": "build",
                                     "model": {"providerID": "p", "modelID": "m"}})))
    db.execute("INSERT INTO part VALUES ('prt_9999', 'msg_9999', ?, 1790000099000, 1790000099000, ?)",
               (session, json.dumps({"type": "text", "text": "One more thing."})))
    db.commit()
    db.close()
    result = check(scope, store, **homes)
    assert result["since_scan"]["databases_changed"] == 1
    assert result["since_scan"]["records"] == 1 and result["since_scan"]["by_source"] == {"opencode": 1}


def test_find_header_and_status_command_report_the_lag(tmp_path, capsys):
    folder, scope, store, engine, homes = laboratory(tmp_path)
    path = homes["claude_home"] / "projects" / "p" / "s1.jsonl"
    write(path, claude_rows(folder, "s1", ["Use SQLite.", "Done."]))
    args = ["--codex-home", str(homes["codex_home"]), "--claude-home", str(homes["claude_home"]),
            "--opencode-home", str(homes["opencode_home"])]
    assert main(["scan", str(folder), *args]) == 0
    capsys.readouterr()
    assert main(["find", "", str(folder)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.endswith("AI 호출 없음 · 마지막 scan 기준 미분석 기록 2개")
    assert main(["find", "", str(folder), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["freshness"]["pending"]["records"] == 2 and payload["freshness_text"] == "마지막 scan 기준 미분석 기록 2개"
    write(path, claude_rows(folder, "s1", ["Use SQLite.", "Done.", "And tests."]))
    assert main(["status", str(folder)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("ContextTrail · 그래프 v0")
    assert "그때 기준 미분석 기록 2개" in lines[1]
    assert lines[2].startswith("scan 이후: 세션 1개, 기록 1개 (claude 1)")
    assert lines[3].startswith("갱신: contexttrail analyze")
    assert main(["status", str(folder), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["since_scan"]["records"] == 1 and payload["summary"].startswith("마지막 scan 기준 미분석 기록 2개 + scan 이후 세션 1개·기록 1개")


def test_the_index_records_the_length_the_parser_read(tmp_path):
    folder, scope, store, engine, homes = laboratory(tmp_path)
    path = homes["claude_home"] / "projects" / "p" / "s1.jsonl"
    write(path, claude_rows(folder, "s1", ["Use SQLite."]))
    snapshot = engine.scan()
    # a partial last line is not committed: the index keeps the committed length, so the file counts as changed
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "user", "uuid": "half"')
    index = build_index(scope, snapshot, **homes)
    assert index["files"][str(path)]["bytes"] < path.stat().st_size


def test_ago_is_coarse_and_never_negative():
    reference = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert ago("2026-10-06T11:59:30Z", reference) == "방금"
    assert ago("2026-10-06T11:20:00Z", reference) == "40분 전"
    assert ago("2026-10-06T03:00:00Z", reference) == "9시간 전"
    assert ago("2026-10-01T12:00:00Z", reference) == "5일 전"
    assert ago("2026-10-07T12:00:00Z", reference) == "방금"
    assert ago("nonsense", reference) == "" and ago(None, reference) == ""
    i18n.set_language("en")
    assert ago("2026-10-06T10:00:00Z", reference) == "2 h ago"
