"""A task handed to another agent is a work item of its own, and that agent's work hangs under it."""
import json
from dataclasses import replace

from contexttrail.analysis import delegated_path, mark_steps
from contexttrail.demo import FixtureRunner
from contexttrail.diagram import flow_diagram
from contexttrail.light import delegation_items, story_extraction, story_request, written_blocks
from contexttrail.render import event_detail, status_labels
from contexttrail.schema import EvidenceValidator, fill_steps, link_delegations


def codex_parent(make):
    def tool(key, role, text, call_id):
        return replace(make(text, key=key, role=role, provider="codex", session="root-1"), tool_call_id=call_id)
    return [
        make("그림을 따로 맡겨 주세요.", key="ask", provider="codex", session="root-1"),
        tool("spawn", "tool_call", "Tool: spawn_agent\n" + json.dumps(
            {"task_name": "plots", "message": "Draw the impact plots\nfrom the saved table."}), "c1"),
        tool("spawn-out", "tool_result", json.dumps({"task_name": "/root/plots"}), "c1"),
        tool("again", "tool_call", "Tool: followup_task\n" + json.dumps({"target": "plots", "message": "Redo the axes."}), "c2"),
        tool("again-out", "tool_result", "", "c2"),
        tool("wait", "tool_call", "Tool: wait_agent\n" + json.dumps({"timeout_ms": 60000}), "c3"),
        tool("wait-out", "tool_result", "done", "c3"),
        make("그림 작업을 하위 에이전트에 맡겼습니다.", key="said", role="assistant", provider="codex", session="root-1"),
    ]


def test_a_scan_names_the_codex_agent_a_delegation_gave_its_task_to(laboratory):
    _, _, _, _, make = laboratory
    found = codex_parent(make)
    mark_steps(found)
    step = {record.source_id: record.step for record in found}
    # The spawn's result names the path; a follow-up's target is under the caller's own path (the root's).
    assert step["spawn"] == step["spawn-out"] == step["again"] == {"hint": "delegate", "agent": "/root/plots"}
    assert step["wait"] == {"hint": "delegate"}  # waiting hands nothing over
    nested = replace(found[3], lineage={"kind": "subagent", "parent_session_id": "root-1", "agent_path": "/root/plots"})
    assert delegated_path(nested, None) == "/root/plots/plots"
    claude = replace(make("Tool: Agent\n" + json.dumps({"subagent_type": "Explore", "description": "Find the parser"}),
                          key="agent", role="tool_call"), tool_call_id="toolu_1")
    assert delegated_path(claude, None) is None  # a Claude Code sub-agent names the call itself


def test_each_task_handed_over_is_a_work_item_the_story_names_and_a_decision_can_point_at(laboratory):
    _, _, _, _, make = laboratory
    found = codex_parent(make)
    refused = [replace(make("Tool: Agent\n" + json.dumps({"subagent_type": "Explore", "prompt": "Look"}),
                            key="bad", role="tool_call"), tool_call_id="toolu_9"),
               replace(make("Error: no such agent type", key="bad-out", role="tool_result"), tool_call_id="toolu_9")]
    items = delegation_items(found + refused)
    assert [(i["id"], i["agent"], i["task"]) for i in items] == [
        ("D1", "plots", "Draw the impact plots from the saved table."), ("D2", "plots", "Redo the axes.")]
    blocks = written_blocks(found)
    unit = {"id": "u", "sources": [r.source_id for r in found]}
    request = story_request(unit, "snap", {"user_requests": [], "existing_events": []}, items, found, blocks)
    entry = request["work"][0]
    assert entry["kind"] == "delegation" and entry["delegated_to"] == "plots" and "status" not in entry
    answer = {"work": [{"id": "D1", "title": "", "summary": "", "checked_by": [{"run": "D2", "blocks": [blocks[1]["id"]]}]}],
              "story": [{"id": "N1", "kind": "decision", "status": "adopted", "title": "그림은 하위 에이전트에",
                         "why": None, "summary": "", "blocks": [blocks[1]["id"]], "work": ["D1"]}],
              "requests": [], "links": [], "turn_summary": None}
    pool = {r.source_id: r for r in found}
    provided = {r.source_id: [(1, len(r.content.splitlines()))] for r in found}
    output, _ = story_extraction(answer, items, [], unit, "snap", pool, provided, EvidenceValidator(pool, provided),
                                 blocks, [], "Korean")
    by_id = {c["id"]: c for c in output["event_candidates"]}
    assert by_id["tmp:D1"]["title"] == "위임: plots — Draw the impact plots from the saved table."
    assert (by_id["tmp:D1"]["kind"], by_id["tmp:D1"]["status"]) == ("action", "applied")
    assert any((e["from_event_id"], e["to_event_id"], e["relation"]) == ("tmp:N1", "tmp:D1", "motivates")
               for e in output["edge_candidates"])
    # A delegation is not an edit: no check of it is kept.
    assert not any(e["relation"] == "verifies" for e in output["edge_candidates"])


def source(session, role, *, at, call=None, step=None, lineage=None):
    return {"provider": "codex", "session_id": session, "role": role, "recorded_at": at, "tool_call_id": call,
            "step": step or {}, "lineage": lineage or {}}


def event(key, kind, status, at, actor="assistant"):
    return {"id": key, "kind": kind, "status": status, "title": key, "summary": "", "actor": actor,
            "evidence_ids": [f"e_{key}"], "session_ids": [], "recorded_at": at}


def turn(left, right):
    return {"id": f"turn-{right}", "from_event_id": left, "to_event_id": right, "relation": "follows",
            "basis": "structural", "evidence_ids": [f"e_{left}"], "active": True, "origin": "dialog_turn"}


def delegated_graph():
    plots = {"kind": "subagent", "parent_session_id": "root-1", "agent_path": "/root/plots", "agent_nickname": "Kant"}
    sources = {
        "e_q": source("root-1", "user", at="10:00"),
        "e_d1": source("root-1", "tool_call", at="10:01", call="c1", step={"hint": "delegate", "agent": "/root/plots"}),
        "e_d2": source("root-1", "tool_call", at="10:05", call="c2", step={"hint": "delegate", "agent": "/root/plots"}),
        "e_s1": source("sub-1", "assistant", at="10:02", lineage=plots),
        "e_s2": source("sub-1", "assistant", at="10:06", lineage=plots),
        "e_s3": source("sub-1", "assistant", at="10:07", lineage=plots),
        "e_x": source("sub-2", "assistant", at="10:03", lineage={**plots, "agent_path": "/root/other"}),
    }
    graph = {"events": [event("q", "question", "asked", "10:00", actor="user"), event("d1", "action", "applied", "10:01"),
                        event("s1", "outcome", "reported_complete", "10:02"), event("x", "outcome", "reported_complete", "10:03"),
                        event("d2", "action", "applied", "10:05"), event("s2", "outcome", "reported_complete", "10:06"),
                        event("s3", "outcome", "reported_complete", "10:07")],
             "edges": [turn("q", "d1"), turn("q", "s1"), turn("q", "x"), turn("q", "d2"), turn("q", "s2"),
                       {**turn("s2", "s3"), "id": "s2-s3", "relation": "motivates", "basis": "explicit", "origin": None}]}
    return graph, sources.get


def test_a_sub_agents_work_hangs_under_the_delegation_that_gave_it_its_task_not_the_last_message():
    graph, source_of = delegated_graph()
    fill_steps(graph, source_of)
    assert link_delegations(graph, source_of) == 2
    by_id = {e["id"]: e for e in graph["events"]}
    assert by_id["d1"]["step"] == by_id["d2"]["step"] == "delegate"
    # Each event of the agent belongs to the task it was on: before the follow-up, the spawn's; after it, the follow-up's.
    assert {key: by_id[key].get("delegated_by") for key in ("s1", "s2", "s3", "x")} == {
        "s1": "d1", "s2": "d2", "s3": "d2", "x": None}  # another agent is not this delegation's
    links = {(e["from_event_id"], e["to_event_id"]): e for e in graph["edges"]}
    assert links[("d1", "s1")]["origin"] == "delegation" and links[("d2", "s2")]["active"]
    assert ("d2", "s3") not in links  # reached by a relation of its own
    assert not links[("q", "s1")]["active"] and links[("q", "s1")]["invalidation"]["replaced_by"] == links[("d1", "s1")]["id"]
    assert links[("q", "x")]["active"]
    # Every publish runs it again over the whole graph: nothing is added twice.
    before = json.dumps(graph, sort_keys=True)
    assert link_delegations(graph, source_of) == 0 and json.dumps(graph, sort_keys=True) == before
    assert status_labels(graph)["d2"] == "위임함 · 하위 작업 2건"
    assert status_labels(graph)["d1"] == "위임함 · 하위 작업 1건"


def test_a_claude_sub_agent_is_paired_with_its_delegation_by_the_call_id():
    graph, source_of = delegated_graph()
    sources = {"e_d1": {**source_of("e_d1"), "provider": "claude", "tool_call_id": "toolu_1", "step": {"hint": "delegate"}},
               "e_s1": {**source_of("e_s1"), "lineage": {"kind": "subagent", "parent_session_id": "root-1",
                                                          "parent_tool_call_id": "toolu_1", "agent_type": "Explore"}}}
    lookup = lambda key: sources.get(key) or source_of(key)
    fill_steps(graph, lookup)
    link_delegations(graph, lookup)
    assert {e["id"]: e.get("delegated_by") for e in graph["events"]}["s1"] == "d1"


def test_the_flow_stands_a_sub_agents_boxes_beside_its_delegation_and_calls_it_a_delegation():
    graph, source_of = delegated_graph()
    fill_steps(graph, source_of)
    link_delegations(graph, source_of)
    for item in graph["events"]:
        if item["id"] in ("s1", "s2", "s3", "x"):
            item["authoring"] = [{"tool": "codex", "role": "subagent", "agent": "Kant"}]
        item["session_ids"] = ["sub-1" if item["id"].startswith("s") else "root-1"]
    diagram = flow_diagram(graph, 140)
    text = "\n".join(diagram.lines())
    # By time s1 would stand beside d2 (the next step after it); it belongs to d1, whose task it was doing.
    assert diagram.beside["s1"] == "d1" and diagram.beside["s2"] == diagram.beside["s3"] == "d2"
    assert diagram.beside["x"] == "d2"  # no delegation of its own: beside the step after it, as before
    assert "위임 " in text and "위임함 · 하위 작업 1건" in text
    assert "← [02]" not in text and "← [05]" not in text  # the stack is the link, not a note in both boxes
    detail = "\n".join(line for line, _ in event_detail(graph, "s1", lambda _: None))
    assert "(위임)" in detail


def test_an_analysis_hangs_a_claude_sub_agents_work_under_the_task_call(laboratory):
    _, store, engine, records_, make = laboratory
    def at(record, minute):
        return replace(record, recorded_at=f"2026-09-22T10:{minute:02d}:00Z")
    parent = [at(make("파서를 찾아 고쳐 주세요.", key="ask"), 0),
              at(replace(make("Tool: Agent\n" + json.dumps({"subagent_type": "Explore", "description": "Fix the parser"}),
                              key="agent", role="tool_call"), tool_call_id="toolu_1"), 1),
              at(replace(make("Fixed the parser.", key="agent-out", role="tool_result"), tool_call_id="toolu_1"), 5),
              at(make("하위 에이전트가 파서를 고쳤습니다.", key="said", role="assistant"), 6)]
    lineage = {"kind": "subagent", "parent_session_id": "session-1", "parent_tool_call_id": "toolu_1", "agent_type": "Explore"}
    def sub(text, key, role, minute, call=None):
        record = make(text, key=key, role=role, session="sub-1")
        return at(replace(record, lineage=lineage, tool_call_id=call), minute)
    patch = "Tool: Edit\n" + json.dumps({"file_path": "/work/app/src/parse.py", "old_string": "a", "new_string": "b"})
    child = [sub(patch, "edit", "tool_call", 2, "c1"), sub("The file has been updated.", "edit-out", "tool_result", 2, "c1"),
             sub("Tool: Bash\n" + json.dumps({"command": "python -m pytest tests/test_parse.py -q"}), "test", "tool_call", 3, "c2"),
             sub("2 passed", "test-out", "tool_result", 3, "c2")]
    found = parent + child
    mark_steps(found)
    records_.extend(found)
    result = engine.analyze(lambda: FixtureRunner())
    assert result["status"] == "complete", result
    graph = store.graph()
    (delegation,) = [e for e in graph["events"] if e.get("step") == "delegate"]
    assert delegation["title"] == "위임: Explore — Fix the parser"
    work = [e for e in graph["events"] if any(a.get("role") == "subagent" for a in e.get("authoring") or [])]
    assert work and all(e.get("delegated_by") == delegation["id"] for e in work)
    active = [e for e in graph["edges"] if e["active"]]
    assert {e["to_event_id"] for e in active if e.get("origin") == "delegation"}
    assert not any(e.get("origin") == "dialog_turn" and e["to_event_id"] in {w["id"] for w in work} for e in active)
