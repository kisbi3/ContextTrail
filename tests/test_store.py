import sqlite3
import time

from contexttrail import ui
from contexttrail.model import SourceRecord
from contexttrail.store import Store

from test_tui_keys import Screen


def records(count=3, text="body"):
    return [SourceRecord(f"src_{n}", "codex", "session-1", "user", f"{text} {n}",
                         {"kind": "jsonl", "path": "/logs/one.jsonl", "line": n}, recorded_at="2026-09-26T00:00:00Z")
            for n in range(count)]


def count_writes(store):
    with sqlite3.connect(store.path) as db:
        db.executescript("""
            CREATE TABLE writes (n INTEGER);
            INSERT INTO writes VALUES (0);
            CREATE TRIGGER w1 AFTER INSERT ON source_records BEGIN UPDATE writes SET n = n + 1; END;
            CREATE TRIGGER w2 AFTER UPDATE ON source_records BEGIN UPDATE writes SET n = n + 1; END;
            CREATE TRIGGER w3 AFTER INSERT ON sessions BEGIN UPDATE writes SET n = n + 1; END;
            CREATE TRIGGER w4 AFTER UPDATE ON sessions BEGIN UPDATE writes SET n = n + 1; END;
        """)
    def writes():
        with sqlite3.connect(store.path) as db:
            return db.execute("SELECT n FROM writes").fetchone()[0]
    return writes


def test_reads_go_on_while_an_analysis_holds_the_write_lock(tmp_path):
    store = Store(tmp_path / "state", "scope")
    store.set_meta("last_check", {"status": "ok"})
    writer = sqlite3.connect(store.path)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO project_meta VALUES ('pending', '1')")
    try:
        started = time.monotonic()
        assert store.get_meta("last_check") == {"status": "ok"}
        assert store.get_meta("pending") is None  # the reader sees the last committed state
        assert time.monotonic() - started < 1
    finally:
        writer.rollback()
        writer.close()


def test_rescanning_an_unchanged_history_writes_nothing(tmp_path):
    store = Store(tmp_path / "state", "scope")
    store.ingest(records())
    writes = count_writes(store)
    assert store.ingest(records()) == (set(), set())
    assert writes() == 0
    changed, missing = store.ingest(records(2))
    assert (changed, missing, writes()) == (set(), {"src_2"}, 1)
    assert {i: row["available"] for i, row in store.sources().items()} == {"src_0": 1, "src_1": 1, "src_2": 0}
    changed, missing = store.ingest(records(text="edited"))
    assert (changed, missing, writes()) == ({"src_0", "src_1", "src_2"}, set(), 4)
    assert all(row["available"] for row in store.sources().values())


class LockedStore:
    """A store whose every read finds the database locked."""

    def __init__(self, store):
        self.store, self.locked = store, False

    def __getattr__(self, name):
        method = getattr(self.store, name)
        def read(*args, **kwargs):
            if self.locked:
                raise sqlite3.OperationalError("database is locked")
            return method(*args, **kwargs)
        return read


class DrawnScreen(Screen):
    def erase(self):
        self.cells = [[" "] * self.cols for _ in range(self.rows)]

    def refresh(self):
        pass


def test_the_screen_keeps_its_last_values_when_a_read_is_locked_out(tmp_path):
    store = Store(tmp_path / "state", "scope")
    store.set_meta("last_check", {"at": "2026-09-26T09:00:00+00:00", "status": "ok"})
    locked = LockedStore(store)
    app = ui.TerminalApp(locked, lambda *_: None, title="proj")
    screen = DrawnScreen(30, 120)
    app.draw(screen)
    locked.locked = True
    app._token_checked_at = 0.0
    app.draw(screen)
    assert "2026-09-26T09:00:00+00:00 (ok)" in screen.text()
    assert app.panels.evidence("ev_missing") is None


def test_the_call_cap_is_for_one_run_and_an_old_saved_cap_is_dropped(tmp_path):
    import argparse
    from contexttrail.cli import _options
    store = Store(tmp_path / "state", "scope")
    store.set_meta("options", {"runner": "codex", "max_calls": 2})
    assert _options(argparse.Namespace(max_calls=None), store).max_calls == 30
    assert _options(argparse.Namespace(max_calls=5), store).max_calls == 5
    assert store.get_meta("options") == {"runner": "codex"}


def test_a_cancelled_run_stops_waiting_for_the_screens_answer(tmp_path):
    app = ui.TerminalApp(Store(tmp_path / "state", "scope"), lambda *_: None, title="proj")
    app.cancel.set()
    assert app.confirm({"units": 3}) is False


def test_publishing_reads_who_wrote_the_cited_records_from_the_store(tmp_path):
    from contexttrail.model import empty_graph
    store = Store(tmp_path / "state", "scope")
    codex, git = records(1)[0], SourceRecord("src_git", "git", None, "git", "commit", {"kind": "git"})
    codex.authoring = {"model": "gpt-6.1-sol", "effort": "high"}
    store.ingest([codex, git])
    def event(key, basis, evidence_id):
        return {"id": key, "kind": "action", "status": "applied", "title": key, "summary": "", "actor": "assistant",
                "basis": basis, "session_ids": [], "worktree_ids": [], "recorded_at": None, "occurred_at": None,
                "evidence_ids": [evidence_id]}
    graph = empty_graph("scope")
    graph["events"] = [event("a", "tool_record", "e1"), event("b", "git_artifact", "e2")]
    # The evidence item keeps the record as it was read before the field existed; the store has it now.
    old = {k: v for k, v in codex.metadata().items() if k != "authoring"}
    evidence = {"e1": {"id": "e1", "source_id": codex.source_id, "start_line": 1, "end_line": 1, "quote": "body 0", "source": old},
                "e2": {"id": "e2", "source_id": "src_git", "start_line": 1, "end_line": 1, "quote": "commit", "source": git.metadata()}}
    published = store.publish(graph, [], {}, evidence, expected_version=0)
    assert published["events"][0]["authoring"] == [{"tool": "codex", "model": "gpt-6.1-sol", "effort": "high"}]
    assert "authoring" not in published["events"][1]  # a Git record has no agent behind it
    # A later publish (a note, another unit) passes no evidence for them: the stored items and records are read.
    again = store.publish(published, [], {}, {}, expected_version=1)
    assert again["events"][0]["authoring"] == [{"tool": "codex", "model": "gpt-6.1-sol", "effort": "high"}]
    assert store.graph()["events"][0]["authoring"] == again["events"][0]["authoring"]
