"""opencode transcripts: the SQLite database under the opencode data directory.

opencode (anomalyco/opencode, 1.x) keeps every session in one SQLite database in WAL
mode: `session` (one row per session; `directory` is the working directory, `parent_id`
links a sub-agent session to the session that started it), `message` (one row per turn,
`data` is the message JSON) and `part` (one row per piece of a message: text, tool call
with its result, patch, compaction marker and so on). Shapes verified against the
schema in packages/schema/src/v1/session.ts on 2026-10-06.

The database is opened read-only (`mode=ro`, `query_only`) and only those three tables
are read. The same file holds account and credential tables; they are never named in a
query. A session belongs to the project only by its recorded `directory` (and a tool's
explicit `workdir`), never by keyword guessing, so out-of-scope sessions never reach a
model.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..git_context import Scope
from ..model import Snapshot, SourceRecord, segment_record
from ..util import digest, ident, within

PROVIDER = "opencode"
TABLES = ("session", "message", "part")
SEGMENT_CHARS = 32_000
# A tool result opencode truncated keeps its full text in tool-output/; the note that
# replaces the missing tail is code-written English, like the head/tail markers.
TRUNCATED_NOTE = "[opencode truncated this output; the full text in its tool-output directory was not read]"

# Part types that carry no decision narrative. Each entry is a claim that dropping it
# cannot change a reconstructed goal, attempt, failure or decision (see local.py).
IGNORED_NO_ANALYSIS_VALUE = {
    "part:step-start": "step boundary with a filesystem snapshot id — no narrative",
    "part:step-finish": "token counts, cost and finish reason — billing data, tallied separately in the local call ledger",
    "part:snapshot": "filesystem snapshot id — no narrative",
    "part:agent": "an @agent mention; the text part of the same message carries the request",
    "part:reasoning": "private reasoning — never collected, as with Codex and Claude",
}

# Part types that may hold decision-relevant context but are not parsed yet; reported once per scan.
KNOWN_UNPARSED = {
    "part:retry": "a retried provider call with its error — could ground a failed attempt, but is not sent to analysis yet",
}

SELF_RUN_PREFIXES = ("contexttrail-run-", "projectflow-run-")


def opencode_data_dir(home: Path | None = None) -> Path:
    """Where opencode keeps its database: as given, else `$XDG_DATA_HOME/opencode`, else `~/.local/share/opencode`.

    opencode resolves the directory with the xdg-basedir package (packages/core/src/global.ts);
    it has no environment variable of its own for the data directory.
    """
    if home is not None:
        return home.expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path("~/.local/share").expanduser()
    return root / "opencode"


def opencode_databases(home: Path) -> list[Path]:
    """The database files under a data directory: `opencode.db` on release channels, `opencode-<channel>.db` otherwise.

    `OPENCODE_DB` names one file instead (absolute, or relative to the data directory), as in
    packages/core/src/database/database.ts. Symlinks are not followed.
    """
    found: list[Path] = []
    named = os.environ.get("OPENCODE_DB")
    if named and named != ":memory:":
        candidate = Path(named)
        candidate = candidate if candidate.is_absolute() else home / candidate
        if candidate.is_file() and not candidate.is_symlink():
            found.append(candidate)
    if home.is_dir():
        for path in sorted(home.glob("opencode*.db")):
            if path.is_file() and not path.is_symlink() and within(path, home):
                found.append(path)
    return list(dict.fromkeys(found))


def _connect(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def _iso(milliseconds: Any) -> str | None:
    if not isinstance(milliseconds, (int, float)) or isinstance(milliseconds, bool) or milliseconds <= 0:
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _json(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _absolute(value: Any) -> str | None:
    return value if isinstance(value, str) and value and Path(value).is_absolute() else None


def _label(path: Path) -> str:
    return f"{path.parent.name}/{path.name}#{digest(str(path))[:8]}" if path.parent.name else f"{path.name}#{digest(str(path))[:8]}"


def _binary_marker(url: Any, mime: str) -> str | None:
    if isinstance(url, str) and url.startswith("data:"):
        return f"[opencode attachment payload omitted from text analysis: media_type={mime}, encoded_chars={len(url)}, sha256={digest(url)}]"
    return None


class _SessionParser:
    """Records of one in-scope session, in message then part order (the order opencode replays them)."""

    def __init__(self, path: Path, scope: Scope, session: sqlite3.Row, lineage: dict[str, Any],
                 decisions: dict[str, int], unknown: set[str], deferred: dict[str, int]):
        self.path, self.scope, self.session, self.lineage = path, scope, session, lineage
        self.decisions, self.unknown, self.deferred = decisions, unknown, deferred
        self.session_id = str(session["id"])
        self.directory = _absolute(session["directory"])
        self.records: list[SourceRecord] = []
        self.sequence = 0

    def record(self, key: str, role: str, content: str, row: sqlite3.Row, cwd: str | None,
               *, table: str = "part", **kwargs: Any) -> None:
        """One SourceRecord pinned to the database row (a part, or a message for a turn-level note)."""
        if not content:
            return
        if not self.scope.includes(cwd):
            self.decisions["outside_scope"] += 1
            return
        self.decisions["selected"] += 1
        locator = {"kind": "sqlite", "path": str(self.path), "table": table, "row_id": str(row["id"]),
                   "line": self.sequence, "raw_hash": digest(row["data"])}
        if table == "part":
            locator["message_id"] = str(row["message_id"])
        kwargs.setdefault("lineage", self.lineage)
        self.records.append(SourceRecord(ident("src_", PROVIDER, self.session_id, key), PROVIDER, self.session_id,
                                         role, content.replace("\r\n", "\n"), locator, cwd=cwd,
                                         worktree_id=self.scope.worktree_for(cwd),
                                         recorded_at=_iso(row["time_created"]), **kwargs))

    def classify(self, name: str) -> None:
        if name in IGNORED_NO_ANALYSIS_VALUE:
            return
        if name in KNOWN_UNPARSED:
            self.deferred[name] = self.deferred.get(name, 0) + 1
            return
        self.unknown.add(name)

    def message(self, row: sqlite3.Row, parts: list[sqlite3.Row]) -> None:
        data = _json(row["data"])
        if data is None:
            self.unknown.add("message:not-object")
            return
        role = data.get("role")
        if role not in {"user", "assistant"}:
            self.unknown.add("message:role=" + str(role))
            return
        message_id = str(row["id"])
        path = data.get("path") if isinstance(data.get("path"), dict) else {}
        cwd = _absolute(path.get("cwd")) or self.directory
        parent = data.get("parentID") if role == "assistant" else None
        common = {"native_record_id": message_id, "parent_record_id": parent if isinstance(parent, str) else None}
        summary = role == "assistant" and data.get("summary") is True
        for part in parts:
            self.sequence += 1
            self.part(part, role, cwd, summary, common)
        error = data.get("error") if role == "assistant" else None
        if isinstance(error, dict):
            name = str(error.get("name") or "error")
            detail = error.get("data", {}).get("message") if isinstance(error.get("data"), dict) else None
            text = f"Assistant turn ended with error: {name}" + (f": {detail[:500]}" if isinstance(detail, str) and detail else "")
            self.sequence += 1
            self.record(f"{message_id}:error", "metadata", text, row, cwd, table="message", **common,
                        lineage={**self.lineage, "kind": "turn_error"})

    def part(self, part: sqlite3.Row, role: str, cwd: str | None, summary: bool, common: dict[str, Any]) -> None:
        data = _json(part["data"])
        if data is None:
            self.unknown.add("part:not-object")
            return
        kind = data.get("type")
        key = str(part["id"])
        if kind == "text":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            if role == "user":
                # Text opencode put in the user role itself: attachments lifted out of tool results
                # (`synthetic`), text it decided not to send (`ignored`), the follow-up it writes after an
                # automatic compaction. None of it is something the person typed.
                flag = ("synthetic" if data.get("synthetic") else "ignored" if data.get("ignored")
                        else "compaction_continue" if metadata.get("compaction_continue") else None)
                if flag:
                    self.record(key, "metadata", text, part, cwd, **common, lineage={**self.lineage, "kind": flag})
                else:
                    self.record(key, "user", text, part, cwd, **common)
            elif summary:
                # The assistant message that answers a compaction request holds the summary.
                self.record(key, "metadata", text, part, cwd, **common, derivation="summary",
                            lineage={**self.lineage, "kind": "compaction_summary"})
            else:
                self.record(key, "assistant", text, part, cwd, **common)
        elif kind == "compaction":
            mode = "automatic" if data.get("auto") else "requested"
            overflow = ", after a context overflow" if data.get("overflow") else ""
            self.record(key, "metadata", f"[opencode compaction boundary: {mode}{overflow}; the summary follows]",
                        part, cwd, **common, derivation="summary", lineage={**self.lineage, "kind": "compaction"})
        elif kind == "subtask":
            agent = str(data.get("agent") or "unknown")
            description = data.get("description") if isinstance(data.get("description"), str) else ""
            prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
            head = f"Subtask for agent {agent}" + (f": {description}" if description else "")
            self.record(key, "user", head + ("\n" + prompt if prompt else ""), part, cwd, **common,
                        lineage={**self.lineage, "subtask_agent": agent})
        elif kind == "file":
            mime = str(data.get("mime") or "unknown")
            name = data.get("filename") if isinstance(data.get("filename"), str) else None
            marker = _binary_marker(data.get("url"), mime)
            url = data.get("url") if isinstance(data.get("url"), str) else ""
            text = marker or f"[opencode attached file: {name or url or 'unknown'} ({mime})]"
            self.record(key, "metadata", text, part, cwd, **common,
                        lineage={**self.lineage, "kind": "attachment", "attachment_type": mime, "filename": name or ""})
        elif kind == "tool":
            self.tool(part, data, cwd, common)
        elif kind == "patch":
            files = [f for f in data.get("files", []) if isinstance(f, str)] if isinstance(data.get("files"), list) else []
            if files:
                snapshot = str(data.get("hash") or "")[:12]
                text = f"Files changed in this step (opencode snapshot {snapshot}):\n" + "\n".join(files)
                self.record(key, "metadata", text, part, cwd, **common, lineage={**self.lineage, "kind": "patch"})
        else:
            self.classify("part:" + str(kind))

    def tool(self, part: sqlite3.Row, data: dict[str, Any], cwd: str | None, common: dict[str, Any]) -> None:
        key = str(part["id"])
        name = str(data.get("tool") or "unknown")
        call_id = str(data.get("callID") or key)
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        params = state.get("input") if isinstance(state.get("input"), dict) else {}
        item_cwd = cwd
        if "workdir" in params:
            # An explicit tool cwd overrides the session's; a relative one resolves only against
            # a known absolute base, never against our own process.
            supplied = params["workdir"]
            if isinstance(supplied, str) and supplied:
                item_cwd = str((Path(cwd) / supplied).resolve()) if cwd else _absolute(supplied)
            else:
                item_cwd = None
        body = json.dumps(params, ensure_ascii=False, sort_keys=True)
        self.record(f"{key}:call", "tool_call", f"Tool: {name}\n{body}", part, item_cwd, **common, tool_call_id=call_id)
        status = state.get("status")
        if status == "completed":
            output = state.get("output")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else ""
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            if metadata.get("truncated") is True and metadata.get("outputPath"):
                text = (text + "\n" if text else "") + TRUNCATED_NOTE
        elif status == "error":
            error = state.get("error")
            text = "[tool error]\n" + (error if isinstance(error, str) else json.dumps(error, ensure_ascii=False))
        else:
            return  # pending or running: the call is known, its result is not
        self.record(f"{key}:result", "tool_result", text, part, item_cwd, **common, tool_call_id=call_id)


def session_times(path: Path, scope: Scope) -> dict[str, int] | None:
    """`time_updated` of every in-scope session, from the session table alone (no message is read).

    None when the database cannot be read; the caller then treats it as changed.
    """
    try:
        with closing(_connect(path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session" not in names:
                return {}
            rows = connection.execute("SELECT id, directory, time_updated FROM session").fetchall()
    except sqlite3.Error:
        return None
    found: dict[str, int] = {}
    for row in rows:
        directory = _absolute(row["directory"])
        if directory and not Path(directory).name.startswith(SELF_RUN_PREFIXES) and scope.includes(directory):
            found[str(row["id"])] = int(row["time_updated"] or 0)
    return found


def parse_opencode(path: Path, scope: Scope) -> Snapshot:
    """Every record of the sessions whose recorded directory lies in the scope, from one database."""
    warnings: list[str] = []
    decisions = {"selected": 0, "outside_scope": 0, "unattributed": 0}
    sessions_audit = {"examined": 0, "selected": 0, "outside_scope": 0, "unattributed": 0, "self_generated": 0}
    manifest: dict[str, Any] = {"path": str(path)}

    def finish(records: list[SourceRecord]) -> Snapshot:
        manifest["selection"] = {"provider": PROVIDER, "path": str(path), "sessions": sessions_audit,
                                 "normalized_records": len(records), "scope_decisions": decisions,
                                 "status": "included" if records else ("unattributed" if sessions_audit["unattributed"] else "excluded")}
        return Snapshot(records, list(dict.fromkeys(warnings)), [manifest])

    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        manifest.update(bytes=info.st_size, mtime_ns=info.st_mtime_ns)
        connection = _connect(path)
    except (OSError, sqlite3.Error) as exc:
        warnings.append(f"opencode database not read: {path.name} ({type(exc).__name__})")
        return finish([])
    unknown: set[str] = set()
    deferred: dict[str, int] = {}
    records: list[SourceRecord] = []
    try:
        with closing(connection):
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(TABLES) <= names:
                warnings.append(f"opencode database without session/message/part tables, not read: {path.name}")
                return finish([])
            rows = connection.execute("SELECT id, parent_id, directory, agent FROM session ORDER BY time_created, id").fetchall()
            selected: dict[str, sqlite3.Row] = {}
            for session in rows:
                sessions_audit["examined"] += 1
                directory = _absolute(session["directory"])
                if directory is None:
                    sessions_audit["unattributed"] += 1
                elif Path(directory).name.startswith(SELF_RUN_PREFIXES):
                    sessions_audit["self_generated"] += 1
                elif scope.includes(directory):
                    selected[str(session["id"])] = session
                    sessions_audit["selected"] += 1
                else:
                    sessions_audit["outside_scope"] += 1
            if not selected:
                return finish([])
            parts_by_session: dict[str, dict[str, list[sqlite3.Row]]] = {}
            for session_id in selected:
                grouped: dict[str, list[sqlite3.Row]] = {}
                for part in connection.execute("SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY message_id, id", (session_id,)):
                    grouped.setdefault(str(part["message_id"]), []).append(part)
                parts_by_session[session_id] = grouped
            # A sub-agent session is started by a `task` call in its parent; the call id joins them.
            launches: dict[str, str] = {}
            for session_id, grouped in parts_by_session.items():
                for parts in grouped.values():
                    for part in parts:
                        data = _json(part["data"])
                        if not data or data.get("type") != "tool":
                            continue
                        state = data.get("state") if isinstance(data.get("state"), dict) else {}
                        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
                        child = metadata.get("sessionId")
                        if isinstance(child, str) and data.get("callID"):
                            launches[child] = str(data["callID"])
            for session_id, session in selected.items():
                lineage: dict[str, Any] = {}
                parent = session["parent_id"]
                if isinstance(parent, str) and parent:
                    lineage = {"kind": "subagent", "parent_session_id": parent}
                    if session["agent"]:
                        lineage["agent_type"] = str(session["agent"])
                    if session_id in launches:
                        lineage["parent_tool_call_id"] = launches[session_id]
                parser = _SessionParser(path, scope, session, lineage, decisions, unknown, deferred)
                grouped = parts_by_session[session_id]
                for message in connection.execute("SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id", (session_id,)):
                    parser.message(message, grouped.get(str(message["id"]), []))
                records.extend(parser.records)
    except sqlite3.Error as exc:
        warnings.append(f"opencode database not read: {path.name} ({type(exc).__name__})")
        return finish([])
    if sessions_audit["unattributed"]:
        warnings.append(f"opencode path attribution unclear, {sessions_audit['unattributed']} sessions excluded: {path.name}")
    if unknown:
        warnings.append(f"opencode unsupported record types {path.name}: {', '.join(sorted(unknown))}")
    for name, count in sorted(deferred.items()):
        warnings.append(f"opencode unparsed record type {name}, {count} records not sent to analysis ({_label(path)}): {KNOWN_UNPARSED[name]}")
    return finish([piece for record in records for piece in segment_record(record, SEGMENT_CHARS)])
