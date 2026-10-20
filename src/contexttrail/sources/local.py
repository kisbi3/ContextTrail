from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import git_context, util
from ..git_context import Scope
from ..i18n import tr
from ..model import Snapshot, SourceRecord, segment_record
from ..util import FlowError, digest, ident, within
from .opencode import opencode_data_dir, opencode_databases, parse_opencode

# A transcript over this size is not read (it is held in memory twice while parsed). A day of Claude Code work
# with large tool outputs passed 64 MiB, which silently dropped the session, so the limit is 256 MiB.
MAX_FILE_BYTES = 256 * 1024 * 1024
REREAD_CHUNK = 4 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
# A Codex transcript too large to hold (over `MAX_FILE_BYTES` on disk, or once decompressed: one 35 MB file held
# 281 MB) is read through line by line instead, up to this many bytes, so a big session of another project is
# still attributed and left out instead of being a file every project reports it could not read.
STREAM_MAX_BYTES = 4 * 1024 * 1024 * 1024
STREAM_CHUNK = 1024 * 1024
# Compressed input fed at a time. zstd unpacks at most about 32,000 to 1 (a run of one byte), so a piece this small
# unpacks to at most about 128 MiB, whatever the file: what bounds memory in a file that unpacks to far more.
ZSTD_INPUT_CHUNK = 4 * 1024
# A streamed transcript names its session in its first rows; the in-memory reader looks through the whole file.
SESSION_META_ROWS = 64
# Keep a single prompt-visible record below Engine.record_chars (48k by default).
SEGMENT_CHARS = 32_000

# Record types that carry no decision narrative, so dropping them loses nothing.
# Keyed by the literal string the parser would otherwise add to `unknown`.
# Adding an entry here is a claim: "this cannot change a reconstructed goal,
# attempt, failure, or decision". tests/test_source_coverage.py enforces the
# other half — any record type that is neither parsed nor listed here fails CI.
IGNORED_NO_ANALYSIS_VALUE = {
    # Billing only. ContextTrail tracks analysis cost in its own LLM call ledger
    # (store.llm_calls), not from transcript usage records.
    "token_usage_record": "billing data — analysis cost is tallied separately in the local call ledger",
    # Session configuration (model, provider, approval policy, cwd). Describes
    # how the coding agent was configured, not what it decided or why.
    "event_msg:thread_settings_applied": "session settings — no part in the decision narrative",
    # Claude telemetry and session metadata. Counts verified against real logs.
    "system:turn_duration": "turn duration and message count — a performance metric",
    "system:away_summary": "away summary — unrelated to decisions",
    # `system:compact_boundary` is deliberately absent: it becomes a record
    # below and marks a work-unit boundary, so it is not "unread".
    "file-history-snapshot": "file backup snapshot list — no narrative",
    # The change-only form of the snapshot above; the edits themselves are the Edit/Write tool calls.
    "file-history-delta": "file backup change list — no narrative; the edits are in the tool calls",
    "last-prompt": "current leaf UUID pointer — no content",
    "custom-title": "session title given by the person",
    "ai-title": "session title given by Claude — event titles are written anew in the extract stage",
    "atis-latch": "empty state bit",
    "mode": "session mode (normal etc.)",
    "permission-mode": "session permission mode (auto etc.)",
    "agent-name": "session display name",
    # Carries ownerAccountUuid / ownerOrganizationId. Ignored, and deliberately
    # never forwarded: this is an account identifier, not project history.
    "bridge-session": "holds account identifiers (owner account and organization IDs) — not analysis input",
    "bridge-config": "bridge settings",
    "pr-link": "PR link metadata",
    "progress": "progress indicator",
    "tag": "tag",
    # A session-start hook's bookkeeping (Claude Code 2.1.29x): the hook ran (with its JSON answer as stdout), and
    # the context it added. With `contexttrail context --hook` that context is ContextTrail's own saved graph.
    "attachment:hook_success": "a hook ran successfully — its answer, not project work",
    "attachment:hook_additional_context": "context a session-start hook added (ContextTrail's own memory) — not project work",
    # A Stop hook that sent the agent back; the same text is also a `Stop hook feedback: …` user record, which is read.
    "attachment:hook_blocking_error": "a Stop hook's block — its text is read from the 'Stop hook feedback' user message",
}

# Record types that may hold decision-relevant context but are not parsed yet.
# These are surfaced once per scan so a gap is visible instead of silent.
KNOWN_UNPARSED = {
    # `world_state` carries `agents_md.text`, i.e. a verbatim copy of the
    # project instructions the coding agent was operating under. "Why was it
    # done this way" frequently answers "because AGENTS.md said so", so this is
    # analysis-relevant. It is not parsed because it duplicates the repository
    # file and re-sends it on every turn; deciding how to model that is open.
    "world_state": "holds the full project instructions (AGENTS.md etc.) but is not sent to analysis yet",
    # 473 enqueue records across 22 of 40 real Claude files, each carrying the
    # full text of a prompt the person queued. Counted against the `user` records
    # (docs/reports/SESSION_2026-09-28.md 4.3): 338 are also a `user` record, 110 are
    # synthetic blocks Claude wrote (task-notification and the like), and 25 are something the
    # person typed that appears nowhere else, so those requests are invisible to the graph.
    "queue-operation": "holds the full text of a request the person queued but is not sent to analysis yet",
    # 91 records in real logs. A stop hook that ran checks is direct evidence of
    # verification, and a failing one is direct evidence of a failed attempt.
    "system:stop_hook_summary": "stop hook result summary — could ground a check or a failure, but is not sent to analysis yet",
    # 14 records in real logs; records which slash command a person invoked,
    # which is part of intent.
    "system:local_command": "slash command the person ran — part of the intent, but not sent to analysis yet",
}


def _classify_unknown(record_type: str, unknown: set[str], deferred: dict[str, int]) -> None:
    """Route a record type we do not parse into one of three explicit buckets.

    Without this, "the model emitted a billing field we do not use" and "the
    model emitted a field we cannot read" produce the same warning, so a real
    upstream format change hides inside routine noise.
    """
    if record_type in IGNORED_NO_ANALYSIS_VALUE:
        return
    if record_type in KNOWN_UNPARSED:
        deferred[record_type] = deferred.get(record_type, 0) + 1
        return
    unknown.add(record_type)


def _flush_deferred(deferred: dict[str, int], warnings: list[str], label: str, path: Path) -> None:
    """Report unparsed-but-maybe-relevant types once per file, with a count.

    The message carries a path that distinguishes files, not just the basename:
    `collect_logs` deduplicates identical warning strings across files, so two
    same-named files in different session directories would otherwise collapse
    into a single warning and one file's gap would go unreported.
    """
    for record_type, count in sorted(deferred.items()):
        warnings.append(f"{label} unparsed record type {record_type}, {count} records not sent to analysis ({_warning_path(path)}): "
                        f"{KNOWN_UNPARSED[record_type]}")


def _warning_path(path: Path) -> str:
    """A label that is unique per file, so dedup cannot merge two files.

    Session directories nest by date, so the immediate parent is not enough:
    `sessions/2026/01/01/rollout.jsonl` and `sessions/2026/02/01/rollout.jsonl`
    share both basename and parent name. A short digest of the full path keeps
    the message short while staying unique.
    """
    label = path.name
    if path.parent.name:
        label = f"{path.parent.name}/{label}"
    if path.parent.parent.name and path.parent.parent.name not in ("", ".", ".."):
        label = f"{path.parent.parent.name}/{label}"
    return f"{label}#{digest(str(path))[:8]}"


# Codex compresses a session's transcript once it is about a week old (`rollout-….jsonl.zst`, Codex CLI 0.163).
COMPRESSED_SUFFIX = ".jsonl.zst"


def transcript_stem(path: Path) -> str:
    """The transcript's name without `.jsonl` or `.jsonl.zst`: the same session whether Codex compressed it or not."""
    return path.name.removesuffix(".zst").removesuffix(".jsonl")


class TooLarge(FlowError):
    """A transcript larger than the reader holds whole."""


def _feed(stream, data: bytes) -> None:
    try:
        stream.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _zstd_chunks(source: io.BufferedIOBase | io.BytesIO) -> Iterator[bytes]:
    """The content of a zstd-compressed stream (in memory, or a file opened for it, which it closes) in pieces of
    up to `STREAM_CHUNK`, every frame of it: with the standard library's `compression.zstd` (Python 3.14), the
    `zstandard` package, or the `zstd` command, whichever is here. Every failure is a FlowError naming why."""
    try:
        with source:
            try:
                from compression import zstd  # type: ignore[import-not-found]
            except ImportError:
                zstd = None
            if zstd is not None:
                with zstd.ZstdFile(source) as stream:
                    while piece := stream.read(STREAM_CHUNK):
                        yield piece
                return
            try:
                import zstandard  # type: ignore[import-not-found]
            except ImportError:
                zstandard = None
            if zstandard is not None:
                # Frame by frame, every one of them, as `compression.zstd` and the `zstd` command read them; a file
                # that ends inside a frame is refused as they refuse it (its stream reader returns what it has, and
                # nothing for a cut first frame). Small pieces of input keep each piece of output small.
                context = zstandard.ZstdDecompressor()
                frame, fed, ready = context.decompressobj(), False, bytearray()
                while chunk := source.read(ZSTD_INPUT_CHUNK):
                    while chunk:
                        fed = True
                        ready += frame.decompress(chunk)
                        if not frame.eof:
                            break
                        chunk, frame, fed = frame.unused_data, context.decompressobj(), False
                    if len(ready) >= STREAM_CHUNK:  # handed on in pieces of about `STREAM_CHUNK`
                        yield bytes(ready)
                        ready = bytearray()
                if fed and not frame.eof:
                    raise FlowError("zstd could not decompress it (the file ends inside a frame)")
                if ready:
                    yield bytes(ready)
                return
            command = shutil.which("zstd")
            if command is None:
                raise FlowError("zstd-compressed, and neither the zstandard package nor the zstd command is installed")
            in_memory = isinstance(source, io.BytesIO)
            process = subprocess.Popen([command, "-dcq"], stdin=subprocess.PIPE if in_memory else source.fileno(),
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            feeder = None
            if in_memory:
                feeder = threading.Thread(target=_feed, args=(process.stdin, source.getvalue()), daemon=True)
                feeder.start()
            try:
                while piece := process.stdout.read(STREAM_CHUNK):
                    yield piece
                if process.wait(timeout=120) != 0:
                    raise FlowError("zstd could not decompress it")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                if feeder is not None:
                    feeder.join(timeout=5)
    except FlowError:
        raise
    except Exception as exc:  # a corrupt frame, whichever decompressor met it
        raise FlowError(f"zstd could not decompress it ({type(exc).__name__})") from None


def decompress_zstd(data: bytes, limit: int = 0) -> bytes:
    """A zstd file's content (`_zstd_chunks`). More than `limit` bytes (when given) is refused (`TooLarge`), so a
    small file cannot unpack into a huge one in memory. Every failure is a FlowError naming why."""
    out = bytearray()
    chunks = _zstd_chunks(io.BytesIO(data))
    try:
        for piece in chunks:
            out += piece
            if limit and len(out) > limit:
                raise TooLarge(f"over the size limit of {limit} bytes once decompressed")
    finally:
        chunks.close()
    return bytes(out)


def _parsed(raw: bytes, line_no: int, name: str, warnings: list[str],
            only: tuple[bytes, ...] = ()) -> tuple[int, dict, str] | None:
    """One JSONL line as (line number, object, digest), or None with the warning that says why not. With `only`, a
    line holding none of those bytes is passed over unparsed (a reader that needs only some rows)."""
    if not raw.strip() or (only and not any(mark in raw for mark in only)):
        return None
    if len(raw) > MAX_LINE_BYTES:
        warnings.append(f"JSONL record over the size limit: {name}:{line_no}")
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return line_no, value, digest(raw)
    except (UnicodeError, ValueError):
        warnings.append(f"corrupt JSONL record: {name}:{line_no}")
        return None


def _file_chunks(descriptor: int, size: int) -> Iterator[bytes]:
    """The first `size` bytes of an open file (a descriptor it closes) in pieces, as it was when its size was taken;
    a file that shrank while being read is a FlowError."""
    with os.fdopen(descriptor, "rb") as stream:
        left = size
        while left > 0:
            piece = stream.read(min(STREAM_CHUNK, left))
            if not piece:
                raise FlowError("rewritten or truncated while being read")
            left -= len(piece)
            yield piece
        if os.fstat(stream.fileno()).st_size < size:
            raise FlowError("rewritten or truncated while being read")


def _streamed_rows(path: Path, chunks: Iterator[bytes], warnings: list[str], manifest: dict,
                   compressed: bool, only: tuple[bytes, ...] = ()) -> Iterator[tuple[int, dict, str]]:
    """The rows of a transcript too large to hold, read through in pieces: the same line numbers, warnings and
    digest as `_jsonl` gives a file it holds whole, with the manifest completed once the last piece is read. At most
    `STREAM_MAX_BYTES` are read; a file larger than that, or one that fails part-way, keeps the rows before it and
    says so in the warnings."""
    committed, pending = hashlib.sha256(), hashlib.sha256()
    buffer, skipping, line_no, total, consumed = bytearray(), False, 0, 0, 0
    try:
        for chunk in chunks:
            total += len(chunk)
            if total > STREAM_MAX_BYTES:
                # The same every time the file is read: kept by `ScanCache`, unlike a failure part-way.
                manifest["partial"] = "cap"
                warnings.append(f"log file read only in part: {path.name} (over {STREAM_MAX_BYTES} bytes)")
                return
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    piece = chunk[start:]
                    pending.update(piece)
                    if not skipping:
                        buffer += piece
                        if len(buffer) > MAX_LINE_BYTES:
                            skipping, buffer = True, bytearray()
                    break
                pending.update(chunk[start:newline + 1])
                committed = pending.copy()
                consumed = total - len(chunk) + newline + 1  # bytes through this newline
                line_no += 1
                if skipping:
                    warnings.append(f"JSONL record over the size limit: {path.name}:{line_no}")
                    skipping = False
                else:
                    raw = bytes(buffer) + chunk[start:newline]
                    buffer = bytearray()
                    row = _parsed(raw[:-1] if raw.endswith(b"\r") else raw, line_no, path.name, warnings, only)
                    if row is not None:
                        yield row
                start = newline + 1
        if buffer or skipping:
            warnings.append(f"last JSONL record still being written, skipped: {path.name}")
    except (FlowError, OSError) as exc:
        manifest["partial"] = "error"
        warnings.append(f"log file read only in part: {path.name} ({exc if isinstance(exc, FlowError) else type(exc).__name__})")
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
        # A compressed file, or one read to the cap (settled: the same every time), is indexed by its size on disk.
        settled = compressed or manifest.get("partial") == "cap"
        manifest.update(complete_bytes=manifest["bytes"] if settled else consumed, digest=committed.hexdigest(),
                        streamed=True)


def _jsonl(path: Path, *, stream_large: bool = False,
           only: tuple[bytes, ...] = ()) -> tuple[Iterable[tuple[int, dict, str]], list[str], dict]:
    """Freeze the initial byte length. Only newline-terminated records are committed. A zstd-compressed transcript
    (`COMPRESSED_SUFFIX`) is read the same way and then decompressed; its records are the ones it held uncompressed
    (a record's ID and hash do not depend on the file's path). With `stream_large`, a transcript too large to hold
    (on disk, or once decompressed) is not refused: its rows come as an iterator (`_streamed_rows`) that fills the
    warnings and the manifest while it is read (`partial` when it was cut short)."""
    rows, warnings = [], []
    compressed = path.name.endswith(COMPRESSED_SUFFIX)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_FILE_BYTES:
                if stream_large:
                    # The file opened here, not one opened again by its path.
                    copy = os.dup(stream.fileno())
                    manifest = {"path": str(path), "bytes": before.st_size}
                    chunks = (_zstd_chunks(os.fdopen(copy, "rb")) if compressed else _file_chunks(copy, before.st_size))
                    return _streamed_rows(path, chunks, warnings, manifest, compressed, only), warnings, manifest
                # English: the reason lands in the limitations the model reads.
                raise FlowError(f"over the size limit of {MAX_FILE_BYTES} bytes: {before.st_size} bytes")
            data = stream.read(before.st_size)
            after = os.fstat(stream.fileno())
            stream.seek(0)
            # Read again in pieces, so a large file is held in memory once, not twice.
            view, offset = memoryview(data), 0
            while offset < len(data):
                piece = stream.read(min(REREAD_CHUNK, len(data) - offset))
                if not piece or view[offset:offset + len(piece)] != piece:
                    raise FlowError("rewritten or truncated while being read")
                offset += len(piece)
            if after.st_size < before.st_size or len(data) < before.st_size:
                raise FlowError("rewritten or truncated while being read")
        if compressed:
            try:
                data = decompress_zstd(data, MAX_FILE_BYTES)
            except TooLarge:
                if not stream_large:
                    raise
                manifest = {"path": str(path), "bytes": before.st_size}
                return (_streamed_rows(path, _zstd_chunks(io.BytesIO(data)), warnings, manifest, True, only),
                        warnings, manifest)
    except (OSError, FlowError) as exc:
        # The reason stays in the limitation: a file over the size limit used to read as a bare "FlowError".
        return [], [f"log file not read: {path.name} ({exc if isinstance(exc, FlowError) else type(exc).__name__})"], {}
    boundary = data.rfind(b"\n") + 1
    if boundary != len(data):
        warnings.append(f"last JSONL record still being written, skipped: {path.name}")
    for line_no, raw in enumerate(data[:boundary].splitlines(), 1):
        row = _parsed(raw, line_no, path.name, warnings, only)
        if row is not None:
            rows.append(row)
    # `complete_bytes` is the length of the file that was read, which the freshness index compares with the file's
    # size: a compressed file is read whole, so it is the size on disk, not the decompressed length.
    return rows, warnings, {"path": str(path), "bytes": before.st_size,
                            "complete_bytes": before.st_size if compressed else boundary, "digest": digest(data[:boundary])}


def _binary_marker(block: dict[str, Any]) -> str | None:
    """Represent inline binary/image payloads without feeding base64 to the LLM."""
    kind = str(block.get("type") or "binary")
    url = block.get("image_url")
    url = url.get("url") if isinstance(url, dict) else url
    if isinstance(url, str) and url.startswith("data:") and len(url) >= 256:
        # Codex keeps pasted images as data URLs in input_image blocks.
        media = url[5:].split(";", 1)[0].split(",", 1)[0] or "unknown"
        return f"[{kind} payload omitted from text analysis: media_type={media}, encoded_chars={len(url)}, sha256={digest(url)}]"
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    data = source.get("data")
    if not isinstance(data, str) or len(data) < 256:
        return None
    media = str(source.get("media_type") or source.get("type") or "unknown")
    # Raw JSON remains pinned by locator.raw_hash; prompt text stores only a digest/size marker.
    return f"[{kind} payload omitted from text analysis: media_type={media}, encoded_chars={len(data)}, sha256={digest(data)}]"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(x) for x in content if not isinstance(x, dict)
                         or x.get("type") not in {"thinking", "redacted_thinking", "reasoning"})
    if isinstance(content, dict):
        marker = _binary_marker(content)
        if marker:
            return marker
        for key in ("text", "content", "output"):
            if key in content:
                return _text(content[key])
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content) if content is not None else ""


def _record(provider: str, session: str, key: str, role: str, content: str, path: Path,
            line: int, raw_hash: str, cwd: str | None, scope: Scope, **kwargs: Any) -> SourceRecord:
    return SourceRecord(ident("src_", provider, session, key), provider, session, role,
        content.replace("\r\n", "\n"), {"kind": "jsonl", "path": str(path), "line": line, "raw_hash": raw_hash},
        cwd=cwd, worktree_id=scope.worktree_for(cwd), **kwargs)


def _authoring(model: Any, effort: Any, provider: Any = None) -> dict[str, str]:
    """Model, effort and provider as the log names them; a missing or non-text value is left out, never guessed."""
    return {key: value for key, value in (("model", model), ("effort", effort), ("provider", provider))
            if isinstance(value, str) and value}


def _codex_authoring(records: list[SourceRecord], turns: list[tuple[int, dict]]) -> None:
    """Each record gets the model and effort of its turn: the latest `turn_context` before it. Codex writes
    that context before the person's prompt of the turn and the agent's work (checked on real sessions), so
    only what comes before the first context, the injected instructions, stays empty."""
    for record in records:
        line = record.locator.get("line", 0)
        record.authoring = dict(next((authoring for at, authoring in reversed(turns) if at < line), {}))


def _codex_queued(records: list[SourceRecord], triggers: list[tuple[int, Any]]) -> None:
    """A user-role message of a turn Codex started from its queue (`task_started` `turn_attribution.turn_trigger:
    "queue"`, what `codex queue --thread … --message …` sends: a script, a monitor the agent set up) is marked
    `authoring.trigger = "queue"`, so it is not read as the person's message (`model.is_user_prompt`). A message the
    person types in the app starts a turn of `composer` or `user`."""
    for record in records:
        line = record.locator.get("line", 0)
        trigger = next((value for at, value in reversed(triggers) if at < line), None)
        if record.role == "user" and trigger == "queue":
            record.authoring = {**record.authoring, "trigger": "queue"}


def _segment(record: SourceRecord, max_chars: int = SEGMENT_CHARS) -> list[SourceRecord]:
    """Stable, evidence-citable fragments for long textual records.

    Fragments preserve the execution identity in lineage.fragment_of; they are context
    pieces, not separate tool executions/messages.
    """
    return segment_record(record, max_chars)


def _segments(records: list[SourceRecord]) -> list[SourceRecord]:
    return [part for record in records for part in _segment(record)]


def _codex_lineage(metadata: dict[str, Any]) -> dict[str, Any]:
    source = metadata.get("source")
    if not isinstance(source, dict):
        return {}
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return {}
    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, dict) or not spawn.get("parent_thread_id"):
        return {}
    return {"kind": "subagent", "parent_session_id": spawn.get("parent_thread_id"),
            "depth": spawn.get("depth"), "agent_nickname": spawn.get("agent_nickname"),
            "agent_role": spawn.get("agent_role"), "agent_path": spawn.get("agent_path")}


def _render_replacement_history(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    rendered = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        typ = item.get("type", "unknown")
        role = item.get("role")
        body = _text(item.get("content") if "content" in item else item)
        if body:
            rendered.append(f"[{index}] {typ}/{role or '-'}\n{body}")
    return "\n\n".join(rendered)


def _head(rows: Iterable[tuple[int, dict, str]]) -> tuple[list[tuple[int, dict, str]], Iterable[tuple[int, dict, str]]]:
    """The rows a parser looks through before its pass (all of a file held whole; the first `SESSION_META_ROWS` of
    a streamed one, kept to be read again) and the rows to read."""
    if isinstance(rows, list):
        return rows, rows
    iterator = iter(rows)
    head = list(itertools.islice(iterator, SESSION_META_ROWS))
    return head, itertools.chain(head, iterator)


def _session_meta(rows: Iterable[tuple[int, dict, str]]) -> tuple[dict, Iterable[tuple[int, dict, str]]]:
    """The session's `session_meta` payload and the rows to read: from anywhere in a file held whole; from the
    first `SESSION_META_ROWS` rows of a streamed one, which are kept to be read again."""
    if isinstance(rows, list):
        return next((d.get("payload", {}) for _, d, _ in rows if d.get("type") == "session_meta"), {}), rows
    iterator, head = iter(rows), []
    for row in iterator:
        head.append(row)
        if row[1].get("type") == "session_meta":
            return row[1].get("payload", {}), itertools.chain(head, iterator)
        if len(head) >= SESSION_META_ROWS:
            break
    return {}, itertools.chain(head, iterator)


def parse_codex(path: Path, scope: Scope, *, stream_large: bool = True) -> Snapshot:
    """A Codex transcript's in-scope records. With `stream_large` (the default; off for a check on a byte budget,
    `freshness.check`) a transcript too large to hold is read through line by line (`_streamed_rows`)."""
    rows, warnings, manifest = _jsonl(path, stream_large=stream_large)
    decisions = {"selected": 0, "outside_scope": 0, "unattributed": 0}
    def selected(candidate: str | None) -> bool:
        if not isinstance(candidate, str) or not Path(candidate).is_absolute():
            decisions["unattributed"] += 1
            return False
        accepted = scope.includes(candidate)
        decisions["selected" if accepted else "outside_scope"] += 1
        return accepted
    def finish(records: list[SourceRecord], notes: list[str]) -> Snapshot:
        audit = {"provider": "codex", "path": str(path), "session_id": session,
                 "session_cwd": metadata.get("cwd"), "normalized_records": len(records),
                 "scope_decisions": decisions, "status": "included" if records else
                    ("unattributed" if decisions["unattributed"] else "excluded")}
        if metadata.get("forked_from_id"):
            audit["forked_from"] = metadata["forked_from_id"]
            audit["inherited_rows_skipped"] = skipped
        return Snapshot(records, notes, [{**manifest, "selection": audit}] if manifest else [])
    metadata, rows = _session_meta(rows)
    session = metadata.get("id") or transcript_stem(path)
    cwd = metadata.get("cwd")
    cwd = cwd if isinstance(cwd, str) and Path(cwd).is_absolute() else None
    lineage = _codex_lineage(metadata)
    if not lineage and (metadata.get("originator") == "codex_exec" or metadata.get("source") == "exec"):
        # `codex exec`: no person at the terminal. What sits in the user role came from whoever ran it
        # (a script, another agent); `_link_called_tools` makes it that caller's sub-agent when its ID is found.
        lineage = {"kind": "unattended", "agent_type": "codex exec"}
    # A forked sub-agent starts with a copy of its parent's history; its own history begins at
    # this rollout item. The copy is the parent's session, read (and analysed) there, not here.
    # A fork the person made refers to the parent by `history_base` and copies nothing.
    inherited = metadata.get("subagent_history_start_ordinal") if metadata.get("forked_from_id") else None
    inherited = inherited if isinstance(inherited, int) and inherited > 0 else 0
    skipped, seen_items = 0, False
    unknown: set[str] = set()
    deferred: dict[str, int] = {}
    if not metadata.get("id"):
        warnings.append(f"Codex native session ID missing; identified by file name: {path.name}")
    if metadata.get("source") in ("contexttrail", "projectflow") or (cwd and Path(cwd).name.startswith(("contexttrail-run-", "projectflow-run-"))):
        decisions["self_generated"] = 1
        return finish([], [])
    result, call_cwd, unknown, relevant = [], {}, set(), scope.includes(cwd)
    context_git = metadata.get("git", {}) if isinstance(metadata.get("git"), dict) else {}
    turns: list[tuple[int, dict]] = []  # (line, model and effort) of each turn_context, for _codex_authoring
    triggers: list[tuple[int, Any]] = []  # (line, turn_trigger) of each task_started, for _codex_queued
    # Counted in the one pass, so a streamed file is never read twice: how many rows it had, and whether any is a
    # message of the app's UI format (event_msg user_message/agent_message).
    row_count, ui_messages = 0, False
    for line, data, raw_hash in rows:
        row_count += 1
        record_type, payload = data.get("type"), data.get("payload", {})
        if not isinstance(payload, dict):
            unknown.add(str(record_type))
            continue
        if record_type == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
            ui_messages = True
        if line <= inherited and record_type not in {"turn_context", "session_meta"}:
            skipped += 1  # rollout items are numbered from 0; file lines from 1
            continue
        if record_type == "turn_context":
            turns.append((line, _authoring(payload.get("model"), payload.get("effort"), metadata.get("model_provider"))))
            if "cwd" in payload:
                value = payload["cwd"]
                cwd = value if isinstance(value, str) and Path(value).is_absolute() else None
            context_git = {k: payload[k] for k in ("branch", "commit", "git_branch") if k in payload} or context_git
            relevant |= scope.includes(cwd)
            continue
        if record_type == "session_meta":
            continue
        if record_type == "event_msg":
            event_type = payload.get("type")
            if event_type == "task_started":
                attribution = payload.get("turn_attribution")
                triggers.append((line, attribution.get("turn_trigger") if isinstance(attribution, dict) else None))
            if event_type == "thread_goal_updated":
                # Recognised, so it must never fall through to the classifier.
                # An out-of-scope goal is a scope decision, not a parse gap.
                if selected(cwd):
                    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
                    objective = goal.get("objective")
                    if objective:
                        result.append(_record("codex", session, f"line:{line}:thread-goal", "metadata",
                            f"Thread goal: {objective}\nStatus: {goal.get('status', 'unknown')}", path, line, raw_hash, cwd, scope,
                            recorded_at=data.get("timestamp"), lineage={**lineage, "kind": "thread_goal"}, git=context_git))
            elif event_type not in {"user_message", "agent_message", "agent_reasoning", "token_count",
                    "task_started", "task_complete", "turn_aborted", "context_compacted", "item_completed",
                    "item_started", "exec_command_begin", "exec_command_end"}:
                _classify_unknown("event_msg:" + str(event_type), unknown, deferred)
            continue
        if record_type in {"compacted", "compact"}:
            if not selected(cwd):
                continue
            text = _text(payload.get("message") or payload.get("summary"))
            # Mid-file, the replacement history repeats items already read above in this file.
            history = "" if seen_items else _render_replacement_history(payload.get("replacement_history"))
            if history:
                text = ((text + "\n\n") if text else "") + "[Codex compaction replacement_history]\n" + history
            if text:
                result.append(_record("codex", session, f"line:{line}", "metadata", text, path, line, raw_hash,
                                      cwd, scope, recorded_at=data.get("timestamp"), derivation="summary",
                                      lineage={**lineage, "kind": "compaction", "replacement_items": len(payload.get("replacement_history", []))}))
            continue
        if record_type != "response_item":
            _classify_unknown(str(record_type), unknown, deferred)
            continue
        item_type = payload.get("type")
        seen_items = True
        if item_type in {"reasoning", "compaction"}:
            continue
        native_id = payload.get("id")
        key = str(native_id) if native_id else f"line:{line}"
        item_cwd = payload.get("cwd", cwd)
        if not isinstance(item_cwd, str) or not Path(item_cwd).is_absolute():
            item_cwd = None
        call_id = payload.get("call_id")
        if item_type == "message":
            role = payload.get("role")
            if role not in {"user", "assistant", "developer"}:
                # A new role would otherwise vanish: a system or tool message is
                # not the same as no message, and only the model can weigh it.
                _classify_unknown("response_item:message:role=" + str(role), unknown, deferred)
                continue
            # Developer/system context is metadata, not a user decision.
            normalized_role = role if role in {"user", "assistant"} else "metadata"
            content = _text(payload.get("content"))
            role = normalized_role
            if role == "user" and re.fullmatch(
                    r"<environment_context>[\s\S]*</environment_context>", content.strip()):
                role = "metadata"
                item_lineage = {**lineage, "kind": "environment_context"}
            else:
                item_lineage = lineage
        elif item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
            role = "tool_call"
            arguments = payload.get("arguments", payload.get("input", ""))
            if item_type == "tool_search_call":
                arguments = {"query": payload.get("arguments", {}).get("query") if isinstance(payload.get("arguments"), dict) else None,
                             "arguments": payload.get("arguments"), "status": payload.get("status"),
                             "execution": payload.get("execution")}
            try:
                params = json.loads(arguments) if isinstance(arguments, str) else arguments
                if isinstance(params, dict):
                    # An explicit tool cwd overrides the turn; resolve relative paths
                    # only against a known absolute base, never against our own process.
                    supplied = "workdir" if "workdir" in params else ("cwd" if "cwd" in params else None)
                    if supplied:
                        candidate = params[supplied]
                        if isinstance(candidate, str) and candidate:
                            written = str(Path(item_cwd) / candidate) if item_cwd else candidate
                            if Path(written).is_absolute():
                                # The same answer as for the resolved path (the scope resolves links itself), asked
                                # so that `ScanCache` keeps the path the log wrote, not where a link pointed then.
                                scope.includes(written)
                            item_cwd = str((Path(item_cwd) / candidate).resolve()) if item_cwd else (
                                candidate if Path(candidate).is_absolute() else None)
                        else:
                            item_cwd = None
            except ValueError:
                pass
            call_cwd[call_id] = item_cwd
            tool_name = payload.get("name") or ("tool_search" if item_type == "tool_search_call" else "unknown")
            content = f"Tool: {tool_name}\n{_text(arguments)}"
        elif item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            role = "tool_result"
            item_cwd = call_cwd.get(call_id, item_cwd)
            if item_type == "tool_search_output":
                content = json.dumps({"status": payload.get("status"), "execution": payload.get("execution"),
                                      "tools": payload.get("tools", [])}, ensure_ascii=False, sort_keys=True)
            else:
                content = _text(payload.get("output"))
        elif item_type == "web_search_call":
            role = "tool_call"
            call_id = call_id or native_id
            call_cwd[call_id] = item_cwd
            content = "Tool: web_search\n" + json.dumps({"status": payload.get("status"), "action": payload.get("action")},
                                                          ensure_ascii=False, sort_keys=True)
        else:
            unknown.add("response_item:" + str(item_type))
            continue
        relevant |= scope.includes(item_cwd)
        if not content or not selected(item_cwd):
            continue
        result.append(_record("codex", session, key, role, content, path, line, raw_hash, item_cwd, scope,
                              native_record_id=native_id, parent_record_id=payload.get("parent_id"),
                              recorded_at=data.get("timestamp"), tool_call_id=call_id, git=context_git,
                              lineage=item_lineage if item_type == "message" else lineage))
    if not relevant:
        # Keep only local audit metadata. Do not send unrelated bodies to any LLM.
        # A read cut short is said even when what was read was another project's: the rest may not have been.
        return finish([], warnings if decisions["unattributed"] or not row_count else
                      [note for note in warnings if note.startswith("log file read only in part")])
    if decisions["unattributed"]:
        warnings.append(f"Codex path attribution unclear, {decisions['unattributed']} records excluded: {path.name}")
    if unknown:
        warnings.append(f"Codex unsupported record types {path.name}: {', '.join(sorted(unknown))}")
    _flush_deferred(deferred, warnings, "Codex", path)
    if not any(r.role in {"user", "assistant"} for r in result) and ui_messages:
        warnings.append(f"Codex UI message format without response_item is not supported yet: {path.name}")
    _codex_authoring(result, turns)
    _codex_queued(result, triggers)
    return finish(_segments(result), warnings)


def _claude_attachment(data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    attachment = data.get("attachment")
    if not isinstance(attachment, dict):
        return None
    typ = attachment.get("type")
    if typ == "file":
        content = attachment.get("content", {})
        file_data = content.get("file", {}) if isinstance(content, dict) else {}
        text = file_data.get("content") if isinstance(file_data, dict) else None
        if isinstance(text, str):
            filename = attachment.get("filename") or file_data.get("filePath") or "unknown"
            return f"[Claude attached file snapshot: {filename}]\n{text}", {"attachment_type": typ, "filename": str(filename)}
    if typ == "edited_text_file":
        snippet = attachment.get("snippet")
        if isinstance(snippet, str):
            filename = attachment.get("filename") or "unknown"
            return f"[Claude edited-file snapshot: {filename}]\n{snippet}", {"attachment_type": typ, "filename": str(filename)}
    return None


def _claude_session_root(transcript: Path, session: str) -> Path:
    if transcript.parent.name == "subagents" and transcript.parent.parent.name == str(session):
        return transcript.parent.parent
    return transcript.parent / str(session)


def _portable_sidecar(reference: Path, transcript: Path, session: str) -> Path | None:
    expected = _claude_session_root(transcript, session) / "tool-results"
    # Native path is accepted only if it is inside this transcript's own session directory.
    if reference.is_absolute() and within(reference, expected):
        return reference
    # Archive relocation: reconnect only an original .../<same-session>/tool-results/<basename> structure.
    parts = reference.parts
    try:
        index = len(parts) - 1 - list(reversed(parts)).index("tool-results")
    except ValueError:
        return None
    if index < 1 or parts[index - 1] != str(session) or index != len(parts) - 2:
        return None
    candidate = expected / reference.name
    return candidate if within(candidate, expected) else None


def _claude_subagent_links(directory: Path) -> dict[str, dict[str, Any]]:
    """Return only agent transcripts whose parent call AND launch result are both verified."""
    links: dict[str, dict[str, Any]] = {}
    for main in sorted(directory.rglob("*.jsonl")):
        if "subagents" in main.parts or main.is_symlink() or not within(main, directory):
            continue
        # Only rows that can carry a call or a launch result are parsed (`only`; each names its session too), and a
        # parent too large to hold is read through: this runs over every Claude Code transcript at every scan.
        rows, _, _ = _jsonl(main, stream_large=True, only=(b'"tool_use"', b'"agentId"'))
        seen, rows = _head(rows)
        if not seen:
            continue
        session = next((d.get("sessionId") for _, d, _ in seen if d.get("sessionId")), transcript_stem(main))
        # An opaque session ID must never become an arbitrary path capability.
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session):
            continue
        calls: set[str] = set()
        launches: dict[str, tuple[str, str | None]] = {}
        for _, data, _ in rows:
            message = data.get("message")
            if isinstance(message, dict):
                blocks = message.get("content", [])
                if isinstance(blocks, list):
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                            calls.add(str(block["id"]))
            result = data.get("toolUseResult")
            if isinstance(result, dict) and result.get("agentId"):
                tool_id = None
                if isinstance(message, dict):
                    blocks = message.get("content", [])
                    if isinstance(blocks, list):
                        for block in blocks:
                            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                                tool_id = str(block["tool_use_id"])
                                break
                if tool_id:
                    launches[str(result["agentId"])] = (tool_id, data.get("uuid"))
        session_dir = main.parent / str(session) / "subagents"
        if not session_dir.is_dir() or not within(session_dir, directory):
            continue
        for meta in session_dir.glob("*.meta.json"):
            if meta.is_symlink() or not within(meta, session_dir):
                continue
            try:
                fd = os.open(meta, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    if os.fstat(stream.fileno()).st_size > 128_000:
                        continue
                    info = json.loads(stream.read(128_001))
                if not isinstance(info, dict):
                    continue
            except (OSError, UnicodeError, ValueError):
                continue
            agent_id = meta.name.removeprefix("agent-").removesuffix(".meta.json")
            tool_id = info.get("toolUseId")
            launch = launches.get(agent_id)
            transcript = meta.with_name(meta.name.removesuffix(".meta.json") + ".jsonl")
            if (not transcript.is_file() or transcript.is_symlink() or not within(transcript, session_dir)
                    or not tool_id or tool_id not in calls or not launch or launch[0] != tool_id):
                continue
            links[str(transcript)] = {"kind": "subagent", "agent_id": agent_id,
                "parent_session_id": str(session), "parent_tool_call_id": str(tool_id),
                "parent_result_record_id": launch[1], "spawn_depth": info.get("spawnDepth"),
                "agent_type": info.get("agentType"), "model": info.get("model"),
                "description": info.get("description")}
    return links


def parse_claude(path: Path, scope: Scope, *, subagent_link: dict[str, Any] | None = None,
                 stream_large: bool = True) -> Snapshot:
    """A Claude Code transcript's in-scope records. With `stream_large` (the default; off for a check on a byte
    budget) a transcript too large to hold is read through line by line, its session, entrypoint and sidechain mark
    taken from its first rows (`_head`)."""
    rows, warnings, manifest = _jsonl(path, stream_large=stream_large)
    seen, rows = _head(rows)
    session = next((d.get("sessionId") for _, d, _ in seen if d.get("sessionId")), transcript_stem(path))
    cwd = None
    result, unknown, relevant = [], set(), False
    deferred: dict[str, int] = {}
    call_cwd: dict[str, str | None] = {}
    call_authoring: dict[str, dict] = {}  # a tool result is written by the tool, for the model turn that called it
    is_subagent_file = "subagents" in path.parts or any(bool(d.get("isSidechain")) for _, d, _ in seen)
    row_count = 0
    if is_subagent_file and not subagent_link:
        for _, data, _ in rows:
            row_count += 1
            cwd = data.get("cwd", cwd)
            relevant |= scope.includes(cwd)
        if relevant:
            warnings.append(f"Claude subagent excluded from scope, parent link unverified: {path.name}")
        else:
            # Not this project's: what a linked reading of it says (below), so a link changes nothing.
            warnings = warnings if not row_count else [note for note in warnings if note.startswith("log file read only in part")]
        # In the project, its result depends on the parent's transcript, which may yet link it: not kept by
        # `ScanCache`. Out of it, the same paths decide either way, so that result is settled.
        return Snapshot([], warnings, [{**manifest, **({"unlinked_subagent": True} if relevant else {})}] if manifest else [])
    lineage = dict(subagent_link or {})
    entry = next((d.get("entrypoint") for _, d, _ in seen if isinstance(d.get("entrypoint"), str)), "")
    if not lineage and entry.startswith("sdk-"):
        # `claude -p` or an SDK: no person at the terminal (see the Codex `exec` case).
        lineage = {"kind": "unattended", "agent_type": "claude -p" if entry == "sdk-cli" else "claude sdk"}
    for line, data, raw_hash in rows:
        row_count += 1
        cwd = data.get("cwd", cwd)
        relevant |= scope.includes(cwd)
        typ = data.get("type")
        if typ in {"file-history-snapshot", "file-history-delta", "queue-operation", "progress", "last-prompt",
                   "custom-title", "ai-title", "atis-latch", "mode", "permission-mode", "bridge-session",
                   "agent-name", "tag", "bridge-config", "pr-link"}:
            _classify_unknown(str(typ), unknown, deferred)
            continue
        if typ == "system" and not data.get("isCompactSummary"):
            # Two shapes below still produce a compaction record and must reach it:
            # a `compact_boundary` marker (a work-unit boundary) and any system row
            # carrying summary text. Only rows with neither are unread.
            if data.get("subtype") != "compact_boundary" and not data.get("summary"):
                _classify_unknown("system:" + str(data.get("subtype")), unknown, deferred)
                continue
        if data.get("isCompactSummary"):
            if scope.includes(cwd):
                message = data.get("message", {})
                text = _text(message.get("content") if isinstance(message, dict) else message)
                if text:
                    result.append(_record("claude", session, str(data.get("uuid") or f"line:{line}"), "metadata",
                        text, path, line, raw_hash, cwd, scope, derivation="summary", recorded_at=data.get("timestamp"),
                        parent_record_id=data.get("parentUuid"), lineage={**lineage, "kind": "compaction"},
                        git={"branch": data["gitBranch"]} if data.get("gitBranch") else {}))
            continue
        if typ in {"summary", "system"}:
            if scope.includes(cwd) and (data.get("summary") or data.get("subtype") == "compact_boundary"):
                text = _text(data.get("summary") or data.get("content") or "Compaction boundary")
                result.append(_record("claude", session, str(data.get("uuid") or f"line:{line}"), "metadata",
                                      text, path, line, raw_hash, cwd, scope, derivation="summary",
                                      recorded_at=data.get("timestamp"), lineage={**lineage, "kind": "compaction"}))
            continue
        if typ == "attachment":
            extracted = _claude_attachment(data)
            if extracted and scope.includes(cwd):
                text, attachment_meta = extracted
                result.append(_record("claude", session, str(data.get("uuid") or f"line:{line}"), "metadata",
                    text, path, line, raw_hash, cwd, scope, native_record_id=data.get("uuid"),
                    parent_record_id=data.get("parentUuid"), recorded_at=data.get("timestamp"),
                    git={"branch": data["gitBranch"]} if data.get("gitBranch") else {},
                    lineage={**lineage, "kind": "attachment", **attachment_meta}))
            elif not extracted and "attachment" in data:
                # An attachment shape we cannot read is a format gap, not an
                # intentionally skipped file, and it used to vanish silently.
                # Keyed on presence, not truthiness: `[]`, `""` and `{}` are
                # malformed too, and a falsey value must not hide that.
                shape = data["attachment"]
                name = shape.get("type") if isinstance(shape, dict) else type(shape).__name__
                _classify_unknown(f"attachment:{name}", unknown, deferred)
            continue
        if typ not in {"user", "assistant"}:
            unknown.add(str(typ))
            continue
        message = data.get("message", {})
        if not isinstance(message, dict):
            unknown.add("message:not-object")
            continue
        blocks = message.get("content", [])
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        native_id = data.get("uuid")
        base = str(native_id or f"line:{line}")
        # An assistant row names its model and the effort in force (`perTurnEffort`, else the session's
        # `effort`); `<synthetic>` is a message Claude Code wrote itself, with no model behind it.
        authoring = _authoring(message.get("model"), data.get("perTurnEffort") or data.get("effort")) if typ == "assistant" else {}
        if authoring.get("model", "").startswith("<"):
            authoring = {}
        for part, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            role, call_id, item_cwd = typ, None, cwd
            record_lineage, record_authoring = lineage, authoring
            if btype in {"thinking", "redacted_thinking"}:
                continue
            if btype == "text":
                text = _text(block.get("text"))
            elif btype == "image":
                role = "metadata"
                record_lineage = {**lineage, "kind": "binary_attachment"}
                text = _text(block)
            elif btype == "tool_use":
                role, call_id = "tool_call", block.get("id")
                params = block.get("input", {})
                if isinstance(params, dict):
                    other = params.get("cwd") or params.get("workdir")
                    if isinstance(other, str) and Path(other).is_absolute():
                        item_cwd = other
                call_cwd[call_id] = item_cwd
                call_authoring[call_id] = authoring
                # Every argument, so a Write keeps its file path next to the content.
                body = json.dumps(params, ensure_ascii=False, sort_keys=True) if isinstance(params, dict) else _text(params)
                text = f"Tool: {block.get('name', 'unknown')}\n{body}"
            elif btype == "tool_result":
                role, call_id = "tool_result", block.get("tool_use_id")
                item_cwd = call_cwd.get(call_id, item_cwd)
                record_authoring = call_authoring.get(call_id, {})
                text = _text(block.get("content"))
            else:
                unknown.add("content:" + str(btype))
                continue
            if not text or not scope.includes(item_cwd):
                continue
            rec = _record("claude", session, f"{base}:{part}", role, text, path, line, raw_hash, item_cwd, scope,
                          native_record_id=native_id, parent_record_id=data.get("parentUuid"),
                          recorded_at=data.get("timestamp"), tool_call_id=call_id,
                          git={"branch": data["gitBranch"]} if data.get("gitBranch") else {}, lineage=record_lineage,
                          authoring=record_authoring)
            result.append(rec)
            if role == "tool_result":
                matches = re.findall(r"Full output saved to:\s*([^\n<]+)", text)
                for name in matches:
                    reference = Path(name.strip())
                    sidecar = _portable_sidecar(reference, path, str(session))
                    allowed = _claude_session_root(path, str(session)) / "tool-results"
                    if sidecar is None or sidecar.is_symlink() or not within(sidecar, allowed):
                        warnings.append(f"Claude tool sidecar outside the allowed directory not read: {path.name}")
                        continue
                    try:
                        before = sidecar.stat()
                        if before.st_size > MAX_FILE_BYTES:
                            raise OSError("oversize")
                        content = sidecar.read_text(encoding="utf-8")
                        after = sidecar.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise OSError("changed")
                    except (OSError, UnicodeError):
                        warnings.append(f"Claude tool sidecar missing or not read: {sidecar.name}")
                        continue
                    result.append(SourceRecord(ident("src_", rec.source_id, "sidecar"), "claude", session,
                        "tool_result", content, {"kind": "sidecar", "path": str(sidecar), "raw_hash": digest(content),
                        "referenced_from": rec.source_id}, native_record_id=native_id, recorded_at=data.get("timestamp"),
                        cwd=item_cwd, worktree_id=scope.worktree_for(item_cwd), tool_call_id=call_id,
                        lineage={**lineage, "kind": "tool_sidecar", "parent_source_id": rec.source_id},
                        authoring=record_authoring))
    if not relevant:
        # Nothing of this project, nothing reported; the manifest says the file was read (`ScanCache`, the index).
        # A file that could not be read (no rows) says so, as a Codex one does: it may have been this project's;
        # so does a read cut short.
        return Snapshot([], warnings if not row_count else
                        [note for note in warnings if note.startswith("log file read only in part")],
                        [manifest] if manifest else [])
    if unknown:
        warnings.append(f"Claude unsupported record types {path.name}: {', '.join(sorted(unknown))}")
    _flush_deferred(deferred, warnings, "Claude", path)
    return Snapshot(_segments(result), list(dict.fromkeys(warnings)), [manifest] if manifest else [])


def source_homes(codex_home: Path | None = None, claude_home: Path | None = None,
                 opencode_home: Path | None = None) -> tuple[Path, Path, Path]:
    """The three tools' directories: as given, else from each tool's own environment variable, else its default."""
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    claude_home = claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return codex_home, claude_home, opencode_data_dir(opencode_home)


def jsonl_files(codex_home: Path, claude_home: Path) -> list[tuple[Path, str]]:
    """Every transcript file with its provider, in collection order; symlinks and escapes are skipped."""
    found: list[tuple[Path, str]] = []
    for directory, provider in ((codex_home / "archived_sessions", "codex"), (codex_home / "sessions", "codex"),
                                (claude_home / "projects", "claude")):
        if not directory.is_dir():
            continue
        paths = sorted(directory.rglob("*.jsonl"))
        plain = set(paths)
        # A compressed transcript is read unless its uncompressed copy is still there (the same records).
        paths += sorted(p for p in directory.rglob("*" + COMPRESSED_SUFFIX)
                        if p.with_name(p.name.removesuffix(".zst")) not in plain)
        for path in paths:
            if not path.is_symlink() and within(path, directory):
                found.append((path, provider))
    return found


def _drop_forked_copies(records: dict[str, SourceRecord]) -> None:
    """Claude Code's `--fork-session` and resume write a new session file that begins with a copy of the parent's
    recent rows (same `uuid`, the new session ID written over them) before the session's own rows. Those copies
    are the parent's history: when the parent's row is here too, the copy is dropped, and the fork's own records
    keep where they came from (`fork`: the parent session and the time of the last copied row). The parent is
    the session whose own (unshared) rows start first; a session with none is the parent of its forks. A fork
    whose parent is not here keeps its copies, the only text of that history. Codex forks copy nothing."""
    by_uuid: dict[str, list[SourceRecord]] = {}
    for record in records.values():
        if record.provider == "claude" and record.native_record_id and record.session_id:
            by_uuid.setdefault(record.native_record_id, []).append(record)
    shared = {uuid: members for uuid, members in by_uuid.items() if len({m.session_id for m in members}) > 1}
    if not shared:
        return
    first_own: dict[str, str] = {}
    for uuid, members in by_uuid.items():
        if uuid in shared:
            continue
        for member in members:
            if member.recorded_at and member.recorded_at < first_own.get(member.session_id, "~"):
                first_own[member.session_id] = member.recorded_at
    forks: dict[str, tuple[str, str]] = {}  # fork session → (parent session, time of the last copied row)
    for members in shared.values():
        parent = min({m.session_id for m in members}, key=lambda s: (first_own.get(s, ""), s))
        for member in members:
            if member.session_id == parent:
                continue
            records.pop(member.source_id, None)
            at = member.recorded_at or ""
            if at >= forks.get(member.session_id, ("", ""))[1]:
                forks[member.session_id] = (parent, at)
    for record in records.values():
        if record.session_id in forks:
            parent, at = forks[record.session_id]
            record.fork = {"from_session_id": parent, "point_at": at}


def _link_called_tools(records: dict[str, SourceRecord]) -> None:
    """A session run with no person (`codex exec`, `claude -p`) whose session ID appears in another session's
    tool call or result was started by that call: it becomes that session's sub-agent, `agent_type` naming the
    tool. The ID is what the called tool printed (`thread.started`, `session_id`); nothing is matched by time."""
    callees: dict[str, list[SourceRecord]] = {}
    for record in records.values():
        if (record.lineage or {}).get("kind") == "unattended" and record.session_id:
            callees.setdefault(record.session_id, []).append(record)
    for record in records.values():
        if not callees:
            return
        if record.role not in {"tool_call", "tool_result"} or record.session_id in callees:
            continue
        for session_id in [s for s in callees if s != record.session_id and s in record.content]:
            link = {"kind": "subagent", "parent_session_id": record.session_id, "parent_tool_call_id": record.tool_call_id}
            if record.role == "tool_result":
                link["parent_result_record_id"] = record.source_id
            for member in callees.pop(session_id):
                member.lineage = {**member.lineage, **link}


# A file read through more distinct paths than this (cwd, workdir) is read again every scan: its entry would grow.
CACHE_PATHS = 64


@dataclass
class ScanCache:
    """What earlier scans made of each transcript file that gave the project no records, so an unchanged one is not
    read again: Codex keeps hundreds of old sessions compressed (on this machine 563 files, 3.5 GB once
    decompressed), and reading them all took a scan from 15 to 81 seconds. An entry is the file's size and mtime as
    read, the limitations and manifest its reading gave, which are replayed as they were, and every path the file
    was attributed by (`paths`). It is kept only for a result the file itself settles: not a Claude Code sub-agent
    waiting for its parent's link (`unlinked_subagent`), not a read an error cut short (`partial`), not a file that
    changed while it was read. It is replayed only while the file's size and mtime are the same and none of its
    paths falls in the scope now (a symlink that came to point into the project), and only under the same reading
    code and scope roots (`key`, `scan_cache_key`). A file with records is always read."""
    key: str
    entries: dict[str, dict] = field(default_factory=dict)
    fresh: dict[str, dict] = field(default_factory=dict)  # what this scan keeps for the next one
    reused: int = 0


def scan_cache_key(scope: Scope) -> str:
    """The code that reads and attributes transcripts (this module, the scope's path test and `util.within` under it)
    and what attribution reads of the scope (its roots and the folder within them; not the folder it was opened
    from, so worktrees sharing one state share the cache): a cache made under another is not used."""
    code = b"".join(Path(module.__file__).read_bytes() for module in (sys.modules[__name__], git_context, util))
    return digest([digest(code), scope.relative, scope.root is not None, sorted(str(root) for root in scope.roots)])


class _AskedScope:
    """The scope, noting every path a parser asks about (`includes`, `worktree_for`): what decided where a file's
    records belong, kept with its cache entry and asked again before the entry is replayed."""

    def __init__(self, scope: Scope):
        self._scope, self.paths = scope, set()

    def includes(self, cwd: str | None) -> bool:
        if isinstance(cwd, str):
            self.paths.add(cwd)
        return self._scope.includes(cwd)

    def worktree_for(self, cwd: str | None) -> str | None:
        if isinstance(cwd, str):
            self.paths.add(cwd)
        return self._scope.worktree_for(cwd)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._scope, name)


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def collect_logs(scope: Scope, *, codex_home: Path | None = None, claude_home: Path | None = None,
                 opencode_home: Path | None = None, cache: ScanCache | None = None) -> Snapshot:
    """Every in-scope record of the three tools: Codex and Claude Code JSONL files, the opencode database. With a
    `cache`, a transcript file an earlier scan found no records in, and that has not changed since, is not read:
    its limitations and manifest are replayed (marked `reused`), and the cache keeps what this scan found."""
    codex_home, claude_home, opencode_home = source_homes(codex_home, claude_home, opencode_home)
    result, warnings, files = {}, [], []
    claude_dir = claude_home / "projects"
    claude_links = _claude_subagent_links(claude_dir) if claude_dir.is_dir() else {}
    def merge(snapshot: Snapshot) -> None:
        warnings.extend(snapshot.limitations)
        files.extend(snapshot.files)
        for record in snapshot.records:
            previous = result.get(record.source_id)
            if previous and previous.content_hash != record.content_hash:
                warnings.append(f"different content under the same native ID: {record.source_id}; the copy found later was kept.")
            result[record.source_id] = record
    for path, provider in jsonl_files(codex_home, claude_home):
        before = _stat(path) if cache is not None else None
        if before is not None:
            entry = cache.entries.get(str(path))
            if (entry and (entry.get("bytes"), entry.get("mtime_ns")) == before
                    and not any(scope.includes(asked) for asked in entry.get("paths", []))):
                merge(Snapshot([], list(entry["limitations"]), [{**manifest, "reused": True} for manifest in entry["files"]]))
                cache.fresh[str(path)] = entry
                cache.reused += 1
                continue
        asked = _AskedScope(scope) if before is not None else scope
        if provider == "claude":
            snapshot = parse_claude(path, asked, subagent_link=claude_links.get(str(path)))
        else:
            snapshot = parse_codex(path, asked)
        merge(snapshot)
        # Kept only for a result the file settles (`ScanCache`): read whole or to the cap, by its own content.
        settled = (snapshot.files and not any(manifest.get("unlinked_subagent") or manifest.get("partial") == "error"
                                              for manifest in snapshot.files))
        if (before is not None and not snapshot.records and settled and len(asked.paths) <= CACHE_PATHS
                and _stat(path) == before):
            cache.fresh[str(path)] = {"bytes": before[0], "mtime_ns": before[1], "limitations": list(snapshot.limitations),
                                      "files": [{k: v for k, v in manifest.items() if k != "reused"} for manifest in snapshot.files],
                                      "paths": sorted(asked.paths)}
    for path in opencode_databases(opencode_home):
        merge(parse_opencode(path, scope))
    _drop_forked_copies(result)
    _link_called_tools(result)
    ordered = sorted(result.values(), key=lambda r: (r.recorded_at or "", r.provider, r.session_id or "",
                                                    r.locator.get("line", 0), r.locator.get("fragment_index", 0), r.source_id))
    return Snapshot(ordered, list(dict.fromkeys(warnings)), files)
