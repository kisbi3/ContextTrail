# Changelog

All notable changes to ContextTrail are listed here, newest first. Dates are the dates the change landed on `main`. Measurements behind each entry are in `docs/plans/PERFORMANCE_PLAN.md` and `docs/DECISIONS.md`.

## Unreleased

### Added
- **opencode as a third log source.** `sources/opencode.py` reads the opencode SQLite database (`$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`, override with `--opencode-home`; every `opencode*.db` and `OPENCODE_DB`) read-only, from the `session`, `message` and `part` tables only. Sessions are attributed by their recorded directory, sub-agent sessions keep their parent and the `task` call that started them, a compaction becomes a work-unit boundary followed by its summary, `synthetic`/`ignored` text and attachments are metadata, reasoning is never collected. `scan` counts records per source. On this repository the real database (4 of 11 sessions in scope, 1,936 records) parsed in 0.4 s and its files were unchanged afterwards.

## 0.1.0a5 – 2026-10-06

First release under the `contexttrail` name on PyPI.

### Changed
- **One name: ContextTrail.** The PyPI package, the import name and the command are `contexttrail`, with `ct` as a short alias; the `project` and `projectflow` commands and the "Project Flow" product name are gone. Version `0.1.0a5`. Compatibility kept: an existing `.git/projectflow/` (or `.projectflow/`) state directory is still used, `projectflow-eval-v1` fixtures and `projectflow-eval-report-v1` reports still load, and the tool's own `projectflow-run-` runner sessions stay excluded from analysis. `install.sh` links `ct` and removes `project`/`projectflow` links that pointed at this install.

### Added
- **English screen text.** The terminal output, TUI, browser view, CLI help, `find`/`show` output, eval review and input preview are in English or Korean, chosen once per process (`i18n.resolve`): `--language`, else `CONTEXTTRAIL_LANGUAGE`, else the project's saved output language, else the locale, else English. Korean output is byte-identical to before. Text the model reads is now English in one language, no switch: validation messages returned for repair, the input-scope limitations and log-parser warnings that enter the extract input, read-request denials, review questions, and the text code writes into the graph (dialog-turn rationales, unit warnings, dropped-candidate notes). Message shapes and the matchers over them (`QUOTE_MISMATCH_HEADS`, the `informational` tuple, `error_kinds`) changed together. This is a model-input change; the owner's evaluation is pending. The synthetic demo's content is still Korean.
- The synthetic demo has an English story, chosen by the screen language (`demo.cases()`); the mock runner recognises both.
- README rewritten for a first-time reader: what goes in and what comes out on the demo, the same on a real stretch of this repository's own logs, what the tool does and does not do, then install; the measured-numbers table moved to its own section. A TUI screenshot (`docs/images/tui-demo-en.png`) is drawn without a terminal by `scripts/tui_screenshot.py`.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, root `SECURITY.md`, issue and pull-request templates, a `publish` workflow (PyPI Trusted Publishing on a `v*` tag) and `docs/guides/RELEASING.md`.

### Changed
- README measured-numbers table now covers the Claude Code runner (42 eval runs on macOS) and the per-unit cost after the integration change below.

## 2026-10-02 – 2026-10-03

### Changed
- **Integration publishes a code-built draft by default** (`--integrate-output draft`). While the graph is empty the draft is published with no integrate call; against an existing graph the model answers only its changes to the draft (`patch`). On the `repairfix-v2` fixture with Claude Sonnet this halved time and tokens at the same score (`PERFORMANCE_PLAN.md` §12–14). `full` restores the old whole-delta request.
- Relation `basis` is defined in the prompts (explicit / structural / inferred) and `verifies` must be explicit or structural with a covering quote; the repair prompt asks for the quote or removal. `verifies…inferred` rejections went from 4 of 5 runs to 0.
- A quote inside one line is asked to cite exactly that line, not neighbouring lines.
- The integrate prompt states that a resolution targets items of the candidate's own kind and that a new event closing an old open item goes through `open_items_to_resolve`.

### Fixed
- Bookkeeping slips with a single reading are now settled in code and audited instead of costing a repair call: a candidate marked `duplicate` of an item the same delta adds becomes `added`; a relation no candidate names is attributed to the event candidates at its ends; a review patch that removed an item no longer leaves dangling resolutions or attributions; a new event may close an open item it was not opened on.
- A quote repeated within three cited lines is stored as the lines holding every copy; repeats farther apart are rejected with their line numbers so the repair can pick one.
- A `verifies` between a documentation-only change and a run whose command never names that file is dropped and noted in limitations.
- Tool and Git citations a candidate carried are restored when the integrator leaves them out.

### Eval and ops
- `report.json` splits the score by cause (events missing / split / merged; relations scored / unverifiable), sums every token kind the provider reports (including Claude cache tokens), and records where repeated quotes sat. Hidden `eval --unit-records N` splits a fixture into several work units.

## 2026-09-30

### Changed
- Analysis speed: lean extract context (graph-shaped view plus cited lines ±5), larger work units, review answers as a patch instead of a whole-delta rewrite (`--review-output patch`, default), fewer repair calls. Measurements in `PERFORMANCE_PLAN.md` §1–7.
- Extract workers stay at one, with the next unit prefetched in the background while the current one integrates.

## 2026-09-28 – 2026-09-29

### Added
- Initial public release `0.1.0a4` under the MIT License.
- CI on Linux and macOS across Python 3.11–3.13 (`.github/workflows/test.yml`).
- Linux live evaluation on an external project under bubblewrap (`docs/reports/LINUX_LIVE_EVAL_2026-09-28.md`).
- Measured-evidence README with published artifacts.

### Fixed
- `install.sh` virtualenv detection and version hint.
- Evidence reads batched; detour lane overlap in the flow diagram.
