"""Record-type coverage for the log parsers.

A parser that meets a record type it does not read used to add it to the same
bucket as billing fields it does not need, so both produced the identical
"unsupported record" warning. Routine noise then hid real upstream format
changes. These tests pin the three-way split in `sources/local.py`:

  parsed            -> becomes a SourceRecord
  IGNORED_NO_ANALYSIS_VALUE -> dropped silently, with a written reason per entry
  KNOWN_UNPARSED    -> reported once per file, aggregated, with a reason
  anything else     -> "unsupported", i.e. a genuine gap

What this module does and does not guarantee:

- It guarantees that every type in the registry is classified deliberately, that
  no registry entry lacks a reason, that the two registries do not overlap, and
  that account identifiers in Claude `bridge-session` never become records.
- It does NOT enumerate every type the upstream CLIs can emit, so a brand new
  type will surface as an "unsupported" warning at scan time rather than
  failing CI. Closing that gap needs a recorded corpus of real logs, which this
  repository does not ship. Until then, `project scan` output is the signal.
"""

import json

import pytest

from projectflow.git_context import Scope
from projectflow.sources.local import (IGNORED_NO_ANALYSIS_VALUE, KNOWN_UNPARSED,
                                       parse_codex)
from projectflow.util import dumps


def write(path, rows, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dumps(r) for r in rows) + "\n" + tail, encoding="utf-8")


def base_rows(folder):
    return [{"type": "session_meta", "payload": {"id": "native-session", "cwd": str(folder)}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "설치 스크립트를 고쳐줘."}]}}]


def parse_with(folder, extra_rows):
    path = folder / "rollout.jsonl"
    write(path, base_rows(folder) + extra_rows)
    return parse_codex(path, Scope.resolve(folder))


# Recorded shapes of the record types this parser currently meets but does not read.
# Kept as whole lines so a fixture can be built without inventing payload fields.
UNPARSED_FIXTURES = {
    "token_usage_record": {
        "type": "token_usage_record",
        "payload": {"thread_id": "t", "turn_id": "u", "session_id": "t",
                    "usage": {"input_tokens": 14311, "output_tokens": 412}},
    },
    "event_msg:thread_settings_applied": {
        "type": "event_msg",
        "payload": {"type": "thread_settings_applied", "thread_id": "t",
                    "thread_settings": {"model": "gpt-6-luna", "approval_policy": "never"}},
    },
    "world_state": {
        "type": "world_state",
        "payload": {"full": True, "state": {"agents_md": {"directory": "/x", "text": "# Guide"}}},
    },
    "queue-operation": {
        "type": "queue-operation",
        "operation": "enqueue", "content": "Repository: /x. Work ONLY on first-tier bans.",
    },
    "system:stop_hook_summary": {
        "type": "system", "subtype": "stop_hook_summary",
        "message": "lint passed", "timestamp": "2026-09-28T00:00:00Z",
    },
    "system:local_command": {
        "type": "system", "subtype": "local_command",
        "message": "/contexttrail-update 5", "timestamp": "2026-09-28T00:00:00Z",
    },
}


# Shapes the Claude parser also meets but does not read.
IGNORED_CLAUDE_FIXTURES = {
    "queue-operation": UNPARSED_FIXTURES["queue-operation"],
    "system:turn_duration": {
        "type": "system", "subtype": "turn_duration",
        "durationMs": 15197, "messageCount": 12, "timestamp": "2026-09-28T00:00:00Z",
    },
    "file-history-snapshot": {
        "type": "file-history-snapshot",
        "messageId": "m", "snapshot": {"messageId": "m", "trackedFileBackups": {}},
        "isSnapshot": True,
    },
    "permission-mode": {"type": "permission-mode", "permissionMode": "auto", "sessionId": "s"},
    "bridge-session": {"type": "bridge-session", "sessionId": "s", "bridgeSessionId": "b",
                        "ownerAccountUuid": "acct", "ownerOrganizationId": "org"},
    "ai-title": {"type": "ai-title", "aiTitle": "Some session", "sessionId": "s"},
}


def test_ignored_types_are_dropped_without_warned(tmp_path):
    """Billing and session config carry no decision narrative, so no warning."""
    folder = tmp_path / "app"
    folder.mkdir()
    snapshot = parse_with(folder, [UNPARSED_FIXTURES["token_usage_record"],
                                   UNPARSED_FIXTURES["event_msg:thread_settings_applied"]])
    assert not any("unsupported record" in w for w in snapshot.limitations), snapshot.limitations
    assert not any("not sent to analysis" in w for w in snapshot.limitations), snapshot.limitations


def test_every_ignored_type_has_a_written_reason():
    for name, reason in IGNORED_NO_ANALYSIS_VALUE.items():
        assert reason.strip(), f"{name} is ignored without a stated reason"
        assert not reason.strip().lower().startswith("todo")


def test_every_deferred_type_has_a_written_reason():
    for name, reason in KNOWN_UNPARSED.items():
        assert reason.strip(), f"{name} is deferred without a stated reason"


def test_world_state_is_reported_once_with_a_count(tmp_path):
    """`world_state` holds a verbatim copy of AGENTS.md, so a gap must be visible."""
    folder = tmp_path / "app"
    folder.mkdir()
    snapshot = parse_with(folder, [UNPARSED_FIXTURES["world_state"],
                                   UNPARSED_FIXTURES["world_state"]])
    reported = [w for w in snapshot.limitations if "world_state" in w]
    assert len(reported) == 1, snapshot.limitations
    assert "2 records" in reported[0]
    assert "not sent to analysis" in reported[0]


def test_an_unknown_type_still_warns_as_unsupported(tmp_path):
    """The regression this module exists for: a new upstream type must be loud."""
    folder = tmp_path / "app"
    folder.mkdir()
    snapshot = parse_with(folder, [{"type": "brand_new_upstream_type", "payload": {"x": 1}}])
    unsupported = [w for w in snapshot.limitations if "unsupported record" in w]
    assert len(unsupported) == 1, snapshot.limitations
    assert "brand_new_upstream_type" in unsupported[0]


def test_the_three_buckets_do_not_overlap():
    assert not (set(IGNORED_NO_ANALYSIS_VALUE) & set(KNOWN_UNPARSED))


@pytest.mark.parametrize("name", sorted(KNOWN_UNPARSED))
def test_deferred_fixture_matches_its_declared_name(name):
    """Guards the fixture table against drifting from the registry keys."""
    row = UNPARSED_FIXTURES[name]
    observed = row["type"]
    if row["type"] == "event_msg":
        observed = "event_msg:" + row["payload"]["type"]
    if row["type"] == "system":
        observed = "system:" + row["subtype"]
    assert observed == name


def test_every_fixture_maps_to_a_registry_entry():
    """A fixture naming a type the parser never met would pass silently."""
    registry = set(IGNORED_NO_ANALYSIS_VALUE) | set(KNOWN_UNPARSED)
    for name in list(UNPARSED_FIXTURES) + list(IGNORED_CLAUDE_FIXTURES):
        assert name in registry, f"{name} has a fixture but no registry entry"


def test_no_record_type_is_silently_dropped(tmp_path):
    """The invariant the split exists to enforce.

    Every record type either becomes a record, or is classified. Walk both
    parsers over one log per declared type and require that nothing lands in
    `unknown` unless it is genuinely unsupported. Also assert the known-read
    types still produce records, so a parser that dropped everything would fail
    here rather than pass.
    """
    from projectflow.sources.local import parse_claude
    codex_types = {
        "token_usage_record": {"type": "token_usage_record", "payload": {}},
        "event_msg:thread_settings_applied": {
            "type": "event_msg", "payload": {"type": "thread_settings_applied"}},
        "world_state": {"type": "world_state", "payload": {}},
    }
    claude_types = {k: v for k, v in IGNORED_CLAUDE_FIXTURES.items()}
    claude_types.update({k: v for k, v in UNPARSED_FIXTURES.items()
                         if k in ("queue-operation", "system:stop_hook_summary", "system:local_command")})
    folder = tmp_path / "app"
    folder.mkdir()
    cpath = tmp_path / "rollout.jsonl"
    write(cpath, base_rows(folder) + list(codex_types.values()))
    codex_snapshot = parse_codex(cpath, Scope.resolve(folder))
    unsupported = [w for w in codex_snapshot.limitations if "unsupported record" in w]
    assert not unsupported, f"classified types must not warn as unsupported: {unsupported}"
    # The user message from base_rows must survive; dropping it silently is the
    # failure this assertion exists to catch.
    assert len(codex_snapshot.records) == 1, [r.role for r in codex_snapshot.records]

    apath = tmp_path / "session.jsonl"
    write(apath, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                   "message": {"role": "user", "content": "고쳐줘."}}]
           + list(claude_types.values()))
    claude_snapshot = parse_claude(apath, Scope.resolve(folder))
    unsupported = [w for w in claude_snapshot.limitations if "unsupported record" in w]
    assert not unsupported, f"classified types must not warn as unsupported: {unsupported}"
    assert len(claude_snapshot.records) == 1, [r.content for r in claude_snapshot.records]


@pytest.mark.parametrize("malformed", [["not", "a", "dict"], [], "", {}, 0, "text"])
def test_a_malformed_attachment_is_reported_not_raised(tmp_path, malformed):
    """Any attachment shape we cannot read must warn, including falsey ones.

    A truthy non-dict attachment used to reach .get() and raise; a falsey one
    used to disappear silently. Keyed on presence, not truthiness.
    """
    from projectflow.sources.local import parse_claude
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "session.jsonl"
    write(path, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                  "message": {"role": "user", "content": "고쳐줘."}},
                 {"type": "attachment", "cwd": str(folder), "uuid": "u2",
                  "attachment": malformed}])
    snapshot = parse_claude(path, Scope.resolve(folder))   # must not raise
    assert any("attachment:" in w and "unsupported record" in w for w in snapshot.limitations), \
        f"{malformed!r} should be reported: {snapshot.limitations}"
    # A warning alone is not enough: the unread shape must also not have become
    # a record carrying the raw payload.
    assert [r for r in snapshot.records if "attachment" in r.lineage] == [], \
        [r.content for r in snapshot.records]


def test_a_summary_carrying_system_record_still_becomes_a_record(tmp_path):
    """Regression guard.

    Classifying every system row without `isCompactSummary` also dropped rows
    that carry summary text, which the `{"summary", "system"}` branch turns
    into a compaction record.
    """
    from projectflow.sources.local import parse_claude
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "session.jsonl"
    write(path, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                  "message": {"role": "user", "content": "고쳐줘."}},
                 {"type": "system", "subtype": "some_future_kind", "cwd": str(folder),
                  "summary": "Earlier work: fixed the installer", "uuid": "u2"}])
    snapshot = parse_claude(path, Scope.resolve(folder))
    assert any("fixed the installer" in r.content for r in snapshot.records), \
        [r.content for r in snapshot.records]
    assert not any("some_future_kind" in w for w in snapshot.limitations)



def test_deferred_warning_labels_differ_for_same_basename_deeper_paths(tmp_path):
    from projectflow.sources.local import _warning_path
    a = tmp_path / "sessions" / "2026" / "01" / "01" / "rollout.jsonl"
    b = tmp_path / "sessions" / "2026" / "02" / "01" / "rollout.jsonl"
    assert _warning_path(a) != _warning_path(b)
    c = tmp_path / "sessions" / "2026" / "01" / "01" / "other.jsonl"
    assert _warning_path(a) != _warning_path(c)


def test_compact_boundary_still_becomes_a_record(tmp_path):
    """Regression guard.

    Classifying every non-summary `system` record once killed the
    `compact_boundary` path, which emits a record that marks a work-unit
    boundary. Losing it changes work-unit segmentation, not just a warning.
    Checked twice: with a summary, and with no summary so the
    "Compaction boundary" fallback text and the compaction lineage are used.
    """
    from projectflow.sources.local import parse_claude
    for label, row in (
            ("with summary", {"summary": "Compaction boundary"}),
            ("no summary", {})):
        folder = tmp_path / ("app-" + label.replace(" ", "-"))
        folder.mkdir()
        path = tmp_path / f"session-{label.replace(' ', '-')}.jsonl"
        write(path, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                      "message": {"role": "user", "content": "고쳐줘."}},
                     {"type": "system", "subtype": "compact_boundary", "cwd": str(folder),
                      "uuid": "u2", **row}])
        snapshot = parse_claude(path, Scope.resolve(folder))
        boundary = [r for r in snapshot.records if "Compaction" in r.content]
        assert boundary, f"{label}: no boundary record in {[r.content for r in snapshot.records]}"
        # The boundary is only useful downstream if it is marked as a compaction.
        assert boundary[0].derivation == "summary", boundary[0]
        assert boundary[0].lineage.get("kind") == "compaction", boundary[0].lineage
        assert not any("compact_boundary" in w for w in snapshot.limitations), \
            f"{label}: compact_boundary is read, so it must not be reported as unparsed"


def test_compact_boundary_is_not_in_the_unread_registry():
    assert "system:compact_boundary" not in KNOWN_UNPARSED
    assert "system:compact_boundary" not in IGNORED_NO_ANALYSIS_VALUE


def test_bridge_session_account_identifiers_are_never_records(tmp_path):
    from projectflow.sources.local import parse_claude
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "session.jsonl"
    write(path, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                  "message": {"role": "user", "content": "고쳐줘."}},
                 IGNORED_CLAUDE_FIXTURES["bridge-session"]])
    snapshot = parse_claude(path, Scope.resolve(folder))
    blob = " ".join(r.content for r in snapshot.records) + " ".join(snapshot.limitations)
    assert "acct" not in blob and "org" not in blob


def test_claude_ignored_types_are_dropped_without_warned(tmp_path):
    """Same contract for the Claude parser, which had no coverage at all."""
    from projectflow.sources.local import parse_claude
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "session.jsonl"
    write(path, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                  "message": {"role": "user", "content": "설치 스크립트를 고쳐줘."}}]
               + [IGNORED_CLAUDE_FIXTURES[k] for k in
                  ("system:turn_duration", "file-history-snapshot", "permission-mode",
                   "bridge-session", "ai-title")])
    snapshot = parse_claude(path, Scope.resolve(folder))
    assert not any("unsupported record" in w for w in snapshot.limitations), snapshot.limitations
    assert not any("not sent to analysis" in w for w in snapshot.limitations), snapshot.limitations
    # The account identifier must never become a record we could send anywhere.
    assert all("acct" not in r.content for r in snapshot.records)


def test_claude_queue_operation_and_stop_hook_are_reported(tmp_path):
    from projectflow.sources.local import parse_claude
    folder = tmp_path / "app"
    folder.mkdir()
    path = tmp_path / "session.jsonl"
    write(path, [{"type": "user", "cwd": str(folder), "uuid": "u1",
                  "message": {"role": "user", "content": "설치 스크립트를 고쳐줘."}}]
               + [UNPARSED_FIXTURES["queue-operation"],
                  UNPARSED_FIXTURES["system:stop_hook_summary"],
                  UNPARSED_FIXTURES["system:local_command"]])
    snapshot = parse_claude(path, Scope.resolve(folder))
    for name in ("queue-operation", "stop_hook_summary", "local_command"):
        assert any(name in w and "not sent to analysis" in w for w in snapshot.limitations), \
            f"{name} must be reported: {snapshot.limitations}"


def test_two_files_with_the_same_deferred_count_do_not_collapse(tmp_path):
    """collect_logs dedupes identical warning strings, so the path must be in it.

    Uses the same basename in two different session directories, which is the
    case a bare `path.name` would still merge.
    """
    from projectflow.sources.local import collect_logs
    home = tmp_path / "codex"
    folder = tmp_path / "app"
    folder.mkdir()
    for session in ("2026-01-01", "2026-02-02"):
        write(home / "sessions" / session / "rollout.jsonl",
              [{"type": "session_meta", "payload": {"id": "s-" + session, "cwd": str(folder)}},
               {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                    "content": [{"type": "input_text", "text": "고쳐줘."}]}},
               UNPARSED_FIXTURES["world_state"]])
    snapshot = collect_logs(codex_home=home, claude_home=tmp_path / "claude",
                            scope=Scope.resolve(folder))
    hits = [w for w in snapshot.limitations if "world_state" in w]
    assert len(hits) == 2, snapshot.limitations
    assert "2026-01-01" in hits[0] and "2026-02-02" in hits[1], hits


def test_record_shapes_used_here_are_valid_json_lines(tmp_path):
    """Every fixture must survive the parser's own JSONL reader."""
    folder = tmp_path / "app"
    folder.mkdir()
    path = folder / "rollout.jsonl"
    write(path, base_rows(folder) + list(UNPARSED_FIXTURES.values()))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(base_rows(folder)) + len(UNPARSED_FIXTURES)
    for line in lines:
        json.loads(line)
