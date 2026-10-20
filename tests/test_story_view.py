"""The story view, the default screen: the story as boxes, the work and the sub-agents folded into them."""
from contexttrail.diagram import flow_diagram
from contexttrail.schema import story_view
from contexttrail.ui import FlowPanels


def event(key, kind, status, at, *, actor="assistant", basis="explicit_statement", **more):
    return {"id": key, "kind": kind, "status": status, "title": key, "summary": f"{key}의 요약", "actor": actor,
            "basis": basis, "evidence_ids": [], "session_ids": ["root"], "recorded_at": at, **more}


def link(left, right, relation, **more):
    return {"id": f"{left}-{right}", "from_event_id": left, "to_event_id": right, "relation": relation,
            "basis": "explicit", "active": True, **more}


SUB = [{"tool": "codex", "role": "subagent", "agent": "plots"}]


def project():
    events = [
        event("ask", "question", "asked", "10:00", actor="user"),
        event("plan", "decision", "adopted", "10:01"),
        event("hand", "action", "applied", "10:02", basis="tool_record", step="delegate", delegated_to="plots"),
        event("draw", "action", "applied", "10:03", basis="tool_record", authoring=SUB, delegated_by="hand"),
        event("drawn", "outcome", "reported_complete", "10:04", authoring=SUB, delegated_by="hand", title="그림 완성"),
        event("waiting", "action", "applied", "10:05", basis="tool_record", step="delegate", delegated_to="review"),
        event("edit", "action", "applied", "10:06", basis="tool_record"),
        event("test", "outcome", "observed_success", "10:07", basis="tool_record"),
        event("found", "outcome", "reported_complete", "10:08"),
        event("broke", "outcome", "observed_failure", "10:09", basis="tool_record"),
        event("fixed", "outcome", "observed_failure", "10:10", basis="tool_record"),
        event("again", "action", "applied", "10:11", basis="tool_record"),
        event("passed", "outcome", "observed_success", "10:12", basis="tool_record"),
    ]
    edges = [link("ask", "edit", "answers"), link("plan", "hand", "motivates"), link("edit", "test", "verifies"),
             link("test", "found", "produces"), link("again", "fixed", "verifies"), link("again", "passed", "verifies"),
             link("ask", "plan", "follows", basis="structural", origin="dialog_turn")]
    return {"version": 4, "events": events, "edges": edges}


def test_the_story_view_keeps_the_story_and_folds_the_work_and_the_agents_into_it():
    view = story_view(project())
    shown = [e["id"] for e in view["events"]]
    # Requests, intentions and stated findings stay; so does a failure nothing came of. Edits, checks, delegations
    # and a sub-agent's steps fold; a failure whose change a later check passed is settled.
    assert shown == ["ask", "plan", "found", "broke"]
    plan = next(e for e in view["events"] if e["id"] == "plan")
    assert plan["folded_agents"] == [{"agent": "plots", "said": "그림 완성"}, {"agent": "review", "said": None}]
    assert view["homes"]["draw"] == view["homes"]["hand"] == "plan"
    assert view["homes"]["edit"] == "ask"  # a relation first: the request it answers
    assert view["homes"]["test"] == "found"  # the finding that rests on it
    # Numbers and colours are those of the whole list: the request answered by a folded edit still reads answered.
    assert [e["number"] for e in view["events"]] == [1, 2, 9, 10]
    assert view["tones"]["ask"] == "ok"
    assert all(edge["from_event_id"] in shown and edge["to_event_id"] in shown for edge in view["edges"])


def test_the_story_view_draws_one_column_of_titles_with_each_agent_on_a_line():
    diagram = flow_diagram(story_view(project()), 120)
    text = "\n".join(diagram.lines())
    assert {x for _, x, _, _ in diagram.boxes.values()} == {min(x for _, x, _, _ in diagram.boxes.values())}
    assert "↳ plots: 그림 완성" in text and "↳ review: 아직 분석 전" in text
    assert "[09] 결과" in text and "[10] 결과" in text  # numbered as in the whole list
    assert "요약" not in text and "채택" not in text  # the reason and the status are in the detail and the colour
    top, x, _, _ = diagram.boxes["ask"]
    assert diagram.rows[top][x].style == "ok"  # answered: the border says it


def test_v_switches_views_and_a_link_to_a_folded_step_shows_every_step():
    panels = FlowPanels(project(), lambda _: None)
    assert panels.story and [e["id"] for e in panels.graph["events"]] == ["ask", "plan", "found", "broke"]
    panels.select("plan")
    panels.handle("v")
    assert not panels.story and len(panels.graph["events"]) == 13 and panels.current == "plan"
    panels.select("draw")
    panels.handle("v")  # back to the story: the selection moves to the box the step folded into
    assert panels.story and panels.current == "plan"
    panels.select("edit")  # a jump to a folded step (a link, a search, a flag) shows every step
    assert not panels.story and panels.current == "edit" and "전체 보기로 바꿈" in panels.message


def test_the_views_follow_time_not_the_order_units_were_published():
    """A later unit published first: the list, the turns and the story's folds follow when things happened."""
    from contexttrail.diagram import request_turns
    from contexttrail.schema import folded_view
    events = [
        event("ask2", "question", "asked", "2026-10-20T09:00:00Z", actor="user"),
        event("work2", "action", "applied", "2026-10-20T09:05:00Z", basis="tool_record"),
        event("ask1", "question", "asked", "2026-10-20T17:00:00+09:00", actor="user"),  # 08:00 UTC
        event("work1", "action", "applied", "2026-10-20T08:10:00Z", basis="tool_record"),
    ]
    view = folded_view({"version": 1, "events": events, "edges": []})
    assert [e["id"] for e in view["events"]] == ["ask1", "work1", "ask2", "work2"]
    assert request_turns(view) == [("ask1", ["ask1", "work1"]), ("ask2", ["ask2", "work2"])]
    story = story_view(view)
    assert story["homes"] == {"work1": "ask1", "work2": "ask2"}


def test_work_never_folds_into_another_sessions_box_and_a_choice_stays_in_its_turn():
    from contexttrail.diagram import request_turns
    other = dict(session_ids=["other"])
    events = [
        event("ask", "question", "asked", "10:00", actor="user"),
        event("plan", "decision", "adopted", "10:01"),
        event("choice", "decision", "adopted", "10:02", actor="user"),  # the person picked one of the agent's options
        event("elsewhere", "action", "applied", "10:03", basis="tool_record", **other),
        event("drawn", "outcome", "reported_complete", "10:04", authoring=SUB, session_ids=["sub"]),
        event("edit", "action", "applied", "10:05", basis="tool_record"),
    ]
    view = story_view({"version": 1, "events": events, "edges": []})
    assert view["homes"]["edit"] == "choice"  # the latest box before it in its own session
    assert "elsewhere" not in view["homes"] and "drawn" not in view["homes"]  # no box of their own session
    assert view["unplaced"] == 2
    assert not any(e.get("folded_agents") for e in view["events"])
    from contexttrail.diagram import story_svg
    assert "상자에 들지 않은 단계 2개" in story_svg(view)  # said, not silently dropped
    turns = dict(request_turns({"version": 1, "events": events, "edges": []}))
    assert turns["ask"][:3] == ["ask", "plan", "choice"]
