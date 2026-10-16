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


def test_publishing_hangs_a_forked_sessions_first_request_under_the_parents_last_request_before_the_fork(tmp_path):
    from contexttrail.model import empty_graph
    store = Store(tmp_path / "state", "scope")
    parent1 = SourceRecord("src_p1", "claude", "parent", "user", "Start the work.", {"kind": "jsonl", "path": "/l", "line": 1},
                           native_record_id="u1", recorded_at="2026-09-25T08:40:00Z")
    parent2 = SourceRecord("src_p3", "claude", "parent", "user", "Now the docs.", {"kind": "jsonl", "path": "/l", "line": 3},
                           native_record_id="u3", recorded_at="2026-09-26T15:00:00Z")
    parent3 = SourceRecord("src_p5", "claude", "parent", "user", "And the parent goes on.", {"kind": "jsonl", "path": "/l", "line": 5},
                           native_record_id="u5", recorded_at="2026-09-26T15:40:00Z")
    own = SourceRecord("src_f1", "claude", "fork", "user", "Continue with the tests.", {"kind": "jsonl", "path": "/f", "line": 3},
                       native_record_id="fork-1", recorded_at="2026-09-26T15:50:00Z",
                       fork={"from_session_id": "parent", "point_at": "2026-09-26T15:01:00Z"})
    store.ingest([parent1, parent2, parent3, own])

    def question(key, record):
        return {"id": key, "kind": "question", "status": "asked", "title": record.content, "summary": "", "actor": "user",
                "basis": "explicit_statement", "session_ids": [record.session_id], "worktree_ids": [],
                "recorded_at": record.recorded_at, "occurred_at": None, "evidence_ids": [f"e_{key}"]}
    graph = empty_graph("scope")
    # An event an earlier analysis made from a copied row (the copy is gone now) is not the fork's first request.
    stale = SourceRecord("src_copy", "claude", "fork", "user", "Now the docs.", {"kind": "jsonl", "path": "/f", "line": 1},
                         native_record_id="u3", recorded_at="2026-09-26T15:00:00Z")
    graph["events"] = [question("p1", parent1), question("p3", parent2), question("p5", parent3),
                       question("c3", stale), question("f1", own)]
    evidence = {f"e_{k}": {"id": f"e_{k}", "source_id": r.source_id, "start_line": 1, "end_line": 1, "quote": r.content,
                           "source": r.metadata()} for k, r in (("p1", parent1), ("p3", parent2), ("p5", parent3),
                                                                  ("c3", stale), ("f1", own))}
    published = store.publish(graph, [], {}, evidence, expected_version=0)
    forks = [edge for edge in published["edges"] if edge.get("origin") == "fork"]
    # The parent's last request before the fork point (not the one it went on with afterwards), structural, not a cause.
    assert [(e["from_event_id"], e["to_event_id"], e["relation"], e["basis"]) for e in forks] == [("p3", "f1", "follows", "structural")]
    assert store.publish(published, [], {}, {}, expected_version=1)["edges"].count(forks[0]) == 1  # not added twice


def test_a_parents_copy_of_a_sub_agents_report_is_drawn_as_the_sub_agents_box(tmp_path):
    from contexttrail.model import empty_graph
    from contexttrail.schema import folded_view
    store = Store(tmp_path / "state", "scope")
    lineage = {"kind": "subagent", "parent_session_id": "parent", "parent_tool_call_id": "toolu_1",
               "parent_result_record_id": "src_res", "agent_type": "Explore"}
    call = SourceRecord("src_call", "claude", "parent", "tool_call", '{"name": "Agent"}', {"kind": "jsonl", "path": "/p", "line": 2},
                        tool_call_id="toolu_1", recorded_at="2026-10-16T10:04:00Z")
    result = SourceRecord("src_res", "claude", "parent", "tool_result", "Three call sites, all through post_review.",
                          {"kind": "jsonl", "path": "/p", "line": 3}, tool_call_id="toolu_1", recorded_at="2026-10-16T10:06:00Z")
    grep = SourceRecord("src_g", "claude", "agent-1", "tool_result", "src/review.py:3", {"kind": "jsonl", "path": "/a", "line": 2},
                        recorded_at="2026-10-16T10:05:00Z", lineage=lineage)
    said = SourceRecord("src_s", "claude", "agent-1", "assistant", "Three call sites.", {"kind": "jsonl", "path": "/a", "line": 3},
                        recorded_at="2026-10-16T10:05:30Z", lineage=lineage)
    fix = SourceRecord("src_fix", "claude", "parent", "tool_call", "edit review.py", {"kind": "jsonl", "path": "/p", "line": 4},
                       recorded_at="2026-10-16T10:07:00Z")
    pool = [call, result, grep, said, fix]
    store.ingest(pool)

    def ev(key, kind, status, record, at):
        return {"id": key, "kind": kind, "status": status, "title": key, "summary": "", "actor": "assistant",
                "basis": "tool_record", "session_ids": [record.session_id], "worktree_ids": [], "recorded_at": at,
                "occurred_at": None, "evidence_ids": [f"e_{key}"]}
    graph = empty_graph("scope")
    graph["events"] = [ev("report", "outcome", "observed_success", result, result.recorded_at),
                       ev("grep", "outcome", "observed_success", grep, grep.recorded_at),
                       ev("concl", "outcome", "reported_complete", said, said.recorded_at),
                       ev("fix", "action", "applied", fix, fix.recorded_at)]
    graph["edges"] = [{"id": "l1", "from_event_id": "report", "to_event_id": "fix", "relation": "motivates", "basis": "explicit",
                       "evidence_ids": ["e_report"], "rationale": "", "active": True},
                      {"id": "l2", "from_event_id": "grep", "to_event_id": "concl", "relation": "follows", "basis": "inferred",
                       "evidence_ids": ["e_grep"], "rationale": "", "active": True}]
    records = dict(zip(("report", "grep", "concl", "fix"), (result, grep, said, fix)))
    evidence = {f"e_{k}": {"id": f"e_{k}", "source_id": r.source_id, "start_line": 1, "end_line": 1, "quote": r.content,
                           "source": r.metadata()} for k, r in records.items()}
    published = store.publish(graph, [], {}, evidence, expected_version=0)
    by_id = {e["id"]: e for e in published["events"]}
    # The parent's copy of the report points at the sub-agent's last outcome; nothing else is marked or removed.
    assert by_id["report"]["folded_into"] == "concl"
    assert [k for k, e in by_id.items() if "folded_into" in e] == ["report"] and len(published["edges"]) == 2
    view = folded_view(published)
    assert [e["id"] for e in view["events"]] == ["grep", "concl", "fix"]
    assert {(e["from_event_id"], e["to_event_id"]) for e in view["edges"]} == {("concl", "fix"), ("grep", "concl")}
    assert next(e for e in view["events"] if e["id"] == "concl")["received_reports"] == [
        {"id": "report", "title": "report", "status": "observed_success"}]
    assert store.graph() == published  # the stored graph keeps both
    # Before the sub-agent's own events are in the graph there is nothing to fold into.
    alone = empty_graph("scope")
    alone["events"] = [ev("report", "outcome", "observed_success", result, result.recorded_at)]
    again = store.publish(alone, [], {}, {}, expected_version=1)
    assert "folded_into" not in again["events"][0]


def test_graph_versions_are_kept_compressed_and_older_text_ones_are_packed_once(tmp_path):
    import sqlite3
    import pytest
    from contexttrail.model import empty_graph
    from contexttrail.util import FlowError, dumps, now
    store = Store(tmp_path / "state", "scope")
    graph = empty_graph("scope")
    graph["events"] = [{"id": "a", "kind": "question", "status": "asked", "title": "x" * 500, "summary": "", "actor": "user",
                        "basis": "explicit_statement", "session_ids": [], "worktree_ids": [], "recorded_at": None,
                        "occurred_at": None, "evidence_ids": []}]
    published = store.publish(graph, [], {}, {}, expected_version=0)
    with sqlite3.connect(store.path) as db:
        kind, size = db.execute("SELECT typeof(graph), length(graph) FROM graph_versions").fetchone()
        assert kind == "blob" and size < len(dumps(published)) / 3
        # A state written by 0.1.0a7: text rows, schema 1.
        db.execute("INSERT INTO graph_versions VALUES (?,?,?)", (2, dumps({**published, "version": 2}), now()))
        db.execute("PRAGMA user_version=1")
    assert store.graph(2)["events"] == published["events"]  # still readable before it is packed
    reopened = Store(tmp_path / "state", "scope")
    with sqlite3.connect(store.path) as db:
        assert [row[0] for row in db.execute("SELECT typeof(graph) FROM graph_versions")] == ["blob", "blob"]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        db.execute("PRAGMA user_version=3")
    assert reopened.graph(1) == published and reopened.graph()["version"] == 2
    with pytest.raises(FlowError, match="Unsupported DB schema: 3|지원하지 않는 DB schema: 3"):
        Store(tmp_path / "state", "scope")
