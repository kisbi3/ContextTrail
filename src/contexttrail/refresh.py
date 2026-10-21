"""`contexttrail refresh`: a graph built under older rules brought up to today's, by code alone.

No model is called and nothing is sent, so no consent is asked. A scan reads the transcripts (`Engine.scan`) and
stores what it read, so every record has its full content and lineage as today's readers give them; then, on the
stored graph:

1. every event's `authoring` and `step` are read again from the records it cites (`store.derive`, which every
   publish runs too: an older graph's events keep what the records said when they were last published, so a Codex
   sub-agent first stored under its random nickname, `Dirac`, is named by its task, `plots`, once read again);
2. a person's event resting only on user-role messages that today's rules say the person did not type
   (`model.is_user_prompt`: harness text, the Codex app's plugin list, a host control command such as `/compact`, the
   `[Image: original …]` note alone that Claude Code adds after the agent reads an image file, a queued message, a
   sub-agent's or an unattended run's prompt) is set aside: status `withdrawn` with an `invalidation`
   (`schema.retired`), its relations kept, no request any more in any view; one set aside earlier whose messages read
   as the person's again is given back its status (`read_as`, `retire_requests`). The person's answer to the agent's
   own question is theirs: when every answer is one of the options the agent offered, it becomes their decision as
   `analysis.link_request_turns` makes one (relations kept), else it is left as it is. A later piece of a long
   message is read with its first piece; an event citing only later pieces, or records this scan did not read, is
   left as it is and counted;
3. the work that only a dialog-turn link from a set-aside event reaches is hung again as a fresh publish would hang it
   (`rehang`, by `analysis.turn_link`), the old link set inactive;
4. the graph-wide steps of a publish run once more (`store.derive`: fork links, delegation links, folded reports).

Titles, summaries, statuses of checks and the relations a model wrote are left as they are (a choice made the
person's decision gets the title and summary code gives one). The result is published
as one new graph version under the analysis lock (refused while an analysis runs), and nothing is published when
nothing changed. The run is recorded with trigger `refresh`; it is not the screens' last check.
"""
from __future__ import annotations

import copy
import uuid
from typing import Callable

from .analysis import (Engine, choice_fields, chosen_options, offered_options, prompt_nodes, turn_link,
                       turn_messages)
from .i18n import tr
from .model import SourceRecord, is_user_prompt, question_reply
from .schema import DELEGATED, DELEGATION_ORIGIN, FOLDED, FORK_ORIGIN, retired
from .util import ident, now

TRIGGER = "refresh"
RETIRED_REASON = "not the person's request under the current rules"
# The person's events a refresh reads: requests, and goals or proposals of graphs written before code made requests.
REQUEST_KINDS = ("question", "goal", "proposal")


UNREAD, PIECE, REPLY, CHOICE, THEIRS, NOT_THEIRS = "unread", "piece", "reply", "choice", "theirs", "not_theirs"


def _piece(record: SourceRecord) -> int:
    return (record.lineage or {}).get("fragment_index") or 1


def whole_messages(records: list[SourceRecord]) -> list[SourceRecord]:
    """The cited records less the later pieces of a long message whose first piece is cited too (`segment_record`:
    a split message is read at its first piece, the rest is its continuation)."""
    firsts = {(record.lineage or {}).get("fragment_of") for record in records if _piece(record) == 1}
    return [record for record in records
            if _piece(record) == 1 or (record.lineage or {}).get("fragment_of") not in firsts]


def read_as(records: list[SourceRecord | None],
            offered: Callable[[SourceRecord], dict[str, tuple[str, ...]]]) -> tuple[str, list[dict]]:
    """How today's rules read the user-role messages a person's event cites, and the person's choices when they are
    some:

    - `unread`: a record this scan did not read (its transcript is gone): left as it is;
    - `piece`: only a later piece of a long message is cited, never its first, which decides: left as it is;
    - `choice`: every message is the person's answer to the agent's own question (`model.question_reply`) and every
      answer is one of the options the agent offered (`offered`, `analysis.offered_options`): the person's decision,
      as `analysis.link_request_turns` makes it;
    - `reply`: an answer to the agent's question otherwise (a free answer): the person's words, left as it is;
    - `not_theirs`: no message is one the person typed (`model.is_user_prompt`): set aside;
    - `theirs`: otherwise."""
    if not records or any(record is None for record in records):
        return UNREAD, []
    records = whole_messages(records)
    if any(_piece(record) > 1 for record in records):
        return PIECE, []
    replies = [question_reply(record) for record in records]
    if any(reply is not None for reply in replies):
        if not all(replies):
            return REPLY, []
        chosen = [answer for record, reply in zip(records, replies) for answer in chosen_options(reply, offered(record))]
        return (CHOICE, chosen) if len(chosen) == sum(map(len, replies)) else (REPLY, [])
    return (NOT_THEIRS if all(record.role == "user" and not is_user_prompt(record) for record in records)
            else THEIRS), []


def _restore(event: dict) -> None:
    event["status"] = event.pop("invalidation").get("status_before") or "asked"


def retire_requests(graph: dict, records_of: Callable[[dict], list[SourceRecord | None]], run_id: str,
                    offered: Callable[[SourceRecord], dict[str, tuple[str, ...]]] = lambda record: {}
                    ) -> dict[str, list[str]]:
    """Set aside, in place, the person's events that today's rules say rest on no message of the person's, make the
    person's choice among the agent's options their decision (relations kept), and give back one set aside before
    whose messages read as the person's again. Returns the IDs: `retired`, `chosen`, `restored`, and those left as
    they are because their records were not read by this scan (`unread`) or only a later piece of a message is
    cited (`pieces`)."""
    found: dict[str, list[str]] = {"retired": [], "chosen": [], "restored": [], "unread": [], "pieces": []}
    for event in graph["events"]:
        if event.get("actor") != "user" or event["kind"] not in REQUEST_KINDS:
            continue
        verdict, chosen = read_as(records_of(event), offered)
        by_refresh = retired(event) and event["invalidation"].get("by") == TRIGGER
        if verdict == UNREAD:
            found["unread"].append(event["id"])
        elif verdict == PIECE:
            found["pieces"].append(event["id"])
        elif verdict == NOT_THEIRS:
            if not retired(event):
                event["invalidation"] = {"reason": RETIRED_REASON, "by": TRIGGER, "run_id": run_id,
                                         "status_before": event["status"]}
                event["status"] = "withdrawn"
                found["retired"].append(event["id"])
        elif verdict == CHOICE:
            if by_refresh:
                _restore(event)
            event.update(choice_fields(chosen))
            found["chosen"].append(event["id"])
        elif by_refresh:
            _restore(event)
            found["restored"].append(event["id"])
    return found


REHUNG_REASON = "the message it hung from is no request of the person's under the current rules"


def rehang(graph: dict, pool: dict[str, SourceRecord], evidence: dict[str, dict], run_id: str,
           chosen: frozenset[str] | set[str] = frozenset()) -> int:
    """Hang again, in place, the work that only a dialog-turn link from a retired event reaches, by the rule
    `analysis.link_request_turns` uses for a new event (`analysis.turn_link`: the last message of the person's at or
    before it in its session, or for a sub-agent the parent session's last one before it), so the graph reads as a
    fresh publish would. The old link is set inactive with `invalidation.replaced_by` (none when no message of the
    person's has an event there). A link set inactive so whose source is given back is made active again, and the
    link that replaced it inactive. A person's choice made their decision (`chosen`) that nothing reaches hangs in
    the turn of the request it continues, as `link_request_turns` hangs a new one. Returns how many events were hung
    again (the choices not counted)."""
    by_id = {event["id"]: event for event in graph["events"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    for edge in graph["edges"]:
        undone = edge.get("invalidation") or {}
        source = by_id.get(edge["from_event_id"])
        if edge["active"] or undone.get("reason") != REHUNG_REASON or source is None or retired(source):
            continue
        edge["active"] = True
        edge.pop("invalidation")
        replacement = edges.get(undone.get("replaced_by") or "")
        if replacement and replacement["active"] and replacement.get("origin") == "dialog_turn":
            replacement["active"] = False
            replacement["invalidation"] = {"reason": "the message it replaced reads as the person's again",
                                           "evidence_ids": [], "replaced_by": edge["id"], "by": TRIGGER,
                                           "run_id": run_id}
    set_aside = {event["id"] for event in graph["events"] if retired(event)}
    incoming: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        if edge["active"]:
            incoming.setdefault(edge["to_event_id"], []).append(edge)
    position = {source_id: n for n, source_id in enumerate(pool)}
    by_session = turn_messages(pool)
    nodes = prompt_nodes([event for event in graph["events"] if not retired(event)], pool, evidence)
    moved = 0
    for event in graph["events"]:
        hangs = incoming.get(event["id"], [])
        unreached = event["id"] in chosen and not hangs
        if not unreached and (event["id"] in set_aside or not hangs or not all(
                edge.get("origin") == "dialog_turn" and edge["from_event_id"] in set_aside for edge in hangs)):
            continue
        cited = [source_id for source_id in ((evidence.get(i) or {}).get("source_id") for i in event["evidence_ids"])
                 if source_id in position]
        if not cited:
            continue  # its records were not read by this scan: left as it is
        link = turn_link(event, cited, pool, position, by_session, nodes)
        if link is not None:
            if link["id"] in edges:
                edges[link["id"]]["active"] = True
                edges[link["id"]].pop("invalidation", None)
            else:
                graph["edges"].append(link)
                edges[link["id"]] = link
        for edge in hangs:
            edge["active"] = False
            edge["invalidation"] = {"reason": REHUNG_REASON, "evidence_ids": [], "by": TRIGGER, "run_id": run_id,
                                    **({"replaced_by": link["id"]} if link else {})}
        moved += not unreached
    return moved


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
            items = store.evidence_many([i for event in before["events"] for i in event.get("evidence_ids", [])])

            def records_of(event: dict) -> list[SourceRecord | None]:
                return [pool.get((items.get(i) or {}).get("source_id")) for i in event.get("evidence_ids", [])]

            graph = copy.deepcopy(before)
            requests = retire_requests(graph, records_of, run_id,
                                       lambda reply: offered_options(reply, snapshot.records))
            moved = rehang(graph, pool, items, run_id, set(requests["chosen"]))
            after = store.derived(graph)
            counts = {**changes(before, after), "requests_retired": len(requests["retired"]),
                      "requests_restored": len(requests["restored"]), "choices_made_decisions": len(requests["chosen"]),
                      "turn_links_moved": moved, "requests_unread": len(requests["unread"]),
                      "requests_piece_only": len(requests["pieces"])}
            changed = _comparable(after) != _comparable(before)
            version = before["version"]
            if changed:
                version = store.publish(graph, [], {}, {}, expected_version=before["version"], refreshed=True)["version"]
            result = {"status": "refreshed" if changed else "noop", "run_id": run_id, "runner_calls": 0,
                      "graph_version": version, "previous_version": before["version"], "events": len(after["events"]),
                      "counts": counts, "retired": requests["retired"], "restored": requests["restored"],
                      "chosen": requests["chosen"]}
            store.finish_run(run_id, result["status"], {key: value for key, value in result.items()
                                                        if key not in ("retired", "restored", "chosen")}, check=False)
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
    if counts["requests_piece_only"]:
        unread.append(tr(f"  긴 메시지의 뒷부분만 인용해 판단하지 못하고 그대로 둔 사람의 사건 {counts['requests_piece_only']}건",
                         f"  the person's events left as they are, citing only a later piece of a long message: "
                         f"{counts['requests_piece_only']}"))
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
    if counts["choices_made_decisions"]:
        lines.append(tr(f"  에이전트가 제시한 선택지에서 고른 답을 사람의 결정으로 바꿈 {counts['choices_made_decisions']}건",
                        f"  answers among the agent's options made the person's decisions: {counts['choices_made_decisions']}"))
    if counts["turn_links_moved"]:
        lines.append(tr(f"  내려놓은 요청 아래 있던 작업을 다시 매단 것 {counts['turn_links_moved']}건",
                        f"  work hung again from under a request set aside: {counts['turn_links_moved']}"))
    if counts["requests_restored"]:
        lines.append(tr(f"  다시 요청으로 돌린 것 {counts['requests_restored']}건",
                        f"  requests given back: {counts['requests_restored']}"))
    return lines + unread
