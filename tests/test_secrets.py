"""Credential-shaped values are masked when transcripts are read: not stored, not sent to a model, not exported."""
import http.client
import json
import sqlite3
import threading
import time
from pathlib import Path

from contexttrail import journal, session_context
from contexttrail.agent_view import find, find_text, show, show_text
from contexttrail.analysis import AnalysisConfig, Engine, Harness
from contexttrail.demo import FixtureRunner
from contexttrail.evaluation import fixture_records
from contexttrail.git_context import Scope
from contexttrail.model import SourceRecord, empty_graph, segment_record
from contexttrail.render import event_detail, export_text
from contexttrail.store import Store
from contexttrail.util import SECRET_HIDDEN, digest, dumps, mask_record, mask_secrets
from contexttrail.webview import LocalViewer

KEY = "sk-proj-" + "A1b2C3d4" * 6
PEM = ["-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn", "AAAAB3NzaC1yc2EAAAADAQABAAABAQ==",
       "-----END RSA PRIVATE KEY-----"]
SESSION = "sess-secret-1"


def row(folder: Path, n: int, kind: str, content: list[dict]) -> dict:
    return {"type": kind, "uuid": f"u{n}", "parentUuid": f"u{n - 1}" if n else None, "sessionId": SESSION,
            "cwd": str(folder), "timestamp": f"2026-10-07T10:{n:02d}:00Z", "message": {"content": content}}


def project(tmp_path: Path):
    folder = tmp_path / "app"
    folder.mkdir()
    claude = tmp_path / "claude"
    rows = [
        row(folder, 0, "user", [{"type": "text", "text": f"Probe the API with OPENAI_API_KEY={KEY} please."}]),
        row(folder, 1, "assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                      "input": {"command": f"OPENAI_API_KEY={KEY} python probe.py"}}]),
        row(folder, 2, "user", [{"type": "tool_result", "tool_use_id": "t1",
                                 "content": "\n".join(["probe ok", *PEM, f"key: {KEY}"])}]),
        row(folder, 3, "assistant", [{"type": "text", "text": "SQLite도 검토할 수 있습니다."}]),
    ]
    path = claude / "projects" / "app" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(dumps(r) for r in rows) + "\n", encoding="utf-8")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    homes = dict(codex_home=tmp_path / "codex", claude_home=claude, opencode_home=tmp_path / "opencode")
    return scope, store, Engine(scope, store, AnalysisConfig(**homes)), homes


def test_masking_keeps_every_line_and_json_string_and_is_idempotent():
    pem = "\n".join(["before", *PEM[:3], "", PEM[3], "after"])
    masked = mask_secrets(pem)
    assert masked.splitlines() == ["before", *[SECRET_HIDDEN] * 3, "", SECRET_HIDDEN, "after"]
    for text in (pem, f"OPENAI_API_KEY={KEY}", f'"api_token": "abcdefgh123"', "Authorization: Bearer " + "x" * 30,
                 json.dumps({"command": f'echo "API_KEY={KEY}" \\"TOKEN=abcdefgh12\\"', "pem": pem})):
        once = mask_secrets(text)
        assert mask_secrets(once) == once and len(once.splitlines()) == len(text.splitlines())
        assert KEY not in once and "abcdefgh12" not in once
    json.loads(mask_secrets(json.dumps({"command": f'echo "API_KEY={KEY}" \\"TOKEN=abcdefgh12\\"', "pem": pem})))
    # What is no secret stays as written.
    for text in ("max_tokens=128000", 'self.token = os.environ["X"]', "token = get_token()", "api_key=self._key"):
        assert mask_secrets(text) == text
    record = SourceRecord("s1", "claude", "s", "user", "nothing to hide", {"kind": "jsonl", "line": 1})
    assert mask_record(record) is record and segment_record(record) == [record]


def test_a_scan_masks_a_key_before_it_is_stored_sent_to_the_model_or_exported(tmp_path):
    scope, store, engine, _ = project(tmp_path)
    snapshot = engine.scan()
    text = "\n".join(r.content for r in snapshot.records)
    assert KEY not in text and "MIIEow" not in text and SECRET_HIDDEN in text
    result = next(r for r in snapshot.records if r.role == "tool_result")
    assert len(result.content.splitlines()) == len(PEM) + 2  # the key block keeps its lines
    call = next(r for r in snapshot.records if r.role == "tool_call")
    json.loads(call.content.partition("\n")[2])  # still readable arguments

    class Watching(FixtureRunner):
        seen: list = []

        def run(self, task, schema, cancel):
            Watching.seen.append(json.dumps(task, ensure_ascii=False))
            return super().run(task, schema, cancel)

    assert engine.analyze(Watching)["status"] == "complete"
    assert Watching.seen and not any(KEY in task or "MIIEow" in task for task in Watching.seen)
    assert any(SECRET_HIDDEN in task for task in Watching.seen)
    graph = store.graph()
    evidence = store.evidence_many([i for e in graph["events"] for i in e["evidence_ids"]])
    assert any(SECRET_HIDDEN in item["quote"] for item in evidence.values())
    assert not any(KEY in item["quote"] for item in evidence.values())
    for fmt in ("md", "mmd", "json"):
        assert KEY not in export_text(graph, evidence, fmt)
    assert not any(item.get("masked") for item in json.loads(export_text(graph, evidence, "json"))["evidence"].values())


def test_a_note_reads_the_same_masked_records_as_a_scan(tmp_path):
    scope, store, engine, homes = project(tmp_path)
    store.set_meta("options", {key: str(value) for key, value in homes.items()})
    scanned = {r.source_id: r for r in engine.scan().records}
    noted = journal.session_records(scope, store, SESSION)
    assert noted and all(r.content_hash == scanned[r.source_id].content_hash for r in noted)
    assert not any(KEY in r.content for r in noted)


def test_a_secret_after_a_url_another_assignment_or_a_json_escape_is_masked():
    value = "SECRETVALUE1234567"
    for text in (f"curl https://api.example.com/v1?access_token={value}",
                 json.dumps({"url": f"https://x.io/cb?api_key={value}"}),
                 f"postgres://user:pw@db/x?password={value}", f"foo=bar12345:API_KEY={value}",
                 json.dumps({"command": f"cd app\nOPENAI_API_KEY={KEY} python probe.py"}),
                 json.dumps({"output": f"started\n{KEY}\n"}), json.dumps({"output": f"ok\tBearer {'abc' * 10}"}),
                 json.dumps({"output": f"ok\r\n{'ghp_' + 'x1' * 18}"})):
        masked = mask_secrets(text)
        assert value not in masked and KEY not in masked and "abcabcabc" not in masked and "x1x1x1" not in masked
        assert SECRET_HIDDEN in masked and mask_secrets(masked) == masked
        if text.startswith("{"):
            json.loads(masked)
    # The value of a secret assignment runs to its end, whatever separators it holds.
    assert mask_secrets(json.dumps({"password": "pa:ss=word-1"})) == json.dumps({"password": SECRET_HIDDEN})
    assert mask_secrets("TOKEN=abc:def=ghij0 next") == f"TOKEN={SECRET_HIDDEN} next"
    assert mask_secrets(f"TOKEN={'QUJD' * 4}==") == f"TOKEN={SECRET_HIDDEN}"  # base64 padding
    # A word that only starts like a token is not one.
    for text in ("risk-" + "a" * 30, "the_sk-" + "a" * 30, "mask-" + "b" * 30):
        assert mask_secrets(text) == text


def test_masking_work_is_bounded_on_adversarial_text():
    size = 200_000
    units = ("-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n", "\nAAAA",
             "token", "token=", "token:a", "=a", "\"token\": \"", "sk-", "\\nsk-", "Bearer ", "password=abcdefgh:",
             "API_KEY=" + "a" * 600 + " ")
    for unit in units:
        text = (unit * (size // len(unit) + 1))[:size]
        started = time.perf_counter()
        mask_secrets(text)
        assert time.perf_counter() - started < 0.5, unit
    # A value longer than SECRET_VALUE_MAX is no value it masks; a key block with no END keeps to its first 100 lines.
    assert mask_secrets("API_KEY=" + "a" * 600) == "API_KEY=" + "a" * 600
    block = "\n".join([PEM[0], *["QUJD" * 16] * 150])
    assert mask_secrets(block).splitlines().count(SECRET_HIDDEN) == 101


def test_a_records_lineage_and_git_strings_are_masked_and_hashed_as_masked():
    record = SourceRecord("s1", "codex", "s", "tool_call", "nothing to hide", {"kind": "jsonl", "line": 1},
                          lineage={"kind": "subagent", "task": f"probe with OPENAI_API_KEY={KEY}"},
                          git={"remote": f"https://user:{'ghp_' + 'x1' * 18}@github.com/o/r", "branch": "main"})
    masked = mask_record(record)
    assert KEY not in json.dumps(masked.lineage) and "x1x1x1" not in json.dumps(masked.git)
    assert masked.lineage["kind"] == "subagent" and masked.git["branch"] == "main" and masked.content == record.content
    assert masked.content_hash != record.content_hash and segment_record(record)[0].content_hash == masked.content_hash


def test_eval_fixture_records_are_masked_like_read_records():
    item = {"source_id": "src_1", "provider": "codex", "session_id": "s", "role": "tool_result",
            "content": f"probe ok\nkey: {KEY}", "locator": {"kind": "jsonl", "line": 1},
            "lineage": {"task": f"use OPENAI_API_KEY={KEY}"}}
    record, = fixture_records({"records": [item]})
    assert KEY not in record.content and KEY not in json.dumps(record.lineage) and SECRET_HIDDEN in record.content


LONG = "x" * 900 + f" OPENAI_API_KEY={KEY} " + "y" * 900


def old_state(tmp_path: Path):
    """A state an earlier version wrote: a key in an event's text and in evidence quotes, as stored."""
    folder = tmp_path / "old"
    folder.mkdir()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    source = {"role": "tool_result", "provider": "claude", "session_id": SESSION, "locator": {"kind": "jsonl", "line": 3},
              "recorded_at": "2026-10-01T00:00:00Z"}
    quotes = {"evi_1": "\n".join(["probe ok", *PEM, f"key: {KEY}"]), "evi_2": LONG, "evi_3": "probe ok, nothing secret"}
    focus = {"evi_1": [[0, 8]], "evi_2": [[800, 820]], "evi_3": [[0, 5]]}
    evidence = {key: {"id": key, "source_id": f"src_{n}", "source_version": "0" * 16, "content_hash": "c" * 64,
                      "start_line": 1, "end_line": len(quote.splitlines()), "quote": quote, "quote_hash": digest(quote),
                      "focus": focus[key], "source": source} for n, (key, quote) in enumerate(quotes.items(), 1)}
    graph = empty_graph(scope.id)
    graph["events"] = [{"id": "ev_5ec4e7aa", "kind": "outcome", "status": "observed_failure", "actor": "assistant",
                        "basis": "tool_record", "title": f"Leaked {KEY}", "summary": f"The probe printed {KEY}.",
                        "evidence_ids": list(evidence), "recorded_at": "2026-10-01T00:00:00Z", "session_id": SESSION}]
    graph["open_items"] = [{"id": "open_1", "status": "open", "text": f"Rotate {KEY}", "evidence_ids": ["evi_1"]}]
    store.publish(graph, [], {}, evidence, expected_version=0)
    return store, quotes


def stored_rows(store: Store) -> str:
    with sqlite3.connect(store.path) as db:
        return "\n".join(row[0] for row in db.execute("SELECT data FROM evidence_items"))


def test_the_store_gives_out_masked_copies_of_what_an_earlier_version_stored(tmp_path):
    store, quotes = old_state(tmp_path)
    graph = store.graph()
    assert KEY not in json.dumps(graph) and graph["events"][0]["id"] == "ev_5ec4e7aa"
    assert graph["events"][0]["evidence_ids"] == ["evi_1", "evi_2", "evi_3"] and store.graph() == graph
    items = store.evidence_many(["evi_1", "evi_2", "evi_3"])
    assert items == {key: store.evidence(key) for key in items}
    for key in ("evi_1", "evi_2"):
        item = items[key]
        assert KEY not in item["quote"] and "MIIEow" not in item["quote"] and item["masked"] is True
        assert "focus" not in item  # offsets into the stored text
        assert item["id"] == key and item["quote_hash"] == digest(quotes[key]) and item["content_hash"] == "c" * 64
        assert len(item["quote"].splitlines()) == len(quotes[key].splitlines())
    assert items["evi_3"]["focus"] == [[0, 5]] and "masked" not in items["evi_3"]
    assert KEY in stored_rows(store)  # the stored rows are not changed
    # A graph published on top of a masked read stores the masked text, under the same IDs and hashes.
    store.publish(graph, [], {}, {}, expected_version=graph["version"])
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM graph_versions").fetchone()[0] == 2
    newer = store.graph()
    assert newer["version"] == 2 and newer["events"][0]["title"] == graph["events"][0]["title"]
    assert store.evidence("evi_1")["quote_hash"] == digest(quotes["evi_1"]) and KEY not in json.dumps(store.graph(1))


def test_every_reader_of_an_earlier_state_sees_it_masked(tmp_path):
    store, _ = old_state(tmp_path)
    graph = store.graph()
    texts = ["\n".join(show_text(show("ev_5ec4", graph, store.evidence, store.graph))),
             json.dumps(show("ev_5ec4", graph, store.evidence, store.graph)),
             "\n".join(find_text(find(graph, "probe", store.evidence_many))),
             "\n".join(line for line, _ in event_detail(graph, "ev_5ec4e7aa", store.evidence, quote_lines=40)),
             session_context.build(store)]
    evidence = store.evidence_many(["evi_1", "evi_2", "evi_3"])
    texts += [export_text(graph, evidence, fmt) for fmt in ("md", "mmd", "json")]
    later = SourceRecord("src_9", "claude", SESSION, "user", "next step", {"kind": "jsonl", "line": 9},
                         recorded_at="2026-10-02T00:00:00Z")
    harness = Harness(None, {later.source_id: later}, graph, store, AnalysisConfig(), threading.Event())
    shown = json.dumps(harness.context([later]))
    assert "ev_5ec4e7aa" in shown  # the model's context holds the earlier event, masked
    texts.append(shown)
    viewer = LocalViewer(store, lambda: {"status": "noop", "runner_calls": 0}, port=0).start()
    try:
        for path in ("/graph", "/graph.mmd", "/events/ev_5ec4e7aa", "/evidence/evi_1", "/evidence/evi_2"):
            connection = http.client.HTTPConnection("127.0.0.1", viewer.port, timeout=3)
            connection.request("GET", path, headers={"Authorization": "Bearer " + viewer.token})
            response = connection.getresponse()
            assert response.status == 200, path
            texts.append(response.read().decode("utf-8"))
            connection.close()
    finally:
        viewer.close()
    for text in texts:
        assert text and KEY not in text and "MIIEow" not in text and "A1b2C3d4A1b2" not in text
    assert all(SECRET_HIDDEN in text for text in texts[:4])
    exported = json.loads(export_text(graph, evidence, "json"))["evidence"]["evi_1"]
    assert exported["masked"] is True and "focus" not in exported
