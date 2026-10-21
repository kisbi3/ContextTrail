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
  in between. A Claude Code transcript that only grew is read from where the last reading stopped
  (`read_claude_appended`, with the state that reading left): a live session of another project
  passed 300 MB here and was parsed whole at every check. A file none of whose rows was the project's
  holds no count, so its state outlives the scan and the stored records, and the scan's own reading
  of such a file (`scan_cache`) gives the first one.

Nothing here estimates. What was not parsed is reported as not parsed.
"""
from __future__ import annotations

import dataclasses
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_context import Scope
from .i18n import tr
from .model import Snapshot
from .sources.local import (COMPRESSED_SUFFIX, RESUME_VERSION, jsonl_files, parse_claude, parse_codex,
                            read_claude_appended, reading_code, reads, resume_current, scan_cache_key,
                            source_homes)
from .sources.opencode import opencode_databases, parse_opencode, session_times
from .store import Store, in_journal
from .util import digest, now

INDEX_KEY = "source_index"
INDEX_VERSION = 1
CACHE_KEY = "freshness_cache"
# What earlier scans made of the transcript files that gave this project nothing (`ScanCache`).
SCAN_CACHE_META = "scan_cache"
# `find` parses changed files up to this many bytes (about a second and a half at the measured
# 80 MB/s); `status` has no limit.
FIND_BUDGET_BYTES = 128 * 1024 * 1024
UNIT_DONE = {"integrated", "superseded"}


def build_index(scope: Scope, snapshot: Snapshot, *, codex_home: Path | None, claude_home: Path | None,
                opencode_home: Path | None, sources=None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the scan saw: each transcript file's size and mtime, each in-scope opencode session's update time.

    A scan that read only some tools (`sources`) keeps the `previous` index's entries (files, opencode databases,
    homes) of the tools it did not read, so a later check of every tool compares like with like instead of taking
    their unchanged files for new, or a one-off `--sources` for the project's choice. A file's size is the length
    the parser read (its manifest), so a file that grew during the scan still counts as changed next time.
    """
    codex_home, claude_home, opencode_home = source_homes(codex_home, claude_home, opencode_home)
    homes = {"codex": str(codex_home), "claude": str(claude_home), "opencode": str(opencode_home)}
    read = {entry["path"]: entry.get("complete_bytes", entry.get("bytes")) for entry in snapshot.files if entry.get("path")}
    files: dict[str, dict[str, Any]] = {}
    for path, provider in jsonl_files(codex_home, claude_home, sources=sources):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        size = read.get(str(path))
        files[str(path)] = {"bytes": size if isinstance(size, int) else info.st_size, "mtime_ns": info.st_mtime_ns,
                            "provider": provider}
    databases: dict[str, dict[str, Any]] = {}
    for path in opencode_databases(opencode_home) if reads(sources, "opencode") else []:
        databases[str(path)] = {"sessions": session_times(path, scope) or {}}
    if sources is not None and previous and previous.get("version") == INDEX_VERSION:
        before = previous.get("homes") or {}
        for path, entry in (previous.get("files") or {}).items():
            provider = entry.get("provider") or _home_of(path, before)
            if provider and not reads(sources, provider) and path not in files:
                files[path] = {**entry, "provider": provider}
        if not reads(sources, "opencode"):
            databases = dict(previous.get("opencode") or {})
        homes.update({tool: before[tool] for tool in before if tool in homes and not reads(sources, tool)})
    return {"version": INDEX_VERSION, "scanned_at": now(), "files": files, "opencode": databases, "homes": homes}


def _home_of(path: str, homes: dict[str, str]) -> str | None:
    """The tool whose home holds a transcript file an older index listed without its tool."""
    for tool in ("codex", "claude"):
        if homes.get(tool) and Path(path).is_relative_to(homes[tool]):
            return tool
    return None


def stored(store: Store) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, dict]]:
    """What the store alone says: the graph, the records the last scan ingested that no analysis processed, the
    skipped and unaudited units. No transcript file is touched. Returns (result without `since_scan`, the saved
    source index, the stored records)."""
    graph = store.graph()
    index = store.get_meta(INDEX_KEY)
    sources = store.sources()
    stored_units = store.units(results=False)
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
        # Records no analysis processed that the last scan no longer found (a moved, compressed or unreadable file):
        # they leave the pending count, which must not read as progress.
        # Git commits that left the history window are not lost transcripts and are not counted here.
        "unavailable_unprocessed": sum(1 for row in sources.values() if not row["available"]
                                       and row["processed_hash"] != row["content_hash"]
                                       and row["metadata"].get("provider") != "git"),
        "failed_units": len(failed),
        "unaudited": unaudited(store, sources, stored_units),
        "since_scan": None, "took_ms": 0}
    return result, index, sources


def lag(store: Store) -> dict[str, Any]:
    """The part of `stored` a session start needs, without reading every stored record: when the last scan was,
    how many records no analysis processed (counted in SQL, those of units skipped for a failure of their own
    left out as in `stored`), and how many units were skipped. Same numbers as `stored`, a fraction of the time
    (60,000 records: about 0.05 s against 1.5 s)."""
    index = store.get_meta(INDEX_KEY)
    units = store.units(results=False)
    failures = store.unit_failures()
    rows = store.sources_by_id({i for entry in failures.values() for i in entry.get("sources") or []})
    failed = held_failures(store, rows, units)
    held = {i for entry in failed.values() for i in entry["sources"]}
    pending = store.pending_records() - sum(
        1 for i in held if rows[i]["available"] and rows[i]["processed_hash"] != rows[i]["content_hash"])
    return {"scanned_at": index.get("scanned_at") if index else None, "pending_records": pending,
            "failed_units": len(failed)}


def cached_since_scan(store: Store, scanned_at: str | None) -> dict[str, Any] | None:
    """Records written since the last scan, as an earlier `find`/`status` counted them; None when no count was
    kept for this scan. Nothing is parsed or measured here: the count is as old as the check that made it."""
    cache = store.get_meta(CACHE_KEY) or {}
    if not scanned_at or (cache.get("basis") or {}).get("scanned_at") != scanned_at or cache.get("not_read"):
        return None  # no count for this scan, or one short of a file the check could not read
    files = cache.get("files") or {}
    return {"records": sum(entry.get("records", 0) for entry in files.values()),
            "sessions": len({tuple(s) for entry in files.values() for s in entry.get("sessions", [])})}


def check(scope: Scope, store: Store, *, codex_home: Path | None = None, claude_home: Path | None = None,
          opencode_home: Path | None = None, budget_bytes: int | None = None, sources=None) -> dict[str, Any]:
    """The lag between the graph and the transcripts (of `sources` only, when given), as counts.

    `since_scan.parsed` says whether the changed files were parsed (then `sessions`, `records`,
    `newest_at` and `by_source` are exact) or only counted against the budget.
    """
    started = time.perf_counter()
    tools = sources  # the tools read; `sources` below is the stored records, as in `stored`
    result, index, sources = stored(store)
    if not index:
        result["took_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    codex_home, claude_home, opencode_home = source_homes(codex_home, claude_home, opencode_home)
    known = index.get("files", {})
    changed: list[tuple[Path, str]] = []
    stats: dict[str, dict[str, int]] = {}
    infos: dict[str, os.stat_result] = {}
    new_files, changed_bytes, newest_mtime = 0, 0, 0
    for path, provider in jsonl_files(codex_home, claude_home, sources=tools):
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
        infos[str(path)] = info
        changed_bytes += info.st_size
        newest_mtime = max(newest_mtime, info.st_mtime_ns)
    deleted = sum(1 for path in known if not Path(path).exists())
    databases_changed: list[tuple[Path, dict[str, int]]] = []
    database_bytes = 0
    for path in opencode_databases(opencode_home) if reads(tools, "opencode") else []:
        before = (index.get("opencode", {}).get(str(path)) or {}).get("sessions", {})
        times = session_times(path, scope)
        if times is None or any(before.get(session) != stamp for session, stamp in times.items()):
            databases_changed.append((path, times or {}))
            try:
                database_bytes += os.stat(path).st_size
            except OSError:
                pass
            if times:
                newest_mtime = max(newest_mtime, max(times.values()) * 1_000_000)
    since: dict[str, Any] = {"files_changed": len(changed), "files_new": new_files, "files_deleted": deleted,
                             "databases_changed": len(databases_changed), "bytes": changed_bytes + database_bytes,
                             "newest_change_at": _iso_ns(newest_mtime), "parsed": False}
    result["since_scan"] = since
    # Per-file results of an earlier check against the same scan and the same stored records (a "new" record is
    # one the store does not hold with that hash, so the cache is keyed on both); a file is re-read only when it
    # changed again, and a Claude Code transcript that only grew is read from where that check stopped. Counts made
    # by other reading code or under other scope roots (a worktree added or removed) are not those of this check
    # (`scan_cache_key`: the reading code and what attribution reads of the scope).
    basis = {"scanned_at": index.get("scanned_at"),
             "sources": digest(sorted((key, row["content_hash"]) for key, row in sources.items())),
             "journaled": sorted(store.journaled_sessions()), "reading": scan_cache_key(scope)}
    cache = store.get_meta(CACHE_KEY) or {}
    same = cache.get("basis") == basis
    earlier = cache.get("files", {})
    cached = earlier if same else {}
    seeds: dict[str, dict] | None = None
    starts: dict[str, tuple[dict, dict]] = {}  # file → (the state to read on from, the counts up to it)
    to_read = database_bytes  # what this check reads: the budget is on that
    for path, provider in changed:
        key, info = str(path), infos[str(path)]
        entry = cached.get(key)
        if entry and entry.get("bytes") == info.st_size and entry.get("mtime_ns") == info.st_mtime_ns:
            continue
        start = None
        if provider == "claude" and not key.endswith(COMPRESSED_SUFFIX):
            entry = earlier.get(key) or {}
            state = entry.get("resume")
            if state and resume_current(state) and (same or _holds_nothing(state)):
                start = (state, entry if same else {})
            else:
                seeds = _scan_seeds(store, scope) if seeds is None else seeds
                start = (seeds[key], {}) if key in seeds else None
        if start and (start[0].get("dev"), start[0].get("ino")) == (info.st_dev, info.st_ino) \
                and info.st_size >= start[0].get("end", info.st_size + 1):
            starts[key] = start
            to_read += info.st_size - start[0]["end"]
        else:
            to_read += info.st_size
    if budget_bytes is not None and to_read > budget_bytes:
        result["took_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    spent = to_read  # rises when a file charged only its appended bytes has to be read whole after all
    kept: dict[str, dict[str, Any]] = {}
    parsed_now = False

    journaled, noted_since, tails = store.journaled_sessions(), store.journal_since(), store.journal_tails()

    def tally(records) -> dict[str, Any]:
        count, newest, by_source, sessions = 0, None, {}, set()
        for record in records:
            row = sources.get(record.source_id)
            if row and row["content_hash"] == record.content_hash:
                continue
            # The agent writes these sessions itself (`contexttrail note`); analysis will not read them.
            if in_journal(record.session_id, record.lineage, record.recorded_at, journaled, noted_since, tails):
                continue
            count += 1
            sessions.add((record.provider, record.session_id))
            by_source[record.provider] = by_source.get(record.provider, 0) + 1
            if record.recorded_at and (newest is None or record.recorded_at > newest):
                newest = record.recorded_at
        return {"records": count, "newest": newest, "by_source": by_source, "sessions": sorted(sessions)}

    unread = 0
    for path, provider in changed:
        key = str(path)
        entry = cached.get(key)
        if not (entry and entry.get("bytes") == stats[key]["bytes"] and entry.get("mtime_ns") == stats[key]["mtime_ns"]):
            appended = read_claude_appended(path, scope, starts[key][0]) if key in starts else None
            if appended:
                snapshot, state = appended
                entry = {**stats[key], **_added(starts[key][1], tally(snapshot.records)), "resume": state}
                parsed_now = True
                kept[key] = entry
                continue
            if key in starts:
                # Its appended lines could not be read on their own: the file is read whole, of which the budget
                # was charged only the appended part. Past the budget it is not read and counts as not parsed.
                spent += starts[key][0]["end"]
                if budget_bytes is not None and spent > budget_bytes:
                    unread += 1
                    continue
            # On a byte budget a transcript too large to hold is not read through (its size on disk says nothing
            # of what it holds once decompressed); `status`, with no budget, reads it.
            snapshot = (parse_claude(path, scope, stream_large=budget_bytes is None, resumable=True) if provider == "claude"
                        else parse_codex(path, scope, stream_large=budget_bytes is None))
            if any(note.startswith("log file not read") for note in snapshot.limitations):
                # Not read (too large to hold on a budget, or unreadable): not parsed, and not kept as 0 records,
                # so a later check without a budget (`status`) reads it.
                unread += 1
                continue
            entry = {**stats[key], **tally(snapshot.records)}
            state = next((manifest["resume"] for manifest in snapshot.files if manifest.get("resume")), None)
            if state:
                entry["resume"] = state
            parsed_now = True
        kept[key] = entry
    for path, times in databases_changed:
        key = str(path)
        entry = cached.get(key)
        if not (entry and entry.get("sessions_at") == times):
            entry = {"sessions_at": times, **tally(parse_opencode(path, scope).records)}
            parsed_now = True
        kept[key] = entry
    if parsed_now or unread:
        try:
            store.set_meta(CACHE_KEY, {"basis": basis, "files": kept, "not_read": unread})
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
    # A changed file that could not be read leaves the count short: not parsed, so no reader takes it as exact.
    since.update(parsed=not unread, not_read=unread, sessions=len(sessions), records=count, newest_at=newest,
                 by_source=by_source)
    result["took_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _holds_nothing(state: dict[str, Any]) -> bool:
    """A reading state of a file whose rows add no record whatever the store holds (a sub-agent's file read with no
    parent link, or one none of whose rows was the project's): its count, 0, does not depend on the stored records."""
    return bool(state.get("subagent")) or not state.get("relevant")


def _added(counts: dict[str, Any], more: dict[str, Any]) -> dict[str, Any]:
    """The counts of a file up to where a reading stopped, with those of the lines appended since."""
    by_source = dict(counts.get("by_source") or {})
    for provider, n in more["by_source"].items():
        by_source[provider] = by_source.get(provider, 0) + n
    newest = max((value for value in (counts.get("newest"), more["newest"]) if value), default=None)
    sessions = {tuple(s) for s in counts.get("sessions", [])} | {tuple(s) for s in more["sessions"]}
    return {"records": counts.get("records", 0) + more["records"], "newest": newest, "by_source": by_source,
            "sessions": sorted(sessions, key=lambda s: tuple(str(part) for part in s))}


def _scan_seeds(store: Store, scope: Scope) -> dict[str, dict[str, Any]]:
    """A state to read on from for each plain Claude Code transcript the last scan read to its end and found nothing
    of the project in (its `ScanCache` entry, made by the same reading code and scope roots: `scan_cache_key`):
    none of its rows was the project's, so its lines appended since are all a check needs to read. Every path the
    scan asked about is asked again before the state is used (`read_claude_appended`)."""
    saved = store.get_meta(SCAN_CACHE_META) or {}
    proven = [Path(p) for p in store.get_meta("known_worktree_roots", []) or []]
    try:
        key = scan_cache_key(scope.with_roots(proven))
    except Exception:
        return {}
    if saved.get("key") != key:
        return {}
    seeds: dict[str, dict[str, Any]] = {}
    for path, entry in (saved.get("files") or {}).items():
        manifests = entry.get("files") or []
        if path.endswith(COMPRESSED_SUFFIX) or len(manifests) != 1:
            continue
        manifest = manifests[0]
        if manifest.get("partial") or not all(k in manifest for k in ("dev", "ino", "lines", "head", "tail", "complete_bytes")):
            continue
        seeds[path] = {"v": RESUME_VERSION, "code": reading_code(), "end": manifest["complete_bytes"],
                       "lines": manifest["lines"], "head": manifest["head"], "tail": manifest["tail"], "dev": manifest["dev"], "ino": manifest["ino"],
                       "streamed": bool(manifest.get("streamed")), "rows": 0, "session": None, "entry": None,
                       "subagent": False, "cwd": None, "relevant": False, "calls": [], "cwds": [], "authorings": [],
                       "paths": {asked: None for asked in entry.get("paths", [])}}
    return seeds


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
    journaled, noted_since, tails = store.journaled_sessions(), store.journal_since(), store.journal_tails()
    if not journaled:
        return {"sessions": 0, "records": 0}
    covered = {i for unit in units if unit["status"] == "integrated" for i in unit["sources"]}
    records, sessions = 0, set()
    for source_id, row in sources.items():
        meta = row["metadata"]
        if not row["available"] or meta.get("provider") == "git" or source_id in covered:
            continue
        if in_journal(meta.get("session_id"), meta.get("lineage"), meta.get("recorded_at"), journaled, noted_since, tails):
            records += 1
            sessions.add((meta.get("provider"), meta.get("session_id")))
    return {"sessions": len(sessions), "records": records}


_SCANNED_AT: dict[str, tuple[float, str | None]] = {}
SCANNED_AT_SECONDS = 20


def scanned_at(store: Store) -> str | None:
    """When the project was last scanned, read at most every `SCANNED_AT_SECONDS` per state: a screen keys its
    freshness phrase on it with the graph version, so a scan run elsewhere (another terminal, a hook) is seen
    without reading the index at every redraw."""
    key = str(store.directory)
    at, value = _SCANNED_AT.get(key, (0.0, None))
    if time.monotonic() - at > SCANNED_AT_SECONDS:
        try:
            value = (store.get_meta(INDEX_KEY) or {}).get("scanned_at")
        except Exception:
            pass
        _SCANNED_AT[key] = (time.monotonic(), value)
    return value


def saved_homes(store: Store) -> dict[str, Path]:
    """The source directories a check reads: the project's saved ones, else the ones the last scan read (a scan's
    flags are not saved, but its index is what the check compares with), else the defaults (left out here)."""
    options = store.get_meta("options", {}) or {}
    scanned = (store.get_meta(INDEX_KEY) or {}).get("homes") or {}
    return {key: Path(options.get(key) or scanned[key.removesuffix("_home")])
            for key in ("codex_home", "claude_home", "opencode_home")
            if options.get(key) or scanned.get(key.removesuffix("_home"))}


def saved_sources(store: Store) -> tuple[str, ...] | None:
    """The tools a check reads: the project's saved `--sources` (only `analyze` saves it), else all (None). A scan's
    one-off `--sources` is not taken from its index: the index keeps the other tools' entries instead."""
    value = (store.get_meta("options", {}) or {}).get("sources")
    return tuple(value) if value else None


def check_from_store(scope: Scope, store: Store, *, budget_bytes: int | None = FIND_BUDGET_BYTES,
                     sources=None) -> dict[str, Any] | None:
    """The check with `saved_homes` and `sources` (else `saved_sources`); None when the state cannot be read."""
    try:
        return check(scope, store, budget_bytes=budget_bytes, sources=sources or saved_sources(store),
                     **saved_homes(store))
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
    pending = result["pending"]
    if not scanned:
        if pending["records"]:
            # A state an older version wrote has records but no index of the files: it was scanned, not indexed.
            lines.append(tr(f"기록 파일 색인이 아직 없습니다 · 저장된 미분석 기록 {pending['records']}개 · "
                            "색인 만들기: contexttrail scan . (AI 호출 없음)",
                            f"No index of the transcript files yet · {pending['records']} stored records not analyzed · "
                            "to index them: contexttrail scan . (no AI calls)"))
        else:
            lines.append(tr("기록을 아직 scan하지 않았습니다: contexttrail scan . 또는 contexttrail analyze .",
                            "Transcripts were never scanned: contexttrail scan . or contexttrail analyze ."))
        return lines
    lines.append(tr(f"마지막 scan {_local(scanned)} ({ago(scanned)}) · 그때 기준 미분석 기록 {pending['records']}개"
                    + (f", 작업 단위 {pending['units']}개 대기" if pending["units"] else ""),
                    f"Last scan {_local(scanned)} ({ago(scanned)}) · {pending['records']} records not analyzed as of then"
                    + (f", {pending['units']} work units waiting" if pending["units"] else "")))
    if result.get("unavailable_unprocessed"):
        lost = result["unavailable_unprocessed"]
        lines.append(tr(f"분석 전에 더는 읽히지 않는 기록 {lost}개: 미분석 수에서 빠졌습니다(파일이 옮겨졌거나 읽지 못함). "
                        "scan의 limitations가 파일을 적습니다.",
                        f"{lost} records not analyzed are no longer read: they left the count above (a file moved or "
                        "could not be read). scan's limitations name the files."))
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
