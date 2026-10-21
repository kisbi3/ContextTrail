"""Credential-shaped values are masked when transcripts are read: not stored, not sent to a model, not exported."""
import json
from pathlib import Path

from contexttrail import journal
from contexttrail.analysis import AnalysisConfig, Engine, model_context
from contexttrail.demo import FixtureRunner
from contexttrail.git_context import Scope
from contexttrail.model import SourceRecord, segment_record
from contexttrail.render import export_text
from contexttrail.store import Store
from contexttrail.util import SECRET_HIDDEN, digest, dumps, mask_record, mask_secrets

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


def test_exports_and_model_input_mask_what_an_earlier_version_stored():
    quote = f"probe: OPENAI_API_KEY={KEY}"
    graph = {"version": 3, "analyzed_at": None, "limitations": [], "edges": [], "open_items": [],
             "events": [{"id": "ev_5ec4e7aa", "kind": "outcome", "status": "observed_failure", "actor": "assistant",
                         "basis": "tool_record", "title": f"Leaked {KEY}", "summary": f"The probe printed {KEY}.",
                         "evidence_ids": ["evi_1"], "recorded_at": "2026-10-01T00:00:00Z"}]}
    evidence = {"evi_1": {"id": "evi_1", "source_id": "src_1", "start_line": 1, "end_line": 1, "quote": quote,
                          "quote_hash": digest(quote), "source": {"role": "tool_result", "locator": {"line": 1}}}}
    for fmt in ("md", "mmd", "json"):
        assert KEY not in export_text(graph, evidence, fmt)
    exported = json.loads(export_text(graph, evidence, "json"))["evidence"]["evi_1"]
    assert exported["masked"] is True and exported["quote_hash"] == digest(quote)  # the hash is of the stored text
    assert evidence["evi_1"]["quote"] == quote and KEY in graph["events"][0]["title"]  # the store is not changed
    shown = model_context({"existing_events": graph["events"], "existing_evidence": evidence, "context_selection": {}})
    assert KEY not in json.dumps(shown) and "context_selection" not in shown


def test_show_json_masks_a_summary_an_earlier_version_stored():
    from contexttrail.agent_view import event_record
    graph = {"version": 2, "edges": [], "open_items": [],
             "events": [{"id": "ev_5ec4e7aa", "kind": "outcome", "status": "observed_failure", "actor": "assistant",
                         "title": "Leaked key", "summary": f"The probe printed {KEY}.", "evidence_ids": [],
                         "recorded_at": "2026-10-01T00:00:00Z"}]}
    record = event_record(graph, "ev_5ec4e7aa", lambda key: None)
    assert KEY not in json.dumps(record) and SECRET_HIDDEN in record["summary"]
