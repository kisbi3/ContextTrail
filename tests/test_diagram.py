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
    # The story is the left lane; the checks of the change (the run and the failure) stand beside it on the right.
    assert left == {"q", "plan", "script", "fix", "docs", "answer"}
    assert diagram.boxes["run"][1] == diagram.boxes["fail"][1] > diagram.boxes["q"][1]
    assert diagram.beside == {"run": "script", "fail": "script"}
    assert "│ 답변" in text and "│ 동기" in text and "│ 수정" in text  # q → plan → script → fix down the column
    # Both checks leave the change on labelled lines; their other links are written in both boxes.
    assert text.count("─검증──▶") == 2 and "→ [06] 동기" in text and "← [05] 동기" in text
    assert "→ [04] 검증" in text and "← [06] 검증" in text
    # Links that skip boxes run on margin rails and arrive under the number of the box they left.
    assert "[01] 답변 ▶" in text
    assert "흐름" not in text  # one session, one flow: no heading
    assert "연결된 사건 없음" not in text  # that is the detail pane's to say
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
    spine = {cell.char: cell.edges for row in diagram.rows[top - 2:top] for cell in row if cell.char in "│▼"}
    assert spine["▼"] == {"script-fix"}  # the change and its fix are neighbours in the story now
    run_top, run_left, _, _ = diagram.boxes["run"]
    head = [cell for cell in diagram.rows[run_top + 1][:run_left] if cell.char == "▶"]
    assert head and head[0].edges == {"script-run"}  # the line beside the change carries the check's link


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
    script_top, _, script_height, _ = diagram.boxes["script"]
    label = next(row for row in diagram.rows[script_top + script_height:diagram.boxes["fix"][0]]
                 if "수정" in "".join(cell.char for cell in row))
    lit = [attr for text, attr in panels._cell_runs(label) if "수정" in text]
    assert lit and lit[0] & curses.A_BOLD and not lit[0] & curses.A_DIM  # the line from [03] into the fix
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


def test_a_box_keeps_who_did_the_step_for_the_detail_pane_not_the_flow():
    graph = story()
    for event in graph["events"]:
        event["authoring"] = [{"tool": "codex", "model": "gpt-6.1-sol", "effort": "high"}]
    text = "\n".join(flow_diagram(graph, 110).lines())
    assert "gpt-6.1-sol" not in text  # the agent line is noise in the flow; `render.event_detail` keeps it


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
    def border_style(key):
        top, left, height, _ = diagram.boxes[key]
        return {diagram.rows[top][left].style, diagram.rows[top + height - 1][left].style}
    # A sub-agent is magenta; a run of another model (codex exec, claude -p) has a colour of its own.
    assert border_style("run") == {"sub"} and border_style("docs") == {"llm"}
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


def test_the_line_from_a_forked_sessions_first_request_is_called_forked():
    graph = story()
    graph["events"].append(node("again", "question", "asked", "이어서 테스트도", session="s2"))
    graph["edges"].append({**link("q", "again", "follows"), "basis": "structural", "origin": "fork"})
    text = "\n".join(flow_diagram(graph, 110).lines())
    assert "갈라짐" in text and "후속" not in text


def forked():
    """A conversation that forked: the parent went on (one result) and the fork did its own work, each side with a
    sub-agent."""
    sub = [{"tool": "claude", "model": "claude-sonnet-5-5", "role": "subagent", "agent": "Explore"}]
    events = [node("q1", "question", "asked", "문서 정리까지 해줘", session="A"),
              node("docs", "action", "applied", "문서 갱신", session="A"),
              node("ex", "outcome", "reported_complete", "로그 조사", session="A"),
              node("evals", "action", "applied", "evals 기준 갱신", session="A"),
              node("q2", "question", "asked", "이어서 slash command", session="B"),
              node("cmd", "action", "applied", "설치 명령 추가", session="B"),
              node("t", "outcome", "observed_success", "pytest 496 passed", session="B"),
              node("rev", "outcome", "reported_complete", "검수 지적", session="B")]
    for n, event in enumerate(events):
        event["recorded_at"] = f"2026-09-26T15:{n:02d}:00Z"
        event["actor"] = "user" if event["kind"] == "question" else "assistant"
    events[2]["authoring"] = sub
    events[7]["authoring"] = [{"tool": "codex", "model": "gpt-6.1-sol", "role": "subagent", "agent": "codex exec"}]
    edges = [link("q1", "docs", "motivates"),
             {**link("q1", "q2", "follows"), "basis": "structural", "origin": "fork", "fork_point_at": "2026-09-26T15:01:30Z"},
             link("q2", "cmd", "motivates"), link("cmd", "t", "verifies")]
    return {"events": events, "edges": edges}


def test_a_forked_conversation_is_drawn_as_two_sides_after_the_fork_with_sub_agents_folded_when_narrow():
    diagram = flow_diagram(forked(), 126)
    text = "\n".join(diagram.lines())
    left = diagram.boxes["q1"][1]
    assert diagram.boxes["docs"][1] == left and diagram.boxes["evals"][1] == left  # the parent goes on in its column
    assert diagram.boxes["q2"][1] > left and diagram.boxes["cmd"][1] == diagram.boxes["q2"][1]  # the fork beside it
    assert diagram.boxes["q2"][0] > diagram.boxes["docs"][0] and diagram.boxes["evals"][0] == diagram.boxes["q2"][0]
    assert "갈라짐" in text and "세션 A 이어짐" in text and "갈라진 세션 B" in text
    # Narrow: results fold into the status line and a sub-agent is one magenta line in the box, no side boxes.
    assert "t" not in diagram.boxes and "ex" not in diagram.boxes and "rev" not in diagram.boxes
    assert "→ 하위 Explore · 로그 조사" in text and "→ codex exec · 검수 지적" in text
    assert "검증: pytest 496 passed" in text


def test_a_forked_conversation_keeps_side_boxes_when_the_screen_is_wide():
    diagram = flow_diagram(forked(), 200)
    assert "t" in diagram.boxes and "ex" in diagram.boxes and "rev" in diagram.boxes
    assert diagram.boxes["t"][1] > diagram.boxes["cmd"][1] and diagram.boxes["ex"][1] > diagram.boxes["docs"][1]
    assert diagram.boxes["q2"][1] > diagram.boxes["ex"][1]  # the fork's column comes after the parent's side column


def test_a_fork_whose_parent_went_no_further_is_one_column_with_a_forked_link():
    graph = forked()
    graph["events"] = [e for e in graph["events"] if e["id"] not in ("ex", "evals")]
    diagram = flow_diagram(graph, 126)
    assert diagram.boxes["q2"][1] == diagram.boxes["q1"][1]
    assert "[01] 갈라짐" in "\n".join(diagram.lines())


def test_a_sub_agents_boxes_stand_beside_the_step_that_followed_them_in_time_not_in_list_order():
    # A sub-agent's events are listed after the whole parent session (they were analysed later), but they ran
    # between the parent's second and third steps, so they stand beside the third.
    graph = story()
    by_id = {event["id"]: event for event in graph["events"]}
    times = {"q": "10:00", "plan": "10:01", "script": "10:05", "run": "10:06", "fail": "10:07", "fix": "10:10",
             "docs": "10:12", "answer": "10:13"}
    for key, hm in times.items():
        by_id[key]["recorded_at"] = f"2026-10-16T{hm}:00Z"
    sub = {"id": "s", "kind": "outcome", "status": "reported_complete", "title": "하위 조사", "summary": "", "actor": "assistant",
           "evidence_ids": [], "session_ids": ["s1"], "recorded_at": "2026-10-16T10:03:00Z",
           "authoring": [{"tool": "claude", "model": "claude-sonnet-5-5", "role": "subagent", "agent": "Explore"}]}
    graph["events"].append(sub)
    graph["edges"].append({**link("q", "s", "follows"), "origin": "dialog_turn"})
    diagram = flow_diagram(graph, 120)
    assert diagram.boxes["s"][0] >= diagram.boxes["script"][0]
    assert diagram.boxes["s"][0] < diagram.boxes["fail"][0] + diagram.boxes["fail"][2]


def lanes(events, edges):
    diagram = flow_diagram({"events": events, "edges": edges}, 120)
    left = min(box[1] for box in diagram.boxes.values())
    return diagram, {key for key, box in diagram.boxes.items() if box[1] == left}


def timed(*events):
    for n, event in enumerate(events):
        event["recorded_at"] = f"2026-10-12T04:{n:02d}:00Z"
    return list(events)


def test_a_result_that_led_to_a_decision_stands_beside_it_as_its_cause():
    events = timed(node("bad", "outcome", "observed_failure", "7일 블록 bootstrap 퇴화"),
                   node("month", "decision", "adopted", "한 달 자료로 확대"),
                   node("start", "action", "in_progress", "한 달 분석 시작"))
    diagram, left = lanes(events, [link("bad", "month", "motivates")])
    assert left == {"month", "start"} and diagram.beside == {"bad": "month"}
    row = diagram.lines()[diagram.boxes["bad"][0] + 1]
    assert "◀" in row and "동기" in row  # the cause feeds into the decision, named by its relation


def test_a_check_with_no_relation_stands_beside_the_step_before_it_on_a_plain_line():
    events = timed(node("month", "decision", "adopted", "한 달 자료로 확대"),
                   node("hash", "outcome", "observed_success", "산출물 해시 일치"),
                   node("start", "action", "in_progress", "한 달 분석 시작"))
    diagram, left = lanes(events, [])
    assert left == {"month", "start"} and diagram.beside == {"hash": "month"}
    top, x, _, _ = diagram.boxes["hash"]
    line = diagram.rows[top + 1][diagram.boxes["month"][1] + diagram.boxes["month"][3]:x]
    text = "".join(cell.char for cell in line)
    assert "▶" not in text and "◀" not in text and "검증" not in text  # beside, never caused by
    assert not any(cell.edges for cell in line)  # a layout line carries no link


def test_documentation_edits_and_test_runs_known_from_their_records_stand_on_the_right():
    events = timed(node("ask", "question", "asked", "문서도 고쳐 줘"),
                   node("code", "action", "applied", "수집기 수정"),
                   dict(node("readme", "action", "applied", "README 기록"), step="doc"),
                   dict(node("tests", "outcome", "reported_complete", "pytest 105건 통과 보고"), step="check"),
                   node("claim", "outcome", "reported_complete", "내부 최댓값 가설 미지지"),
                   node("broke", "outcome", "observed_failure", "다운로드 실패"))
    events[0]["actor"] = "user"
    diagram, left = lanes(events, [])
    # A claim in words and a failure with no relation stay in the story; so does every code change.
    assert left == {"ask", "code", "claim", "broke"}
    assert diagram.beside == {"readme": "code", "tests": "code"}
    assert "! 완료 보고·미검증" in "\n".join(diagram.lines())  # a reported pass keeps its warning on the right


def test_a_flow_of_only_checks_stays_one_column_and_time_placement_keeps_to_the_session():
    events = timed(node("a", "outcome", "observed_success", "검사 A"), node("b", "outcome", "observed_success", "검사 B"))
    diagram, left = lanes(events, [])
    assert left == {"a", "b"} and diagram.beside == {}
    events = timed(node("s1", "action", "applied", "세션 1 변경", session="one"),
                   node("s2", "action", "applied", "세션 2 변경", session="two"),
                   node("c1", "outcome", "observed_success", "세션 1 검사", session="one"))
    diagram, _ = lanes(events, [link("s1", "s2", "motivates")])
    assert diagram.beside == {"c1": "s1"}  # not the later step of the other session


def test_a_box_beside_the_selected_step_is_not_dimmed():
    import curses
    from contexttrail.ui import FlowPanels
    graph = {"events": timed(node("month", "decision", "adopted", "한 달 자료로 확대"),
                             node("hash", "outcome", "observed_success", "산출물 해시 일치"),
                             node("other", "decision", "adopted", "재현 분리")), "edges": []}
    panels = FlowPanels(graph, lambda _: None)
    panels._drawn = diagram = panels.diagram(120)
    panels.current = "month"
    hash_row = diagram.rows[diagram.boxes["hash"][0] + 1]
    assert not any(attr & curses.A_DIM for text, attr in panels._cell_runs(hash_row) if "해시" in text)
    panels.current = "other"
    assert all(attr & curses.A_DIM for text, attr in panels._cell_runs(hash_row) if "해시" in text)
