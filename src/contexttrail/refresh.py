"""`contexttrail refresh`: a graph built under older rules brought up to today's, by code alone.

No model is called and nothing is sent, so no consent is asked. A scan reads the transcripts (`Engine.scan`) and
stores what it read, so every record has its full content and lineage as today's readers give them; then, on the
stored graph:

1. every event's `authoring` and `step` are read again from the records it cites (`store.derive`, which every
   publish runs too: an older graph's events keep what the records said when they were last published, so a Codex
   sub-agent first stored under its random nickname, `Dirac`, is named by its task, `plots`, once read again);
2. a person's event resting only on user-role messages that today's rules say the person did not type
   (`model.is_user_prompt`: harness text, the Codex app's plugin list, a host control command such as `/compact`, an
   image note alone, a queued message, a sub-agent's or an unattended run's prompt) is set aside: status `withdrawn`
   with an `invalidation` (`schema.retired`), its relations kept, no request any more in any view; one set aside
   earlier whose messages read as the person's again is given back its status;
3. the graph-wide steps of a publish run once more (`store.derive`: fork links, delegation links, folded reports).

Titles, summaries, statuses of checks and the relations a model wrote are left as they are. The result is published
as one new graph version under the analysis lock (refused while an analysis runs), and nothing is published when
nothing changed. The run is recorded with trigger `refresh`; it is not the screens' last check.
"""
from __future__ import annotations

import copy
import uuid
from typing import Callable

from .analysis import Engine
from .i18n import tr
from .model import SourceRecord, is_user_prompt
from .schema import DELEGATED, DELEGATION_ORIGIN, FOLDED, FORK_ORIGIN, retired
from .util import ident, now

TRIGGER = "refresh"
RETIRED_REASON = "not the person's request under the current rules"
# The person's events a refresh reads: requests, and goals or proposals of graphs written before code made requests.
REQUEST_KINDS = ("question", "goal", "proposal")


def not_the_persons(records: list[SourceRecord | None]) -> bool | None:
    """Whether today's rules say none of these user-role messages is one the person typed: True to set the event
    aside, False to keep it, None when it cannot be told (a record not read by this scan, a later piece of a long
    message, whose first piece decides)."""
    if not records or any(record is None for record in records):
        return None
    if any(((record.lineage or {}).get("fragment_index") or 1) > 1 for record in records):
        return None
    return all(record.role == "user" and not is_user_prompt(record) for record in records)


def retire_requests(graph: dict, records_of: Callable[[dict], list[SourceRecord | None]], run_id: str) -> dict[str, list[str]]:
    """Set aside, in place, the person's events that today's rules say rest on no message of the person's, and give
    back one set aside before whose messages read as the person's again. Returns the IDs: `retired`, `restored`, and
    `unread` (a person's event whose records this scan did not read, left as it is)."""
    found: dict[str, list[str]] = {"retired": [], "restored": [], "unread": []}
    for event in graph["events"]:
        if event.get("actor") != "user" or event["kind"] not in REQUEST_KINDS:
            continue
        verdict = not_the_persons(records_of(event))
        if verdict is None:
            found["unread"].append(event["id"])
        elif verdict and not retired(event):
            event["invalidation"] = {"reason": RETIRED_REASON, "by": TRIGGER, "run_id": run_id,
                                     "status_before": event["status"]}
            event["status"] = "withdrawn"
            found["retired"].append(event["id"])
        elif not verdict and retired(event) and event["invalidation"].get("by") == TRIGGER:
            event["status"] = event.pop("invalidation").get("status_before") or "asked"
            found["restored"].append(event["id"])
    return found


def changes(before: dict, after: dict) -> dict[str, int]:
    """What a refresh changed, counted per kind of field it may touch."""
    old = {event["id"]: event for event in before["events"]}
    old_edges = {edge["id"]: edge for edge in before["edges"]}

    def agents(event: dict) -> set[str]:
        return {agent.get("agent") or "" for agent in event.get("authoring") or [] if agent.get("agent")}

    def differ(key: str) -> int:
        return sum(old[event["id"]].get(key) != event.get(key) for event in after["events"] if event["id"] in old)

    renamed = sum(bool(agents(old[event["id"]])) and agents(old[event["id"]]) != agents(event)
                  for event in after["events"] if event["id"] in old)
    added = [edge for edge in after["edges"] if edge["id"] not in old_edges]
    return {"authoring_changed": differ("authoring"), "agents_renamed": renamed, "steps_changed": differ("step"),
            "delegations_paired": differ(DELEGATED), "delegated_to_changed": differ("delegated_to"),
            "reports_folded": differ(FOLDED),
            "delegation_links_added": sum(edge.get("origin") == DELEGATION_ORIGIN for edge in added),
            "fork_links_added": sum(edge.get("origin") == FORK_ORIGIN for edge in added),
            "turn_links_replaced": sum(old_edges[edge["id"]]["active"] and not edge["active"]
                                       for edge in after["edges"] if edge["id"] in old_edges)}


def _comparable(graph: dict) -> dict:
    return {key: value for key, value in graph.items() if key not in ("version", "analyzed_at", "refreshed_at")}


def refresh(engine: Engine) -> dict:
    """Scan, bring the stored graph up to today's rules and publish it when anything changed (see the module)."""
    store = engine.store
    run_id = ident("refresh_", engine.scope.id, now(), uuid.uuid4().hex)
    with store.analyze_lock():
        store.start_run(run_id, {"scope_id": engine.scope.id, "trigger": TRIGGER})
        try:
            snapshot = engine.scan()
            # Stored as a scan stores them, so what publishing reads off the records is what this scan read.
            store.ingest(snapshot.records)
            store.acknowledge_environment_context(snapshot.records)
            store.acknowledge_journaled(snapshot.records)
            before = store.graph()
            pool = {record.source_id: record for record in snapshot.records}
            asked = [event for event in before["events"] if event.get("actor") == "user"]
            items = store.evidence_many([i for event in asked for i in event.get("evidence_ids", [])])

            def records_of(event: dict) -> list[SourceRecord | None]:
                return [pool.get((items.get(i) or {}).get("source_id")) for i in event.get("evidence_ids", [])]

            graph = copy.deepcopy(before)
            requests = retire_requests(graph, records_of, run_id)
            after = store.derived(graph)
            counts = {**changes(before, after), "requests_retired": len(requests["retired"]),
                      "requests_restored": len(requests["restored"]), "requests_unread": len(requests["unread"])}
            changed = _comparable(after) != _comparable(before)
            version = before["version"]
            if changed:
                version = store.publish(graph, [], {}, {}, expected_version=before["version"], refreshed=True)["version"]
            result = {"status": "refreshed" if changed else "noop", "run_id": run_id, "runner_calls": 0,
                      "graph_version": version, "previous_version": before["version"], "events": len(after["events"]),
                      "counts": counts, "retired": requests["retired"], "restored": requests["restored"]}
            store.finish_run(run_id, result["status"], {key: value for key, value in result.items()
                                                        if key not in ("retired", "restored")}, check=False)
            return result
        except BaseException as exc:
            store.finish_run(run_id, "failed", {"run_id": run_id, "error": str(exc) or type(exc).__name__}, check=False)
            raise


def summary_lines(result: dict) -> list[str]:
    """`contexttrail refresh` in a few lines, in the screen language."""
    counts = result["counts"]
    unread = [tr(f"  기록을 읽지 못해 그대로 둔 사람의 사건 {counts['requests_unread']}건",
                 f"  the person's events left as they are, their records not read: {counts['requests_unread']}")
              ] if counts["requests_unread"] else []
    if result["status"] == "noop":
        return [tr(f"바뀐 것 없음: 그래프 v{result['graph_version']}은 이미 지금 규칙대로입니다. AI 호출 없음.",
                   f"Nothing changed: graph v{result['graph_version']} already follows the current rules. No AI calls."),
                *unread]
    lines = [tr(f"그래프 v{result['previous_version']} → v{result['graph_version']} (지금 규칙으로 다시 읽음, AI 호출 없음)",
                f"Graph v{result['previous_version']} → v{result['graph_version']} (read again under the current rules, no AI calls)"),
             tr(f"  작성자 다시 읽음 {counts['authoring_changed']}건 (에이전트 이름 바뀜 {counts['agents_renamed']}건)",
                f"  authoring read again: {counts['authoring_changed']} events ({counts['agents_renamed']} agents renamed)"),
             tr(f"  단계(검사·문서·위임) 바뀜 {counts['steps_changed']}건", f"  steps (check, doc, delegate) changed: {counts['steps_changed']}"),
             tr(f"  사람의 요청이 아니라서 내려놓은 요청 {counts['requests_retired']}건",
                f"  requests set aside as not the person's: {counts['requests_retired']}"),
             tr(f"  위임 연결 추가 {counts['delegation_links_added']}건 (짝지은 하위 작업 {counts['delegations_paired']}건)",
                f"  delegation links added: {counts['delegation_links_added']} ({counts['delegations_paired']} sub-agent events paired)")]
    if counts["requests_restored"]:
        lines.append(tr(f"  다시 요청으로 돌린 것 {counts['requests_restored']}건",
                        f"  requests given back: {counts['requests_restored']}"))
    return lines + unread
