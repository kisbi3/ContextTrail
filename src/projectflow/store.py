from __future__ import annotations

import copy
import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .i18n import tr
from .model import SourceRecord, empty_graph
from .util import FlowError, dumps, merge_focus, now, private_dir

# Conservative floor for SQLite's per-statement bound variable limit (999).
_SQL_VARIABLES = 900


class Store:
    """Short transactions only. No DB write transaction is held across an AI call."""

    def __init__(self, directory: Path, scope_id: str):
        self.directory, self.scope_id = directory, scope_id
        private_dir(directory)
        self.path = directory / "state.sqlite"
        if self.path.is_symlink():
            raise FlowError(tr("SQLite 상태 파일 symlink는 허용하지 않습니다.", "A SQLite state file that is a symlink is not allowed."))
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise FlowError(tr(f"지원하지 않는 DB schema: {version}", f"Unsupported DB schema: {version}"))
            db.executescript("""
                CREATE TABLE IF NOT EXISTS project_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_records (
                    id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, metadata TEXT NOT NULL,
                    processed_hash TEXT, available INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS evidence_items (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_units (
                    id TEXT PRIMARY KEY, sources TEXT NOT NULL, dependencies TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, cache_key TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS graph_versions (
                    version INTEGER PRIMARY KEY, graph TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, manifest TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT NOT NULL, stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    metadata TEXT NOT NULL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id, started_at);
                PRAGMA user_version=1;
            """)
            # WAL lets the screen and the browser keep reading while an analysis writes
            # (a rollback journal locks readers out for the whole of a large ingest).
            # State lives on local disk only (docs/SPEC.md), where WAL is supported.
            db.execute("PRAGMA journal_mode=WAL")
            row = db.execute("SELECT value FROM project_meta WHERE key='scope_id'").fetchone()
            if row and json.loads(row[0]) != scope_id:
                raise FlowError(tr("저장된 프로젝트 scope와 요청 scope가 일치하지 않습니다.", "The saved project scope does not match the requested scope."))
            db.execute("INSERT OR IGNORE INTO project_meta VALUES ('scope_id', ?)", (dumps(scope_id),))
            db.commit()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def analyze_lock(self) -> Iterator[None]:
        path = self.directory / "analyze.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FlowError(tr("이 scope에서 이미 분석이 진행 중입니다.", "An analysis is already running in this scope.")) from exc
            yield
        finally:
            os.close(fd)

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connection() as db:
            row = db.execute("SELECT value FROM project_meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.connection() as db, db:
            db.execute("INSERT INTO project_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, dumps(value)))

    def graph(self, version: int | None = None) -> dict:
        with self.connection() as db:
            if version is None:
                row = db.execute("SELECT graph FROM graph_versions ORDER BY version DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT graph FROM graph_versions WHERE version=?", (version,)).fetchone()
            if version is not None and row is None:
                raise FlowError(tr("요청한 그래프 버전이 없습니다.", "The requested graph version does not exist."))
            return json.loads(row[0]) if row else empty_graph(self.scope_id)

    def sources(self) -> dict[str, dict]:
        with self.connection() as db:
            return {row["id"]: {**dict(row), "metadata": json.loads(row["metadata"])}
                    for row in db.execute("SELECT * FROM source_records")}

    def ingest(self, records: list[SourceRecord]) -> tuple[set[str], set[str]]:
        previous = self.sources()
        identity_fields = ("provider", "session_id", "native_record_id", "parent_record_id", "recorded_at")
        hashes = {record.source_id: record.content_hash for record in records}  # a digest per call; take it once
        migrations = {}
        for record in records:
            old = previous.get(record.source_id)
            if old and old["content_hash"] != hashes[record.source_id] and old["content_hash"] == record.legacy_content_hash and all(
                    old["metadata"].get(k) == getattr(record, k) for k in identity_fields):
                migrations[record.source_id] = (old["content_hash"], hashes[record.source_id])
        changed = {r.source_id for r in records if r.source_id in previous
                   and previous[r.source_id]["content_hash"] != hashes[r.source_id] and r.source_id not in migrations}
        present = {r.source_id for r in records}
        missing = {i for i, row in previous.items() if row["available"] and i not in present}
        # Only rows that differ are written, so a re-scan of an unchanged history is a short transaction.
        stored = {i: (row["content_hash"], row["metadata"], row["available"]) for i, row in previous.items()}
        written, sessions = [], {}
        for record in records:
            metadata = record.metadata()
            if stored.get(record.source_id) != (metadata["content_hash"], metadata, 1):
                written.append((record.source_id, metadata["content_hash"], dumps(metadata)))
            if record.session_id:
                sessions[record.provider + ":" + record.session_id] = {
                    "provider": record.provider, "session_id": record.session_id,
                    "path": record.locator.get("path"), "cwd": record.cwd}
        with self.connection() as db, db:
            known = {row["id"]: json.loads(row["metadata"]) for row in db.execute("SELECT * FROM sessions")}
            db.executemany("UPDATE source_records SET available=0 WHERE id=?", [(i,) for i in missing])
            # Hash-format upgrade alone must not spend the user's account quota again.
            # Only migrate when the old body digest AND all newly hashed native metadata match.
            for source_id, (old_hash, new_hash) in migrations.items():
                db.execute("UPDATE source_records SET processed_hash=? WHERE id=? AND processed_hash=?",
                           (new_hash, source_id, old_hash))
            if migrations:
                for row in db.execute("SELECT id,dependencies FROM work_units").fetchall():
                    dependencies = json.loads(row["dependencies"])
                    for source_id, (old_hash, new_hash) in migrations.items():
                        if dependencies.get(source_id) == old_hash:
                            dependencies[source_id] = new_hash
                    db.execute("UPDATE work_units SET dependencies=? WHERE id=?", (dumps(dependencies), row["id"]))
            db.executemany("""INSERT INTO source_records (id,content_hash,metadata,available) VALUES (?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET content_hash=excluded.content_hash,
                metadata=excluded.metadata,available=1""", written)
            db.executemany("INSERT INTO sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata",
                           [(key, dumps(value)) for key, value in sessions.items() if known.get(key) != value])
        return changed, missing

    def acknowledge_environment_context(self, records: list[SourceRecord]) -> int:
        """Account for standalone host context without spending a model call.

        Records already assigned to a WorkUnit are left pending so a role change
        can be reanalysed with its original surrounding conversation.
        """
        candidates = [r for r in records if r.lineage.get("kind") == "environment_context"]
        if not candidates:
            return 0
        assigned = {source_id for unit in self.units() for source_id in unit["sources"]}
        selected = [r for r in candidates if r.source_id not in assigned]
        with self.connection() as db, db:
            for record in selected:
                db.execute("""UPDATE source_records SET processed_hash=?
                    WHERE id=? AND content_hash=? AND available=1""",
                    (record.content_hash, record.source_id, record.content_hash))
        return len(selected)

    def mark_processed(self, processed: dict[str, str]) -> None:
        with self.connection() as db, db:
            db.executemany("UPDATE source_records SET processed_hash=? WHERE id=? AND content_hash=?",
                           [(content_hash, source_id, content_hash) for source_id, content_hash in processed.items()])

    def units(self) -> list[dict]:
        with self.connection() as db:
            rows = list(db.execute("SELECT * FROM work_units ORDER BY updated_at,id"))
        return [{**dict(row), "sources": json.loads(row["sources"]),
                 "dependencies": json.loads(row["dependencies"]),
                 "result": json.loads(row["result"]) if row["result"] is not None else None} for row in rows]

    # The `cache_key` column is vestigial and nothing reads it. Extraction reuse
    # is decided in Engine._extract_unit from `routing_signature` and
    # `context_digest` inside the stored result. Two call sites used to compute
    # the column from different formulas and never read either back, so the
    # parameter is gone and callers cannot pass a value that goes nowhere. Rows
    # written by earlier versions keep whatever they stored, and the named-column
    # INSERT leaves those in place. The column itself stays: dropping it needs a
    # schema migration, and an unread nullable column costs nothing.
    def save_unit(self, unit_id: str, sources: list[str], dependencies: dict[str, str], status: str,
                  result: dict | None = None) -> None:
        with self.connection() as db, db:
            # Columns are named rather than positional so the vestigial
            # cache_key column can stay in the schema without being written.
            db.execute("""INSERT INTO work_units (id,sources,dependencies,status,result,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET sources=excluded.sources,dependencies=excluded.dependencies,
                status=excluded.status,result=excluded.result,updated_at=excluded.updated_at""",
                (unit_id, dumps(sources), dumps(dependencies), status,
                 dumps(result) if result is not None else None, now()))

    def evidence(self, evidence_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT data FROM evidence_items WHERE id=?", (evidence_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def evidence_many(self, ids: list[str]) -> dict[str, dict]:
        """Fetch many evidence items in one query.

        A Harness is built twice per work unit and collects every evidence id in
        the graph, so calling `evidence` per id meant thousands of connection
        open/close cycles per unit: 3.7s for 4,000 items on this machine, which
        grew with the graph. Chunked below, 0.02s. SQLite caps bound variables
        per statement, so the id list is sent in batches.
        """
        wanted = sorted({i for i in ids if isinstance(i, str)})
        if not wanted:
            return {}
        result: dict[str, dict] = {}
        with self.connection() as db:
            for start in range(0, len(wanted), _SQL_VARIABLES - 10):
                batch = wanted[start:start + _SQL_VARIABLES - 10]
                marks = ",".join("?" * len(batch))
                for row in db.execute(
                        f"SELECT id, data FROM evidence_items WHERE id IN ({marks})", batch):
                    # Same contract as the per-id read this replaced: a row that
                    # decodes to a falsey value is not reported, so callers that
                    # treat a missing key as "no evidence" keep doing so.
                    item = json.loads(row[1])
                    if item:
                        result[row[0]] = item
        return result

    def publish(self, graph: dict, unit_ids: list[str], processed: dict[str, str], evidence: dict[str, dict],
                *, expected_version: int) -> dict:
        graph = copy.deepcopy(graph)
        graph["version"] = expected_version + 1
        graph["analyzed_at"] = now()
        with self.connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT MAX(version) FROM graph_versions").fetchone()[0] or 0
                if current != expected_version:
                    raise FlowError(tr("기준 graph version이 달라졌습니다. 통합 결과를 적용하지 않았습니다.",
                                       "The base graph version changed; the integration result was not applied."))
                for item_id, item in evidence.items():
                    db.execute("INSERT OR IGNORE INTO evidence_items VALUES (?,?)", (item_id, dumps(item)))
                    if item.get("focus"):
                        # Evidence text is immutable; only the cited focus ranges accumulate.
                        stored = json.loads(db.execute("SELECT data FROM evidence_items WHERE id=?",
                                                       (item_id,)).fetchone()[0])
                        focus = merge_focus(stored.get("focus"), item["focus"])
                        if focus != stored.get("focus"):
                            db.execute("UPDATE evidence_items SET data=? WHERE id=?",
                                       (dumps({**stored, "focus": focus}), item_id))
                db.execute("INSERT INTO graph_versions VALUES (?,?,?)", (graph["version"], dumps(graph), now()))
                for unit_id in unit_ids:
                    db.execute("UPDATE work_units SET status='integrated',updated_at=? WHERE id=?", (now(), unit_id))
                for source_id, content_hash in processed.items():
                    db.execute("UPDATE source_records SET processed_hash=? WHERE id=? AND content_hash=?",
                               (content_hash, source_id, content_hash))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return graph


    def start_llm_call(self, call_id: str, run_id: str, unit_id: str, stage: str, attempt: int, metadata: dict) -> None:
        with self.connection() as db, db:
            db.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,NULL,'running',?,'{}')",
                       (call_id, run_id, unit_id, stage, attempt, now(), dumps(metadata)))

    def finish_llm_call(self, call_id: str, status: str, details: dict) -> None:
        with self.connection() as db, db:
            db.execute("UPDATE llm_calls SET finished_at=?,status=?,details=? WHERE id=?",
                       (now(), status, dumps(details), call_id))

    def llm_calls(self, run_id: str | None = None) -> list[dict]:
        with self.connection() as db:
            rows = list(db.execute("SELECT * FROM llm_calls WHERE run_id=? ORDER BY rowid", (run_id,))) if run_id else list(db.execute("SELECT * FROM llm_calls ORDER BY rowid"))
        return [{**dict(row), "metadata": json.loads(row["metadata"]), "details": json.loads(row["details"])} for row in rows]

    def llm_token_totals(self) -> tuple[int, int, int]:
        """Known input + output tokens, calls with both counts, all host calls."""
        input_number = "json_type(details, '$.usage.input_tokens') IN ('integer', 'real')"
        output_number = "json_type(details, '$.usage.output_tokens') IN ('integer', 'real')"
        complete = f"{input_number} AND {output_number}"
        with self.connection() as db:
            row = db.execute(f"""SELECT COUNT(*) AS calls,
                COALESCE(SUM(CASE WHEN {complete} THEN 1 ELSE 0 END), 0) AS known_calls,
                COALESCE(SUM(CASE WHEN {complete} THEN
                    json_extract(details, '$.usage.input_tokens') +
                    json_extract(details, '$.usage.output_tokens') ELSE 0 END), 0) AS tokens
                FROM llm_calls""").fetchone()
        return int(row["tokens"]), int(row["known_calls"]), int(row["calls"])

    def start_run(self, run_id: str, manifest: dict) -> None:
        with self.connection() as db, db:
            db.execute("INSERT INTO analysis_runs VALUES (?,?,NULL,'scanning',?,'{}')", (run_id, now(), dumps(manifest)))

    def finish_run(self, run_id: str, status: str, details: dict) -> None:
        stamp = now()
        with self.connection() as db, db:
            db.execute("UPDATE analysis_runs SET finished_at=?,status=?,details=? WHERE id=?",
                       (stamp, status, dumps(details), run_id))
        self.set_meta("last_check", {"at": stamp, "status": status, **details})

    def mark_interrupted_runs(self) -> None:
        # Called only while holding the cross-process analysis lock.
        with self.connection() as db, db:
            db.execute("UPDATE llm_calls SET status='cancelled',finished_at=? WHERE finished_at IS NULL", (now(),))
            db.execute("UPDATE analysis_runs SET status='cancelled',finished_at=? WHERE finished_at IS NULL", (now(),))
