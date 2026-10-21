from __future__ import annotations

import copy
import fcntl
import json
import zlib
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .i18n import tr
from .model import SourceRecord, empty_graph
from .schema import fill_authoring, fill_steps, fold_delegate_reports, link_delegations, link_forks
from .util import FlowError, dumps, mask_data, mask_evidence, merge_focus, now, private_dir

# Conservative floor for SQLite's per-statement bound variable limit (999).
_SQL_VARIABLES = 900


JOURNAL_KEY = "journal_sessions"
JOURNAL_SETTINGS_KEY = "journal"  # journal.SETTINGS_KEY: {"enabled_at", "since"}
JOURNAL_ACTIVITY_KEY = "journal_activity"  # journal.ACTIVITY_KEY: per session "noted_at", "reminded_at", …
# Per noted session: [from, to], the records after its last note, given back to analysis once the session went
# quiet (`TAIL_IDLE_SECONDS` after its last record) with no note for them.
JOURNAL_TAILS_KEY = "journal_tails"
TAIL_IDLE_SECONDS = 3600
FAILURES_KEY = "unit_failures"


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def in_journal(session_id: str | None, lineage: dict | None, recorded_at: str | None,
               sessions: dict | set, since: float | None, tails: dict | None = None) -> bool:
    """Whether notes account for a record: it belongs to a noted session (or one of its sub-agents) and was
    written once notes were on (`since`). A noted session's older records stay with analysis, and so do the
    records after its last note once it went quiet (`tails`, `Store.journal_tails`); a record with no time
    counts as noted."""
    owner = session_id if session_id in sessions else (lineage or {}).get("parent_session_id")
    if owner not in sessions:
        return False
    stamp = _timestamp(recorded_at)
    if stamp is not None and tails and owner in tails:
        start, end = tails[owner]
        if start < stamp <= end:
            return False
    return since is None or stamp is None or stamp >= since


# Schema 2: graph versions are stored zlib-compressed behind GRAPH_MARK. A version is a whole graph, and the
# views and pasted references (`contexttrail:ev_…@vN`) read old ones, so they are kept, only smaller (about 5x).
GRAPH_SCHEMA = 2
GRAPH_MARK = b"zlib1:"


def pack_text(text: str) -> bytes:
    return GRAPH_MARK + zlib.compress(text.encode("utf-8"), 6)


def unpack_graph(value: str | bytes) -> dict:
    if isinstance(value, bytes) and value.startswith(GRAPH_MARK):
        return json.loads(zlib.decompress(value[len(GRAPH_MARK):]).decode("utf-8"))
    return json.loads(value)


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
            if version not in (0, 1, GRAPH_SCHEMA):
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
            """)
            if version < GRAPH_SCHEMA:
                self._pack_old_graphs(db)
            db.execute(f"PRAGMA user_version={GRAPH_SCHEMA}")
            # WAL lets the screen and the browser keep reading while an analysis writes
            # (a rollback journal locks readers out for the whole of a large ingest).
            # State lives on local disk only (docs/SPEC.md), where WAL is supported.
            db.execute("PRAGMA journal_mode=WAL")
            row = db.execute("SELECT value FROM project_meta WHERE key='scope_id'").fetchone()
            if row and json.loads(row[0]) != scope_id:
                raise FlowError(scope_mismatch(directory))
            db.execute("INSERT OR IGNORE INTO project_meta VALUES ('scope_id', ?)", (dumps(scope_id),))
            db.commit()

    @staticmethod
    def _pack_old_graphs(db: sqlite3.Connection) -> None:
        """Schema 2 keeps each graph version compressed (`pack_graph`); versions written as text before are packed
        once, here, one row at a time. The pages they free are reused by later writes (no VACUUM, which would
        rewrite the whole file under readers)."""
        for (version,) in db.execute("SELECT version FROM graph_versions WHERE typeof(graph)='text'").fetchall():
            # Another process opening the same state may have packed it meanwhile.
            row = db.execute("SELECT graph FROM graph_versions WHERE version=? AND typeof(graph)='text'", (version,)).fetchone()
            if row:
                db.execute("UPDATE graph_versions SET graph=? WHERE version=?", (pack_text(row[0]), version))
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
        """A published graph version (the newest by default). Its strings come back masked (`util.mask_data`): a
        version stored before records were masked at reading may hold a credential in a title, summary or rationale,
        and every reader (screens, exports, session memory, the model's context) reads it through here. Publishing
        a graph built on it stores the masked text; IDs are never masked. A version never changes, so the masked
        text of the last one read is kept (`_shown`) and the next read of it only parses it."""
        with self.connection() as db:
            if version is None:
                key = db.execute("SELECT version, created_at FROM graph_versions ORDER BY version DESC LIMIT 1").fetchone()
            else:
                key = db.execute("SELECT version, created_at FROM graph_versions WHERE version=?", (version,)).fetchone()
            if key is None:
                if version is not None:
                    raise FlowError(tr("요청한 그래프 버전이 없습니다.", "The requested graph version does not exist."))
                return empty_graph(self.scope_id)
            key = tuple(key)
            shown = getattr(self, "_shown", None)
            if shown is None or shown[0] != key:
                row = db.execute("SELECT graph FROM graph_versions WHERE version=?", (key[0],)).fetchone()
                shown = self._shown = (key, json.dumps(mask_data(unpack_graph(row[0])), ensure_ascii=False))
        return json.loads(shown[1])

    def sources(self) -> dict[str, dict]:
        with self.connection() as db:
            return {row["id"]: {**dict(row), "metadata": json.loads(row["metadata"])}
                    for row in db.execute("SELECT * FROM source_records")}

    def latest_version(self) -> int:
        """The newest published graph version, without loading it."""
        with self.connection() as db:
            return int(db.execute("SELECT MAX(version) FROM graph_versions").fetchone()[0] or 0)

    def sources_by_id(self, ids: set[str] | list[str]) -> dict[str, dict]:
        """`sources()` for the named records only (a few rows instead of the whole table)."""
        wanted, found = list(ids), {}
        with self.connection() as db:
            for start in range(0, len(wanted), _SQL_VARIABLES):
                chunk = wanted[start:start + _SQL_VARIABLES]
                marks = ",".join("?" * len(chunk))
                for row in db.execute(f"SELECT * FROM source_records WHERE id IN ({marks})", chunk):
                    found[row["id"]] = {**dict(row), "metadata": json.loads(row["metadata"])}
        return found

    def pending_records(self) -> int:
        """How many available records no analysis has processed at their current content (counted in SQL)."""
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM source_records WHERE available=1 "
                              "AND (processed_hash IS NULL OR processed_hash != content_hash)").fetchone()[0]

    def ingest(self, records: list[SourceRecord], *, partial: bool = False) -> tuple[set[str], set[str]]:
        """Store the records a scan read. A full scan marks every stored record it did not see as unavailable;
        a `partial` one (`contexttrail note`, which reads only its own session) leaves the others alone."""
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
        missing = set() if partial else {i for i, row in previous.items() if row["available"] and i not in present}
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

    def journaled_sessions(self) -> dict[str, str]:
        """Sessions an agent wrote notes in (`contexttrail note`), with the time of the first note."""
        return self.get_meta(JOURNAL_KEY, {}) or {}

    def note_session(self, session_id: str) -> None:
        sessions = self.journaled_sessions()
        if session_id not in sessions:
            self.set_meta(JOURNAL_KEY, {**sessions, session_id: now()})

    def journal_since(self) -> float | None:
        """Since when notes account for the noted sessions: the earlier of when notes were first turned on and
        the oldest record a note cites (`journal.write` keeps it as `since`). State saved before that was kept
        falls back to `enabled_at` and the notes in the graph. None: no boundary is known."""
        settings = self.get_meta(JOURNAL_SETTINGS_KEY) or {}
        if settings.get("since"):
            return _timestamp(settings["since"])
        times = [t for t in [_timestamp(settings.get("enabled_at"))] if t is not None]
        times += [t for event in self.graph()["events"] if event.get("origin") == "note"
                  and (t := _timestamp(event.get("recorded_at"))) is not None]
        return min(times) if times else None

    def journal_tails(self) -> dict[str, tuple[float, float]]:
        """The stretch of each noted session given back to analysis (`acknowledge_journaled`), as timestamps."""
        found = {}
        for session, (start, end) in (self.get_meta(JOURNAL_TAILS_KEY, {}) or {}).items():
            if (a := _timestamp(start)) is not None and (b := _timestamp(end)) is not None:
                found[session] = (a, b)
        return found

    def _find_tails(self, records: list[SourceRecord], sessions: dict, moment: float) -> dict[str, tuple[float, float]]:
        """A noted session that went quiet with records after its last note: those records are analysis's.
        With the end-of-turn reminder sent only once work piles up, a session may end with work no note wrote."""
        stored = dict(self.get_meta(JOURNAL_TAILS_KEY, {}) or {})
        activity = self.get_meta(JOURNAL_ACTIVITY_KEY, {}) or {}
        last: dict[str, tuple[float, str]] = {}
        for r in records:
            owner = r.session_id if r.session_id in sessions else (r.lineage or {}).get("parent_session_id")
            stamp = _timestamp(r.recorded_at)
            if owner in sessions and stamp is not None and stamp > last.get(owner, (0.0, ""))[0]:
                last[owner] = (stamp, r.recorded_at)
        changed = False
        for session, (stamp, written) in last.items():
            noted = (activity.get(session) or {}).get("noted_at")
            start = _timestamp(noted)
            previous = stored.get(session)
            if previous and (_timestamp(previous[1]) or 0) >= stamp:
                continue  # given back already, nothing new since
            if start is None or stamp <= start or moment - stamp < TAIL_IDLE_SECONDS:
                continue
            # A later stretch after a resumed session starts at the old stretch's end when no note came between.
            begin = previous[0] if previous and (_timestamp(previous[1]) or 0) > start else noted
            stored[session] = [begin, written]
            changed = True
        if changed:
            self.set_meta(JOURNAL_TAILS_KEY, stored)
        return {s: (a, b) for s, (x, y) in stored.items()
                if (a := _timestamp(x)) is not None and (b := _timestamp(y)) is not None}

    def acknowledge_journaled(self, records: list[SourceRecord], moment: float | None = None) -> int:
        """Mark the records notes account for as processed (`in_journal`), so analysis does not write the same
        work a second time; a sub-agent session counts with its parent. A noted session's records from before
        notes were on stay with analysis, and are given back to it when an earlier version (or a note) marked
        them, unless an analysis covered them. Returns how many were marked."""
        sessions = self.journaled_sessions()
        if not sessions:
            return 0
        since = self.journal_since()
        tails = self._find_tails(records, sessions, time.time() if moment is None else moment)
        selected, earlier = [], []
        for r in records:
            if in_journal(r.session_id, r.lineage, r.recorded_at, sessions, since, tails):
                selected.append(r)
            elif r.session_id in sessions or (r.lineage or {}).get("parent_session_id") in sessions:
                earlier.append(r)
        self.mark_processed({r.source_id: r.content_hash for r in selected})
        if earlier:
            covered = {i for unit in self.units() if unit["status"] == "integrated" for i in unit["sources"]}
            back = [r.source_id for r in earlier if r.source_id not in covered
                    and (r.lineage or {}).get("kind") != "environment_context"]
            with self.connection() as db, db:
                db.executemany("UPDATE source_records SET processed_hash=NULL WHERE id=? AND processed_hash IS NOT NULL",
                               [(i,) for i in back])
        return len(selected)

    def mark_processed(self, processed: dict[str, str]) -> None:
        with self.connection() as db, db:
            db.executemany("UPDATE source_records SET processed_hash=? WHERE id=? AND content_hash=?",
                           [(content_hash, source_id, content_hash) for source_id, content_hash in processed.items()])

    def unit_failures(self) -> dict[str, dict]:
        """Units skipped for a failure of their own, by unit ID: what failed, and the input it failed on."""
        return self.get_meta(FAILURES_KEY, {}) or {}

    def record_unit_failure(self, unit_id: str, entry: dict) -> None:
        failures = self.unit_failures()
        failures[unit_id] = {**entry, "attempts": failures.get(unit_id, {}).get("attempts", 0) + 1}
        self.set_meta(FAILURES_KEY, failures)

    def clear_unit_failure(self, unit_id: str) -> dict | None:
        """Forget a unit's failure (it was integrated, or superseded); returns the entry it had."""
        failures = self.unit_failures()
        entry = failures.pop(unit_id, None)
        if entry is not None:
            self.set_meta(FAILURES_KEY, failures)
        return entry

    def integrated_units(self) -> int:
        """How many work units are integrated (it only grows, so it orders a failure against later units)."""
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM work_units WHERE status='integrated'").fetchone()[0]

    def unit(self, unit_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM work_units WHERE id=?", (unit_id,)).fetchone()
        return None if row is None else {**dict(row), "sources": json.loads(row["sources"]),
                                         "dependencies": json.loads(row["dependencies"]),
                                         "result": json.loads(row["result"]) if row["result"] is not None else None}

    def units(self, *, results: bool = True) -> list[dict]:
        """Every work unit. A caller that counts units by status passes `results=False`: the stored extraction
        results are the bulk of the table and are then not read (`result` is None)."""
        columns = "*" if results else "id,sources,dependencies,status,updated_at"
        with self.connection() as db:
            rows = list(db.execute(f"SELECT {columns} FROM work_units ORDER BY updated_at,id"))
        return [{**dict(row), "sources": json.loads(row["sources"]),
                 "dependencies": json.loads(row["dependencies"]),
                 "result": json.loads(row["result"]) if results and row["result"] is not None else None} for row in rows]

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
        """An evidence item, masked as it is given out (`util.mask_evidence`; the stored row keeps its text)."""
        with self.connection() as db:
            row = db.execute("SELECT data FROM evidence_items WHERE id=?", (evidence_id,)).fetchone()
            return mask_evidence(json.loads(row[0])) if row else None

    def evidence_many(self, ids: list[str]) -> dict[str, dict]:
        """Fetch many evidence items in one query, each masked like `evidence`.

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
                        result[row[0]] = mask_evidence(item)
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
                    raise FlowError(tr("기준 graph version이 달라졌습니다. 이 단위의 결과를 적용하지 않았습니다.",
                                       "The base graph version changed; this unit's result was not applied."))
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
                source_of = self._source_of(db, graph, evidence)
                fill_authoring(graph, source_of)
                fill_steps(graph, source_of)
                link_forks(graph, source_of)
                link_delegations(graph, source_of)
                fold_delegate_reports(graph, source_of)
                db.execute("INSERT INTO graph_versions VALUES (?,?,?)", (graph["version"], pack_text(dumps(graph)), now()))
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

    @staticmethod
    def _source_of(db: sqlite3.Connection, graph: dict, evidence: dict[str, dict]) -> Callable[[str], dict | None]:
        """A lookup from an evidence ID to the cited record's metadata as stored now (so `authoring` read by a
        later scan counts), else the copy the evidence item keeps; what `fill_authoring` reads at publish."""
        items = dict(evidence)
        wanted = sorted({i for event in graph["events"] for i in event.get("evidence_ids", [])} - set(items))
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            for row in db.execute(f"SELECT id, data FROM evidence_items WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                items[row["id"]] = json.loads(row["data"])
        source_ids = sorted({item["source_id"] for item in items.values() if item.get("source_id")})
        metadata: dict[str, dict] = {}
        for start in range(0, len(source_ids), 500):
            chunk = source_ids[start:start + 500]
            for row in db.execute(f"SELECT id, metadata FROM source_records WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                metadata[row["id"]] = json.loads(row["metadata"])

        def source_of(evidence_id: str) -> dict | None:
            item = items.get(evidence_id)
            if not item:
                return None
            return metadata.get(item.get("source_id")) or item.get("source")
        return source_of


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

    def llm_token_totals(self) -> tuple[int, int, int, float | None]:
        """Known tokens, calls with both counts, all host calls, and the cost the CLIs reported (None when none did).

        Tokens are input + output, plus Claude's cache writes and reads, which its `input_tokens` leaves out (on this
        repository's ledger they were 18 of 18.7 million); Codex's `input_tokens` already holds its cached part."""
        input_number = "json_type(details, '$.usage.input_tokens') IN ('integer', 'real')"
        output_number = "json_type(details, '$.usage.output_tokens') IN ('integer', 'real')"
        complete = f"{input_number} AND {output_number}"
        with self.connection() as db:
            row = db.execute(f"""SELECT COUNT(*) AS calls,
                COALESCE(SUM(CASE WHEN {complete} THEN 1 ELSE 0 END), 0) AS known_calls,
                COALESCE(SUM(CASE WHEN {complete} THEN
                    json_extract(details, '$.usage.input_tokens') +
                    json_extract(details, '$.usage.output_tokens') +
                    COALESCE(json_extract(details, '$.usage.cache_creation_input_tokens'), 0) +
                    COALESCE(json_extract(details, '$.usage.cache_read_input_tokens'), 0) ELSE 0 END), 0) AS tokens,
                SUM(json_extract(details, '$.usage.reported_cost_usd')) AS cost
                FROM llm_calls""").fetchone()
        return int(row["tokens"]), int(row["known_calls"]), int(row["calls"]), (
            float(row["cost"]) if row["cost"] is not None else None)

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


def scope_mismatch(directory: Path) -> str:
    """Why a saved state does not open: the scope is the project's absolute folder (or Git directory) path."""
    return tr(f"저장된 프로젝트 scope와 요청 scope가 일치하지 않습니다. 이 상태({directory})는 다른 경로의 프로젝트에서 "
              "만들어졌습니다: 프로젝트 폴더나 Git 저장소를 옮기거나 이름을 바꾸면 이렇게 됩니다. 원래 경로로 되돌리면 다시 열립니다.",
              f"The saved project scope does not match the requested scope. This state ({directory}) was made for the "
              "project at another path: moving or renaming the project folder or its Git repository does this. Moving it "
              "back to its old path opens it again.")


class ReadOnlyStore(Store):
    """The saved state opened for reading only: nothing is created, migrated or written, and no file is left behind.

    For a hook that must not leave anything in a project (`contexttrail context --hook`). The connection is
    read-only with a short busy timeout, so an analysis writing at the same time (the database is in WAL
    mode) neither blocks it nor is blocked by it. Even a read-only connection to a WAL database creates its
    `-wal` and `-shm` files and cannot remove them, so when neither exists (no connection is open, and the
    last one checkpointed everything into the main file) it opens the file `immutable`, which creates
    nothing; when they exist it reads through them as an ordinary reader. Every read method of `Store` works;
    a write raises `sqlite3.OperationalError`.
    """

    def __init__(self, directory: Path, scope_id: str = ""):
        self.directory, self.scope_id = directory, scope_id
        self.path = directory / "state.sqlite"
        if self.path.is_symlink() or not self.path.is_file():
            raise FlowError(tr("저장된 상태가 없습니다.", "There is no saved state."))

    def check_readable(self) -> "ReadOnlyStore":
        """Refuse what `Store()` would refuse, without its writes: a schema it cannot read, or another scope's state."""
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            row = db.execute("SELECT value FROM project_meta WHERE key='scope_id'").fetchone()
        if version not in (1, GRAPH_SCHEMA):
            raise FlowError(tr(f"지원하지 않는 DB schema: {version}", f"Unsupported DB schema: {version}"))
        if row and self.scope_id and json.loads(row[0]) != self.scope_id:
            raise FlowError(scope_mismatch(self.directory))
        return self

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        side_files = [self.path.with_name(self.path.name + suffix) for suffix in ("-wal", "-shm")]
        mode = "mode=ro" if any(path.exists() for path in side_files) else "mode=ro&immutable=1"
        db = sqlite3.connect(f"{self.path.as_uri()}?{mode}", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        try:
            yield db
        finally:
            db.close()
