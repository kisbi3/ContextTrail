"""Draw the terminal view of a saved graph onto a fake screen and write it as a square SVG.

No terminal, no model call: it loads the store, draws `GraphApp` as the TUI would, and QuickLook or
any SVG renderer turns the file into a PNG (`qlmanage -t -s 2000 -o <dir> out.svg`). Used for the
README screenshot; the demo project is the usual input (`contexttrail demo` with CONTEXTTRAIL_LANGUAGE=en). The
README's is `<demo>/sample-project tui.svg 62 106 --whole --select 3 --title sample-project`, then
`sips --cropToHeightWidth 2000 1690`. Keys: `--whole` the whole flow, `--all` every step, `--flag` the next event
that needs a look, `--select N` the Nth event down with its detail open.

Usage: PYTHONPATH=src python scripts/tui_screenshot.py <project folder> <out.svg> [rows cols] [--whole] [--all] [--flag] [--select N] [--title T]
"""
import curses
import html
import sys
from pathlib import Path

from wcwidth import wcwidth

from contexttrail import i18n
from contexttrail.git_context import Scope
from contexttrail.store import Store
from contexttrail.ui import GraphApp

FG = {"": "#d6dde6", "bold": "#ffffff", "dim": "#7f8a96"}
BG = "#10151b"
# The TUI's colour pairs (`ui._styles`), drawn here as bits of their own above curses' attributes.
COLORS = {"frame": "#56c2d6", "ok": "#5fbf7f", "warn": "#e0b04a", "fail": "#e06c62", "sub": "#c584e0",
          "llm": "#6f9be8"}
BITS = {name: 1 << (40 + n) for n, name in enumerate(COLORS)}


def colour_styles():
    """`ui._styles(True)` with the fake colour bits in place of curses' colour pairs."""
    styles = {"": 0, "plain": 0, "title": curses.A_BOLD, "heading": curses.A_BOLD | BITS["frame"],
              "dim": curses.A_DIM, "ok": 0, "warn": curses.A_BOLD, "fail": curses.A_BOLD, "frame": 0, "sub": 0,
              "llm": 0}
    for name, bit in BITS.items():
        styles[name] |= bit
    return styles


# The selected box's heavy border (`ui.HEAVY`) comes from a fallback font whose glyphs sit off Menlo's cells; drawn
# as the light glyphs in bold, the box keeps its column.
LIGHT = str.maketrans("┏┓┗┛━┃┣┫┳┻╋", "┌┐└┘─│├┤┬┴┼")


def ink(attr):
    for name, bit in BITS.items():
        if attr & bit:
            return COLORS[name]
    return FG["bold"] if attr & curses.A_BOLD else FG["dim"] if attr & curses.A_DIM else FG[""]


class Screen:
    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.cells = [[(" ", 0)] * cols for _ in range(rows)]

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        self.cells = [[(" ", 0)] * self.cols for _ in range(self.rows)]

    def refresh(self):
        pass

    def addstr(self, row, col, text, attr=0):
        for char in text:
            size = max(1, wcwidth(char))
            if col + size > self.cols:
                raise curses.error("off screen")
            self.cells[row][col] = (char, attr)
            if size == 2:
                self.cells[row][col + 1] = ("", attr)
            col += size


def runs(row):
    out, current, attr = [], "", None
    for char, a in row:
        if attr is None or a == attr:
            current += char
            attr = a
        else:
            out.append((current, attr))
            current, attr = char, a
    if current:
        out.append((current, attr))
    return out


def main():
    args = sys.argv[1:]
    folder, out = Path(args[0]), Path(args[1])
    rows, cols = (int(args[2]), int(args[3])) if len(args) > 3 and args[2].isdigit() else (34, 118)
    select = int(args[args.index("--select") + 1]) if "--select" in args else None
    title = args[args.index("--title") + 1] if "--title" in args else folder.name
    i18n.set_language("en")
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    app = GraphApp(store.graph(), store.evidence, title=title)
    app.panels.styles = colour_styles()
    screen = Screen(rows, cols)
    app.draw(screen)  # the first draw opens on the latest request; keys below act on what it shows
    if "--all" in args:
        app.panels.handle("v")  # every step instead of the story view
    if "--whole" in args:
        app.panels.handle("A")  # the whole flow, not one request's turn
    if "--flag" in args:
        app.panels.handle("!")  # the next event that needs a look
        app.panels.handle("\t")
    if select is not None:
        for _ in range(select):
            app.panels.handle("j")  # move the selection down
        app.panels.handle("\t")  # open the detail pane
    app.draw(screen)
    cw, lh, pad = 8.4, 17.5, 24  # Menlo 14px advance, line height, padding
    width = int(cols * cw + pad * 2)
    height = int(rows * lh + pad * 2)
    side = max(width, height)
    top = (side - height) // 2
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" viewBox="0 0 {side} {side}">',
           f'<rect width="{side}" height="{side}" fill="{BG}"/>',
           f'<rect x="{(side - width) // 2}" y="{top}" width="{width}" height="{height}" rx="10" fill="#0b0f14"/>',
           '<g font-family="Menlo, Monaco, monospace" font-size="14">']
    x0 = (side - width) // 2 + pad
    for r, row in enumerate(screen.cells):
        y = top + pad + (r + 1) * lh - 5
        x = x0
        for text, attr in runs(row):
            width_cells = sum(max(1, wcwidth(c)) if c else 0 for c in text)
            fill = ink(attr)
            if attr & curses.A_REVERSE:  # the selected line: its colour as the background
                svg.append(f'<rect x="{x:.1f}" y="{y - lh + 5}" width="{width_cells * cw:.1f}" height="{lh}" '
                           f'fill="{fill}"/>')
                fill = BG
            weight = ' font-weight="bold"' if attr & curses.A_BOLD else ""
            # Each glyph at its own cell: a box-drawing glyph from a fallback font is wider than Menlo's advance,
            # and a run of them pushed every border after it off its column.
            at = x
            for char in text.translate(LIGHT):
                if char.strip():
                    svg.append(f'<text x="{at:.1f}" y="{y}" fill="{fill}"{weight}>{html.escape(char)}</text>')
                at += (max(1, wcwidth(char)) if char else 0) * cw
            x += width_cells * cw
    svg.append("</g></svg>")
    out.write_text("\n".join(svg), encoding="utf-8")
    print(out, side, "x", side, "content", width, "x", height)


if __name__ == "__main__":
    main()
