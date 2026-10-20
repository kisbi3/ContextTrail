"""Boxes and arrows for a terminal, in the manner of cli-diagram.

The left column is the story in recorded order. An observed result that checks a change
and leads nowhere else sits to the right of that change, joined by a labelled arrow, so
"what checked this change" reads straight across; a result that leads on (a failure that
motivated a fix) stays in the story column. Links between neighbouring boxes in the column
are drawn down it, and neighbours with no link between them are joined by a dotted line with no
label: what happened next, in recorded order, never a cause. Longer links run on rails in the left margin and arrive under the number of
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
from typing import Any

from wcwidth import wcswidth, wcwidth

from .i18n import tr
from .render import ASCII_MARK, MARK, RELATION, _when, delegate_label, kind_label, status_labels, status_tones
from .schema import STORY_VIEW
from .util import cell_slice, safe_text

OBSERVED = {"observed_success", "observed_failure"}
# Relations that put a result beside the event it came from, most telling first.
ANCHOR_ORDER = {"verifies": 0, "produces": 1}
LIGHT = {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│", "lt": "├", "rt": "┤",
         "down": "┬", "cross": "┼", "right": "▶", "left": "◀", "arrow_down": "▼", "up": "↑", "back": "←",
         "forward": "→", "flow": "═", "then": "┊", "then_down": "▽", "sub_mark": "↳"}
PLAIN = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|", "lt": "+", "rt": "+",
         "down": "+", "cross": "+", "right": ">", "left": "<", "arrow_down": "v", "up": "^", "back": "<-",
         "forward": "->", "flow": "=", "then": ":", "then_down": "v", "sub_mark": "->"}


def _sub_agent(event: dict) -> bool:
    """Whether the event is a sub-agent's work (a tool another session's call ran counts; a run with no person
    and no known caller does not: it has no parent step to stand beside)."""
    return any(agent.get("role") == "subagent" for agent in event.get("authoring") or [])


CALLED_TOOLS = ("codex exec", "claude -p", "claude sdk")
# Relations by which a result leads into a main step: what it caused, revised or answered.
CAUSES = {"motivates", "revises", "answers", "produces"}


def _called(event: dict) -> bool:
    """Whether the event is a run of another model a call started, or one with no person (`codex exec`,
    `claude -p`): told apart from a sub-agent by the tool its log names, whatever its lineage became."""
    return any(agent.get("role") in ("subagent", "unattended") and agent.get("agent") in CALLED_TOOLS
               for agent in event.get("authoring") or [])


def _detail(event: dict) -> bool:
    """Whether an event reads as a step in between rather than the story itself: a check that passed, a result
    resting only on test runs, an edit of documentation only (`step`, read off the cited records by code).
    A failure and a claim in words stay in the story unless a relation places them (`flow_diagram`)."""
    if event.get("actor") == "user":
        return False
    if event["kind"] == "outcome":
        return event["status"] == "observed_success" or event.get("step") == "check"
    return event["kind"] in ("action", "revision") and event.get("step") == "doc"


def _first_sentence(text: str) -> str:
    """The first sentence of a summary (wrapped by the box, never cut)."""
    import re
    found = re.match(r"(.+?[.!?。])(\s|$)", text)
    return (found.group(1) if found else text).strip()


def _edge_label(edge: dict) -> str:
    """What a line is called: its relation, or `갈라짐` for the follows that joins a forked session's first
    request to the parent's conversation (`schema.link_forks`)."""
    if edge.get("origin") == "fork":
        return tr("갈라짐", "forked")
    if edge.get("origin") == "delegation":
        return tr("위임", "delegated")
    return RELATION.get(edge["relation"], edge["relation"])


def _sub_label(event: dict) -> str:
    """The sub-agent's name for the line joining its box: `Explore`, `codex exec`."""
    return next((agent.get("agent") or agent.get("tool", "") for agent in event.get("authoring") or []
                 if agent.get("role") == "subagent"), "")
# The selected box is redrawn with heavy lines.
HEAVY = str.maketrans("┌┐└┘─│├┤┬┼", "┏┓┗┛━┃┣┫┳╋")
MAX_RAILS = 4
# A link drawn as a margin rail passes at most this many boxes (story boxes and the boxes beside them); a longer
# one is written in both boxes, since a line running past a screen of boxes is not read as joining anything.
RAIL_BOXES = 3
STORY_BOX = 76  # the story view's one column is at most this wide, the width that reads well
MIN_BOX = 22  # the narrowest box that still reads
WIDE_BOX = 30  # a fork's side keeps result and sub-agent boxes beside it only when they can be this wide
BRANCH_GAP = 4  # between a fork's sides
ARROW_GAP = 11  # between the two columns: "──┬─검증──▶"; a wider relation label widens it


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
    beside: dict[str, str] = field(default_factory=dict)  # a right-hand box → the main step it stands beside

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
    from .schema import is_request
    events = graph["events"]
    if not any(is_request(event) for event in events):
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
        if is_request(event):
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
    # A view of the graph (`schema.story_view`) keeps each event's number in the whole list.
    number = {event["id"]: f"[{event.get('number', n):02d}]" for n, event in enumerate(graph["events"], 1)}
    story_view = graph.get("view") == STORY_VIEW
    events = [event for event in graph["events"] if only is None or event["id"] in only]
    if not events:
        return None
    g = PLAIN if ascii_only else LIGHT
    marks = ASCII_MARK if ascii_only else MARK
    ids = {event["id"] for event in events}
    order = {event["id"]: n for n, event in enumerate(events)}
    by_id = {event["id"]: event for event in events}
    labels, tones = graph.get("labels") or status_labels(graph), graph.get("tones") or status_tones(graph)
    active = [edge for edge in graph["edges"] if edge["active"] and
              edge["from_event_id"] in number and edge["to_event_id"] in number]
    edges = [edge for edge in active if edge["from_event_id"] in ids and edge["to_event_id"] in ids]
    # A turn link is the column's order: drawn neither as a line nor as a note (the detail lists it).
    outside = [edge for edge in active if (edge["from_event_id"] in ids) != (edge["to_event_id"] in ids)
               and edge.get("origin") != "dialog_turn"]

    groups = _flows(events, edges)

    def when(key: str) -> tuple[str, int]:
        # Events of different sessions are listed in the order they were analysed; time decides what came next.
        return (by_id[key].get("recorded_at") or "", order[key])

    # Two lanes: the story on the left (requests, decisions, code changes, failures, claims), the steps in between
    # on the right (checks, documentation edits, sub-agents and runs of another model), each beside a main step.
    # Where a right-hand box stands is decided by a relation first; with none, a check or an edit stands beside
    # the main step before it and a sub-agent's work beside the one after it, with a plain line that is layout,
    # never a relation. A failure or a claim that leads into a main step stands beside it as its cause.
    relations = [edge for edge in edges if edge.get("origin") not in ("dialog_turn", "delegation")]
    handed = {edge["to_event_id"]: edge for edge in edges if edge.get("origin") == "delegation"}
    placed: dict[str, tuple[str, str, dict | None]] = {}  # right-hand box → (main step, out|in|near|sub, edge)
    for group in ([] if story_view else groups):  # the story view is one column: what it folded is in the boxes
        detail = {key for key in group if _sub_agent(by_id[key]) or _detail(by_id[key])}
        if not set(group) - detail:
            detail = {key for key in group if _sub_agent(by_id[key])}
        main = set(group) - detail
        # A failure or a claim that leads into a main step, or a failed check of one, joins it on the right.
        for key in sorted(main, key=when):
            event = by_id[key]
            if event["kind"] != "outcome" or event.get("actor") == "user" or len(main) == 1:
                continue
            leads = any(edge["from_event_id"] == key and edge["relation"] in CAUSES and edge["to_event_id"] in main
                        for edge in relations)
            checked = any(edge["to_event_id"] == key and edge["relation"] in ANCHOR_ORDER and edge["from_event_id"] in main
                          for edge in relations)
            if leads or checked:
                main.discard(key)
                detail.add(key)
        story = sorted(main, key=when)
        if not story:
            continue
        for key in sorted(detail, key=when):
            # Placed by time, a box stays with its own session's steps (another session's story is not its own).
            sessions = set(by_id[key].get("session_ids") or [])
            own = [s for s in story if sessions & set(by_id[s].get("session_ids") or [])] or story
            if _sub_agent(by_id[key]):
                # Beside the delegation that gave its agent the task (`schema.link_delegations`; a nested
                # agent's, beside the step its caller's work stands beside), else beside the step after it.
                delegation, seen = by_id[key].get("delegated_by"), {key}
                while delegation in detail and delegation not in seen:
                    seen.add(delegation)
                    delegation = by_id[delegation].get("delegated_by")
                if delegation in main:
                    placed[key] = (delegation, "sub", handed.get(key))
                else:
                    placed[key] = (next((s for s in own if when(s) > when(key)), own[-1]), "sub", None)
                continue
            options = []
            for edge in relations:
                source, target = edge["from_event_id"], edge["to_event_id"]
                if target == key and source in main:
                    rank = 0 if edge["relation"] in ANCHOR_ORDER else 2
                    options.append((rank, ANCHOR_ORDER.get(edge["relation"], 9), abs(order[source] - order[key]),
                                    source, "out", edge))
                elif source == key and target in main:
                    rank = 1 if edge["relation"] in CAUSES else 3
                    options.append((rank, 9, abs(order[target] - order[key]), target, "in", edge))
            if options:
                _, _, _, beside, kind, edge = min(options, key=lambda option: option[:3])
                placed[key] = (beside, kind, edge)
            else:
                before = [s for s in own if when(s) <= when(key)]
                placed[key] = (before[-1] if before else own[0], "near", None)
    # The boxes beside each main step: those joined to it first, then those only from around then, each in time order.
    stacks: dict[str, list[tuple[str, str, dict | None]]] = {}
    for key in sorted(placed, key=lambda key: (placed[key][1] == "near", when(key))):
        beside, kind, edge = placed[key]
        stacks.setdefault(beside, []).append((kind, key, edge))
    columns = [[key for key in group if key not in placed] for group in groups]
    # A unit's summary (`--unit-summary`) heads the first of its events drawn in the story column.
    heads: dict[str, str] = {}
    for summary in (graph.get("unit_summaries") or {}).values():
        shown = [key for key in summary.get("event_ids", []) if key in ids and key not in placed]
        if shown and summary.get("text"):
            first = min(shown, key=when)
            heads[first] = " / ".join(filter(None, [heads.get(first), summary["text"]]))

    def session_of(key: str) -> str | None:
        sessions = by_id[key].get("session_ids") or []
        return sessions[0] if sessions else None

    def branches_of(column: list[str]) -> tuple[list[str], list[tuple[str | None, list[str]]], list[dict]] | None:
        """A forked conversation (`schema.link_forks`): the story up to the fork, then the parent's continuation
        and each forked session as sides drawn next to each other. None without a fork, or when only one side
        went on (then the fork is a link like any other)."""
        forks = [edge for edge in edges if edge.get("origin") == "fork"
                 and edge["from_event_id"] in column and edge["to_event_id"] in column]
        if not forks:
            return None
        parent = session_of(forks[0]["from_event_id"])
        point = min(edge.get("fork_point_at") or by_id[edge["to_event_id"]].get("recorded_at") or "" for edge in forks)
        sides: list[tuple[str | None, list[str]]] = [(parent, [])]
        for edge in forks:
            child = session_of(edge["to_event_id"])
            if child is not None and child not in [s for s, _ in sides]:
                sides.append((child, []))
        prefix: list[str] = []
        for key in column:
            session = session_of(key)
            if session in [s for s, _ in sides[1:]]:
                sides[[s for s, _ in sides].index(session)][1].append(key)
            elif session == parent and (by_id[key].get("recorded_at") or "") > point:
                sides[0][1].append(key)
            else:
                prefix.append(key)
        sides = [(session, keys) for session, keys in sides if keys]
        if len(sides) < 2:
            return None
        return prefix, sides, forks

    def plan(drawn: list[tuple[list[str], bool]], splitters: set[str]):
        """Links of the drawn columns: down the column when neighbours (spine), on a margin rail otherwise
        (columns that allow rails; a fork's sides do not), else written inside the boxes at both ends (only the
        drawn end for a link leaving the view). A fork drawn as a splitter is no link here."""
        spine: dict[tuple[str, str], list[dict]] = {}
        rails: list[tuple[list[dict], int]] = []  # one rail can carry a user message's turn links together
        notes: dict[str, list[tuple[str, str]]] = {}  # box → (text, link id)
        lanes = 0

        def note(edge: dict) -> None:
            if story_view:
                return  # the story view draws neighbours' links only; the rest are in the detail
            relation = _edge_label(edge)
            source, target = edge["from_event_id"], edge["to_event_id"]
            if source in ids:
                notes.setdefault(source, []).append((f"{g['forward']} {number[target]} {relation}", edge["id"]))
            if target in ids:
                notes.setdefault(target, []).append((f"{g['back']} {number[source]} {relation}", edge["id"]))

        for edge in outside:
            note(edge)
        for edge in edges:
            # A link to or from a right-hand box is written inside both boxes, unless it is the one that places
            # the box (drawn as its line); its turn link is the main step it stands beside.
            ends = [edge[end] for end in ("from_event_id", "to_event_id") if edge[end] in placed]
            if ends and edge.get("origin") not in ("dialog_turn", "delegation") and all(
                    placed[end][2] is not edge for end in ends):
                note(edge)
        col_of = {key: n for n, (column, _) in enumerate(drawn) for key in column}
        for column, allow in drawn:
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

            inner_edges = [edge for edge in edges if edge["from_event_id"] in index and edge["to_event_id"] in index
                           and edge["id"] not in splitters]
            longer: list[dict] = []
            for edge in sorted(inner_edges, key=lambda edge: (order[edge["from_event_id"]], order[edge["to_event_id"]])):
                source, target = edge["from_event_id"], edge["to_event_id"]
                start, end = index[source], index[target]
                if edge.get("origin") == "dialog_turn":
                    continue  # what came after a message: the column's order says it
                if end == start + 1:
                    spine.setdefault((source, target), []).append(edge)
                else:
                    longer.append(edge)
            # A fork's side has no rails: anything longer than a neighbour's link is written in the boxes.
            for edge in longer:
                start, end = index[edge["from_event_id"]], index[edge["to_event_id"]]
                passed = sum(1 + len(stacks.get(key, [])) for key in column[start + 1:end])
                near = passed <= (0 if story_view else RAIL_BOXES)
                lane = take(start, end) if allow and end > start and near else None
                if lane is None:
                    note(edge)
                else:
                    rails.append(([edge], lane))
            lanes = max(lanes, len(busy))
        # A link between two sides of one story is written inside both boxes.
        for edge in edges:
            a, b = col_of.get(edge["from_event_id"]), col_of.get(edge["to_event_id"])
            if a is not None and b is not None and a != b and edge["id"] not in splitters:
                note(edge)
        return spine, rails, notes, lanes

    def arrival(edge: dict) -> str:
        """What a rail says as it reaches its target: where it came from and why."""
        return f" {number[edge['from_event_id']]} {_edge_label(edge)} {g['right']}"

    def geometry(rails: list[tuple[list[dict], int]], lanes: int) -> tuple[int, int, int]:
        # Rails at columns 1, 3, …; then at least one line cell and the widest arrival label.
        label_room = max((wcswidth(arrival(edge)) for group, _ in rails for edge in group), default=0)
        margin = 2 * lanes + 1 + label_room if lanes else 0
        # The gap between the columns holds "──┬─" + the widest label drawn across it + "──▶".
        across = max([wcswidth(_edge_label(edge)) for _, _, edge in placed.values() if edge is not None]
                     + [wcswidth(_sub_label(by_id[key])) for key, (_, kind, _) in placed.items() if kind == "sub"],
                     default=0)
        arrow_gap = max(ARROW_GAP, across + 7)
        return margin, arrow_gap, (width - margin - arrow_gap) // 2

    def lanes_for(sides: int, margin: int, arrow_gap: int) -> list[tuple[int, int, int | None]] | None:
        """Side by side lanes for a fork's sides: (x_left, box width, x_right or None when results and sub-agents
        fold into the boxes). None when even folded boxes would be too narrow."""
        room = width - margin - (sides - 1) * BRANCH_GAP
        lane_width = room // sides
        wide = (lane_width - arrow_gap) // 2 >= WIDE_BOX
        box_w = (lane_width - arrow_gap) // 2 if wide else lane_width
        if box_w < MIN_BOX:
            return None
        return [(margin + n * (lane_width + BRANCH_GAP), box_w,
                 margin + n * (lane_width + BRANCH_GAP) + box_w + arrow_gap if wide else None) for n in range(sides)]

    # First as one column per story; then, where a fork's sides fit next to each other, again with the sides as
    # their own columns (no rails there), which can only narrow the margin.
    layouts: list[tuple] = [("plain", column) for column in columns]
    spine, rails, notes, lanes = plan([(column, True) for column in columns], set())
    margin, arrow_gap, box_width = geometry(rails, lanes)
    forked = False
    for n, column in enumerate(columns):
        split = branches_of(column)
        if split and lanes_for(len(split[1]), margin, arrow_gap):
            layouts[n] = ("fork", *split)
            forked = True
    if forked:
        drawn, splitters = [], set()
        for layout in layouts:
            if layout[0] == "plain":
                drawn.append((layout[1], True))
            else:
                drawn.append((layout[1], True))
                drawn += [(keys, False) for _, keys in layout[2]]
                splitters |= {edge["id"] for edge in layout[3]}
        spine, rails, notes, lanes = plan(drawn, splitters)
        margin, arrow_gap, box_width = geometry(rails, lanes)
    if story_view:
        box_width = min(width - margin - 4, STORY_BOX)
    if box_width < MIN_BOX:
        return None
    x_left, x_right = margin, margin + box_width + arrow_gap
    # Each rail end takes its own inner row of a box; a box with many rails grows to fit them.
    ends: dict[str, int] = {}
    for group, _ in rails:
        ends[group[0]["from_event_id"]] = ends.get(group[0]["from_event_id"], 0) + 1
        for edge in group:
            ends[edge["to_event_id"]] = ends.get(edge["to_event_id"], 0) + 1
    drawn_lines: dict[tuple[str, int, bool], list[tuple[str, str, frozenset[str]]]] = {}

    def contents(key: str, box_w: int, fold: bool) -> list[tuple[str, str, frozenset[str]]]:
        """A box's inner lines: the title, the status, and links written in. Who did the step and that a box has no
        link are in the detail pane, not the flow. With `fold`, results stay in the status line and a sub-agent's
        box becomes one line."""
        if (key, box_w, fold) not in drawn_lines:
            inner = box_w - 4
            none = frozenset()
            lines = [(part, "title", none) for part in _wrap(safe_text(by_id[key]["title"], multiline=False), inner)]
            if story_view:
                # The title and the agents handed work here, each with what it last said; the status is the
                # border's colour, the reason and every step are in the detail and the full view.
                for agent in by_id[key].get("folded_agents", []):
                    said = safe_text(agent["said"], multiline=False) if agent["said"] else tr("아직 분석 전", "not analysed yet")
                    text = f"{g['sub_mark']} {safe_text(agent['agent'], multiline=False)}: {said}"
                    lines += [(part, "sub", none) for part in _wrap(text, inner)]
                drawn_lines[(key, box_w, fold)] = lines
                return lines
            if by_id[key]["kind"] in ("decision", "outcome") and by_id[key].get("summary") and key not in placed:
                # A decision says why it was made, a result in the story what was found: the first sentence of its
                # summary (the prompts ask a decision's to start with the reason, `common.md`). A title alone left
                # readers without the numbers and conclusions (holdout reader test, `PIPELINE_V2.md`).
                reason = _first_sentence(safe_text(by_id[key]["summary"], multiline=False))
                lines += [(part, "dim", none) for part in _wrap(reason, inner)]
            status = _segments(safe_text(labels[key], multiline=False), inner - 2)
            lines += [(f"{marks[tones[key]] if n == 0 else ' '} {part}", tones[key], none)
                      for n, part in enumerate(status)]
            if fold:
                for kind, item, _ in stacks.get(key, []):
                    if kind == "sub":
                        text = f"{g['forward']} {delegate_label(by_id[item])} · {safe_text(by_id[item]['title'], multiline=False)}"
                        lines += [(part, "llm" if _called(by_id[item]) else "sub", none) for part in _wrap(text, inner)]
            for text, edge_id in notes.get(key, []):
                lines += [(part, "dim", frozenset({edge_id})) for part in _wrap(text, inner)]
            lines += [("", "", none)] * max(0, ends.get(key, 0) - len(lines))
            drawn_lines[(key, box_w, fold)] = lines
        return drawn_lines[(key, box_w, fold)]

    def height(key: str, box_w: int, fold: bool) -> int:
        return len(contents(key, box_w, fold)) + 2

    diagram = Diagram(width, beside={key: main for key, (main, _, _) in placed.items()})

    def border_of(key: str) -> str:
        # Another model's run (cyan in a terminal) and a sub-agent (magenta) keep their colour whatever their status.
        if delegate_label(by_id[key]):
            return "llm" if _called(by_id[key]) else "sub"
        return tones[key] if tones[key] in ("ok", "warn", "fail") else ""

    def box(top: int, x: int, key: str, box_w: int, fold: bool = False) -> None:
        tone = tones[key]
        # A sub-agent's box (or a run another call started) has its own border colour, whatever its status;
        # the status keeps its mark and label inside. The header says whose work it is.
        delegate = delegate_label(by_id[key])
        border = border_of(key)
        kind = kind_label(by_id[key]) + (f" · {delegate}" if delegate else "")
        lines = contents(key, box_w, fold)
        header = f"{g['h']} {number[key]} {kind} "
        diagram.put(top, x, g["tl"] + g["h"] * (box_w - 2) + g["tr"], border, key, True)
        diagram.put(top, x + 1, header, border, key, True)
        for n, (text, style, links) in enumerate(lines, 1):
            diagram.put(top + n, x, g["v"], border, key, True)
            diagram.put(top + n, x + 1, " " * (box_w - 2), "", key)
            diagram.put(top + n, x + 2, text, style, key, edges=links)
            diagram.put(top + n, x + box_w - 1, g["v"], border, key, True)
        bottom = top + len(lines) + 1
        diagram.put(bottom, x, g["bl"] + g["h"] * (box_w - 2) + g["br"], border, key, True)
        diagram.boxes[key] = (top, x, len(lines) + 2, box_w)

    def arrow(y: int, start: int, label: str, links: frozenset[str], right: int) -> None:
        # "─검증──▶" from `start` to the right column, ending on the arrowhead.
        tail = g["h"] + label
        diagram.put(y, start, tail, "dim", edges=links)
        end = start + wcswidth(tail)
        diagram.put(y, end, g["h"] * max(0, right - 1 - end) + g["right"], "dim", edges=links)

    def heading(y: int, n: int, group: list[str]) -> None:
        sessions = {session for key in group for session in by_id[key].get("session_ids") or []}
        times = sorted(by_id[key]["recorded_at"] for key in group if by_id[key].get("recorded_at"))
        first, last = (_when(times[0]), _when(times[-1])) if times else ("", "")
        if last[:5] == first[:5]:
            last = last[6:]
        parts = [tr(f"흐름 {n}/{len(groups)}", f"flow {n}/{len(groups)}"),
                 tr(f"사건 {len(group)}개", f"{len(group)} events")]
        parts += [tr(f"세션 {len(sessions)}개", f"{len(sessions)} sessions")] if sessions else []
        parts += [first + ("–" + last if last and last != first[6:] else "")] if first else []
        text = f"{g['flow'] * 2} {' · '.join(parts)} "
        diagram.put(y, 0, text + g["flow"] * max(0, width - 1 - wcswidth(text)), "heading")

    def draw_column(column: list[str], lane: tuple[int, int, int | None], y: int) -> int:
        """Boxes down one story column (a lane: left edge, box width, right column or None to fold), with each
        box's results and sub-agents beside it; returns the row after the last box."""
        lx, box_w, rx = lane
        fold = rx is None
        if fold:
            # No right column: a checked result stays in its change's status line and a sub-agent becomes a line in
            # the box; every other right-hand box follows its main step down the column.
            column = [shown for key in column for shown in [key] + [
                item for kind, item, edge in stacks.get(key, [])
                if kind != "sub" and not (kind == "out" and edge["relation"] == "verifies")]]
        previous: str | None = None
        for key in column:
            joined = previous is not None and (previous, key) in spine
            if previous is not None:
                # Room below the previous box for "│ 라벨" and the arrowhead.
                y = max(y, diagram.boxes[previous][0] + diagram.boxes[previous][2] + 2)
            if key in heads:
                # Beside the column's line (lx + 3), so a dotted or labelled link down it is never covered.
                for part in _wrap(safe_text(tr("구간 · ", "stretch · ") + heads[key], multiline=False), max(12, width - lx - 6)):
                    diagram.put(y, lx + 5, part, "heading")
                    y += 1
            top = y
            box(top, lx, key, box_w, fold)
            if previous is not None and not joined:
                # What happened next: a dotted, unlabelled line, so order is never read as a cause.
                above = diagram.boxes[previous]
                column_x = lx + 3
                for row in range(above[0] + above[2], top - 1):
                    diagram.put(row, column_x, g["then"], "dim")
                diagram.put(top - 1, column_x, g["then_down"], "dim")
            if joined:
                above = diagram.boxes[previous]
                column_x = lx + 3
                first, last = above[0] + above[2], top - 1
                between = spine[(previous, key)]
                links = frozenset(edge["id"] for edge in between)
                for row in range(first, last):
                    diagram.put(row, column_x, g["v"], "dim", edges=links)
                diagram.put(first, column_x + 2, "·".join(_edge_label(edge) for edge in between), "dim", edges=links)
                diagram.put(last, column_x, g["arrow_down"], "dim", edges=links)
            previous = key
            items = [] if fold or key in placed else stacks.get(key, [])
            row, arrows = top, []  # (row, kind, label, link id or None, the right-hand box)
            captioned = False
            for kind, item, edge in items:
                if kind == "near" and not captioned:
                    # Boxes with no recorded link to this step: from around then, set apart and joined by no line,
                    # so they are not read as belonging to the step.
                    for part in _wrap(tr("┄ 같은 무렵 · 이어진 관계 없음", "┄ around then · no recorded link"), box_w):
                        diagram.put(row, rx, part, "dim")
                        row += 1
                    captioned = True
                box(row, rx, item, box_w)
                if kind != "near":
                    label = _sub_label(by_id[item]) if kind == "sub" else _edge_label(edge) if edge else ""
                    if kind == "sub" and arrows and arrows[-1][1] == "sub" and _sub_label(by_id[arrows[-1][4]]) == label:
                        label = ""  # an agent is named once, at the top of its stack
                    arrows.append((row + 1, kind, label, edge["id"] if edge else None, item))
                row += height(item, box_w, False) + 1
            if arrows:
                joint = lx + box_w + 2
                first_row = arrows[0][0]
                every = frozenset(edge_id for _, _, _, edge_id, _ in arrows if edge_id)
                # A result checking this step leaves the box ("├──검증──▶"); what led into it, and a sub-agent's
                # work, feed into it ("◀──동기──┤", "◀──┬─Explore──┤"); a box placed only by time hangs on a plain
                # line with no arrowhead and no label: beside, never caused by.
                entry = g["left"] if arrows[0][1] in ("in", "sub") else g["lt"]
                diagram.put(first_row, lx + box_w - 1, entry, diagram.rows[first_row][lx].style, key, True, every)
                diagram.put(first_row, lx + box_w, g["h"] * 2, "dim", edges=every)
                for index, (arrow_row, kind, label, edge_id, item) in enumerate(arrows):
                    last = index == len(arrows) - 1
                    joint_char = (g["down"] if not last else g["h"]) if index == 0 else (g["bl"] if last else g["lt"])
                    onward = frozenset(later for _, _, _, later, _ in arrows[index:] if later)
                    diagram.put(arrow_row, joint, joint_char, "dim", edges=onward)
                    links = frozenset({edge_id}) if edge_id else frozenset()
                    if kind == "out":
                        arrow(arrow_row, joint + 1, label, links, rx)
                    else:
                        head = g["left"] if kind == "in" and entry != g["left"] else ""
                        tail = head + (g["h"] + label if label else g["h"])
                        diagram.put(arrow_row, joint + 1, tail, "dim", edges=links)
                        end = joint + 1 + wcswidth(tail)
                        diagram.put(arrow_row, end, g["h"] * max(0, rx - end), "dim", edges=links)
                        diagram.put(arrow_row, rx, g["rt"], border_of(item), item, True, links)
                    if not last:
                        for between_row in range(arrow_row + 1, arrows[index + 1][0]):
                            diagram.put(between_row, joint, g["v"], "dim", edges=onward - {edge_id})
            own = diagram.boxes[key][0] + diagram.boxes[key][2]
            y = max(own, row - 1 if items else own) + 1
        return y

    def splitter(y: int, sides: list[tuple[str | None, list[str]]], lanes_xy: list[tuple[int, int, int | None]],
                 previous: str | None) -> int:
        """Where a conversation forked: one line from the story's column to each side's column, the fork's name,
        and under each column whose session goes on there. Returns the row the sides' boxes start on."""
        if previous is not None:
            y = max(y, diagram.boxes[previous][0] + diagram.boxes[previous][2])
        xs = [lx + 3 for lx, _, _ in lanes_xy]
        diagram.put(y, xs[0], g["lt"], "dim")
        for x in range(xs[0] + 1, xs[-1]):
            diagram.put(y, x, g["down"] if x in xs else g["h"], "dim")
        diagram.put(y, xs[-1], g["tr"], "dim")
        diagram.put(y, xs[-1] + 2, tr("갈라짐", "forked"), "dim")
        for n, ((session, _), x) in enumerate(zip(sides, xs)):
            short = (session or "")[:8] + ("…" if session and len(session) > 8 else "")
            caption = tr(f"세션 {short} 이어짐", f"session {short} goes on") if n == 0 else tr(f"갈라진 세션 {short}", f"forked session {short}")
            diagram.put(y + 1, x, g["then"], "dim")
            diagram.put(y + 1, x + 2, caption, "dim")
            diagram.put(y + 2, x, g["then_down"], "dim")
        return y + 3

    y = 0
    for n, (group, layout) in enumerate(zip(groups, layouts), 1):
        if len(groups) > 1:
            heading(y, n, group)
            y += 2
        if layout[0] == "plain":
            y = draw_column(layout[1], (x_left, box_width, x_right), y)
            continue
        prefix, sides, _ = layout[1], layout[2], layout[3]
        y = draw_column(prefix, (x_left, box_width, x_right), y)
        side_lanes = lanes_for(len(sides), margin, arrow_gap) or []
        start = splitter(y, sides, side_lanes, prefix[-1] if prefix else None)
        y = max(draw_column(keys, lane, start) for (_, keys), lane in zip(sides, side_lanes))

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


# The story view in the browser (`story_svg`): one column of boxes this wide, its text wrapped at these many cells
# (conservative for upper-case Latin in the page's own font).
SVG_BOX = 600
SVG_TITLE_CELLS, SVG_LINE_CELLS = 64, 78
SVG_TONE = {"ok": "#3f8f5a", "warn": "#b8862b", "fail": "#c0453d", "plain": "#ced7df"}


def story_svg(view: dict) -> str:
    """The story view (`schema.story_view`) as the browser draws it: each flow (events joined by a session or a
    relation, `_flows`) as one column of boxes in the order of the list, under its own heading when there are
    several, each box with its number and kind, its title and a line per agent handed work there, wrapped (never
    cut), its status as the border's colour; a box joined to the one before it by a relation gets that arrow
    (dashed with `·추정` when inferred, as on the rest of the page), and boxes with no relation get no line: order is
    not a cause. Each box lists the steps folded into it (`data-folds`), so the page can light the box of a folded
    step. Only the elements the page imports (svg, defs, marker, path, g, rect, text), every text escaped."""
    import html
    events = view["events"]
    tones = view.get("tones") or status_tones(view)
    labels = view.get("labels") or status_labels(view)
    shown = {event["id"] for event in events}
    active = [edge for edge in view["edges"] if edge["active"] and edge["from_event_id"] in shown
              and edge["to_event_id"] in shown]
    joins: dict[tuple[str, str], list[dict]] = {}
    for edge in active:
        if edge.get("origin") not in ("dialog_turn", "delegation"):
            joins.setdefault((edge["from_event_id"], edge["to_event_id"]), []).append(edge)
    folds: dict[str, list[str]] = {}
    for folded, home in (view.get("homes") or {}).items():
        folds.setdefault(home, []).append(folded)
    by_id = {event["id"]: event for event in events}
    order = {event["id"]: n for n, event in enumerate(events)}
    heads: dict[str, str] = {}
    for summary in (view.get("unit_summaries") or {}).values():
        keys = [key for key in summary.get("event_ids", []) if key in shown]
        if keys and summary.get("text"):
            first = min(keys, key=order.get)
            heads[first] = " / ".join(filter(None, [heads.get(first), summary["text"]]))
    flows = _flows(events, active)
    x, y, gap = 32, 28, 46
    body: list[str] = []
    for index, flow in enumerate(flows, 1):
        if len(flows) > 1:
            if index > 1:
                y += 18
            heading = tr(f"흐름 {index}/{len(flows)} · 사건 {len(flow)}개", f"flow {index}/{len(flows)} · {len(flow)} events")
            body.append(f'<text x="{x}" y="{y + 12}" fill="#486273" font-size="12" font-weight="700">{html.escape(heading)}</text>')
            y += 30
        previous: str | None = None
        bottom = 0  # of the previous box
        for key in flow:
            event = by_id[key]
            # A unit's summary heads the first of its boxes, beside the line that comes down to it.
            for part in (_wrap(safe_text(tr("구간 · ", "stretch · ") + heads[key], multiline=False), SVG_LINE_CELLS - 6)
                         if key in heads else []):
                body.append(f'<text x="{x + 56}" y="{y + 12}" fill="#2f7f8f" font-size="12" font-weight="600">{html.escape(part)}</text>')
                y += 18
            if key in heads:
                y += 10
            between = joins.get((previous, key), []) if previous is not None else []
            if between:
                # Every relation between the two, each inferred one marked; dashed only when all are inferred.
                label = "·".join(_edge_label(edge) + (tr("·추정", "·inferred") if edge.get("basis") == "inferred" else "")
                                 for edge in between)
                dashed = ' stroke-dasharray="5 5"' if all(edge.get("basis") == "inferred" for edge in between) else ""
                body.append(f'<path d="M{x + 40} {bottom} V{y - 4}" fill="none" stroke="#778593" stroke-width="1.7"{dashed} marker-end="url(#arrow)"/>')
                body.append(f'<text x="{x + 52}" y="{bottom + 22}" fill="#667788" font-size="11">{html.escape(label)}</text>')
            title_text = safe_text(event["title"], multiline=False)
            title = _wrap(title_text, SVG_TITLE_CELLS)
            agents = [line for agent in event.get("folded_agents", [])
                      for line in _wrap(f"↳ {safe_text(agent['agent'], multiline=False)}: "
                                        + (safe_text(agent["said"], multiline=False) if agent["said"]
                                           else tr("아직 분석 전", "not analysed yet")), SVG_LINE_CELLS)]
            height = 16 + 18 + 20 * len(title) + 18 * len(agents) + 12
            tone = tones.get(key, "plain")
            number = event.get("number", order[key] + 1)
            label = f"{number:02d} · {title_text} · {safe_text(labels.get(key, ''), multiline=False)}"
            folded = " ".join(folds.get(key, []))
            body.append(f'<g data-event-id="{html.escape(key, quote=True)}" tabindex="0" role="button" '
                        f'aria-label="{html.escape(label, quote=True)}"'
                        + (f' data-folds="{html.escape(folded, quote=True)}"' if folded else "")
                        + f' transform="translate({x},{y})">')
            body.append(f'<rect width="{SVG_BOX}" height="{height}" rx="9" fill="#ffffff" stroke="{SVG_TONE[tone]}" '
                        f'stroke-width="{1.6 if tone != "plain" else 1}"/>')
            body.append(f'<text x="16" y="24" fill="#7b8995" font-size="11">{number:02d} · {html.escape(kind_label(event))}</text>')
            line_y = 24 + 22
            for part in title:
                body.append(f'<text x="16" y="{line_y}" fill="#203343" font-size="14" font-weight="600">{html.escape(part)}</text>')
                line_y += 20
            for part in agents:
                body.append(f'<text x="16" y="{line_y - 2}" fill="#8e4fb0" font-size="12">{html.escape(part)}</text>')
                line_y += 18
            body.append("</g>")
            bottom = y + height
            y = bottom + gap
            previous = key
    if not events:
        empty = (tr("핵심 보기로 그릴 요청·결정·결론이 없습니다. 위 버튼으로 모든 단계를 보세요.",
                    "Nothing to draw in the story view (no requests, decisions or findings); the button above shows every step.")
                 if view.get("hidden") else tr("저장된 사건이 없습니다.", "No saved events."))
        for n, part in enumerate(_wrap(empty, 76)):
            body.append(f'<text x="32" y="{70 + 22 * n}" fill="#617686" font-size="15">{html.escape(part)}</text>')
        y = 120 + 22 * len(_wrap(empty, 76))
    width, height = x * 2 + SVG_BOX, max(y - gap + 28, 120)
    # `story`: the page marks a selected box without painting over its border, which is its status here.
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" class="story" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{html.escape(tr("프로젝트 흐름 · 핵심", "Project flow · story"), quote=True)}" '
            'font-family="Noto Sans CJK KR, Noto Sans KR, Malgun Gothic, Apple SD Gothic Neo, sans-serif">'
            '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
            '<path d="M0 0 L8 4 L0 8 z" fill="#778593"/></marker></defs>')
    return head + "\n".join(body) + "</svg>"
