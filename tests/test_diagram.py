from contexttrail.diagram import flow_diagram, request_turns


def node(key, kind, status, title=None, session="s1"):
    return {"id": key, "kind": kind, "status": status, "title": title or key, "summary": "",
            "actor": "assistant", "evidence_ids": [], "session_ids": [session]}


def link(left, right, relation):
    return {"id": f"{left}-{right}", "from_event_id": left, "to_event_id": right, "relation": relation,
            "basis": "explicit", "active": True}


def story():
    events = [node("q", "question", "asked", "설치가 자동인가"),
              node("plan", "proposal", "proposed", "설치 스크립트 제안"),
              node("script", "action", "applied", "install.sh 추가"),
              node("run", "outcome", "observed_success", "install.sh 실행 성공"),
              node("fail", "outcome", "observed_failure", "충돌 검사 실패"),
              node("fix", "revision", "applied", "충돌 검사 시점 수정"),
              node("docs", "action", "applied", "문서 갱신"),
              node("answer", "outcome", "reported_complete", "설치 방법 답변")]
    edges = [link("q", "plan", "answers"), link("plan", "script", "motivates"),
             link("script", "run", "verifies"), link("script", "fail", "verifies"),
             link("fail", "fix", "motivates"), link("script", "fix", "revises"),
             link("fix", "run", "verifies"), link("q", "answer", "answers")]
    return {"events": events, "edges": edges}


def test_boxes_and_labelled_arrows_show_each_relation_once():
    graph = story()
    diagram = flow_diagram(graph, 110)
    text = "\n".join(diagram.lines())
    assert set(diagram.boxes) == {event["id"] for event in graph["events"]}
    left = {key for key, (_, x, _, _) in diagram.boxes.items() if x == diagram.boxes["q"][1]}
    # Only a result that leads nowhere sits on the right; the failure that motivated the fix stays in the story.
    assert left == {"q", "plan", "script", "fail", "fix", "docs", "answer"}
    assert "│ 답변" in text  # q → plan: consecutive boxes, arrow down the column
    assert "│ 검증" in text and "│ 동기" in text  # script → fail → fix reads down the column
    # Two verifies links into the run: beside script, and fix → run pointing back up at [04].
    assert text.count("─검증──▶") == 2 and "[04] ↑ 위" in text
    # Links that skip boxes run on margin rails and arrive under the number of the box they left.
    assert "[03] 수정 ▶" in text and "[01] 답변 ▶" in text
    assert "흐름" not in text  # one session, one flow: no heading
    assert "연결된 사건 없음" in text.split("[07]")[1]  # docs has no relation at all, and says so
    assert flow_diagram(graph, 60) is None  # too narrow to read: callers fall back to the text list


def test_long_titles_and_labels_wrap_without_being_cut():
    graph = story()
    graph["events"][2]["title"] = "install.sh 추가와 기존 명령 충돌 검사, 관리 표식 기록, 설치 후 경로 안내까지 한 번에 처리"
    diagram = flow_diagram(graph, 110)
    top, x, height, width = diagram.boxes["script"]
    inside = ["".join(cell.char for cell in row[x + 2:x + width - 2]).strip()
              for row in diagram.rows[top + 1:top + height - 1]]
    assert " ".join(inside[:3]) == graph["events"][2]["title"]  # three lines, nothing cut
    assert inside[3:] == ["✗ 변경 적용 · 검증 통과 1건", "· 검증 실패 1건"]  # a label breaks between its parts


def test_links_without_room_are_written_in_both_boxes():
    events = [node(key, "action", "applied") for key in "abcdef"]
    edges = [link("a", "d", "follows"), link("a", "e", "follows"), link("a", "f", "follows"),
             link("b", "e", "follows"), link("b", "f", "motivates")]
    diagram = flow_diagram({"events": events, "edges": edges}, 110)
    rows = diagram.lines()

    def inside(key):
        top, _, height, _ = diagram.boxes[key]
        return "\n".join(rows[top:top + height])

    assert "→ [06] 동기" in inside("b") and "← [02] 동기" in inside("f")  # a fifth rail does not fit


def test_unrelated_sessions_are_separate_flows():
    graph = story()
    graph["events"] += [node("other", "action", "applied", "별도 기능 추가", session="s2"),
                        node("other_run", "outcome", "observed_success", "별도 기능 테스트 통과", session="s2")]
    graph["edges"].append(link("other", "other_run", "verifies"))
    diagram = flow_diagram(graph, 110)
    rows = diagram.lines()
    headings = [n for n, row in enumerate(rows) if "흐름" in row]
    assert len(headings) == 2 and "흐름 1/2 · 사건 8개" in rows[headings[0]]
    assert "흐름 2/2 · 사건 2개" in rows[headings[1]]
    assert diagram.boxes["answer"][0] < headings[1] < diagram.boxes["other"][0]  # each flow keeps its own order
    graph["edges"].append(link("answer", "other", "motivates"))  # a relation across sessions joins them
    assert not any("흐름" in row for row in flow_diagram(graph, 110).lines())


def test_rails_sharing_a_box_use_separate_rows():
    graph = story()
    graph["edges"].append(link("plan", "answer", "follows"))
    diagram = flow_diagram(graph, 110)
    top = diagram.boxes["answer"][0]
    rows = diagram.lines()
    assert "[01] 답변 ▶" in rows[top + 1] and "[02] 후속 ▶" in rows[top + 2]


def test_selected_box_is_drawn_with_heavy_lines():
    from contexttrail.ui import FlowPanels
    panels = FlowPanels(story(), lambda _: None)
    panels.current = "script"
    diagram = panels.diagram(110)
    top = diagram.boxes["script"][0]
    drawn = "".join(text for text, _ in panels._cell_runs(diagram.rows[top]))
    assert "┏" in drawn and "┓" in drawn
    panels.current = "q"
    assert "┏" not in "".join(text for text, _ in panels._cell_runs(diagram.rows[top]))


def test_turn_links_of_a_user_message_share_one_bracket():
    events = [node("ask", "question", "asked", "설치 스크립트를 만들어 줘")] + [
        node(key, "action", "applied", key) for key in ("a", "b", "c", "d")]
    events[0]["actor"] = "user"
    turn = [dict(link("ask", key, "follows"), basis="structural", origin="dialog_turn") for key in ("b", "c", "d")]
    diagram = flow_diagram({"events": events, "edges": turn + [link("a", "c", "motivates")]}, 110)
    rows = diagram.lines()
    ticks = [n for n, row in enumerate(rows) if row.startswith(" ├") or row.startswith(" └")]
    assert len(ticks) == 3 and rows[ticks[-1]].startswith(" └")  # one rail, a tick per target
    assert all("[01] 후속 ▶" in rows[n] for n in ticks)
    assert "[02] 동기 ▶" in "\n".join(rows)  # the model's own link still gets a rail of its own


def test_a_box_that_sends_and_receives_rails_uses_a_row_for_each():
    events = [node(key, "action", "applied") for key in "abcde"]
    diagram = flow_diagram({"events": events, "edges": [link("a", "c", "motivates"), link("c", "e", "follows"),
                                                        link("b", "d", "revises")]}, 110)
    top = diagram.boxes["c"][0]
    rows = diagram.lines()
    assert "[01] 동기 ▶" in rows[top + 1] and "┤" in rows[top + 2]  # arrives on one row, leaves on the next


def test_each_line_cell_knows_the_links_it_draws():
    graph = story()
    diagram = flow_diagram(graph, 110)
    top = diagram.boxes["fix"][0]
    arrival = [cell for cell in diagram.rows[top + 1] if cell.char == "▶"]
    assert arrival and arrival[0].edges == {"script-fix"}  # the rail from [03] into the fix
    spine = {cell.char: cell.edges for row in diagram.rows[top - 2:top] for cell in row if cell.char in "│▼"}
    assert spine["▼"] == {"fail-fix"}


def conversation():
    def said(key, text, session="s1"):
        return dict(node(key, "question", "asked", text, session), actor="user")
    events = [said("m1", "설치 스크립트를 만들어 줘"), node("script", "action", "applied", "install.sh 추가"),
              node("run", "outcome", "observed_success", "설치 실행 성공"), node("git", "action", "applied", "커밋"),
              said("m2", "문서도 고쳐 줘"), node("docs", "action", "applied", "README 갱신"),
              node("elsewhere", "action", "applied", "다른 세션 작업", session="s2")]
    events[3]["session_ids"] = []  # a Git commit: no session, but the script leads to it
    turn = lambda left, right: dict(link(left, right, "follows"), basis="structural", origin="dialog_turn")
    edges = [turn("m1", "script"), link("script", "run", "verifies"), link("script", "git", "produces"),
             turn("m2", "docs"), link("script", "docs", "motivates")]
    return {"events": events, "edges": edges}


def test_a_turn_is_a_request_and_what_its_session_did_before_the_next_one():
    assert request_turns(conversation()) == [("m1", ["m1", "script", "run", "git"]), ("m2", ["m2", "docs"]),
                                             (None, ["elsewhere"])]
    assert request_turns(story()) == []  # no user message: nothing to split by


def test_one_turn_is_drawn_with_its_links_outside_written_inside():
    graph = conversation()
    diagram = flow_diagram(graph, 110, only=["m2", "docs"])
    rows = diagram.lines()
    assert set(diagram.boxes) == {"m2", "docs"}
    assert "[06] 변경" in rows[diagram.boxes["docs"][0]]  # numbers stay those of the whole list
    assert "← [02] 동기" in "\n".join(rows)  # the motivating change is in the turn before


def test_panels_move_by_request_and_follow_links_back():
    import curses
    from contexttrail.ui import FlowPanels
    panels = FlowPanels(conversation(), lambda _: None)
    assert panels.current == "m1" and panels.turn() == 0
    panels.handle("]")
    assert (panels.current, panels.turn()) == ("m2", 1)
    panels.handle("a")
    assert panels.turn() is None and panels.current == "m2"  # the whole flow keeps the selection
    panels.handle("a")
    panels.select("script")
    detail = [text for text, _ in panels._detail("script", 80)[0]]
    assert "  1 ✓ [03] 설치 실행 성공" in detail  # a check is listed first, with its key
    assert any(line.startswith("  4 → 동기") and "[06] README 갱신" in line for line in detail)
    panels.handle("4")
    assert (panels.current, panels.turn()) == ("docs", 1)  # following a link can change the request
    panels.handle(curses.KEY_BACKSPACE)
    assert panels.current == "script"
    panels.handle("9")  # no ninth link: nothing happens
    assert panels.current == "script" and panels.history == []


def test_selected_box_lights_its_links_and_dims_unrelated_boxes():
    import curses
    from contexttrail.ui import FlowPanels
    panels = FlowPanels(story(), lambda _: None)
    diagram = panels.diagram(110)
    panels.current = "fix"
    arrival = diagram.rows[diagram.boxes["fix"][0] + 1]
    lit = [attr for text, attr in panels._cell_runs(arrival) if "수정" in text]
    assert lit and lit[0] & curses.A_BOLD and not lit[0] & curses.A_DIM  # the rail from [03] into the fix
    docs = diagram.rows[diagram.boxes["docs"][0] + 1]
    assert all(attr & curses.A_DIM for text, attr in panels._cell_runs(docs) if "문서" in text)
    plan = diagram.rows[diagram.boxes["plan"][0] + 1]
    assert all(attr & curses.A_DIM for text, attr in panels._cell_runs(plan) if "제안" in text)
    panels.current = "plan"
    assert not any(attr & curses.A_DIM for text, attr in panels._cell_runs(plan) if "제안" in text)


def test_story_boxes_with_no_link_between_them_are_joined_in_order_by_an_unlabelled_dotted_line():
    graph = story()
    diagram = flow_diagram(graph, 110)
    rows = diagram.lines()
    column = diagram.boxes["q"][1] + 3
    def between(upper, lower):
        top = diagram.boxes[upper][0] + diagram.boxes[upper][2]
        return [rows[row][column:column + 1] for row in range(top, diagram.boxes[lower][0])]
    # fix → docs → answer have no relation between neighbours: dotted, ending on an open arrowhead.
    assert set(between("fix", "docs")) <= {"┊", "▽"} and between("fix", "docs")[-1] == "▽"
    assert set(between("docs", "answer")) <= {"┊", "▽"}
    # A related pair keeps its labelled solid line, and the dotted line carries no link (nothing lights it).
    assert "│" in between("q", "plan") and "┊" not in between("q", "plan")
    top = diagram.boxes["fix"][0] + diagram.boxes["fix"][2]
    assert not diagram.links(top, column)
    # Separate flows are not joined to each other.
    graph["events"].append(node("other", "action", "applied", "별도 기능 추가", session="s2"))
    split = flow_diagram(graph, 110)
    above = split.boxes["answer"][0] + split.boxes["answer"][2]
    assert "┊" not in "".join(row[column:column + 1] for row in split.lines()[above:split.boxes["other"][0]])
    assert ":" in "\n".join(flow_diagram(story(), 110, ascii_only=True).lines())


def test_a_box_names_the_agent_that_did_the_step_and_a_persons_box_only_the_tool():
    graph = story()
    for event in graph["events"]:
        event["authoring"] = [{"tool": "codex", "model": "gpt-6.1-sol", "effort": "high"}]
    graph["events"][0]["actor"] = "user"
    diagram = flow_diagram(graph, 110)
    rows = diagram.lines()
    assert "codex · gpt-6.1-sol · high" in "\n".join(rows)
    top, _, height, _ = diagram.boxes["q"]
    question = rows[top:top + height]
    assert any("codex" in row for row in question) and not any("gpt-6.1-sol" in row for row in question)
    plain = "\n".join(flow_diagram(story(), 110).lines())
    assert "codex" not in plain  # an older graph without the field draws as before


def test_a_sub_agents_box_says_so_in_its_header_and_has_its_own_border_colour():
    graph = story()
    by_id = {event["id"]: event for event in graph["events"]}
    by_id["run"]["authoring"] = [{"tool": "claude", "model": "claude-sonnet-5-5", "effort": "high",
                                  "role": "subagent", "agent": "Explore"}]
    by_id["docs"]["authoring"] = [{"tool": "codex", "model": "gpt-6.1-sol", "role": "unattended", "agent": "codex exec"}]
    by_id["fix"]["authoring"] = [{"tool": "claude", "model": "claude-opus-5-5", "effort": "high"}]
    diagram = flow_diagram(graph, 110)
    text = "\n".join(diagram.lines())
    assert "결과 · 하위 Explore " in text and "변경 · codex exec " in text
    assert "claude › Explore · sonnet-5-5 · high" in text and "codex exec · 사람 없음 · gpt-6.1-sol" in text
    def border_style(key):
        top, left, height, _ = diagram.boxes[key]
        return {diagram.rows[top][left].style, diagram.rows[top + height - 1][left].style}
    assert border_style("run") == {"sub"} and border_style("docs") == {"sub"}
    assert "sub" not in border_style("fix")  # the session's own agent keeps the status-coloured border


def test_a_sub_agents_boxes_stand_beside_the_parents_next_step_joined_by_one_line_into_it():
    graph = story()
    by_id = {event["id"]: event for event in graph["events"]}
    # The sub-agent ran between the script change and the fix; its two boxes stand beside the fix.
    for key in ("fail", "docs"):
        by_id[key]["authoring"] = [{"tool": "claude", "model": "claude-sonnet-5-5", "role": "subagent", "agent": "Explore"}]
    graph["edges"] = [edge for edge in graph["edges"] if edge["to_event_id"] not in ("fail", "docs")]
    graph["edges"].append({**link("q", "fail", "follows"), "origin": "dialog_turn"})
    graph["events"] = [by_id[k] for k in ("q", "plan", "script", "run", "fail", "docs", "fix", "answer")]
    diagram = flow_diagram(graph, 120)
    text = "\n".join(diagram.lines())
    fix_top, fix_left, _, _ = diagram.boxes["fix"]
    right = diagram.boxes["run"][1]
    assert diagram.boxes["fail"][1] == right and diagram.boxes["docs"][1] == right
    assert diagram.boxes["fail"][0] >= fix_top and diagram.boxes["docs"][0] > diagram.boxes["fail"][0]
    assert "결과 · 하위 Explore" in text and "─Explore" in text
    # The line enters the parent's box ("◀") and the fix → run result still leaves it ("├") on its own line.
    row = "".join(cell.char for cell in diagram.rows[fix_top + 1])
    assert "├" in row[fix_left:] or "◀" in row[fix_left:]
    # The turn link to the sub-agent is the stack itself, not a note; a relation from it is a note in both boxes.
    assert "[01] 후속" not in text
    assert "후속" not in "".join(diagram.lines())
    # A dialog-turn-only sub-agent box is not in the story column, so the column reads q → plan → script → fix.
    assert [k for k in diagram.order() if diagram.boxes[k][1] == fix_left] == ["q", "plan", "script", "fix", "answer"]
