"""Draw the terminal view of a saved graph onto a fake screen and write it as a square SVG.

No terminal, no model call: it loads the store, draws `GraphApp` as the TUI would, and QuickLook or
any SVG renderer turns the file into a PNG (`qlmanage -t -s 2000 -o <dir> out.svg`). Used for the
README screenshot; the demo project is the usual input.

Usage: PYTHONPATH=src python scripts/tui_screenshot.py <project folder> <out.svg> [rows cols] [--whole] [--flag] [--select N] [--title T]
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
    if "--whole" in args:
        app.panels.handle("A")  # the whole flow, not one request's turn
    if "--flag" in args:
        app.panels.handle("!")  # the next event that needs a look
        app.panels.handle("\t")
    if select is not None:
        for _ in range(select):
            app.panels.handle("j")  # move the selection down
        app.panels.handle("\t")  # open the detail pane
    screen = Screen(rows, cols)
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
            if text.strip():
                style = "bold" if attr & curses.A_BOLD else "dim" if attr & curses.A_DIM else ""
                weight = ' font-weight="bold"' if style == "bold" else ""
                svg.append(f'<text x="{x:.1f}" y="{y}" fill="{FG[style]}"{weight} xml:space="preserve">'
                           f'{html.escape(text).replace(" ", " ")}</text>')
            x += width_cells * cw
    svg.append("</g></svg>")
    out.write_text("\n".join(svg), encoding="utf-8")
    print(out, side, "x", side, "content", width, "x", height)


if __name__ == "__main__":
    main()
