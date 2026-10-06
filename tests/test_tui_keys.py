import curses

import pytest
from wcwidth import wcwidth

from contexttrail import ui
from contexttrail.ui import FlowPanels

from test_diagram import conversation, story


class Screen:
    """Enough of a curses window to draw the panels and read the text back."""

    def __init__(self, rows=40, cols=120):
        self.rows, self.cols = rows, cols
        self.cells = [[" "] * cols for _ in range(rows)]

    def getmaxyx(self):
        return self.rows, self.cols

    def addstr(self, row, col, text, attr=0):
        for char in text:
            size = max(1, wcwidth(char))
            if col + size > self.cols:
                raise curses.error("off screen")
            self.cells[row][col] = char
            if size == 2:
                self.cells[row][col + 1] = ""
            col += size

    def text(self):
        return "\n".join("".join(row) for row in self.cells)


def drawn(panels, rows=40, cols=120):
    screen = Screen(rows, cols)
    panels.draw(screen, 0, rows, cols)
    return screen


def evidence_of(graph, lines):
    """One quoted evidence item per event; `lines` long for every event."""
    items = {}
    for event in graph["events"]:
        key = f"q_{event['id']}"
        event["evidence_ids"] = [key]
        items[key] = {"id": key, "quote": "\n".join(f"{event['title']} {n}" for n in range(lines)),
                      "start_line": 1, "end_line": lines, "source": {"role": "tool_call"}}
    return items.get


def test_side_keys_move_between_a_change_and_its_result():
    panels = FlowPanels(story(), lambda _: None)
    panels.select("script")
    drawn(panels)
    panels.handle(curses.KEY_RIGHT)
    assert panels.current == "run"  # the result drawn beside the change
    panels.handle("h")
    assert panels.current == "script"
    panels.select("docs")
    panels.handle("l")
    assert panels.current == "docs"  # nothing beside it: stay


def test_bang_cycles_through_what_needs_a_look():
    panels = FlowPanels(story(), lambda _: None)
    panels.select("q")
    seen = []
    for _ in range(4):
        panels.handle("!")
        seen.append(panels.current)
    assert seen == ["script", "fail", "docs", "answer"]  # ✗, ✗, !, ! in list order
    panels.handle("!")
    assert panels.current == "script" and panels.message.startswith("확인 필요 1/4")  # wraps around
    panels.handle(curses.KEY_BACKSPACE)
    assert panels.current == "answer"  # a jump can be undone


def test_search_is_typed_then_moves_between_matches():
    graph = conversation()
    panels = FlowPanels(graph, evidence_of(graph, 2))
    for key in "/설치":
        panels.handle(key)
    assert panels.modal() and panels.status_line().startswith("/설치")
    panels.handle("\n")
    assert not panels.modal()
    assert panels.matches == ["m1", "run"] and panels.current == "m1"  # the selection itself matches
    assert "검색 '설치' 2개" in drawn(panels).text()
    panels.handle("n")
    assert panels.current == "run"
    panels.handle("n")
    assert panels.current == "m1"  # wraps
    panels.handle("N")
    assert panels.current == "run"
    panels.handle(ui.ESC)
    assert panels.matches == [] and panels.query == ""
    for key in ["/", "q", "\x7f", "없는말", "\n"]:
        panels.handle(key)
    assert panels.message == "'없는말'을(를) 찾지 못했습니다"


def test_evidence_can_be_unfolded_and_the_detail_scrolled_from_the_flow():
    graph = story()
    panels = FlowPanels(graph, evidence_of(graph, 20))
    lines = [text for text, _ in panels._detail("q", 80)[0]]
    assert any("12줄 더 있음" in line for line in lines)
    panels.handle("e")
    lines = [text for text, _ in panels._detail("q", 80)[0]]
    assert not any("더 있음" in line for line in lines) and any("설치가 자동인가 19" in line for line in lines)
    panels.handle("J")
    assert panels.detail_top == 3 and panels.focus == "graph"


def test_zoom_help_and_escape():
    panels = FlowPanels(story(), lambda _: None, app_keys=(("Q", "종료"),))
    panels.handle("z")
    text = drawn(panels).text()
    assert "크게 보기" in text and "선택한 사건" not in text
    panels.handle("\t")
    text = drawn(panels).text()
    assert "선택한 사건 · 크게 보기" in text and "사건 흐름" not in text
    panels.handle(ui.ESC)
    assert not panels.zoom and panels.focus == "detail"
    panels.handle(ui.ESC)
    assert panels.focus == "graph"
    panels.handle("?")
    text = drawn(panels).text()
    assert panels.modal() and "키 도움말" in text and "다음 확인 필요한 곳" in text and "종료" in text
    panels.handle("q")  # closes the help; the screen does not quit
    assert not panels.help


def test_english_screen_language_renders_help_titles_and_footer():
    from contexttrail import i18n
    i18n.set_language("en")
    from contexttrail.diagram import flow_diagram
    panels = FlowPanels(story(), lambda _: None, app_keys=(("Q", "quit"),))
    text = drawn(panels).text()
    assert "Event flow" in text and "Selected event" in text
    boxes = "\n".join(flow_diagram(story(), 110).lines())
    assert "[04] ↑ up" in boxes and "no linked events" in boxes.split("[07]")[1]
    assert ui.keys().startswith("↑↓←→ move") and "verified" in ui.legend()
    panels.handle("?")
    text = drawn(panels).text()
    assert "Key help" in text and "next place that needs a look" in text and "This screen" in text
    assert ui.token_usage_label(150, 1, 2) == "analysis tokens 150+ (1/2 calls)"


def test_keys_typed_in_korean_input_mode_act_as_their_latin_key():
    panels = FlowPanels(story(), lambda _: None)
    drawn(panels)
    panels.handle("ㅓ")
    assert panels.current == "plan"
    panels.handle("ㅏ")
    assert panels.current == "q"
    assert ui.latin("ㅂ") == "q" and ui.latin("ㄲ") == "R" and ui.latin(curses.KEY_UP) == curses.KEY_UP


def click(monkeypatch, panels, x, y, state=curses.BUTTON1_PRESSED):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, x, y, 0, state))
    panels.handle(curses.KEY_MOUSE)


def at(screen, text):
    """Screen (x, y) of the first cell of `text`."""
    for y, row in enumerate(screen.cells):
        columns = [x for x, char in enumerate(row) if char]  # skip the right halves of wide glyphs
        index = "".join(row[x] for x in columns).find(text)
        if index >= 0:
            return columns[index], y
    raise AssertionError(text)


def test_mouse_selects_boxes_follows_labels_and_scrolls(monkeypatch):
    graph = story()
    panels = FlowPanels(graph, evidence_of(graph, 20))
    screen = drawn(panels, rows=60, cols=160)
    click(monkeypatch, panels, *at(screen, "문서 갱신"))
    assert panels.current == "docs"
    screen = drawn(panels, rows=60, cols=160)
    click(monkeypatch, panels, *at(screen, "[03] 수정 ▶"))
    assert panels.current == "script" and panels.history == ["docs"]  # the label names where the rail left
    screen = drawn(panels, rows=60, cols=160)
    x, y = at(screen, "검증한 결과")
    click(monkeypatch, panels, x + 4, y + 1)  # the numbered check under that heading
    assert panels.current in ("run", "fail") and panels.focus == "detail"
    screen = drawn(panels, rows=60, cols=160)
    click(monkeypatch, panels, *at(screen, "원문 근거"), state=ui.WHEEL_DOWN)
    assert panels.detail_top == 3
    before = panels._order.index(panels.current)
    x, y = at(screen, "사건 흐름")
    click(monkeypatch, panels, x, y + 3, state=curses.BUTTON4_PRESSED)  # wheel up over the flow: previous event
    assert panels._order.index(panels.current) == before - 1


def test_mouse_picks_a_request_from_the_list(monkeypatch):
    panels = FlowPanels(conversation(), lambda _: None)
    screen = drawn(panels, rows=40, cols=200)
    assert "요청 2개" in screen.text()
    click(monkeypatch, panels, *at(screen, "문서도 고쳐 줘"))
    assert panels.current == "m2" and panels.turn() == 1
