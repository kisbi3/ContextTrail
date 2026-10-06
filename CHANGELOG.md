# Changelog

All notable changes to ContextTrail are listed here, newest first. Dates are the dates the change landed on `main`. Measurements behind each entry are in `docs/plans/PERFORMANCE_PLAN.md` and `docs/DECISIONS.md`.

## Unreleased

### Added
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, root `SECURITY.md`, issue and pull-request templates.

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
