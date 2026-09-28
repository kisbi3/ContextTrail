"""Boxes and arrows for a terminal, in the manner of cli-diagram.

The left column is the story in recorded order. An observed result that checks a change
and leads nowhere else sits to the right of that change, joined by a labelled arrow, so
"what checked this change" reads straight across; a result that leads on (a failure that
motivated a fix) stays in the story column. Links between neighbouring boxes in the column
are drawn down it; longer ones run on rails in the left margin and arrive under the number of
the box they left ("[02] 동기 ▶"), so a rail need not be traced back. A link that cannot be
drawn is written inside both boxes ("→ [06] 동기", "← [05] 동기"), so every active relation is
visible from each end and a box with no relation at all says so.

Every connector cell records the links it draws, so a screen can light up the links of the
selected box. Drawn for one user request (`only`), links to events outside it are written
inside the box, like links without room.

Events that share no session and no relation are separate flows (two features built in
two unrelated sessions), drawn one after another under their own heading.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from wcwidth import wcswidth, wcwidth

from .render import ASCII_MARK, KIND, MARK, RELATION, _when, status_labels, status_tones
from .util import cell_slice, safe_text

OBSERVED = {"observed_success", "observed_failure"}
# Relations that put a result beside the event it came from, most telling first.
ANCHOR_ORDER = {"verifies": 0, "produces": 1}
LIGHT = {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│", "lt": "├", "rt": "┤",
         "down": "┬", "cross": "┼", "right": "▶", "arrow_down": "▼", "up": "↑ 위", "back": "←", "forward": "→",
         "flow": "═"}
PLAIN = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|", "lt": "+", "rt": "+",
         "down": "+", "cross": "+", "right": ">", "arrow_down": "v", "up": "^ 위", "back": "<-", "forward": "->",
         "flow": "="}
# The selected box is redrawn with heavy lines.
HEAVY = str.maketrans("┌┐└┘─│├┤┬┼", "┏┓┗┛━┃┣┫┳╋")
MAX_RAILS = 4
ARROW_GAP = 11  # between the two columns: "──┬─검증──▶"


@dataclass
class Cell:
    char: str = " "
    style: str = ""
    event: str | None = None
    border: bool = False
    edges: frozenset[str] = frozenset()  # the links this cell helps draw


@dataclass
class Diagram:
    width: int
    rows: list[list[Cell]] = field(default_factory=list)
    boxes: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)  # top, left, height, width

    def order(self) -> list[str]:
        """Events top to bottom, left before right: the order arrow keys move through."""
        return [key for key, _ in sorted(self.boxes.items(), key=lambda item: (item[1][0], item[1][1]))]

    def lines(self) -> list[str]:
        return ["".join(cell.char for cell in row).rstrip() for row in self.rows]

    def _row(self, y: int) -> list[Cell]:
        while len(self.rows) <= y:
            self.rows.append([Cell() for _ in range(self.width)])
        return self.rows[y]

    def put(self, y: int, x: int, text: str, style: str = "", event: str | None = None,
            border: bool = False, edges: frozenset[str] = frozenset()) -> int:
        """Write text at a display column; a wide glyph takes two cells. Returns the next column."""
        row = self._row(y)
        for char in text:
            size = wcwidth(char)
            if size < 1:
                continue
            if x < 0 or x + size > self.width:
                break
            if row[x].char == "" and x > 0:  # overwriting the right half of a wide glyph
                row[x - 1].char = " "
            row[x] = Cell(char, style, event, border, edges)
            if size == 2:
                row[x + 1] = Cell("", style, event, border, edges)
            after = x + size
            if after < self.width and row[after].char == "":
                row[after].char = " "
            x = after
        return x

    def char(self, y: int, x: int) -> str:
        return self.rows[y][x].char if y < len(self.rows) and 0 <= x < self.width else " "

    def links(self, y: int, x: int) -> frozenset[str]:
        return self.rows[y][x].edges if y < len(self.rows) and 0 <= x < self.width else frozenset()


def _wrap(text: str, width: int) -> list[str]:
    """Wrap at spaces; a word longer than a line is cut across lines. Nothing is dropped."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}" if line else word
        if wcswidth(trial) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        while wcswidth(word) > width:
            lines.append(cell_slice(word, 0, width))
            word = word[len(lines[-1]):]
        line = word
    if line:
        lines.append(line)
    return lines or [""]


def _segments(text: str, width: int) -> list[str]:
    """Wrap a "상태 · 검증 …" label between its parts first; continuation lines start with "·"."""
    lines: list[str] = []
    for part in text.split(" · "):
        if lines and wcswidth(f"{lines[-1]} · {part}") <= width:
            lines[-1] += f" · {part}"
        else:
            lines.append(f"· {part}" if lines else part)
    wrapped: list[str] = []
    for line in lines:
        if line.startswith("· "):  # a part longer than a line stays indented under its dot
            pieces = _wrap(line[2:], width - 2)
            wrapped += ["· " + pieces[0]] + ["  " + piece for piece in pieces[1:]]
        else:
            wrapped += _wrap(line, width)
    return wrapped


def _flows(events: list[dict], edges: list[dict]) -> list[list[str]]:
    """Events joined by a session or a relation, as flows in the order of their first event."""
    parent = {event["id"]: event["id"] for event in events}

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def join(one: str, other: str) -> None:
        parent[root(one)] = root(other)

    first: dict[str, str] = {}
    for event in events:
        for session in event.get("session_ids") or []:
            join(event["id"], first.setdefault(session, event["id"]))
    for edge in edges:
        join(edge["from_event_id"], edge["to_event_id"])
    flows: dict[str, list[str]] = {}
    for event in events:
        flows.setdefault(root(event["id"]), []).append(event["id"])
    return list(flows.values())


def request_turns(graph: dict) -> list[tuple[str | None, list[str]]]:
    """Each user message with the events of its turn, in the order the list numbers them.

    A turn is the message and what came after it in the same session before that session's
    next message. An event with no session joins the turn of an event that leads to it;
    events in no turn are gathered under None, placed where the first of them is. Empty when
    the graph has no user message.
    """
    events = graph["events"]
    if not any(event.get("actor") == "user" for event in events):
        return []
    position = {event["id"]: n for n, event in enumerate(events)}
    sources: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        if edge["active"]:
            sources.setdefault(edge["to_event_id"], []).append(edge["from_event_id"])
    latest: dict[str, str] = {}
    where: dict[str, str | None] = {}
    turns: dict[str | None, list[str]] = {}
    for event in events:
        key = event["id"]
        sessions = event.get("session_ids") or []
        if event.get("actor") == "user":
            turn = key
            for session in sessions:
                latest[session] = key
        else:
            found = [latest[session] for session in sessions if session in latest]
            turn = max(found, key=position.get) if found else next(
                (where[source] for source in sources.get(key, []) if where.get(source)), None)
        where[key] = turn
        turns.setdefault(turn, []).append(key)
    return list(turns.items())


def flow_diagram(graph: dict, width: int, *, ascii_only: bool = False,
                 only: Collection[str] | None = None) -> Diagram | None:
    """Lay the graph out as boxes and arrows in `width` cells; None if too narrow to read.

    With `only`, just those events are drawn (one user request's turn); their links to the
    rest are written inside their boxes. Numbers stay those of the whole list.
    """
    number = {event["id"]: f"[{n:02d}]" for n, event in enumerate(graph["events"], 1)}
    events = [event for event in graph["events"] if only is None or event["id"] in only]
    if not events:
        return None
    g = PLAIN if ascii_only else LIGHT
    marks = ASCII_MARK if ascii_only else MARK
    ids = {event["id"] for event in events}
    order = {event["id"]: n for n, event in enumerate(events)}
    by_id = {event["id"]: event for event in events}
    labels, tones = status_labels(graph), status_tones(graph)
    active = [edge for edge in graph["edges"] if edge["active"] and
              edge["from_event_id"] in number and edge["to_event_id"] in number]
    edges = [edge for edge in active if edge["from_event_id"] in ids and edge["to_event_id"] in ids]
    outside = [edge for edge in active if (edge["from_event_id"] in ids) != (edge["to_event_id"] in ids)]
    linked = {edge[end] for edge in active for end in ("from_event_id", "to_event_id")}
    leads_on = {edge["from_event_id"] for edge in active}

    # A result that leads nowhere sits beside the event it came from; the rest stay in the story column.
    anchor: dict[str, dict] = {}
    for event in events:
        key = event["id"]
        if event["kind"] != "outcome" or event["status"] not in OBSERVED or key in leads_on:
            continue
        incoming = [edge for edge in edges if edge["to_event_id"] == key and edge["relation"] in ANCHOR_ORDER]
        if incoming:
            anchor[key] = min(incoming, key=lambda edge: (ANCHOR_ORDER[edge["relation"]],
                                                          order[edge["from_event_id"]]))
    # Every link into a right-hand result: the one that places it draws the box, others point at it.
    stacks: dict[str, list[tuple[str, dict]]] = {}
    for edge in sorted(edges, key=lambda edge: order[edge["to_event_id"]]):
        if edge["to_event_id"] in anchor:
            stacks.setdefault(edge["from_event_id"], []).append(
                ("box" if anchor[edge["to_event_id"]] is edge else "ref", edge))
    groups = _flows(events, edges)
    columns = [[key for key in group if key not in anchor] for group in groups]

    # Column → column links: down the column when neighbours, on a margin rail otherwise,
    # else written inside the boxes at both ends (only the drawn end for a link leaving the view).
    spine: dict[tuple[str, str], list[dict]] = {}
    rails: list[tuple[list[dict], int]] = []  # one rail can carry a user message's turn links together
    notes: dict[str, list[tuple[str, str]]] = {}  # box → (text, link id)
    lanes = 0

    def note(edge: dict) -> None:
        relation = RELATION.get(edge["relation"], edge["relation"])
        source, target = edge["from_event_id"], edge["to_event_id"]
        if source in ids:
            notes.setdefault(source, []).append((f"{g['forward']} {number[target]} {relation}", edge["id"]))
        if target in ids:
            notes.setdefault(target, []).append((f"{g['back']} {number[source]} {relation}", edge["id"]))

    for edge in outside:
        note(edge)
    for column in columns:
        index = {key: n for n, key in enumerate(column)}
        busy: list[int] = []  # last box index each rail is busy until

        def take(start: int, end: int) -> int | None:
            lane = next((n for n, until in enumerate(busy) if until < start), None)
            if lane is None and len(busy) < MAX_RAILS:
                lane = len(busy)
                busy.append(-1)
            if lane is not None:
                busy[lane] = end
            return lane

        inner_edges = [edge for edge in edges if edge["from_event_id"] in index and edge["to_event_id"] in index]
        turns: dict[str, list[dict]] = {}
        longer: list[dict] = []
        for edge in sorted(inner_edges, key=lambda edge: (order[edge["from_event_id"]], order[edge["to_event_id"]])):
            source, target = edge["from_event_id"], edge["to_event_id"]
            start, end = index[source], index[target]
            if end == start + 1:
                spine.setdefault((source, target), []).append(edge)
            elif end > start and edge.get("origin") == "dialog_turn":
                turns.setdefault(source, []).append(edge)
            else:
                longer.append(edge)
        # A message's turn links go first and share one rail, like a bracket down its turn;
        # turns follow one another, so they usually all fit on the same lane.
        for source, group in turns.items():
            lane = take(index[source], max(index[edge["to_event_id"]] for edge in group))
            if lane is None:
                for edge in group:
                    note(edge)
            else:
                rails.append((group, lane))
        for edge in longer:
            start, end = index[edge["from_event_id"]], index[edge["to_event_id"]]
            lane = take(start, end) if end > start else None
            if lane is None:
                note(edge)
            else:
                rails.append(([edge], lane))
        lanes = max(lanes, len(busy))

    def arrival(edge: dict) -> str:
        """What a rail says as it reaches its target: where it came from and why."""
        return f" {number[edge['from_event_id']]} {RELATION.get(edge['relation'], edge['relation'])} {g['right']}"

    # Rails at columns 1, 3, …; then at least one line cell and the widest arrival label.
    label_room = max((wcswidth(arrival(edge)) for group, _ in rails for edge in group), default=0)
    margin = 2 * lanes + 1 + label_room if lanes else 0
    box_width = (width - margin - ARROW_GAP) // 2
    if box_width < 22:
        return None
    x_left, x_right = margin, margin + box_width + ARROW_GAP
    inner = box_width - 4
    # Each rail end takes its own inner row of a box; a box with many rails grows to fit them.
    ends: dict[str, int] = {}
    for group, _ in rails:
        ends[group[0]["from_event_id"]] = ends.get(group[0]["from_event_id"], 0) + 1
        for edge in group:
            ends[edge["to_event_id"]] = ends.get(edge["to_event_id"], 0) + 1
    drawn: dict[str, list[tuple[str, str, frozenset[str]]]] = {}

    def contents(key: str) -> list[tuple[str, str, frozenset[str]]]:
        if key not in drawn:
            none = frozenset()
            lines = [(part, "title", none) for part in _wrap(safe_text(by_id[key]["title"], multiline=False), inner)]
            status = _segments(safe_text(labels[key], multiline=False), inner - 2)
            lines += [(f"{marks[tones[key]] if n == 0 else ' '} {part}", tones[key], none)
                      for n, part in enumerate(status)]
            for text, edge_id in notes.get(key, []):
                lines += [(part, "dim", frozenset({edge_id})) for part in _wrap(text, inner)]
            if key not in linked:
                lines.append(("연결된 사건 없음", "dim", none))
            lines += [("", "", none)] * max(0, ends.get(key, 0) - len(lines))
            drawn[key] = lines
        return drawn[key]

    def height(key: str) -> int:
        return len(contents(key)) + 2

    diagram = Diagram(width)

    def box(top: int, x: int, key: str) -> None:
        tone = tones[key]
        border = tone if tone in ("ok", "warn", "fail") else ""
        kind = KIND.get(by_id[key]["kind"], by_id[key]["kind"])
        lines = contents(key)
        header = f"{g['h']} {number[key]} {kind} "
        diagram.put(top, x, g["tl"] + g["h"] * (box_width - 2) + g["tr"], border, key, True)
        diagram.put(top, x + 1, header, border, key, True)
        for n, (text, style, links) in enumerate(lines, 1):
            diagram.put(top + n, x, g["v"], border, key, True)
            diagram.put(top + n, x + 1, " " * (box_width - 2), "", key)
            diagram.put(top + n, x + 2, text, style, key, edges=links)
            diagram.put(top + n, x + box_width - 1, g["v"], border, key, True)
        bottom = top + len(lines) + 1
        diagram.put(bottom, x, g["bl"] + g["h"] * (box_width - 2) + g["br"], border, key, True)
        diagram.boxes[key] = (top, x, len(lines) + 2, box_width)

    def arrow(y: int, start: int, label: str, links: frozenset[str]) -> None:
        # "─검증──▶" from `start` to the right column, ending on the arrowhead.
        tail = g["h"] + label
        diagram.put(y, start, tail, "dim", edges=links)
        end = start + wcswidth(tail)
        diagram.put(y, end, g["h"] * max(0, x_right - 1 - end) + g["right"], "dim", edges=links)

    def heading(y: int, n: int, group: list[str]) -> None:
        sessions = {session for key in group for session in by_id[key].get("session_ids") or []}
        times = sorted(by_id[key]["recorded_at"] for key in group if by_id[key].get("recorded_at"))
        first, last = (_when(times[0]), _when(times[-1])) if times else ("", "")
        if last[:5] == first[:5]:
            last = last[6:]
        parts = [f"흐름 {n}/{len(groups)}", f"사건 {len(group)}개"]
        parts += [f"세션 {len(sessions)}개"] if sessions else []
        parts += [first + ("–" + last if last and last != first[6:] else "")] if first else []
        text = f"{g['flow'] * 2} {' · '.join(parts)} "
        diagram.put(y, 0, text + g["flow"] * max(0, width - 1 - wcswidth(text)), "heading")

    y = 0
    for n, (group, column) in enumerate(zip(groups, columns), 1):
        if len(groups) > 1:
            heading(y, n, group)
            y += 2
        previous: str | None = None
        for key in column:
            joined = previous is not None and (previous, key) in spine
            if joined:
                # Room below the previous box for "│ 라벨" and the arrowhead.
                y = max(y, diagram.boxes[previous][0] + diagram.boxes[previous][2] + 2)
            top = y
            box(top, x_left, key)
            if joined:
                above = diagram.boxes[previous]
                column_x = x_left + 3
                first, last = above[0] + above[2], top - 1
                between = spine[(previous, key)]
                links = frozenset(edge["id"] for edge in between)
                for row in range(first, last):
                    diagram.put(row, column_x, g["v"], "dim", edges=links)
                diagram.put(first, column_x + 2, "·".join(RELATION.get(edge["relation"], edge["relation"])
                                                          for edge in between), "dim", edges=links)
                diagram.put(last, column_x, g["arrow_down"], "dim", edges=links)
            previous = key
            items = stacks.get(key, [])
            row, arrows = top, []
            for kind, edge in items:
                relation = RELATION.get(edge["relation"], edge["relation"])
                target = edge["to_event_id"]
                if kind == "box":
                    box(row, x_right, target)
                    arrows.append((row + 1, relation, edge["id"]))
                    row += height(target) + 1
                else:  # a result drawn beside another box: point at it instead of drawing it twice
                    arrow_row = row if arrows else row + 1  # the first arrow must leave from inside the box
                    arrows.append((arrow_row, relation, edge["id"]))
                    where = g["up"] if target in diagram.boxes else g["arrow_down"] + " 아래"
                    diagram.put(arrow_row, x_right + 1, f"{number[target]} {where}", "dim", target,
                                edges=frozenset({edge["id"]}))
                    row = arrow_row + 2
            if arrows:
                joint = x_left + box_width + 2
                first_row = arrows[0][0]
                every = frozenset(edge_id for _, _, edge_id in arrows)
                diagram.put(first_row, x_left + box_width - 1, g["lt"], diagram.rows[first_row][x_left].style,
                            key, True, every)
                diagram.put(first_row, x_left + box_width, g["h"] * 2, "dim", edges=every)
                for index, (arrow_row, relation, edge_id) in enumerate(arrows):
                    last = index == len(arrows) - 1
                    joint_char = (g["down"] if not last else g["h"]) if index == 0 else (g["bl"] if last else g["lt"])
                    onward = frozenset(later for _, _, later in arrows[index:])
                    diagram.put(arrow_row, joint, joint_char, "dim", edges=onward)
                    arrow(arrow_row, joint + 1, relation, frozenset({edge_id}))
                    if not last:
                        for between_row in range(arrow_row + 1, arrows[index + 1][0]):
                            diagram.put(between_row, joint, g["v"], "dim", edges=onward - {edge_id})
            own = diagram.boxes[key][0] + diagram.boxes[key][2]
            y = max(own, row - 1 if items else own) + 1

    # Margin rails for longer column links, drawn last so crossings can be marked.
    # Every rail end on a box, leaving or arriving, uses the box's next inner row.
    used: dict[str, int] = {}

    def attach(key: str) -> int:
        top, _, box_height, _ = diagram.boxes[key]
        row = top + 1 + min(used.get(key, 0), box_height - 3)
        used[key] = used.get(key, 0) + 1
        return row

    def line(y: int, x: int, links: frozenset[str], along: str) -> None:
        # A line cell; crossing another line becomes "┼" and carries both lines' links.
        crossing = diagram.char(y, x) == (g["v"] if along == g["h"] else g["h"])
        diagram.put(y, x, g["cross"] if crossing else along, "dim",
                    edges=links | diagram.links(y, x) if crossing else links)

    for group, lane in rails:
        x = 1 + 2 * lane
        source = group[0]["from_event_id"]
        start = attach(source)
        stops = sorted((attach(edge["to_event_id"]), n, edge) for n, edge in enumerate(group))
        last = stops[-1][0]
        every = frozenset(edge["id"] for edge in group)
        for row in range(start + 1, last):
            line(row, x, frozenset(edge["id"] for end, _, edge in stops if end > row), g["v"])
        # Leave the source box on its left side...
        for column in range(x + 1, x_left):
            line(start, column, every, g["h"])
        diagram.put(start, x, g["tl"], "dim", edges=every)
        diagram.put(start, x_left, g["rt"], diagram.rows[start][x_left].style, source, True, every)
        # ...and arrive at each target under the number of the box it left: "[02] 동기 ▶".
        for end, _, edge in stops:
            links = frozenset(edge["id"] for stop, _, edge in stops if stop >= end)
            diagram.put(end, x, g["bl"] if end == last else g["lt"], "dim", edges=links)
            label = arrival(edge)
            label_start = x_left - wcswidth(label)
            for column in range(x + 1, label_start):
                line(end, column, frozenset({edge["id"]}), g["h"])
            diagram.put(end, label_start, label, "dim", edges=frozenset({edge["id"]}))
    return diagram
