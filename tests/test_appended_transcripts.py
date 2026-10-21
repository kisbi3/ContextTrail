"""Reading only the lines appended to a Claude Code transcript gives what reading the whole file gives."""
import dataclasses
import json
import os
import random
from pathlib import Path

import pytest

from contexttrail import freshness
from contexttrail.analysis import AnalysisConfig, Engine
from contexttrail.git_context import Scope
from contexttrail.sources import local
from contexttrail.sources.local import parse_claude, read_claude_appended
from contexttrail.store import Store


def shape(records):
    return [(r.source_id, r.content_hash, r.locator.get("line"), r.authoring, r.lineage) for r in records]


class Writer:
    """Random Claude Code rows: the project's and another folder's, rows with no cwd, tool calls run elsewhere and
    their results (some for a call answered before, some for no call), compactions, attachments, a session ID or an
    entrypoint that shows up late, a sidechain mark, blank and broken lines, CRLF endings."""

    def __init__(self, rng: random.Random, folder: Path, other: Path):
        self.rng, self.folder, self.other = rng, folder, other
        self.calls: list[str] = []
        self.n = 0

    def row(self) -> bytes:
        rng, self.n = self.rng, self.n + 1
        n = self.n
        if rng.random() < 0.03:
            return b"\n" if rng.random() < 0.5 else b'{"type": "user", "broken\n'
        data: dict = {"timestamp": f"2026-10-21T{n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"}
        if rng.random() < 0.85:
            data["uuid"] = f"u{n}"
        if rng.random() < 0.7 or n > 40:
            data["sessionId"] = "s1"
        roll = rng.random()
        if roll < 0.7:
            data["cwd"] = str(rng.choice([self.folder, self.folder / "sub", self.other]))
        elif roll < 0.8:
            data["cwd"] = None
        if rng.random() < 0.02:
            data["entrypoint"] = rng.choice(["cli", "sdk-cli", "sdk-py"])
        if rng.random() < 0.01:
            data["isSidechain"] = True
        kind = rng.random()
        if kind < 0.3:
            data.update(type="user", message={"content": [{"type": "text", "text": f"ask {n}"}]})
        elif kind < 0.5:
            call = f"toolu_{n}" if rng.random() < 0.95 else None
            self.calls.append(call)
            params = {"command": f"run {n}"}
            if rng.random() < 0.3:
                params["workdir"] = str(rng.choice([self.folder, self.other]))
            data.update(type="assistant", perTurnEffort=rng.choice(["low", "high"]),
                        message={"model": rng.choice(["claude-a", "claude-b", "<synthetic>"]),
                                 "content": [{"type": "text", "text": f"doing {n}"},
                                             {"type": "tool_use", "id": call, "name": "Bash", "input": params}]})
        elif kind < 0.75:
            call = rng.choice(self.calls) if self.calls and rng.random() < 0.9 else f"toolu_none_{n}"
            data.update(type="user", message={"content": [{"type": "tool_result", "tool_use_id": call,
                                                           "content": f"output {n}"}]})
        elif kind < 0.85:
            data.update(type="assistant", message={"model": "claude-a", "content": [{"type": "text", "text": f"said {n}"}]})
        elif kind < 0.9:
            data.update(type="system", subtype="compact_boundary")
        elif kind < 0.94:
            data.update(type="attachment", attachment={"type": "edited_text_file", "filename": "a.py", "snippet": f"x = {n}"})
        else:
            data.update(type="file-history-snapshot")
        line = json.dumps(data).encode()
        return line + (b"\r\n" if rng.random() < 0.1 else b"\n")


def follow(path: Path, scope: Scope, writer: Writer, rng: random.Random, steps: int) -> dict[str, int]:
    """Changes the file `steps` times and after each compares reading on from the last state with a whole reading."""
    state, records, used = None, [], {"appended": 0, "whole": 0}
    pending = b""
    for _ in range(steps):
        op = rng.random()
        current = path.read_bytes() if path.exists() else b""
        if op < 0.75 or not current:
            data = pending + b"".join(writer.row() for _ in range(rng.randint(1, 8)))
            cut = rng.randint(1, len(data)) if rng.random() < 0.3 else len(data)
            data, pending = data[:cut], data[cut:]
            with path.open("ab") as stream:
                stream.write(data)
        elif op < 0.82:
            path.write_bytes(current[:rng.randint(0, len(current) - 1)])  # truncated, same file
            pending = b""
        elif op < 0.88:
            fresh = path.with_name(path.name + ".tmp")  # replaced by a file that starts the same
            fresh.write_bytes(current + writer.row())
            os.replace(fresh, path)
        elif op < 0.94:
            # a byte changed near the start or just before the end the last reading stopped at, then a line added
            spot = rng.choice([rng.randint(0, min(len(current), local.RESUME_EDGE) - 1),
                               max(0, (state or {}).get("end", len(current)) - 1 - rng.randint(0, local.RESUME_EDGE - 1))])
            spot = min(spot, len(current) - 1)
            changed = bytearray(current)
            changed[spot] = (changed[spot] + 1) % 256 if changed[spot] not in b"\n\r" else changed[spot]
            with path.open("r+b") as stream:
                stream.write(bytes(changed) + writer.row())
        else:
            pass  # nothing changed
        whole = parse_claude(path, scope)
        appended = read_claude_appended(path, scope, state) if state else None
        if appended:
            records = records + appended[0].records
            state = appended[1]
            used["appended"] += 1
        else:
            snapshot = parse_claude(path, scope, resumable=True)
            records = snapshot.records
            state = next((m["resume"] for m in snapshot.files if m.get("resume")), None)
            used["whole"] += 1
        assert shape(records) == shape(whole.records)
        json.dumps(state)  # kept as JSON in the freshness cache
    return used


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("held", [True, False])
def test_reading_on_from_a_state_gives_what_a_whole_reading_gives(tmp_path, monkeypatch, seed, held):
    folder, other = tmp_path / "app", tmp_path / "other"
    folder.mkdir()
    other.mkdir()
    (folder / "sub").mkdir()
    scope = Scope.resolve(folder)
    rng = random.Random(seed)
    # Small edges and chunks reach every boundary; files over the size limit are read through.
    monkeypatch.setattr(local, "RESUME_EDGE", rng.choice([16, 64, 4096]))
    monkeypatch.setattr(local, "STREAM_CHUNK", rng.choice([7, 64, 1024]))
    monkeypatch.setattr(local, "SESSION_META_ROWS", 5)
    if not held:
        monkeypatch.setattr(local, "MAX_FILE_BYTES", rng.choice([0, 3000]))
    projects = tmp_path / "claude" / "projects" / "p"
    for path in (projects / "s1.jsonl", projects / "s1" / "subagents" / "agent-a.jsonl"):
        path.parent.mkdir(parents=True, exist_ok=True)
        used = follow(path, scope, Writer(rng, folder, other), rng, 60)
        assert used["appended"] > 10  # most steps only appended


def test_a_file_read_through_keeps_the_same_edges_as_one_held_whole(tmp_path, monkeypatch):
    folder = tmp_path / "app"
    folder.mkdir()
    scope = Scope.resolve(folder)
    writer = Writer(random.Random(3), folder, tmp_path)
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"".join(writer.row() for _ in range(300)) + b'{"partial')
    held = parse_claude(path, scope, resumable=True).files[0]
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 0)
    monkeypatch.setattr(local, "STREAM_CHUNK", 33)
    streamed = parse_claude(path, scope, resumable=True).files[0]
    keys = ("complete_bytes", "lines", "head", "tail", "dev", "ino")
    assert {k: held[k] for k in keys} == {k: streamed[k] for k in keys}


def laboratory(tmp_path: Path):
    folder, other = tmp_path / "app", tmp_path / "other"
    folder.mkdir()
    other.mkdir()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = dict(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude", opencode_home=tmp_path / "opencode")
    return folder, other, scope, store, Engine(scope, store, AnalysisConfig(**homes)), homes


def row(cwd: Path, n: int, session: str = "s") -> bytes:
    return json.dumps({"type": "user", "uuid": f"{session}{n}", "sessionId": session, "cwd": str(cwd),
                       "timestamp": f"2026-10-21T00:00:{n:02d}Z",
                       "message": {"content": [{"type": "text", "text": f"m{n}"}]}}).encode() + b"\n"


def test_status_reads_only_what_was_appended_and_counts_as_a_whole_reading(tmp_path, monkeypatch):
    folder, other, scope, store, engine, homes = laboratory(tmp_path)
    mine = homes["claude_home"] / "projects" / "a" / "mine.jsonl"
    theirs = homes["claude_home"] / "projects" / "b" / "theirs.jsonl"
    mine.parent.mkdir(parents=True)
    theirs.parent.mkdir(parents=True)
    mine.write_bytes(b"".join(row(folder, n, "m") for n in range(3)))
    theirs.write_bytes(b"".join(row(other, n, "t") for n in range(50)))
    engine.preview_plan(engine.scan())
    whole, appended = [], []
    original = local.read_claude_appended
    monkeypatch.setattr(freshness, "parse_claude", lambda path, scope, **kw: whole.append(path.name) or parse_claude(path, scope, **kw))
    monkeypatch.setattr(freshness, "read_claude_appended", lambda path, scope, state: appended.append(path.name) or original(path, scope, state))

    seeds = freshness._scan_seeds

    def exact():  # the same check with nothing kept: every changed file parsed whole
        kept = store.get_meta(freshness.CACHE_KEY)
        store.set_meta(freshness.CACHE_KEY, {})
        monkeypatch.setattr(freshness, "_scan_seeds", lambda store, scope: {})
        result = freshness.check(scope, store, **homes)["since_scan"]
        monkeypatch.setattr(freshness, "_scan_seeds", seeds)
        store.set_meta(freshness.CACHE_KEY, kept)
        return result

    # Another project's session grows: the scan's own reading of it is the state, so only the new lines are read.
    with theirs.open("ab") as stream:
        stream.write(row(other, 50, "t"))
    with mine.open("ab") as stream:
        stream.write(row(folder, 3, "m"))
    first = freshness.check(scope, store, **homes)["since_scan"]
    assert appended == ["theirs.jsonl"] and whole == ["mine.jsonl"]
    assert first["records"] == 1 and first["parsed"]
    # Both grow again, one with a partial last line: each is read from where the last check stopped.
    with theirs.open("ab") as stream:
        stream.write(row(other, 51, "t"))
    with mine.open("ab") as stream:
        stream.write(row(folder, 4, "m") + row(folder, 5, "m")[:20])
    whole.clear(), appended.clear()
    second = freshness.check(scope, store, **homes)["since_scan"]
    assert sorted(appended) == ["mine.jsonl", "theirs.jsonl"] and whole == []
    assert second["records"] == 2
    kept = exact()
    assert {k: second[k] for k in ("records", "sessions", "by_source", "newest_at")} == \
        {k: kept[k] for k in ("records", "sessions", "by_source", "newest_at")}
    # A row of the project in the other session: its earlier rows may hold records now, so it is read whole.
    with theirs.open("ab") as stream:
        stream.write(json.dumps({"type": "assistant", "uuid": "t52", "sessionId": "t", "cwd": str(other),
                                 "message": {"content": [{"type": "tool_use", "id": "c1", "name": "Bash",
                                                          "input": {"workdir": str(folder)}}]}}).encode() + b"\n")
        stream.write(row(folder, 53, "t"))
    whole.clear(), appended.clear()
    third = freshness.check(scope, store, **homes)["since_scan"]
    assert appended == ["theirs.jsonl"] and whole == ["theirs.jsonl"]
    assert third["records"] == exact()["records"] == 4
    # After a new scan, a session that was never the project's is read on from the scan's reading, and the
    # project's own session is read whole once against the new stored records.
    elsewhere = homes["claude_home"] / "projects" / "c" / "elsewhere.jsonl"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"".join(row(other, n, "e") for n in range(5)))
    engine.preview_plan(engine.scan())
    with elsewhere.open("ab") as stream:
        stream.write(row(other, 5, "e"))
    with mine.open("ab") as stream:
        stream.write(row(folder, 5, "m")[20:] + row(folder, 6, "m"))  # the partial line completed, and one more
    whole.clear(), appended.clear()
    fourth = freshness.check(scope, store, **homes)["since_scan"]
    assert appended == ["elsewhere.jsonl"] and whole == ["mine.jsonl"]
    assert fourth["records"] == exact()["records"] == 2


def test_a_budgeted_check_counts_a_resume_that_falls_back_as_the_whole_file(tmp_path, monkeypatch):
    folder, other, scope, store, engine, homes = laboratory(tmp_path)
    theirs = homes["claude_home"] / "projects" / "b" / "theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"".join(row(other, n, "t") for n in range(50)))
    engine.preview_plan(engine.scan())
    # A row of the project appended to a file that had none: read on from the scan's state, it falls back to a
    # whole reading, which the budget, sized for the appended bytes only, does not cover.
    with theirs.open("ab") as stream:
        stream.write(row(folder, 50, "t"))
    whole = []
    monkeypatch.setattr(freshness, "parse_claude", lambda path, scope, **kw: whole.append(path.name) or parse_claude(path, scope, **kw))
    since = freshness.check(scope, store, budget_bytes=1000, **homes)["since_scan"]
    assert whole == []
    assert not since["parsed"] and since["not_read"] == 1
    # With no budget (`status`) it is read whole and counted.
    since = freshness.check(scope, store, **homes)["since_scan"]
    assert whole == ["theirs.jsonl"] and since["parsed"] and since["records"] == 1


def test_a_resume_state_left_by_other_reading_code_is_not_read_on_from(tmp_path, monkeypatch):
    folder, other, scope, store, engine, homes = laboratory(tmp_path)
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"".join(row(folder, n) for n in range(3)))
    state = parse_claude(path, scope, resumable=True).files[0]["resume"]
    assert state["code"] == local.reading_code()
    with path.open("ab") as stream:
        stream.write(row(folder, 3))
    assert read_claude_appended(path, scope, state)[0].records
    monkeypatch.setattr(local, "reading_code", lambda: "other code")
    assert read_claude_appended(path, scope, state) is None
    # A state that holds nothing outlives the scan, but not a change of the reading code: the file is read whole.
    theirs = homes["claude_home"] / "projects" / "b" / "theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"".join(row(other, n, "t") for n in range(5)))
    monkeypatch.undo()
    engine.preview_plan(engine.scan())
    with theirs.open("ab") as stream:
        stream.write(row(other, 5, "t"))
    assert freshness.check(scope, store, **homes)["since_scan"]["parsed"]
    assert store.get_meta(freshness.CACHE_KEY)["files"][str(theirs)]["resume"]["code"] == local.reading_code()
    with theirs.open("ab") as stream:
        stream.write(row(other, 6, "t"))
    whole, appended = [], []
    monkeypatch.setattr(local, "reading_code", lambda: "other code")
    monkeypatch.setattr(freshness, "parse_claude", lambda path, scope, **kw: whole.append(path.name) or parse_claude(path, scope, **kw))
    original = local.read_claude_appended
    monkeypatch.setattr(freshness, "read_claude_appended", lambda path, scope, state: appended.append(path.name) or original(path, scope, state))
    assert freshness.check(scope, store, budget_bytes=200, **homes)["since_scan"]["parsed"] is False
    assert appended == [] and whole == []  # charged as a whole reading: over the budget, not read at all


def test_counts_made_under_other_scope_roots_are_not_reused(tmp_path):
    folder, other, scope, store, engine, homes = laboratory(tmp_path)
    theirs = homes["claude_home"] / "projects" / "b" / "theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"".join(row(other, n, "t") for n in range(5)))
    engine.preview_plan(engine.scan())
    with theirs.open("ab") as stream:
        stream.write(row(other, 5, "t"))
    assert freshness.check(scope, store, **homes)["since_scan"]["records"] == 0
    # The other folder becomes a worktree of the project: the same unchanged file holds six of its records now.
    widened = dataclasses.replace(scope, roots=[*scope.roots, other])
    assert freshness.check(widened, store, **homes)["since_scan"]["records"] == 6
