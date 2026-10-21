from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .util import digest, ident, mask_record


@dataclass
class SourceRecord:
    source_id: str
    provider: str
    session_id: str | None
    role: str
    content: str
    locator: dict[str, Any]
    native_record_id: str | None = None
    parent_record_id: str | None = None
    recorded_at: str | None = None
    cwd: str | None = None
    worktree_id: str | None = None
    tool_call_id: str | None = None
    derivation: str = "original"
    git: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    # Which agent wrote the record, as its log names it: `model`, `effort` (Codex `effort`, Claude Code
    # `effort`, opencode `variant`) and `provider` when given; empty when the log says nothing. Code reads
    # it, the analysis model never sees it, and it is outside the content hash, so filling it in for a
    # stored record does not send its unit again.
    authoring: dict[str, Any] = field(default_factory=dict)
    # For a session forked from another (Claude Code `--fork-session`/resume copies the parent's recent rows into
    # the new file): `from_session_id` and `point_at`, the time of the last copied row. Set on the fork's own
    # records by `_drop_forked_copies`; outside the content hash and hidden from the model, like `authoring`.
    fork: dict[str, Any] = field(default_factory=dict)
    # What code makes of a tool call or its result (`analysis.mark_steps` at scan): the call's `hint` (edit, read,
    # run, test, commit, vcs, delegate, poll) and `doc` for an edit of documentation only. Outside the content hash
    # and hidden from the model, like `authoring`; the flow reads it off the cited records (`schema.fill_steps`).
    step: dict[str, Any] = field(default_factory=dict)

    pinned_hash: str | None = None  # host-only reconstruction of preserved excerpt ranges

    @property
    def legacy_content_hash(self) -> str:
        """v1 body digest, used only to verify a no-reanalysis metadata-hash migration."""
        return digest([self.content, self.role, self.cwd, self.worktree_id,
                       self.tool_call_id, self.derivation, self.git, self.lineage])

    @property
    def content_hash(self) -> str:
        # Native time/ancestry changes affect interpretation. Filesystem locator and
        # host observation time do not. Version marker permits a verified v1 migration.
        return self.pinned_hash or digest(["source-v2", self.legacy_content_hash,
            self.provider, self.session_id, self.native_record_id,
            self.parent_record_id, self.recorded_at])

    @property
    def source_version(self) -> str:
        return self.content_hash[:16]

    def metadata(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("content")
        result.pop("pinned_hash")
        result.update(content_hash=self.content_hash, source_version=self.source_version)
        return result

    def for_prompt(self) -> dict[str, Any]:
        result = self.metadata()
        # Never expose original filesystem paths as allowed read operations.
        result.pop("locator")
        result["lines"] = [{"line": i, "text": text}
                           for i, text in enumerate(self.content.splitlines(), 1)]
        return result


# The fixed first words of the project memory a session-start hook gives the agent (`session_context`). opencode
# posts it as a user message, so the analyzer must not take it for a person's request.
SESSION_CONTEXT_HEAD = "ContextTrail project memory for this session"

# Text a harness puts in the user role. None of it is something the person typed.
HARNESS_TEXT = ("<task-notification>", "<command-name>", "<command-message>", "<command-args>",
                "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                "Caveat: The messages below were generated", "[Request interrupted by user", "<system-reminder>",
                "<user_instructions>", "<environment_context>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
                "<user-prompt-submit-hook>", "# AGENTS.md instructions",
                # The note reminder a hook sends back (opencode posts it as a user message) and the wrappers of
                # Claude Code and Codex (0.16x).
                "ContextTrail notes are on for this project", "ContextTrail stored the notes queued",
                "Stop hook feedback", "<hook_prompt",
                # The project memory a session-start hook gives the agent (opencode posts it as a user message).
                SESSION_CONTEXT_HEAD,
                # A skill's body, which Claude Code puts in the user role when the agent invokes the skill.
                "Base directory for this skill:",
                # Claude Code (2.1.x): a sub-agent's or another session's report handed back to the session, and the
                # notices about the account's usage limit.
                "Another Claude session sent a message:", "[Usage limit reached;", "[Earlier usage-limit notes",
                # The Codex app's list of plugins it offers, put in the user role at a session's start.
                "<recommended_plugins>")

# A message that is only one of the host's own control commands (Codex and opencode write a typed `/compact` as the
# person's message): it steers the tool, asks nothing of the agent, and starts no turn. A custom command (a skill
# or a prompt the person wrote) is the person's request and stays one.
HOST_COMMAND = re.compile(r"\A/(?:compact|clear|new|model|effort|status|cost|usage|fast|config|permissions|mcp|help|"
                          r"resume|exit|quit)(?:\s+\S+)?\s*\Z")

# The note Claude Code writes as a user-role row of its own (`isMeta`) after the agent reads an image file ("[Image:
# original 2520x540, displayed at …]"): no image the person attached and no words of theirs. It is set aside before
# the check; words after it, if any, are kept.
IMAGE_NOTE = re.compile(r"\A(?:\s*\[Image: original [^\]\n]*\])+\s*")


# A whole user-role message that is an event of the Codex app's screen, not something the person wrote.
APP_EVENT = re.compile(r"\A<external_codex_apps_open_page>.*?</external_codex_apps_open_page>\Z", re.DOTALL)
# The person's answers to the agent's own question (Codex `request_user_input`): a reply within the turn, not a
# request and not the start of a new turn (`docs/plans/REQUEST_ANSWERS.md` §2).
QUESTION_REPLY = re.compile(r"\A<send_user_message_question_reply>(.*)</send_user_message_question_reply>\Z", re.DOTALL)


def _person_text(record: SourceRecord) -> str | None:
    """The text of a user-role message the session itself received (no harness lineage, first fragment only)."""
    if record.role != "user" or record.derivation != "original":
        return None
    lineage = record.lineage or {}
    # Summaries, attachments and sub-agent transcripts carry a lineage kind; a split
    # message counts once, at its first fragment.
    if lineage.get("kind") or (lineage.get("fragment_index") or 1) > 1:
        return None
    return IMAGE_NOTE.sub("", record.content).strip()


def question_reply(record: SourceRecord) -> list[dict] | None:
    """The person's answers to the agent's question, `[{"question", "answer"}]`, when the message is one; else None.
    A reply whose body cannot be read is still a reply (an empty list)."""
    found = QUESTION_REPLY.match(_person_text(record) or "")
    if not found:
        return None
    try:
        items = json.loads(found.group(1))
    except ValueError:
        return []
    return [{"question": str(item.get("question") or ""), "answer": str(item.get("answer") or "")}
            for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def is_user_prompt(record: SourceRecord) -> bool:
    """A message the person typed, as opposed to text a harness or a parent agent put in the user role. The
    person's reply to the agent's own question is theirs but not a prompt: it continues the turn (`question_reply`).
    A message a program sent through the tool's queue is not theirs either (`is_queued_message`)."""
    text = _person_text(record)
    return (bool(text) and not text.startswith(HARNESS_TEXT) and not APP_EVENT.match(text)
            and not QUESTION_REPLY.match(text) and not HOST_COMMAND.match(text) and not is_queued_message(record))


def is_queued_message(record: SourceRecord) -> bool:
    """A user-role message of a turn the tool started from its queue, as its log says (Codex `turn_trigger:
    "queue"`: what `codex queue` sends, such as a monitor's hourly report the agent set up): it starts a turn of
    its own, but it is no request of the person's."""
    return record.role == "user" and (record.authoring or {}).get("trigger") == "queue"


def segment_record(record: SourceRecord, max_chars: int = 32_000) -> list[SourceRecord]:
    """Split a long source into stable, evidence-citable text fragments.

    Every reader (Codex, Claude Code, opencode, Git) passes its records through here, and credential-shaped values
    are masked first (`util.mask_record`): a key split across fragments is masked whole, and a scan, the freshness
    check and a note read the same content and content hash. The locator still points into the original."""
    record = mask_record(record)
    if len(record.content) <= max_chars:
        return [record]
    pieces: list[tuple[str, int, int]] = []
    start = 0
    text = record.content
    while start < len(text):
        target = min(start + max_chars, len(text))
        if target < len(text):
            newline = text.rfind("\n", start + max_chars // 2, target)
            if newline > start:
                target = newline + 1
        piece = text[start:target]
        if not piece:
            target = min(start + max_chars, len(text))
            piece = text[start:target]
        pieces.append((piece, start, target))
        start = target
    output = []
    total = len(pieces)
    for index, (piece, lo, hi) in enumerate(pieces, 1):
        locator = {**record.locator, "fragment_index": index, "fragment_count": total,
                   "fragment_char_start": lo, "fragment_char_end": hi}
        lineage = {**record.lineage, "fragment_of": record.source_id,
                   "fragment_index": index, "fragment_count": total}
        output.append(replace(record, source_id=ident("srcseg_", record.source_id, index),
                              content=piece, locator=locator, lineage=lineage))
    return output


@dataclass
class Snapshot:
    records: list[SourceRecord]
    limitations: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    # Per tool, where a scan looked and what it found there (`sources.local.source_summary`); empty elsewhere.
    sources: list[dict[str, Any]] = field(default_factory=list)
    # The tools a scan read when it read only some (`--sources`, plus Git); None when it read every tool. Storing it
    # leaves the other tools' records as they were instead of marking them unavailable (`Store.ingest`).
    providers: tuple[str, ...] | None = None

    @property
    def id(self) -> str:
        return ident("snap_", sorted((r.source_id, r.content_hash) for r in self.records))


# 2: question/asked/applied and the verifies/answers relations. Version 1 graphs stay
# readable; the stricter rules apply to items written or updated from version 2 on.
GRAPH_SCHEMA_VERSION = 2


def empty_graph(scope_id: str) -> dict[str, Any]:
    return dict(schema_version=GRAPH_SCHEMA_VERSION, version=0, scope_id=scope_id,
                source_snapshot_id=None, analysis_status="no_data", analyzed_at=None,
                events=[], edges=[], open_items=[], limitations=[])
