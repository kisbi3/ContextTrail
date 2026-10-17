from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .util import digest, ident


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
                "Another Claude session sent a message:", "[Usage limit reached;", "[Earlier usage-limit notes")

# The note Claude Code puts before an image the person attached ("[Image: original 2520x540, displayed at …]");
# the person's own words, if any, follow it.
IMAGE_NOTE = re.compile(r"\A(?:\s*\[Image: original [^\]\n]*\])+\s*")


def is_user_prompt(record: SourceRecord) -> bool:
    """A message the person typed, as opposed to text a harness or a parent agent put in the user role."""
    if record.role != "user" or record.derivation != "original":
        return False
    lineage = record.lineage or {}
    # Summaries, attachments and sub-agent transcripts carry a lineage kind; a split
    # message counts once, at its first fragment.
    if lineage.get("kind") or (lineage.get("fragment_index") or 1) > 1:
        return False
    text = IMAGE_NOTE.sub("", record.content).strip()
    return bool(text) and not text.startswith(HARNESS_TEXT)


def segment_record(record: SourceRecord, max_chars: int = 32_000) -> list[SourceRecord]:
    """Split a long source into stable, evidence-citable text fragments."""
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
