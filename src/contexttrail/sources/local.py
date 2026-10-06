from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..git_context import Scope
from ..i18n import tr
from ..model import Snapshot, SourceRecord, segment_record
from ..util import FlowError, digest, ident, within

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
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
    # full text of a prompt the person queued. If a queued prompt never also
    # appears as a `user` record, the request is invisible to the graph.
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


def _jsonl(path: Path) -> tuple[list[tuple[int, dict, str]], list[str], dict]:
    """Freeze the initial byte length. Only newline-terminated records are committed."""
    rows, warnings = [], []
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_FILE_BYTES:
                raise FlowError(tr(f"로그 파일 한도({MAX_FILE_BYTES} bytes) 초과: {path.name}",
                                   f"Log file over the limit ({MAX_FILE_BYTES} bytes): {path.name}"))
            data = stream.read(before.st_size)
            after = os.fstat(stream.fileno())
            stream.seek(0)
            again = stream.read(before.st_size)
            if data != again or after.st_size < before.st_size:
                raise FlowError(tr(f"로그를 읽는 동안 재작성/축소됨: {path.name}",
                                   f"Log rewritten or truncated while being read: {path.name}"))
    except (OSError, FlowError) as exc:
        return [], [f"log file not read: {path.name} ({type(exc).__name__})"], {}
    boundary = data.rfind(b"\n") + 1
    if boundary != len(data):
        warnings.append(f"last JSONL record still being written, skipped: {path.name}")
    for line_no, raw in enumerate(data[:boundary].splitlines(), 1):
        if not raw.strip():
            continue
        if len(raw) > MAX_LINE_BYTES:
            warnings.append(f"JSONL record over the size limit: {path.name}:{line_no}")
            continue
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("object required")
            rows.append((line_no, value, digest(raw)))
        except (UnicodeError, ValueError):
            warnings.append(f"corrupt JSONL record: {path.name}:{line_no}")
    return rows, warnings, {"path": str(path), "bytes": before.st_size,
                            "complete_bytes": boundary, "digest": digest(data[:boundary])}


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


def parse_codex(path: Path, scope: Scope) -> Snapshot:
    rows, warnings, manifest = _jsonl(path)
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
    metadata = next((d.get("payload", {}) for _, d, _ in rows if d.get("type") == "session_meta"), {})
    session = metadata.get("id") or path.stem
    cwd = metadata.get("cwd")
    cwd = cwd if isinstance(cwd, str) and Path(cwd).is_absolute() else None
    lineage = _codex_lineage(metadata)
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
    for line, data, raw_hash in rows:
        record_type, payload = data.get("type"), data.get("payload", {})
        if not isinstance(payload, dict):
            unknown.add(str(record_type))
            continue
        if line <= inherited and record_type not in {"turn_context", "session_meta"}:
            skipped += 1  # rollout items are numbered from 0; file lines from 1
            continue
        if record_type == "turn_context":
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
        return finish([], warnings if decisions["unattributed"] or not rows else [])
    if decisions["unattributed"]:
        warnings.append(f"Codex path attribution unclear, {decisions['unattributed']} records excluded: {path.name}")
    if unknown:
        warnings.append(f"Codex unsupported record types {path.name}: {', '.join(sorted(unknown))}")
    _flush_deferred(deferred, warnings, "Codex", path)
    if not any(r.role in {"user", "assistant"} for r in result) and any(            d.get("type") == "event_msg" and d.get("payload", {}).get("type") in {"user_message", "agent_message"}
            for _, d, _ in rows):
        warnings.append(f"Codex UI message format without response_item is not supported yet: {path.name}")
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
        rows, _, _ = _jsonl(main)
        if not rows:
            continue
        session = next((d.get("sessionId") for _, d, _ in rows if d.get("sessionId")), main.stem)
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


def parse_claude(path: Path, scope: Scope, *, subagent_link: dict[str, Any] | None = None) -> Snapshot:
    rows, warnings, manifest = _jsonl(path)
    session = next((d.get("sessionId") for _, d, _ in rows if d.get("sessionId")), path.stem)
    cwd = None
    result, unknown, relevant = [], set(), False
    deferred: dict[str, int] = {}
    call_cwd: dict[str, str | None] = {}
    is_subagent_file = "subagents" in path.parts or any(bool(d.get("isSidechain")) for _, d, _ in rows)
    if is_subagent_file and not subagent_link:
        for _, data, _ in rows:
            cwd = data.get("cwd", cwd)
            relevant |= scope.includes(cwd)
        if relevant:
            warnings.append(f"Claude subagent excluded from scope, parent link unverified: {path.name}")
        return Snapshot([], warnings, [manifest] if manifest else [])
    lineage = dict(subagent_link or {})
    for line, data, raw_hash in rows:
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
        for part, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            role, call_id, item_cwd = typ, None, cwd
            record_lineage = lineage
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
                # Every argument, so a Write keeps its file path next to the content.
                body = json.dumps(params, ensure_ascii=False, sort_keys=True) if isinstance(params, dict) else _text(params)
                text = f"Tool: {block.get('name', 'unknown')}\n{body}"
            elif btype == "tool_result":
                role, call_id = "tool_result", block.get("tool_use_id")
                item_cwd = call_cwd.get(call_id, item_cwd)
                text = _text(block.get("content"))
            else:
                unknown.add("content:" + str(btype))
                continue
            if not text or not scope.includes(item_cwd):
                continue
            rec = _record("claude", session, f"{base}:{part}", role, text, path, line, raw_hash, item_cwd, scope,
                          native_record_id=native_id, parent_record_id=data.get("parentUuid"),
                          recorded_at=data.get("timestamp"), tool_call_id=call_id,
                          git={"branch": data["gitBranch"]} if data.get("gitBranch") else {}, lineage=record_lineage)
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
                        lineage={**lineage, "kind": "tool_sidecar", "parent_source_id": rec.source_id}))
    if not relevant:
        return Snapshot([])
    if unknown:
        warnings.append(f"Claude unsupported record types {path.name}: {', '.join(sorted(unknown))}")
    _flush_deferred(deferred, warnings, "Claude", path)
    return Snapshot(_segments(result), list(dict.fromkeys(warnings)), [manifest] if manifest else [])


def collect_logs(scope: Scope, *, codex_home: Path | None = None, claude_home: Path | None = None) -> Snapshot:
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    claude_home = claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    result, warnings, files = {}, [], []
    claude_dir = claude_home / "projects"
    claude_links = _claude_subagent_links(claude_dir) if claude_dir.is_dir() else {}
    specs = ((codex_home / "archived_sessions", parse_codex),
             (codex_home / "sessions", parse_codex),
             (claude_dir, parse_claude))
    for directory, parser in specs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.jsonl")):
            if path.is_symlink() or not within(path, directory):
                continue
            if parser is parse_claude:
                snapshot = parse_claude(path, scope, subagent_link=claude_links.get(str(path)))
            else:
                snapshot = parse_codex(path, scope)
            warnings.extend(snapshot.limitations)
            files.extend(snapshot.files)
            for record in snapshot.records:
                previous = result.get(record.source_id)
                if previous and previous.content_hash != record.content_hash:
                    warnings.append(f"different content under the same native ID: {record.source_id}; the copy found later was kept.")
                result[record.source_id] = record
    ordered = sorted(result.values(), key=lambda r: (r.recorded_at or "", r.provider, r.session_id or "",
                                                    r.locator.get("line", 0), r.locator.get("fragment_index", 0), r.source_id))
    return Snapshot(ordered, list(dict.fromkeys(warnings)), files)
