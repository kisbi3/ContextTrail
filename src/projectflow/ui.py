from __future__ import annotations

import curses
import locale
import os
import queue
import sqlite3
import threading
import time
import unicodedata
from typing import Callable

from wcwidth import wcswidth

from .diagram import HEAVY, Diagram, _wrap, flow_diagram, request_turns
from .render import (ASCII_MARK, MARK, event_detail, linked_events, status_labels, status_tones,
                     terminal_graph)
from .agent_view import reference
from .analysis import plan_choices_text, plan_text
from .store import Store
from .util import FlowError, cell_slice, safe_text
from .webview import LocalViewer


def token_usage_label(tokens: int, known_calls: int, calls: int) -> str:
    if not calls:
        return "분석 토큰 0"
    if not known_calls:
        return f"분석 토큰 미확인 (0/{calls} 호출)"
    suffix = f"+ ({known_calls}/{calls} 호출)" if known_calls < calls else ""
    return f"분석 토큰 {tokens:,}{suffix}"


def _wrap_cells(value: str, width: int) -> list[str]:
    """Wrap terminal text at display-cell boundaries, including wide Korean glyphs."""
    from wcwidth import wcwidth
    width = max(1, width)
    lines: list[str] = []
    for paragraph in safe_text(value).splitlines() or [""]:
        line, cells = "", 0
        for char in paragraph:
            size = max(0, wcwidth(char))
            if line and cells + size > width:
                lines.append(line)
                line, cells = "", 0
            line += char
            cells += size
        lines.append(line)
    return lines


def _printable(text: str) -> str:
    # Keep indentation (safe_text(multiline=False) would collapse it); drop control characters.
    return "".join(c for c in text.replace("\t", "    ") if unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def _put_runs(screen, row: int, col: int, runs: list[tuple[str, int]], width: int, skip: int = 0) -> None:
    """Draw (text, attr) runs as display cells from `skip`, clipped to `width` cells."""
    from wcwidth import wcwidth
    height, screen_width = screen.getmaxyx()
    width = min(width, screen_width - col - 1)
    if not 0 <= row < height or col < 0 or width <= 0:
        return
    position, x = 0, col
    for text, attr in runs:
        visible, cells = "", 0
        for char in _printable(text):
            size = max(0, wcwidth(char))
            if position + size <= skip:
                position += size
                continue
            if position < skip:  # a wide glyph cut by the horizontal scroll
                char, size = " ", position + size - skip
            if x - col + cells + size > width:
                break
            visible += char
            cells += size
            position += size
        if visible:
            try:
                screen.addstr(row, x, visible, attr)
            except curses.error:
                pass
            x += cells
        if x - col >= width:
            return


def _shorten(text: str, width: int) -> str:
    return text if wcswidth(text) <= width else cell_slice(text, 0, max(0, width - 1)) + "…"


def _styles(color: bool) -> dict[str, int]:
    """curses attributes per detail style. Tones keep their glyph (✓ ! ✗) without color."""
    styles = {"": 0, "plain": 0, "title": curses.A_BOLD, "heading": curses.A_BOLD, "dim": curses.A_DIM,
              "ok": 0, "warn": curses.A_BOLD, "fail": curses.A_BOLD, "frame": 0}
    if not color or os.environ.get("NO_COLOR"):
        return styles
    try:
        if not curses.has_colors():
            return styles
        curses.start_color()
        curses.use_default_colors()
        for number, (name, value) in enumerate((("frame", curses.COLOR_CYAN), ("ok", curses.COLOR_GREEN),
                                                ("warn", curses.COLOR_YELLOW), ("fail", curses.COLOR_RED)), 1):
            curses.init_pair(number, value, -1)
            styles[name] |= curses.color_pair(number)
        styles["heading"] |= curses.color_pair(1)
    except curses.error:
        pass
    return styles


# Keys typed while a Korean input method is on arrive as jamo; each is read as the key in its place.
JAMO = dict(zip("ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔㅁㄴㅇㄹㅎㅗㅓㅏㅣㅋㅌㅊㅍㅠㅜㅡ", "qwertyuiopasdfghjklzxcvbnm"))
JAMO.update(zip("ㅃㅉㄸㄲㅆㅒㅖ", "QWERTOP"))
ESC = "\x1b"
BACK = (curses.KEY_BACKSPACE, "\x7f", "\b")
ENTER = ("\n", "\r", curses.KEY_ENTER)
# ncurses before mouse version 2 has no button 5 and reports wheel-down as a position report.
WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0) or curses.REPORT_MOUSE_POSITION
CLICK = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED

# The footer lists the common panel keys; `?` shows every key (HELP plus the screen's own).
KEYS = "↑↓←→ 이동  [ ] 요청  1-9 연결  ! 확인 필요  / 검색  Tab 상세  ? 도움말"
HELP = [
    ("이동", [("↑ ↓   j k", "이전·다음 사건. 요청별 흐름에서는 차례 끝에서 다음 요청으로"),
             ("← →   h l", "옆 칸의 이어진 상자로(변경 ↔ 오른쪽 결과). 가지 목록에서는 가로 스크롤"),
             ("PgUp PgDn   Space", "한 화면씩"),
             ("g G   Home End", "처음·마지막 사건"),
             ("[ ]", "이전·다음 요청"),
             ("1-9", "선택한 사건 칸의 번호 붙은 연결로"),
             ("Backspace", "이동하기 전 사건으로"),
             ("!", "다음 확인 필요한 곳(! ✗). 끝에서 처음으로"),
             ("/   n N", "제목·요약·원문 근거 검색, 다음·이전 결과")]),
    ("에이전트", [("y", "선택한 사건의 참조(contexttrail:ev_…@v12) 복사. Claude Code·Codex 대화에 붙여넣으면 "
                       "에이전트가 근거와 함께 읽음")]),
    ("보기", [("Tab   Enter", "흐름 칸 ↔ 선택한 사건 칸"),
             ("J K", "칸을 옮기지 않고 선택한 사건 칸 스크롤"),
             ("e", "원문 근거 전부 펼치기 ↔ 8줄까지"),
             ("z", "지금 칸을 화면 전체로 ↔ 되돌리기"),
             ("A", "전체 흐름 ↔ 요청별 흐름"),
             ("Esc", "검색·크게 보기 닫기, 선택한 사건 칸에서 흐름 칸으로"),
             ("?", "이 도움말")]),
    ("마우스", [("클릭", "상자·요청·번호 붙은 연결로. 선이나 [02] 동기 ▶ 같은 표시는 이어진 상자로"),
              ("휠", "흐름에서 사건, 요청 목록에서 요청 이동, 선택한 사건 칸 스크롤"),
              ("--no-mouse", "마우스를 끄고 터미널의 글자 선택을 씀")]),
]


def copy_text(text: str) -> str | None:
    """Put text on the clipboard: pbcopy on a local Mac, else OSC 52 (most terminals, also over SSH).

    Returns "pbcopy" when that surely worked, "osc52" when the terminal was asked, None otherwise.
    """
    import base64
    import subprocess
    import sys
    sequence = f"\033]52;c;{base64.b64encode(text.encode()).decode()}\a"
    if os.environ.get("TMUX"):
        sequence = "\033Ptmux;" + sequence.replace("\033", "\033\033") + "\033\\"
    sent = False
    try:
        os.write(sys.__stdout__.fileno(), sequence.encode())
        sent = True
    except (OSError, AttributeError, ValueError):
        pass
    if sys.platform == "darwin" and not os.environ.get("SSH_CONNECTION"):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True, timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "pbcopy"
        except (OSError, subprocess.SubprocessError):
            pass
    return "osc52" if sent else None


def latin(key):
    return JAMO.get(key, key) if isinstance(key, str) else key


def legend(ascii_only: bool = False) -> str:
    marks = ASCII_MARK if ascii_only else MARK
    return f"{marks['ok']} 확인됨  {marks['warn']} 확인 필요  {marks['fail']} 실패"


class FlowPanels:
    """The request list, the event flow and the selected event's detail, shared by both screens.

    When the graph has user messages, the flow shows the turn of one request at a time
    (`diagram.request_turns`): `[` `]` move between requests, A shows everything. The list of
    requests takes the left side on a wide terminal and folds into the flow panel's title
    otherwise. The flow is drawn as boxes and arrows (`diagram.flow_diagram`) when the panel is
    wide enough, else as the branching text list; the selected box's links are lit and the rest
    dimmed. The detail numbers its links 1–9: a number jumps there, Backspace goes back.
    Selection is by event, so it survives every view. `?` lists every key (HELP); the mouse
    selects what it clicks and the wheel moves or scrolls the panel under it.
    """

    LIST_COLUMNS = 180  # narrower terminals fold the request list away
    QUOTE_LINES = 8

    def __init__(self, graph: dict, evidence: Callable[[str], dict | None], *, ascii_only: bool = False,
                 app_keys: tuple[tuple[str, str], ...] = ()):
        self.evidence, self.ascii_only, self.app_keys = evidence, ascii_only, app_keys
        self.marks = ASCII_MARK if ascii_only else MARK
        self.styles = _styles(False)
        self.graph: dict | None = None
        self.current: str | None = None
        self.focus = "graph"
        self.whole = self.expand = self.zoom = self.help = False
        self.typing: str | None = None  # the search being typed after `/`
        self.query, self.matches, self.message = "", [], ""
        self.history: list[str] = []
        self.graph_top = self.detail_top = self.left = self.list_top = self.help_top = 0
        self._shown: tuple[bool, int | None] | None = None
        self._width = 0
        self._drawn: Diagram | None = None
        self._areas: dict[str, tuple[int, int, int, int]] = {}
        self._list_owners: list[int] = []
        self.set_graph(graph)

    def set_graph(self, graph: dict) -> None:
        keep = self.current
        self.graph = graph
        self.lines = terminal_graph(graph, ascii_only=self.ascii_only, marks=True)
        self.labels, self.tones = status_labels(graph), status_tones(graph)
        self.numbers = {event["id"]: f"[{n:02d}]" for n, event in enumerate(graph["events"], 1)}
        self.position = {event["id"]: n for n, event in enumerate(graph["events"])}
        self.titles = {event["id"]: event["title"] for event in graph["events"]}
        self.edges = {edge["id"]: edge for edge in graph["edges"] if edge["active"]}
        self.turns = request_turns(graph)
        self.turn_of = {key: n for n, (_, keys) in enumerate(self.turns) for key in keys}
        ids = [event["id"] for event in graph["events"]]
        self.current = keep if keep in ids else (ids[0] if ids else None)
        self.history = [key for key in self.history if key in ids]
        self.matches = [key for key in self.matches if key in self.position]
        # Until the first draw, arrow keys follow the text list's order.
        self._order = list(dict.fromkeys(event_id for _, event_id in self.lines if event_id))
        self._diagrams: dict[tuple[int | None, int], Diagram | None] = {}
        self._details: dict[tuple[str | None, int, bool], tuple[list[tuple[str, str]], list[int | None]]] = {}
        self._near: tuple[str | None, set[str], set[str]] = (None, set(), set())
        self._haystack: dict[str, str] | None = None

    def selected_id(self) -> str | None:
        return self.current

    def modal(self) -> bool:
        """True while the panels take every key (help shown or a search being typed)."""
        return self.help or self.typing is not None

    def status_line(self) -> str | None:
        """What the footer shows instead of the key list: the search being typed or a notice."""
        if self.typing is not None:
            return f"/{self.typing}▌   Enter 찾기 · Esc 취소"
        return self.message or None

    def turn(self) -> int | None:
        """The request whose turn the flow shows; None while the whole flow is shown."""
        if self.whole or self.current not in self.turn_of:
            return None
        return self.turn_of[self.current]

    def diagram(self, width: int, turn: int | None = None) -> Diagram | None:
        if (turn, width) not in self._diagrams:
            only = None if turn is None else self.turns[turn][1]
            self._diagrams[(turn, width)] = flow_diagram(self.graph, width, ascii_only=self.ascii_only, only=only)
        return self._diagrams[(turn, width)]

    def _near_links(self) -> tuple[set[str], set[str]]:
        """The links of the selected event and the events at their other ends."""
        if self._near[0] != self.current:
            edges = [edge for edge in self.edges.values() if self.current in (edge["from_event_id"], edge["to_event_id"])]
            events = {edge[end] for edge in edges for end in ("from_event_id", "to_event_id")} | {self.current}
            self._near = (self.current, {edge["id"] for edge in edges}, events)
        return self._near[1], self._near[2]

    def _runs(self, text: str, event_id: str | None, base: int, width: int) -> list[tuple[str, int]]:
        """Color an event line's glyph and status label with its tone.

        A line wider than the panel shortens its title first, so the status stays visible.
        """
        if not event_id:
            return [(text, base)]
        tone = self.tones[event_id]
        mark = self.marks[tone]
        head, found, rest = text.partition("] " + mark + " ")
        if not found:
            return [(text, base)]
        runs = [(head + "] ", base), (mark, base | self.styles[tone]), (" ", base)]
        label = safe_text(self.labels[event_id], multiline=False)
        if not rest.endswith(" / " + label):
            return runs + [(rest, base)]
        title = rest[:-len(label) - 3]
        if self.left == 0 and wcswidth(text) > width:
            room = width - wcswidth(head) - 4 - wcswidth(label) - 3
            if room >= 8:
                title = _shorten(title, room)
            else:
                return runs + [(_shorten(title, width - wcswidth(head) - 4), base)]
        return runs + [(title + " / ", base), (label, base | self.styles[tone])]

    def _cell_runs(self, cells) -> list[tuple[str, int]]:
        """A diagram row as (text, attr) runs.

        The selected box gets heavy, bold lines; its links are lit and boxes it is not linked
        to are dimmed. Titles of search matches are underlined.
        """
        near_edges, near_events = self._near_links() if self.current is not None else (set(), set())
        found = set(self.matches)
        runs: list[tuple[str, int]] = []
        for cell in cells:
            if cell.char == "":  # right half of a wide glyph
                continue
            char, attr = cell.char, self.styles.get(cell.style, 0)
            if self.current is not None:
                if cell.edges & near_edges:
                    attr = (attr & ~curses.A_DIM) | curses.A_BOLD | self.styles["frame"]
                elif cell.event is not None and cell.event not in near_events:
                    attr |= curses.A_DIM
            if cell.border and cell.event is not None and cell.event == self.current:
                char, attr = char.translate(HEAVY), attr | curses.A_BOLD
            if cell.style == "title" and cell.event in found:
                attr |= curses.A_UNDERLINE
            if runs and runs[-1][1] == attr:
                runs[-1] = (runs[-1][0] + char, attr)
            else:
                runs.append((char, attr))
        return runs

    def _detail(self, event_id: str | None, width: int) -> tuple[list[tuple[str, str]], list[int | None]]:
        """The detail's wrapped lines, and for each line the link number it belongs to."""
        key = (event_id, width, self.expand)
        if key not in self._details:
            lines: list[tuple[str, str]] = []
            links: list[int | None] = []
            linked = linked_events(self.graph, event_id) if event_id else []
            source = (event_detail(self.graph, event_id, self.evidence, ascii_only=self.ascii_only, keys=True,
                                   quote_lines=10 ** 6 if self.expand else self.QUOTE_LINES)
                      if event_id else [("사건을 선택하면 설명과 원문 근거가 표시됩니다.", "dim")])
            for text, style in source:
                indent = len(text) - len(text.lstrip(" "))
                parts = _wrap_cells(text[indent:], max(1, width - indent)) if text.strip() else [""]
                lines += [(" " * indent + part, style) for part in parts]
                number = None
                if len(text) > 3 and text.startswith("  ") and text[2] in "123456789" and text[3] == " ":
                    candidate = int(text[2])
                    if candidate <= len(linked) and self.numbers[linked[candidate - 1]] in text:
                        number = candidate
                links += [number] * len(parts)
            self._details[key] = (lines, links)
        return self._details[key]

    def _requests(self, width: int) -> tuple[list[list[tuple[str, int]]], list[int], tuple[int, int]]:
        """The request list as rows of runs, wrapped; the request of each row; the selected entry's rows."""
        rows: list[list[tuple[str, int]]] = []
        owners: list[int] = []
        chosen = (0, 0)
        pointer = ">" if self.ascii_only else "▸"
        selected = self.turn_of.get(self.current)
        for n, (request, keys) in enumerate(self.turns):
            here = n == selected
            lead = f"{pointer} " if here else "  "
            strong = curses.A_BOLD if here else 0
            first = len(rows)
            if request is None:
                for part in _wrap(f"요청 기록 없는 사건 {len(keys)}개", max(1, width - 4)):
                    rows.append([(lead if len(rows) == first else "  ", strong), ("  " + part, self.styles["dim"] | strong)])
            else:
                tone = self.tones[request]
                head = f"{self.marks[tone]} {self.numbers[request]} "
                parts = _wrap(safe_text(self.titles[request], multiline=False), max(1, width - 2 - wcswidth(head)))
                for m, part in enumerate(parts):
                    if m == 0:
                        rows.append([(lead, strong), (self.marks[tone], self.styles[tone] | strong),
                                     (head[len(self.marks[tone]):] + part, strong)])
                    else:
                        rows.append([("  " + " " * wcswidth(head) + part, strong)])
            owners += [n] * (len(rows) - first)
            if here:
                chosen = (first, len(rows) - 1)
        return rows, owners, chosen

    def _panel(self, screen, top: int, left: int, height: int, width: int, title: str) -> None:
        if height < 3 or width < 4:
            return
        frame = self.styles["frame"]
        corners = "++++" if self.ascii_only else "╭╮╰╯"
        horizontal, vertical = ("-", "|") if self.ascii_only else ("─", "│")
        _put_runs(screen, top, left, [(corners[0] + horizontal * (width - 2) + corners[1], frame)], width + 1)
        _put_runs(screen, top, left + 2, [(f" {title} ", frame | curses.A_BOLD)], width - 3)
        for row in range(top + 1, top + height - 1):
            _put_runs(screen, row, left, [(" " * width, 0)], width)
            _put_runs(screen, row, left, [(vertical, frame)], 1)
            _put_runs(screen, row, left + width - 1, [(vertical, frame)], 1)
        _put_runs(screen, top + height - 1, left, [(corners[2] + horizontal * (width - 2) + corners[3], frame)], width + 1)

    def _scroll_to(self, top: int, height: int, visible: int) -> None:
        if top < self.graph_top:
            self.graph_top = top
        elif top + height > self.graph_top + visible:
            self.graph_top = max(0, min(top, top + height - visible))

    def _flow_title(self, turn: int | None) -> str:
        parts = ["사건 흐름"]
        if self.turns and turn is None:
            parts.append("전체")
        elif self.turns:
            request = self.turns[turn][0]
            where = f"요청 {sum(1 for key, _ in self.turns[:turn + 1] if key)}/{sum(1 for key, _ in self.turns if key)}"
            parts.append(where + " " + self.numbers[request] if request else "요청 기록 없는 사건")
        if self.query:
            parts.append(f"검색 '{self.query}' {len(self.matches)}개")
        if self.zoom:
            parts.append("크게 보기")
        return " · ".join(parts)

    def _help_lines(self) -> list[tuple[str, str]]:
        sections = HELP + ([("이 화면", list(self.app_keys))] if self.app_keys else [])
        lines: list[tuple[str, str]] = []
        for heading, keys in sections:
            lines += [(heading, "heading")] + [(f"  {key}{' ' * (18 - wcswidth(key))} {text}", "")
                                               for key, text in keys] + [("", "")]
        lines.append(("한글 입력 상태에서도 같은 자리의 키로 동작합니다. 아무 키나 누르면 닫힙니다.", "dim"))
        return lines

    def draw(self, screen, top: int, height: int, columns: int) -> None:
        columns -= 1  # the last screen column is not drawn, so frames end one short
        listing = bool(self.turns) and columns >= self.LIST_COLUMNS and not self.zoom
        list_box = graph_box = detail_box = None
        if self.zoom:
            whole = (top, 0, height, columns)
            graph_box, detail_box = (whole, None) if self.focus == "graph" else (None, whole)
        elif columns >= 150:
            list_width = max(28, min(36, columns // 6)) if listing else 0
            detail_width = max(46, columns * (30 if listing else 36) // 100)
            if listing:
                list_box = (top, 0, height, list_width)
            graph_box = (top, list_width, height, columns - detail_width - list_width)
            detail_box = (top, columns - detail_width, height, detail_width)
        else:
            graph_h = max(8, height * 3 // 5)
            graph_box, detail_box = (top, 0, graph_h, columns), (top + graph_h, 0, height - graph_h, columns)
        self._areas = {name: box for name, box in (("list", list_box), ("graph", graph_box), ("detail", detail_box))
                       if box}
        pointer = " <" if self.ascii_only else " ◀"
        turn = self.turn()
        if self._shown != (self.whole, turn):  # another request or view: start from its top
            self._shown, self.graph_top = (self.whole, turn), 0
        if list_box:
            list_y, list_x, list_h, list_w = list_box
            self._panel(screen, *list_box, f"요청 {sum(1 for key, _ in self.turns if key)}개")
            rows, self._list_owners, (first, last) = self._requests(list_w - 4)
            visible = max(1, list_h - 2)
            if first < self.list_top:
                self.list_top = first
            elif last >= self.list_top + visible:
                self.list_top = min(first, last - visible + 1)
            self.list_top = min(self.list_top, max(0, len(rows) - visible))
            for offset, runs in enumerate(rows[self.list_top:self.list_top + visible]):
                _put_runs(screen, list_y + 1 + offset, list_x + 2, runs, list_w - 4)
        if graph_box:
            graph_y, graph_x, graph_h, graph_w = graph_box
            self._panel(screen, *graph_box, self._flow_title(turn) + (pointer if self.focus == "graph" else ""))
            visible, inner = max(1, graph_h - 2), max(1, graph_w - 4)
            self._width = inner
            diagram = self._drawn = self.diagram(inner, turn)
            if diagram is not None:
                self._order = diagram.order()
                if self.current in diagram.boxes:
                    box_top, _, box_height, _ = diagram.boxes[self.current]
                    self._scroll_to(box_top, box_height, visible)
                self.graph_top = min(self.graph_top, max(0, len(diagram.rows) - visible))
                for offset, cells in enumerate(diagram.rows[self.graph_top:self.graph_top + visible]):
                    _put_runs(screen, graph_y + 1 + offset, graph_x + 2, self._cell_runs(cells), inner)
            else:
                chosen = next((n for n, (_, event_id) in enumerate(self.lines) if event_id == self.current), 0)
                self._order = list(dict.fromkeys(event_id for _, event_id in self.lines if event_id))
                self._scroll_to(chosen, 1, visible)
                self.graph_top = min(self.graph_top, max(0, len(self.lines) - visible))
                for offset, (line, event_id) in enumerate(self.lines[self.graph_top:self.graph_top + visible]):
                    base = curses.A_REVERSE | curses.A_BOLD if self.graph_top + offset == chosen and event_id else 0
                    _put_runs(screen, graph_y + 1 + offset, graph_x + 2, self._runs(line, event_id, base, inner),
                              inner, self.left)
        if detail_box:
            detail_y, detail_x, detail_h, detail_w = detail_box
            title = "선택한 사건" + (" · 크게 보기" if self.zoom else "")
            self._panel(screen, *detail_box, title + (pointer if self.focus == "detail" else ""))
            detail, _ = self._detail(self.current, max(1, detail_w - 4))
            visible = max(1, detail_h - 2)
            self.detail_top = min(self.detail_top, max(0, len(detail) - visible))
            for offset, (line, style) in enumerate(detail[self.detail_top:self.detail_top + visible]):
                _put_runs(screen, detail_y + 1 + offset, detail_x + 2, [(line, self.styles.get(style, 0))],
                          max(1, detail_w - 4))
        if self.help:
            lines = self._help_lines()
            self._panel(screen, top, 0, height, columns, "키 도움말 · 아무 키나 누르면 닫힘")
            visible = max(1, height - 2)
            self.help_top = min(self.help_top, max(0, len(lines) - visible))
            for offset, (line, style) in enumerate(lines[self.help_top:self.help_top + visible]):
                _put_runs(screen, top + 1 + offset, 2, [(line, self.styles.get(style, 0))], columns - 4)

    def select(self, event_id: str, *, remember: bool = False) -> None:
        if event_id == self.current:
            return
        if remember and self.current is not None:
            self.history = (self.history + [self.current])[-50:]
        self.current, self.detail_top = event_id, 0

    def _step(self, direction: int, step: int) -> None:
        """Move through the drawn order; past either end of a request's turn, into the next one."""
        if not self._order:
            return
        index = self._order.index(self.current) if self.current in self._order else 0
        moved = index + direction * step
        turn = self.turn()
        if turn is not None and not 0 <= moved < len(self._order):
            neighbour = turn + direction
            if 0 <= neighbour < len(self.turns):
                drawn = self.diagram(self._width, neighbour) if self._width else None
                keys = drawn.order() if drawn else self.turns[neighbour][1]
                self.select(keys[0] if direction > 0 else keys[-1])
                return
        self.select(self._order[max(0, min(len(self._order) - 1, moved))])

    def _side(self, direction: int) -> None:
        """Move to the box beside the selected one: a linked box first, else one level with it."""
        diagram = self._drawn
        if diagram is None or self.current not in diagram.boxes:
            return
        top, left, height, _ = diagram.boxes[self.current]
        _, near = self._near_links()

        def rank(key: str) -> tuple[bool, int, int]:
            other_top, _, other_height, _ = diagram.boxes[key]
            gap = max(0, other_top - (top + height - 1), top - (other_top + other_height - 1))
            return key not in near, gap, other_top

        beside = sorted((key for key, box in diagram.boxes.items() if (box[1] - left) * direction > 0), key=rank)
        if beside and (beside[0] in near or rank(beside[0])[1] == 0):
            self.select(beside[0])

    def _flagged(self) -> None:
        """Jump to the next event that needs a look (! or ✗), wrapping around."""
        flagged = [event["id"] for event in self.graph["events"] if self.tones[event["id"]] in ("warn", "fail")]
        if not flagged:
            self.message = "확인이 필요한 사건이 없습니다"
            return
        here = self.position.get(self.current, -1)
        target = next((key for key in flagged if self.position[key] > here), flagged[0])
        self.select(target, remember=True)
        self.message = (f"확인 필요 {flagged.index(target) + 1}/{len(flagged)} · {self.numbers[target]} "
                        f"{safe_text(self.labels[target], multiline=False)}")

    def _search_text(self) -> dict[str, str]:
        """Each event's title, summary and quoted evidence, folded for matching."""
        if self._haystack is None:
            self._haystack = {}
            for event in self.graph["events"]:
                quotes = [(self.evidence(key) or {}).get("quote", "") for key in event["evidence_ids"]]
                self._haystack[event["id"]] = "\n".join([event["title"], event["summary"], *quotes]).casefold()
        return self._haystack

    def _search(self, query: str) -> None:
        self.query = query
        needle = query.casefold()
        self.matches = sorted((key for key, text in self._search_text().items() if needle in text),
                              key=self.position.get) if query else []
        if query and not self.matches:
            self.message = f"'{query}'을(를) 찾지 못했습니다"
        elif query:
            self._next_match(1, inclusive=True)

    def _next_match(self, direction: int, *, inclusive: bool = False) -> None:
        if not self.matches:
            self.message = "/로 먼저 검색하세요" if not self.query else f"'{self.query}'을(를) 찾지 못했습니다"
            return
        here = self.position.get(self.current, -1)
        ahead = [key for key in self.matches if (self.position[key] - here) * direction > 0
                 or (inclusive and key == self.current)]
        target = (ahead[0] if direction > 0 else ahead[-1]) if ahead else (
            self.matches[0] if direction > 0 else self.matches[-1])
        self.select(target, remember=True)
        self.message = f"검색 '{self.query}' {self.matches.index(target) + 1}/{len(self.matches)}"

    def _type(self, key) -> None:
        if key == ESC:
            self.typing = None
        elif key in ENTER:
            query, self.typing = self.typing.strip(), None
            self._search(query)
        elif key in BACK:
            self.typing = self.typing[:-1]
        elif key == "\x15":  # Ctrl-U
            self.typing = ""
        elif isinstance(key, str) and key.isprintable():
            self.typing += key

    def _follow(self, number: int) -> None:
        linked = linked_events(self.graph, self.current) if self.current else []
        if number <= len(linked):
            self.select(linked[number - 1], remember=True)

    def _mouse(self) -> None:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return
        area = next((name for name, (top, left, height, width) in self._areas.items()
                     if top <= y < top + height and left <= x < left + width), None)
        if area is None:
            return
        top, left, _, _ = self._areas[area]
        row, col = y - top - 1, x - left - 2  # inside the frame and its padding
        if state & (curses.BUTTON4_PRESSED | WHEEL_DOWN):
            direction = -1 if state & curses.BUTTON4_PRESSED else 1
            if area == "detail":
                self.detail_top = max(0, self.detail_top + 3 * direction)
            elif area == "list":
                self._request(direction)
            else:
                self._step(direction, 1)
            return
        if not state & CLICK or row < 0:
            return
        if area == "list":
            index = self.list_top + row
            if index < len(self._list_owners):
                self.select(self.turns[self._list_owners[index]][1][0])
        elif area == "detail":
            self.focus = "detail"
            _, links = self._detail(self.current, max(1, self._areas["detail"][3] - 4))
            index = self.detail_top + row
            if index < len(links) and links[index]:
                self._follow(links[index])
        else:
            self.focus = "graph"
            self._click_flow(self.graph_top + row, col)

    def _click_flow(self, row: int, col: int) -> None:
        """Select what was clicked: a box, the box a note or reference names, or the far end of a line."""
        diagram = self._drawn
        if diagram is None:
            if 0 <= row < len(self.lines) and self.lines[row][1]:
                self.select(self.lines[row][1])
            return
        if not (0 <= row < len(diagram.rows) and 0 <= col < diagram.width):
            return
        cell = diagram.rows[row][col]
        if cell.char == "" and col > 0:  # right half of a wide glyph
            cell = diagram.rows[row][col - 1]
        edges = [self.edges[key] for key in cell.edges if key in self.edges]
        if cell.border or not edges:
            if cell.event is not None:
                self.select(cell.event)
            return
        if cell.event is not None:
            box = diagram.boxes.get(cell.event)
            inside = box and box[0] <= row < box[0] + box[2] and box[1] <= col < box[1] + box[3]
            if not (inside and len(edges) == 1):  # "[08] ↑ 위" names its box
                self.select(cell.event, remember=True)
                return
            owner = cell.event  # "← [02] 동기" inside a box names the other end
        else:
            owner = self.current
        sources = {edge["from_event_id"] for edge in edges}
        if len(edges) == 1 and owner in (edges[0]["from_event_id"], edges[0]["to_event_id"]):
            edge = edges[0]
            target = edge["to_event_id"] if edge["from_event_id"] == owner else edge["from_event_id"]
        elif len(sources) == 1 and owner not in sources:
            # A rail or its arrival label ("[02] 동기 ▶") leads back to where it left.
            margin = min(box[1] for box in diagram.boxes.values())
            target = sources.pop() if col < margin or len(edges) > 1 else edges[0]["to_event_id"]
        else:
            return
        self.select(target, remember=True)

    def _request(self, direction: int) -> None:
        if self.current in self.turn_of:
            turn = max(0, min(len(self.turns) - 1, self.turn_of[self.current] + direction))
            self.select(self.turns[turn][1][0])

    def handle(self, key) -> bool:
        """Move within the panels; returns False for keys the screen itself handles."""
        self.message = ""
        if self.help:
            key = latin(key)
            if key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", curses.KEY_NPAGE, curses.KEY_PPAGE, " "):
                self.help_top = max(0, self.help_top + (-1 if key in (curses.KEY_UP, "k") else 1) *
                                    (8 if key in (curses.KEY_NPAGE, curses.KEY_PPAGE, " ") else 1) *
                                    (-1 if key == curses.KEY_PPAGE else 1))
            else:
                self.help = False
            return True
        if self.typing is not None:
            self._type(key)
            return True
        if key == curses.KEY_MOUSE:
            self._mouse()
            return True
        key = latin(key)
        if key in ("\t", *ENTER):
            self.focus = "detail" if self.focus == "graph" else "graph"
        elif key == ESC:
            if self.zoom:
                self.zoom = False
            elif self.focus == "detail":
                self.focus = "graph"
            elif self.query:
                self._search("")
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", curses.KEY_NPAGE, curses.KEY_PPAGE, " "):
            step = 5 if key in (curses.KEY_NPAGE, curses.KEY_PPAGE, " ") else 1
            direction = -1 if key in (curses.KEY_UP, "k", curses.KEY_PPAGE) else 1
            if self.focus == "detail":
                self.detail_top = max(0, self.detail_top + direction * step * (8 if step > 1 else 1))
            else:
                self._step(direction, step)
        elif key in ("J", "K"):
            self.detail_top = max(0, self.detail_top + (3 if key == "J" else -3))
        elif key in (curses.KEY_LEFT, "h", curses.KEY_RIGHT, "l"):
            direction = -1 if key in (curses.KEY_LEFT, "h") else 1
            if self._drawn is not None:
                self._side(direction)
            else:
                self.left = max(0, self.left + 8 * direction)
        elif key in ("[", "]"):
            self._request(1 if key == "]" else -1)
        elif key in ("a", "A"):
            self.whole = not self.whole
        elif isinstance(key, str) and len(key) == 1 and key in "123456789":
            self._follow(int(key))
        elif key in BACK:
            if self.history:
                self.current, self.detail_top = self.history.pop(), 0
        elif key == "!":
            self._flagged()
        elif key == "/":
            self.typing = ""
        elif key in ("n", "N"):
            self._next_match(1 if key == "n" else -1)
        elif key in ("e", "E"):
            self.expand = not self.expand
            self.message = "원문 근거 전부 펼침" if self.expand else f"원문 근거 {self.QUOTE_LINES}줄까지"
        elif key in ("z", "Z"):
            self.zoom = not self.zoom
        elif key in ("y", "Y"):
            if self.current in {event["id"] for event in self.graph["events"]}:
                ref = reference(self.graph, self.current)
                how = copy_text(ref)
                self.message = (f"복사함: {ref} · 대화에 붙여넣으세요" if how == "pbcopy" else
                                f"복사 요청: {ref} · 안 되면 이 글자를 선택해 복사" if how else
                                f"복사하지 못했습니다. 이 글자를 선택해 복사하세요: {ref}")
        elif key == "?":
            self.help, self.help_top = True, 0
        elif key in (curses.KEY_HOME, "g", curses.KEY_END, "G"):
            if self._order:
                self.current = self._order[0] if key in (curses.KEY_HOME, "g") else self._order[-1]
            self.detail_top = 0
            if key in (curses.KEY_HOME, "g"):
                self.left = self.graph_top = 0
        else:
            return False
        return True


def _start_screen(screen, color: bool, mouse: bool = True) -> dict[str, int]:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    try:
        curses.set_escdelay(25)  # Esc closes things; do not wait a second for an escape sequence
    except (AttributeError, curses.error):
        pass
    if mouse:
        try:
            # Clicks only (no motion reports); wheel-down needs the position bit where button 5 is missing.
            curses.mousemask(curses.ALL_MOUSE_EVENTS |
                             (0 if getattr(curses, "BUTTON5_PRESSED", 0) else curses.REPORT_MOUSE_POSITION))
            curses.mouseinterval(0)
        except curses.error:
            pass
    return _styles(color)


class GraphApp:
    """Read-only graph browser for project stores and isolated eval outputs."""

    def __init__(self, graph: dict, evidence: Callable[[str], dict | None], *, title: str,
                 ascii_only: bool = False, color: bool = True, mouse: bool = True):
        self.title, self.color, self.mouse = title, color, mouse
        self.panels = FlowPanels(graph, evidence, ascii_only=ascii_only, app_keys=(("Q", "종료"),))

    def draw(self, screen) -> None:
        screen.erase()
        rows, cols = screen.getmaxyx()
        graph, styles = self.panels.graph, self.panels.styles
        _put_runs(screen, 0, 1, [("◆ CONTEXTTRAIL", curses.A_BOLD | styles["frame"])], cols)
        _put_runs(screen, 1, 1, [(f"{self.title}   v{graph['version']}   ·   {len(graph['events'])} 사건   ·   "
                                  f"{sum(edge['active'] for edge in graph['edges'])} 관계   ·   "
                                  f"{graph['analysis_status']}", 0)], cols)
        if rows < 14 or cols < 36:
            _put_runs(screen, 3, 1, [("터미널을 넓혀 주세요. Q 종료", 0)], cols)
            screen.refresh()
            return
        self.panels.draw(screen, 3, rows - 5, cols)
        status = self.panels.status_line()
        _put_runs(screen, rows - 2, 1, [(status, 0) if status else (KEYS + "  Q 종료", styles["dim"])], cols)
        _put_runs(screen, rows - 1, 1, [(legend(self.panels.ascii_only) + "   ·   저장된 결과만 표시", styles["dim"])],
                  cols)
        screen.refresh()

    def _run(self, screen) -> None:
        self.panels.styles = _start_screen(screen, self.color, self.mouse)
        while True:
            self.draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if not self.panels.modal() and latin(key) in ("q", "Q"):
                break
            self.panels.handle(key)

    def run(self) -> None:
        try:
            locale.setlocale(locale.LC_ALL, "")
            curses.wrapper(self._run)
        except curses.error as exc:
            raise FlowError("그래프 화면을 열 수 없습니다. TERM과 터미널 크기를 확인하세요.") from exc


class TerminalApp:
    """Curses UI: keyboard-only path, resize handling, and no terminal image extensions."""

    def __init__(self, store: Store, analyze: Callable, *, title: str, initial_analyze: bool = False,
                 ascii_only: bool = False, authorize: Callable | None = None, color: bool = True,
                 mouse: bool = True):
        self.store, self.analyze, self.authorize = store, analyze, authorize
        self.title, self.initial, self.ascii_only, self.color = title, initial_analyze, ascii_only, color
        self.mouse = mouse
        self.cancel = threading.Event()
        self.messages = queue.Queue()
        self.worker = None
        self.viewer = None
        self.notice = "저장된 결과 열람 · 자동 분석 없음"
        self.panels = FlowPanels(store.graph(), lambda key: self._read(lambda: store.evidence(key), None),
                                 ascii_only=ascii_only,
                                 app_keys=(("R", "변경분 분석(이전 그림 유지)"), ("B", "브라우저 주소와 SSH 안내"),
                                           ("Q", "종료. 분석 중이면 취소 확인")))
        self.quit_pending = False
        self._token_checked_at = 0.0
        self._token_totals = (0, 0, 0)
        self._check = {}

    def _read(self, read: Callable, fallback):
        """A read the analysis writer holds up keeps what the screen already shows; it never ends the app."""
        try:
            return read()
        except sqlite3.OperationalError:
            return fallback

    @property
    def graph(self) -> dict:
        return self.panels.graph

    def busy(self):
        return bool((self.worker and self.worker.is_alive()) or
                    (self.viewer and self.viewer.refresh_thread and self.viewer.refresh_thread.is_alive()))

    def _put(self, screen, y, x, value, attr=0):
        _put_runs(screen, y, x, [(safe_text(value).replace("\n", " "), attr)], screen.getmaxyx()[1])

    def ask(self, screen, question: str, choices: str) -> str:
        while True:
            rows, _ = screen.getmaxyx()
            self._put(screen, rows - 2, 0, " " * 400)
            self._put(screen, rows - 2, 0, question, curses.A_REVERSE)
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            key = latin(key)
            if isinstance(key, str) and key.lower() in choices:
                return key.lower()
            if key == curses.KEY_RESIZE:
                self.draw(screen)

    def start_analysis(self, screen):
        if self.busy():
            self.notice = "이미 분석이 진행 중입니다. 중복 실행하지 않습니다."
            return
        if self.authorize and not self.authorize(lambda question, choices: self.ask(screen, question, choices)):
            self.notice = "분석을 시작하지 않았습니다."
            return
        self.cancel.clear()
        self.quit_pending = False
        self.notice = "입력 변화 확인 중 · 이전 그림 유지"
        def run():
            try:
                result = self.analyze(self.cancel, lambda msg: self.messages.put(("status", msg)), self.confirm)
                self.messages.put(("done", result))
            except Exception as exc:
                self.messages.put(("error", str(exc) if isinstance(exc, FlowError) else type(exc).__name__))
        self.worker = threading.Thread(target=run, name="projectflow-analysis", daemon=True)
        self.worker.start()

    def ask_units(self, screen, plan: dict) -> bool | int:
        """How many work units to run: digits then Enter, Enter alone keeps the plan, n or Esc declines."""
        typed = ""
        while True:
            rows, columns = screen.getmaxyx()
            # Wrapped, never cut: the plan, the choices, then the answer being typed, above the legend.
            lines = [(line, curses.A_BOLD) for line in _wrap(plan_text(plan), columns - 1)]
            lines += [(line, 0) for line in _wrap("선택지: " + plan_choices_text(plan), columns - 1)]
            lines += [(line, curses.A_REVERSE) for line in _wrap(
                f"처리할 작업 단위 수를 입력하고 Enter [Enter={plan['units_this_run']}개 · n 취소]: {typed}_",
                columns - 1)]
            top = max(3, rows - 1 - len(lines))
            for offset, (line, attr) in enumerate(lines[-(rows - 1 - top):]):
                self._put(screen, top + offset, 0, " " * 400)
                self._put(screen, top + offset, 0, line, attr)
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            key = latin(key)
            if key in ("\n", "\r", curses.KEY_ENTER):
                return int(typed) if typed and int(typed) > 0 else (True if not typed else False)
            if key in ("n", "N", "\x1b"):
                return False
            if key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                typed = typed[:-1]
            elif isinstance(key, str) and key.isdigit() and len(typed) < 6:
                typed += key
            elif key == curses.KEY_RESIZE:
                self.draw(screen)

    def confirm(self, plan: dict) -> bool | int:
        """Asked from the analysis thread; the screen's loop asks how many units and returns the answer."""
        reply = {"answer": False, "done": threading.Event()}
        self.messages.put(("confirm", (plan, reply)))
        while not reply["done"].wait(0.1):
            if self.cancel.is_set():
                return False
        return reply["answer"]

    def selected_id(self):
        return self.panels.selected_id()

    def draw(self, screen):
        screen.erase()
        rows, columns = screen.getmaxyx()
        graph, styles = self.graph, self.panels.styles
        check = self._check = self._read(lambda: self.store.get_meta("last_check", {}), self._check)
        self._put(screen, 0, 1, f"◆ CONTEXTTRAIL   {self.title}   v{graph['version']}   ·   "
                  f"{len(graph['events'])} 사건 · {sum(e['active'] for e in graph['edges'])} 관계",
                  curses.A_BOLD | styles["frame"])
        self._put(screen, 1, 1, f"분석 기준 {graph.get('analyzed_at') or '없음'}   ·   마지막 확인 "
                  f"{check.get('at', '없음')} ({check.get('status', 'no_data')})", styles["dim"])
        # Before anything happens in this session, a failed last check says why.
        idle = not self.busy() and self.notice.startswith("저장된 결과")
        self._put(screen, 2, 1, "마지막 확인 오류: " + check["error"] if idle and check.get("error") else self.notice)
        if rows < 12 or columns < 36:
            self._put(screen, 4, 0, "화면을 넓히거나 --no-tui 사용. Q 종료")
            screen.refresh()
            return
        self.panels.draw(screen, 3, rows - 5, columns)
        status = self.panels.status_line()
        self._put(screen, rows - 2, 1, status or KEYS + "  R 분석  B 브라우저  Q 종료", 0 if status else styles["dim"])
        if time.monotonic() - self._token_checked_at >= 1:
            self._token_totals = self._read(self.store.llm_token_totals, self._token_totals)
            self._token_checked_at = time.monotonic()
        self._put(screen, rows - 1, 1, f"{legend(self.ascii_only)}   ·   {token_usage_label(*self._token_totals)}"
                  "   ·   자동 감시 없음", styles["dim"])
        screen.refresh()

    def show_browser(self, screen):
        if not self.viewer:
            self.viewer = LocalViewer(self.store, refresh=lambda: self.analyze(self.cancel, lambda _: None)).start()
        address = self.viewer.url(self.graph["version"], self.selected_id())
        rows, cols = screen.getmaxyx()
        screen.erase()
        self._put(screen, 0, 0, "브라우저 상세 보기 · 현재 그래프 버전 고정", curses.A_BOLD)
        self._put(screen, 2, 0, "PC에서 SSH 포트 포워딩 후 아래 주소로 접속하세요.")
        self._put(screen, 4, 0, f"ssh -L 127.0.0.1:{self.viewer.port}:127.0.0.1:{self.viewer.port} user@server")
        for n in range(0, len(address), max(1, cols - 2)):
            self._put(screen, 6 + n // max(1, cols - 2), 0, address[n:n + cols - 2])
        self._put(screen, rows - 2, 0, "접근 주소는 비밀정보입니다. 외부에 공유하지 마세요. 아무 키로 돌아갑니다.")
        screen.refresh()
        while True:
            try:
                screen.get_wch()
                break
            except curses.error:
                continue

    def _run(self, screen):
        self.panels.styles = _start_screen(screen, self.color, self.mouse)
        screen.timeout(100)
        self.draw(screen)
        if self.initial:
            self.start_analysis(screen)
        while True:
            while not self.messages.empty():
                kind, payload = self.messages.get_nowait()
                if kind == "confirm":
                    plan, reply = payload
                    reply["answer"] = self.ask_units(screen, plan)
                    reply["done"].set()
                    if not reply["answer"]:
                        self.notice = "분석을 시작하지 않았습니다."
                elif kind in ("status", "error"):
                    self.notice = payload
                else:
                    self.panels.set_graph(payload["graph"])
                    self.notice = f"{payload['status']} · AI 호출 {payload.get('runner_calls',0)} · " + payload.get("error", "저장된 결과 표시")
            # Another explicit refresh (browser/second terminal) may publish a graph.
            # This reads the local DB only; it never scans sources or calls a model.
            if not self.busy():
                saved = self._read(self.store.graph, self.graph)
                if saved["version"] != self.graph["version"]:
                    self.panels.set_graph(saved)
            if self.quit_pending and not self.busy():
                break
            self.draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if self.panels.modal():  # help or a search being typed takes every key
                self.panels.handle(key)
                continue
            key = latin(key)
            if key in ("q", "Q"):
                if self.busy():
                    if self.ask(screen, "분석을 취소하고 종료할까요? [y/n]", "yn") == "y":
                        self.cancel.set()
                        self.quit_pending = True
                        self.notice = "분석 취소·자식 프로세스 정리 중"
                else:
                    break
            elif key in ("r", "R"):
                self.start_analysis(screen)
            elif key in ("b", "B"):
                self.show_browser(screen)
            else:
                self.panels.handle(key)

    def run(self):
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            locale.setlocale(locale.LC_ALL, "C.UTF-8")
        try:
            curses.wrapper(self._run)
        except curses.error as exc:
            raise FlowError("터미널 초기화/화면 처리 실패. TERM 설정을 확인하거나 --no-tui 또는 --ascii로 실행하세요.") from exc
        finally:
            self.cancel.set()
            if self.worker:
                self.worker.join(timeout=5)
            if self.viewer:
                self.viewer.close()
