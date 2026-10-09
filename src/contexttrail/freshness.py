"""How far the saved graph lags behind the transcripts: counted, with no model call and no full re-parse.

Two numbers answer "is this graph current?":

- records the last scan ingested that no analysis has processed yet (from the store alone);
- records written since the last scan. Parsing every transcript again costs tens of seconds on a
  machine with a few gigabytes of sessions, so the last scan leaves an index of every transcript
  file (size, mtime) and of every in-scope opencode session (`time_updated`) in the store, and this
  check parses only the files that are new or changed. A caller with a time budget (the agent's
  `find` header) can skip that parse above a byte limit and gets the file count instead. What one
  check parsed is kept per file (`freshness_cache`: size, mtime and the counts) under the scan and
  the stored records it was measured against, so a `find` after a `find` re-reads only what changed
  in between.

Nothing here estimates. What was not parsed is reported as not parsed.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_context import Scope
from .i18n import tr
from .model import Snapshot
from .sources.local import jsonl_files, parse_claude, parse_codex, source_homes
from .sources.opencode import opencode_databases, parse_opencode, session_times
from .store import Store, in_journal
from .util import digest, now

INDEX_KEY = "source_index"
INDEX_VERSION = 1
CACHE_KEY = "freshness_cache"
# `find` parses changed files up to this many bytes (about a second and a half at the measured
# 80 MB/s); `status` has no limit.
FIND_BUDGET_BYTES = 128 * 1024 * 1024
UNIT_DONE = {"integrated", "superseded"}


def build_index(scope: Scope, snapshot: Snapshot, *, codex_home: Path | None, claude_home: Path | None,
                opencode_home: Path | None) -> dict[str, Any]:
    """What the scan saw: each transcript file's size and mtime, each in-scope opencode session's update time.

    A file's size is the length the parser read (its manifest), so a file that grew during the
    scan still counts as changed next time.
    """
    codex_home, claude_home, opencode_home = source_homes(codex_home, claude_home, opencode_home)
    read = {entry["path"]: entry.get("complete_bytes", entry.get("bytes")) for entry in snapshot.files if entry.get("path")}
    files: dict[str, dict[str, int]] = {}
    for path, _ in jsonl_files(codex_home, claude_home):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        size = read.get(str(path))
        files[str(path)] = {"bytes": size if isinstance(size, int) else info.st_size, "mtime_ns": info.st_mtime_ns}
    databases: dict[str, dict[str, Any]] = {}
    for path in opencode_databases(opencode_home):
        databases[str(path)] = {"sessions": session_times(path, scope) or {}}
    return {"version": INDEX_VERSION, "scanned_at": now(), "files": files, "opencode": databases,
            "homes": {"codex": str(codex_home), "claude": str(claude_home), "opencode": str(opencode_home)}}


def check(scope: Scope, store: Store, *, codex_home: Path | None = None, claude_home: Path | None = None,
          opencode_home: Path | None = None, budget_bytes: int | None = None) -> dict[str, Any]:
    """The lag between the graph and the transcripts, as counts.

    `since_scan.parsed` says whether the changed files were parsed (then `sessions`, `records`,
    `newest_at` and `by_source` are exact) or only counted against the budget.
    """
    started = time.perf_counter()
    graph = store.graph()
    index = store.get_meta(INDEX_KEY)
    sources = store.sources()
    stored_units = store.units()
    failed = held_failures(store, sources, stored_units)
    held = {i for entry in failed.values() for i in entry["sources"]}
    # Records of a unit skipped for a failure of its own are counted with that unit, not as waiting.
    pending = sum(1 for i, row in sources.items()
                  if row["available"] and row["processed_hash"] != row["content_hash"] and i not in held)
    units = sum(1 for unit in stored_units if unit["status"] not in UNIT_DONE and unit["id"] not in failed)
    result: dict[str, Any] = {
        "graph": {"version": graph["version"], "analysis_status": graph.get("analysis_status"),
                  "analyzed_at": graph.get("analyzed_at")},
        "scanned_at": index.get("scanned_at") if index else None,
        "pending": {"records": pending, "units": units},
        "failed_units": len(failed),
        "unaudited": unaudited(store, sources, stored_units),
        "since_scan": None, "took_ms": 0}
    if not index:
        result["took_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    codex_home, claude_home, opencode_home = source_homes(codex_home, claude_home, opencode_home)
    known = index.get("files", {})
    changed: list[tuple[Path, str]] = []
    stats: dict[str, dict[str, int]] = {}
    new_files, changed_bytes, newest_mtime = 0, 0, 0
    for path, provider in jsonl_files(codex_home, claude_home):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        before = known.get(str(path))
        if before and before.get("bytes") == info.st_size and before.get("mtime_ns") == info.st_mtime_ns:
            continue
        if not before:
            new_files += 1
        changed.append((path, provider))
        stats[str(path)] = {"bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
        changed_bytes += info.st_size
        newest_mtime = max(newest_mtime, info.st_mtime_ns)
    deleted = sum(1 for path in known if not Path(path).exists())
    databases_changed: list[tuple[Path, dict[str, int]]] = []
    for path in opencode_databases(opencode_home):
        before = (index.get("opencode", {}).get(str(path)) or {}).get("sessions", {})
        times = session_times(path, scope)
        if times is None or any(before.get(session) != stamp for session, stamp in times.items()):
            databases_changed.append((path, times or {}))
            try:
                changed_bytes += os.stat(path).st_size
            except OSError:
                pass
            if times:
                newest_mtime = max(newest_mtime, max(times.values()) * 1_000_000)
    since: dict[str, Any] = {"files_changed": len(changed), "files_new": new_files, "files_deleted": deleted,
                             "databases_changed": len(databases_changed), "bytes": changed_bytes,
                             "newest_change_at": _iso_ns(newest_mtime), "parsed": False}
    result["since_scan"] = since
    if budget_bytes is not None and changed_bytes > budget_bytes:
        result["took_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    # Per-file results of an earlier check against the same scan and the same stored records (a "new" record is
    # one the store does not hold with that hash, so the cache is keyed on both); a file is re-read only when it
    # changed again.
    basis = {"scanned_at": index.get("scanned_at"),
             "sources": digest(sorted((key, row["content_hash"]) for key, row in sources.items())),
             "journaled": sorted(store.journaled_sessions())}
    cache = store.get_meta(CACHE_KEY) or {}
    cached = cache.get("files", {}) if cache.get("basis") == basis else {}
    kept: dict[str, dict[str, Any]] = {}
    parsed_now = False

    journaled, noted_since = store.journaled_sessions(), store.journal_since()

    def tally(records) -> dict[str, Any]:
        count, newest, by_source, sessions = 0, None, {}, set()
        for record in records:
            row = sources.get(record.source_id)
            if row and row["content_hash"] == record.content_hash:
                continue
            # The agent writes these sessions itself (`contexttrail note`); analysis will not read them.
            if in_journal(record.session_id, record.lineage, record.recorded_at, journaled, noted_since):
                continue
            count += 1
            sessions.add((record.provider, record.session_id))
            by_source[record.provider] = by_source.get(record.provider, 0) + 1
            if record.recorded_at and (newest is None or record.recorded_at > newest):
                newest = record.recorded_at
        return {"records": count, "newest": newest, "by_source": by_source, "sessions": sorted(sessions)}

    for path, provider in changed:
        key = str(path)
        entry = cached.get(key)
        if not (entry and entry.get("bytes") == stats[key]["bytes"] and entry.get("mtime_ns") == stats[key]["mtime_ns"]):
            snapshot = parse_claude(path, scope) if provider == "claude" else parse_codex(path, scope)
            entry = {**stats[key], **tally(snapshot.records)}
            parsed_now = True
        kept[key] = entry
    for path, times in databases_changed:
        key = str(path)
        entry = cached.get(key)
        if not (entry and entry.get("sessions_at") == times):
            entry = {"sessions_at": times, **tally(parse_opencode(path, scope).records)}
            parsed_now = True
        kept[key] = entry
    if parsed_now:
        try:
            store.set_meta(CACHE_KEY, {"basis": basis, "files": kept})
        except Exception:  # a read path never fails for want of its cache
            pass
    sessions: set[tuple[str, str | None]] = set()
    by_source: dict[str, int] = {}
    newest: str | None = None
    count = 0
    for entry in kept.values():
        count += entry.get("records", 0)
        sessions.update(tuple(s) for s in entry.get("sessions", []))
        for provider, n in (entry.get("by_source") or {}).items():
            by_source[provider] = by_source.get(provider, 0) + n
        if entry.get("newest") and (newest is None or entry["newest"] > newest):
            newest = entry["newest"]
    since.update(parsed=True, sessions=len(sessions), records=count, newest_at=newest, by_source=by_source)
    result["took_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def held_failures(store: Store, sources: dict[str, dict], units: list[dict]) -> dict[str, dict]:
    """The units skipped for a failure of their own that are still the input they failed on and still not done.

    From the store alone. The routing signature is not compared here: a changed setting sends the unit again
    at the next run, and until then it is still counted as skipped.
    """
    failures = store.unit_failures()
    if not failures:
        return {}
    status = {unit["id"]: unit["status"] for unit in units}
    covered = {i for unit in units if unit["status"] == "integrated" for i in unit["sources"]}
    held = {}
    for unit_id, entry in failures.items():
        ids = entry.get("sources") or []
        if status.get(unit_id) in UNIT_DONE or not ids:
            continue
        rows = [sources.get(i) for i in ids]
        if not all(row and row["available"] and row["content_hash"] == (entry.get("hashes") or {}).get(i)
                   for i, row in zip(ids, rows)):
            continue
        waiting = (not all(i in covered for i in ids) if entry.get("audit") else
                   any(row["processed_hash"] != row["content_hash"] for row in rows))
        if waiting:
            held[unit_id] = entry
    return held


def unaudited(store: Store, sources: dict[str, dict], units: list[dict]) -> dict[str, int]:
    """What `analyze --audit` would read, from the store alone: the stored records of sessions with notes
    (and their sub-agents) that no integrated unit covers. A record too large to send stays in the count."""
    journaled, noted_since = store.journaled_sessions(), store.journal_since()
    if not journaled:
        return {"sessions": 0, "records": 0}
    covered = {i for unit in units if unit["status"] == "integrated" for i in unit["sources"]}
    records, sessions = 0, set()
    for source_id, row in sources.items():
        meta = row["metadata"]
        if not row["available"] or meta.get("provider") == "git" or source_id in covered:
            continue
        if in_journal(meta.get("session_id"), meta.get("lineage"), meta.get("recorded_at"), journaled, noted_since):
            records += 1
            sessions.add((meta.get("provider"), meta.get("session_id")))
    return {"sessions": len(sessions), "records": records}


def check_from_store(scope: Scope, store: Store, *, budget_bytes: int | None = FIND_BUDGET_BYTES) -> dict[str, Any] | None:
    """The check with the project's saved source directories; None when the state cannot be read."""
    try:
        options = store.get_meta("options", {}) or {}
        homes = {key: Path(options[key]) for key in ("codex_home", "claude_home", "opencode_home") if options.get(key)}
        return check(scope, store, budget_bytes=budget_bytes, **homes)
    except Exception:  # a locked or half-written state must not take the read path down
        return None


def _iso_ns(nanoseconds: int) -> str | None:
    if nanoseconds <= 0:
        return None
    moment = datetime.fromtimestamp(nanoseconds / 1e9, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(value: str | None, reference: datetime | None = None) -> str:
    """`10 min ago`, `2 h ago`, `3 d ago`; empty when there is no time."""
    if not value:
        return ""
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
    seconds = max(0, int(((reference or datetime.now(timezone.utc)) - then).total_seconds()))
    if seconds < 90:
        return tr("방금", "just now")
    if seconds < 3600:
        return tr(f"{seconds // 60}분 전", f"{seconds // 60} min ago")
    if seconds < 36 * 3600:
        return tr(f"{seconds // 3600}시간 전", f"{seconds // 3600} h ago")
    return tr(f"{seconds // 86400}일 전", f"{seconds // 86400} d ago")


def summary(result: dict[str, Any] | None) -> str:
    """One phrase for a header: what is not in the graph yet, or `up to date`."""
    if result is None:
        return tr("신선도 확인 불가", "freshness unknown")
    if not result["scanned_at"]:
        return tr("기록을 아직 scan하지 않음", "transcripts never scanned")
    parts: list[str] = []
    pending = result["pending"]["records"]
    if pending:
        parts.append(tr(f"마지막 scan 기준 미분석 기록 {pending}개", f"{pending} records not analyzed as of the last scan"))
    since = result["since_scan"] or {}
    if since.get("parsed"):
        if since["records"]:
            when = ago(since.get("newest_at"))
            parts.append(tr(f"scan 이후 세션 {since['sessions']}개·기록 {since['records']}개" + (f" (최근 {when})" if when else ""),
                            f"{since['sessions']} sessions, {since['records']} records since" + (f" (newest {when})" if when else "")))
    elif since.get("files_changed") or since.get("databases_changed"):
        changed = since.get("files_changed", 0) + since.get("databases_changed", 0)
        when = ago(since.get("newest_change_at"))
        parts.append(tr(f"scan 이후 바뀐 기록 파일 {changed}개 (파싱 안 함" + (f", 최근 {when}" if when else "") + ")",
                        f"{changed} transcript files changed since the scan (not parsed" + (f", newest {when}" if when else "") + ")"))
    skipped = result.get("failed_units", 0)
    if skipped:
        parts.append(tr(f"실패로 건너뛴 작업 단위 {skipped}개", f"{skipped} work units skipped after a failure"))
    audit = (result.get("unaudited") or {}).get("sessions", 0)
    if audit:
        parts.append(tr(f"감사 안 한 note 세션 {audit}개", f"{audit} noted sessions not audited"))
    return " + ".join(parts) if parts else tr("최신", "up to date")


def status_lines(result: dict[str, Any], folder: Path) -> list[str]:
    """The `status` command's screen text."""
    graph = result["graph"]
    version, state = graph["version"], graph.get("analysis_status") or "no_data"
    analyzed = _local(graph.get("analyzed_at"))
    lines = [tr(f"ContextTrail · 그래프 v{version} ({state}) · 분석 기준 {analyzed or '없음'} · AI 호출 없음",
                f"ContextTrail · graph v{version} ({state}) · analyzed as of {analyzed or 'none'} · no AI calls")]
    scanned = result["scanned_at"]
    if not scanned:
        lines.append(tr("기록을 아직 scan하지 않았습니다: contexttrail scan . 또는 contexttrail analyze .",
                        "Transcripts were never scanned: contexttrail scan . or contexttrail analyze ."))
        return lines
    pending = result["pending"]
    lines.append(tr(f"마지막 scan {_local(scanned)} ({ago(scanned)}) · 그때 기준 미분석 기록 {pending['records']}개"
                    + (f", 작업 단위 {pending['units']}개 대기" if pending["units"] else ""),
                    f"Last scan {_local(scanned)} ({ago(scanned)}) · {pending['records']} records not analyzed as of then"
                    + (f", {pending['units']} work units waiting" if pending["units"] else "")))
    since = result["since_scan"] or {}
    if since.get("parsed"):
        if since["records"]:
            sources = ", ".join(f"{name} {count}" for name, count in sorted(since["by_source"].items()))
            lines.append(tr(f"scan 이후: 세션 {since['sessions']}개, 기록 {since['records']}개 ({sources}), 최근 {ago(since['newest_at'])} "
                            f"· 바뀐 파일 {since['files_changed']}개를 {result['took_ms']} ms에 읽음",
                            f"Since then: {since['sessions']} sessions, {since['records']} records ({sources}), newest {ago(since['newest_at'])} "
                            f"· {since['files_changed']} changed files read in {result['took_ms']} ms"))
        else:
            lines.append(tr(f"scan 이후 새 기록 없음 · 바뀐 파일 {since['files_changed']}개 확인에 {result['took_ms']} ms",
                            f"Nothing new since · {since['files_changed']} changed files checked in {result['took_ms']} ms"))
    else:
        lines.append(tr(f"scan 이후 바뀐 기록 파일 {since.get('files_changed', 0) + since.get('databases_changed', 0)}개 (파싱 안 함)",
                        f"{since.get('files_changed', 0) + since.get('databases_changed', 0)} transcript files changed since the scan (not parsed)"))
    if pending["records"] or since.get("records"):
        lines.append(tr(f"갱신: contexttrail analyze {folder} --units N  (에이전트에서는 /contexttrail-update N)",
                        f"To update: contexttrail analyze {folder} --units N  (from an agent: /contexttrail-update N)"))
    else:
        lines.append(tr("그래프가 기록을 따라잡고 있습니다.", "The graph is up to date with the transcripts."))
    skipped = result.get("failed_units", 0)
    if skipped:
        lines.append(tr(f"실패해서 건너뛴 작업 단위 {skipped}개: 기록이나 설정이 바뀌기 전에는 다시 보내지 않습니다. "
                        f"다시 보내려면: contexttrail analyze {folder} --retry-failed",
                        f"{skipped} work units skipped after a failure of their own: not sent again until their records or "
                        f"settings change. To send them again: contexttrail analyze {folder} --retry-failed"))
    audit = result.get("unaudited") or {}
    if audit.get("sessions"):
        lines.append(tr(f"note가 있는 세션 {audit['sessions']}개(기록 {audit['records']}개)는 아직 감사하지 않았습니다. "
                        f"note가 빠뜨린 것을 찾으려면: contexttrail analyze {folder} --audit --units N (미리 보기: scan --audit)",
                        f"{audit['sessions']} sessions with notes ({audit['records']} records) have not been audited. "
                        f"To find what the notes missed: contexttrail analyze {folder} --audit --units N (preview: scan --audit)"))
    return lines


def _local(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""
