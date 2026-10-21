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
    # No box of their own session: each session's work is a box of its own, never folded into another session's.
    assert "elsewhere" not in view["homes"] and "drawn" not in view["homes"]
    boxes = {e["id"]: e for e in view["events"]}
    assert boxes["elsewhere"]["gathered"] and boxes["drawn"]["gathered"] and view["unplaced"] == 0
    assert boxes["drawn"]["folded_agents"] == [{"agent": "plots", "said": "drawn (위임 연결 없음)"}]
    assert "folded_agents" not in boxes["elsewhere"]
    from contexttrail.diagram import story_svg
    assert "상자에 들지 않은 단계" not in story_svg(view)
    turns = dict(request_turns({"version": 1, "events": events, "edges": []}))
    assert turns["ask"][:3] == ["ask", "plan", "choice"]


def test_each_box_counts_its_own_folded_work_with_the_latest_result_and_the_checks():
    view = story_view(project())
    boxes = {e["id"]: e for e in view["events"]}
    # The plan's own steps: the failed and the passed check of the later change, and the change; the delegations
    # and the sub-agent's steps are its agent lines, not its work.
    assert boxes["plan"]["folded_work"] == {"count": 3, "last": "passed", "failed": 1, "passed": 1}
    assert boxes["ask"]["folded_work"] == {"count": 1, "last": "edit", "failed": 0, "passed": 0}  # no result: the step
    assert boxes["found"]["folded_work"] == {"count": 1, "last": "test", "failed": 0, "passed": 1}
    assert "folded_work" not in boxes["broke"]


def test_the_story_box_says_its_work_in_one_dim_line_and_its_status_mark_in_the_header():
    view = story_view(project())
    diagram = flow_diagram(view, 120)
    rows = diagram.lines()
    text = "\n".join(rows)
    assert "↳ 작업 3건: passed · ✗ 1 · ✓ 1" in text and "↳ 작업 1건: edit" in text
    line = next(n for n, row in enumerate(rows) if "↳ 작업 3건" in row)
    assert diagram.rows[line][diagram.boxes["plan"][1] + 2].style == "dim"
    assert "[01] 요청 ✓ ─" in rows[diagram.boxes["ask"][0]]  # readable without colour
    assert "[10] 결과 ✗ ─" in rows[diagram.boxes["broke"][0]]
    assert "[09] 결과 ! ─" in rows[diagram.boxes["found"][0]]
    assert "[02] 결정 ─" in rows[diagram.boxes["plan"][0]]  # plain: no mark
    ascii_rows = flow_diagram(view, 120, ascii_only=True).lines()
    assert any("[01] 요청 v -" in row for row in ascii_rows) and any("[10] 결과 x -" in row for row in ascii_rows)
    assert any("-> 작업 3건: passed · x 1 · v 1" in row for row in ascii_rows)
    full = "\n".join(flow_diagram(project(), 160).lines())  # every step: unchanged
    assert "[01] 요청 ─" in full and "[01] 요청 ✓" not in full and "↳ 작업" not in full


def test_a_request_nothing_is_linked_to_is_plain_and_the_flag_key_passes_it_by():
    events = [event("ask", "question", "asked", "10:00", actor="user"),
              event("broke", "outcome", "observed_failure", "10:01", basis="tool_record"),
              event("later", "question", "asked", "10:02", actor="user")]
    view = story_view({"version": 1, "events": events, "edges": []})
    assert view["labels"]["later"] == "요청 · 이어진 사건 연결 없음" and view["tones"]["later"] == "plain"
    diagram = flow_diagram(view, 100)
    top, x, _, _ = diagram.boxes["later"]
    assert diagram.rows[top][x].style == "" and "[03] 요청 ─" in diagram.lines()[top]
    panels = FlowPanels({"version": 1, "events": events, "edges": []}, lambda _: None)
    panels.select("ask")
    panels.handle("!")
    assert panels.current == "broke"
    panels.handle("!")
    assert panels.current == "broke"  # the only place that needs a look


def test_the_story_svg_writes_the_work_line_and_the_status_mark_in_its_colour():
    from contexttrail.diagram import SVG_TONE, story_svg
    out = story_svg(story_view(project()))
    assert "↳ 작업 3건: passed · ✗ 1 · ✓ 1" in out and 'fill="#617686" font-size="12">↳ 작업' in out
    assert f'fill="{SVG_TONE["ok"]}" font-size="11" font-weight="700">✓</text>' in out
    assert f'fill="{SVG_TONE["fail"]}" font-size="11" font-weight="700">✗</text>' in out
    assert out.count('font-weight="700">') == 3  # ask ✓, found !, broke ✗; the plain decision has none


def test_a_gathered_box_names_its_agent_in_its_line_not_its_header():
    events = [event("ask", "question", "asked", "10:00", actor="user"),
              event("draw", "action", "applied", "10:01", basis="tool_record", authoring=SUB, session_ids=["sub"]),
              event("commit", "action", "applied", "10:02", basis="tool_record", session_ids=[]),
              event("drawn", "outcome", "reported_complete", "10:03", authoring=SUB, session_ids=["sub"], title="그림 완성")]
    edges = [link("draw", "commit", "produces")]
    view = story_view({"version": 1, "events": events, "edges": edges})
    assert [e["id"] for e in view["events"]] == ["ask", "drawn"]
    # The commit with no session joins the work that led to it; the latest of them is the box.
    assert view["homes"] == {"draw": "drawn", "commit": "drawn"} and view["unplaced"] == 0
    drawn = view["events"][1]
    assert drawn["folded_agents"] == [{"agent": "plots", "said": "그림 완성 (위임 연결 없음)"}]
    assert drawn["folded_work"] == {"count": 1, "last": "commit", "failed": 0, "passed": 0}
    text = "\n".join(flow_diagram(view, 100).lines())
    assert "[04] 결과 ─" in text and "하위 plots" not in text and "↳ plots: 그림 완성 (위임 연결 없음)" in text


def test_a_gathered_box_is_drawn_plain_and_the_flag_key_passes_it_by():
    """As the owner's picture has it: work no box took is a grey box with no mark, not a finding to check."""
    from contexttrail.diagram import SVG_TONE, story_svg
    events = [event("ask", "question", "asked", "10:00", actor="user"),
              event("broke", "outcome", "observed_failure", "10:01", basis="tool_record"),
              event("draw", "action", "applied", "10:02", basis="tool_record", authoring=SUB, session_ids=["sub"]),
              event("drawn", "outcome", "reported_complete", "10:03", authoring=SUB, session_ids=["sub"])]
    graph = {"version": 1, "events": events, "edges": []}
    view = story_view(graph)
    assert view["events"][-1]["gathered"] and view["tones"]["drawn"] == "plain"
    assert view["labels"]["drawn"] == "완료 보고·미검증"  # the detail keeps the event's own status
    diagram = flow_diagram(view, 100)
    top, x, _, _ = diagram.boxes["drawn"]
    assert diagram.rows[top][x].style == "" and "[04] 결과 ─" in diagram.lines()[top]
    out = story_svg(view)
    box = out[out.index('data-event-id="drawn"'):]
    assert f'stroke="{SVG_TONE["plain"]}" stroke-width="1"/>' in box[:box.index("</g>")]
    assert 'font-size="11" font-weight="700">✗</text>' in out and 'font-weight="700">!</text>' not in out
    panels = FlowPanels(graph, lambda _: None)
    panels.select("ask")
    panels.handle("!")
    assert panels.current == "broke"
    panels.handle("!")
    assert panels.current == "broke"  # never the gathered box


def test_left_work_moves_with_its_delegation_and_is_never_split_between_boxes():
    def graph(*edges):
        events = [event("ask", "question", "asked", "10:00", actor="user"),
                  event("edit", "action", "applied", "10:01", basis="tool_record"),
                  # A delegation in a session with no request (its caller's turn not analysed) and its agent's work.
                  event("hand", "action", "applied", "10:02", basis="tool_record", step="delegate",
                        delegated_to="plots", session_ids=["caller"]),
                  event("draw", "action", "applied", "10:03", basis="tool_record", authoring=SUB,
                        delegated_by="hand", session_ids=["sub"]),
                  event("drawn", "outcome", "reported_complete", "10:04", authoring=SUB, delegated_by="hand",
                        session_ids=["sub"])]
        return {"version": 1, "events": events, "edges": [link("ask", "edit", "answers"), *edges]}

    # A relation of one of the agent's steps does not pull that step away from the rest of its agent's work.
    view = story_view(graph(link("edit", "draw", "motivates")))
    assert view["homes"] == {"edit": "ask", "hand": "drawn", "draw": "drawn"}
    assert view["events"][-1]["folded_agents"] == [{"agent": "plots", "said": "drawn"}]  # handed: no suffix
    # The delegation's own relation moves its whole group into that box.
    view = story_view(graph(link("edit", "hand", "motivates")))
    assert view["homes"] == {"edit": "ask", "hand": "ask", "draw": "ask", "drawn": "ask"}
    assert [e["id"] for e in view["events"]] == ["ask"]
    assert view["events"][0]["folded_agents"] == [{"agent": "plots", "said": "drawn"}]


def test_a_sub_agent_with_no_delegation_says_so_in_any_box():
    events = [event("ask", "question", "asked", "10:00", actor="user"),
              event("plan", "decision", "adopted", "10:01"),
              event("drawn", "outcome", "reported_complete", "10:02", authoring=SUB, session_ids=["sub"])]
    view = story_view({"version": 1, "events": events, "edges": [link("plan", "drawn", "motivates")]})
    plan = next(e for e in view["events"] if e["id"] == "plan")
    assert not plan.get("gathered") and view["homes"]["drawn"] == "plan"
    assert plan["folded_agents"] == [{"agent": "plots", "said": "drawn (위임 연결 없음)"}]


def test_work_with_no_session_is_its_own_box_unless_a_relation_joins_it():
    def view_of(*edges):
        events = [event("ask", "question", "asked", "10:00", actor="user"),
                  event("one", "action", "applied", "10:01", basis="tool_record", session_ids=[]),
                  event("two", "action", "applied", "10:02", basis="tool_record", session_ids=[])]
        return story_view({"version": 1, "events": events, "edges": list(edges)})

    view = view_of()  # time alone is no relation
    assert [e["id"] for e in view["events"]] == ["ask", "one", "two"] and view["homes"] == {}
    assert all(e.get("gathered") for e in view["events"][1:])
    view = view_of(link("one", "two", "produces"))
    assert [e["id"] for e in view["events"]] == ["ask", "two"] and view["homes"] == {"one": "two"}


def test_a_gathered_box_whose_own_step_failed_stays_a_failure():
    events = [event("ask", "question", "asked", "10:00", actor="user"),
              event("edit", "action", "applied", "10:01", basis="tool_record", authoring=SUB, session_ids=["sub"]),
              event("broke", "outcome", "observed_failure", "10:02", basis="tool_record", authoring=SUB, session_ids=["sub"])]
    view = story_view({"version": 1, "events": events, "edges": []})
    assert "broke" in {e["id"] for e in view["events"]} and view["tones"]["broke"] == "fail"
