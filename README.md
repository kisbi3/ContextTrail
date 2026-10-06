> **⚠️ Development alpha — `0.1.0a4`.** Both real CLIs have now been measured end to end on fixtures and on this repository's own logs, but analysis quality is still a draft to check, not a record. Below is every number we have actually measured. Nothing on this page is an estimate presented as a result.

## Measured so far

| | Measured | Not yet done |
| --- | --- | --- |
| Real model calls | **Codex CLI 12 analysis calls** (8 on this repository's own logs, 4 on the Linux external project, both `gpt-6-sol`) plus 2 `doctor --smoke` round-trips; **20 calls** across the 6 scored fixture runs ([Linux](docs/reports/artifacts/linux-live-eval-2026-09-28.json) · [table](docs/plans/NEXT_STEPS.md)). **Claude Code CLI: 42 eval runs** on macOS with `claude-sonnet-5-5` (2 installer smoke runs, 40 scored runs on the `repairfix-v2` fixture), 3–4 calls each ([record](docs/plans/PERFORMANCE_PLAN.md) §9–14) | Claude Runner on Linux: 0 calls (that host has no Claude CLI). No Claude run on an external project yet |
| Real analysis scale | Linux: 5,282 in-scope records → 8 events / 9 edges, 4 runner calls, read-only check passed. macOS self-analysis: 4,199 records → 17 events / 15 edges ([Linux](docs/reports/artifacts/linux-live-eval-2026-09-28.json) · [macOS](docs/reports/artifacts/self-analysis-a4.json)) | — |
| Cost per work unit | Claude Sonnet, `repairfix-v2` (26–34 records per unit): **1.5–2.4 minutes per run of 2 units**, 130k–240k cached input tokens and 12k–21k output tokens, after the integration step was changed to publish a code-built draft and ask the model only for its changes ([§12–14](docs/plans/PERFORMANCE_PLAN.md)). The first Claude baseline was 2.7 minutes and about twice the tokens for 1 unit | Codex cost after the same change: not measured |
| Archive parse audit | **156 files** (Claude 40, Codex 116), **31,748 records** selected, `limitations: 0` as of the a4 parser ([audit](docs/reports/artifacts/archive-audit-a4.json) · [report](docs/reports/PRELIVE_AUDIT.md)) | **Stale for this parser.** The record-type split in `sources/local.py` now reports types that audit did not. Re-run needed |
| Semantic quality | Codex: **79/111 expectations met (71%)** across 6 runs of 2 fixtures. Claude Sonnet on `repairfix-v2`: **11.8/18 mean** over the latest 5 runs (11, 11, 13, 13, 11), two work units, integration against an existing graph, 0 bookkeeping rejections ([§14](docs/plans/PERFORMANCE_PLAN.md)) | **2** human-checked WorkUnits total (14 records on macOS, 1 unit on Linux). Not a rate. The three expectations that fail in every Claude run are two-hop `verifies` judgements (a test run covering the code it calls) |
| Reproducibility | **Characterized for one configuration.** Five same-configuration Claude Sonnet runs of `repairfix-v2` scored 11, 11, 13, 13, 11; the per-item breakdown shows the spread comes from events being split or merged differently between runs, not from relations ([§11, §14](docs/plans/PERFORMANCE_PLAN.md)). Codex `installer` scored 8/18 and 16/18 on consecutive runs, confounded by different integration effort | No same-configuration repeat with Codex |
| Platform validation | **Linux/Ubuntu 24.04 + bubblewrap: 1 end-to-end run on an external project** ([report](docs/reports/LINUX_LIVE_EVAL_2026-09-28.md)) · macOS: `sandbox-exec` + Codex smoke + self-analysis + the 42 Claude eval runs above | Claude Runner under bubblewrap: 0 calls (no Claude CLI on the Linux host) |
| Tool-denial tests | Canary escape probe at startup on both macOS and Linux ([SECURITY](docs/SECURITY.md)) | `~`, `.ssh`, project tree, and real credential write-blocking unverified on both |
| Tests | **437** in the suite, all passing (2026-10-06; [a4 results](docs/reports/artifacts/tests-a4.txt) · [state](docs/reports/artifacts/self-analysis-a4.json)); CI runs them on Linux + macOS × Python 3.11–3.13 | 0 real model calls in CI, by design |

Evidence behind these numbers is published, not summarized: [design & evaluation history](docs/DECISIONS.md) · [performance plan and Claude measurements](docs/plans/PERFORMANCE_PLAN.md) · [pre-live audit](docs/reports/PRELIVE_AUDIT.md) · [real-CLI evaluation](docs/reports/TWO_CALL_LIVE_EVAL_2026-09-25.md) · [a3 validation](docs/reports/A3_VALIDATION.md) · [what is left](docs/plans/NEXT_STEPS.md).

Redaction in those files: real project names, native session UUIDs, source snapshot IDs, and machine-specific absolute paths are replaced with placeholders. What is deliberately **kept** is the aggregate evidence — record and session counts, work-unit counts, token totals, durations, and the archive SHA-256 digests, because a digest is what proves the audit did not modify the archive. The `LICENSE` copyright name is unchanged, and so is the GitHub account in the badge above.

> The quality row is the honest one: extraction is good enough to be useful on a project you remember well, and there is no measurement showing it is stable across repeated runs. Treat a reconstructed flow as a draft to check, not a record.

A zero-real-model walkthrough is available after installation: `python scripts/prelive_walkthrough.py --output /tmp/pf-prelive-walkthrough`

---

# Project Flow · ContextTrail

[![test](https://github.com/kisbi3/ContextTrail/actions/workflows/test.yml/badge.svg)](https://github.com/kisbi3/ContextTrail/actions/workflows/test.yml)

**Reads local Codex and Claude Code conversation/tool logs and Git changes to reconstruct a project's goals, attempts, failures, and decisions as an evidence-linked flow.**

Version: `0.1.0a4` · 2026-09-23 · Linux / SSH primary, macOS experimental · names are provisional.

> **Development alpha — not a finished MVP release.**
> a3 added model routing, a bounded worker pool, selective re-review, and local eval/ops. a4 strengthens inter-stage evidence passing, source isolation, failure states, and runner output inspection. Since 2026-10-02 the Claude Runner is measured on macOS alongside Codex, and the pipeline was reworked so a cheap model (Claude Sonnet) finishes a work unit in about a minute: the integration step publishes a code-built draft and asks the model only for its changes, and single-reading slips in model output (quote placement, provenance, bookkeeping) are corrected in code and audited instead of costing a repair call. Full semantic reconstruction on a large real project is still not validated.

**Additional features:** [model tiering, project filters, and test usage](docs/guides/TIERED_ANALYSIS.md). Before opening the UI, run `project scan .` to review input selection, and `project eval --fixture demo --runner mock --output /tmp/pf-eval` to exercise the execution path without account calls.

**Languages:** the terminal, TUI, browser view and CLI help are in English or Korean, chosen from `--language`, `CONTEXTTRAIL_LANGUAGE`, the project's saved output language, or the locale. Event titles and summaries are written in the language of your own messages (`--language` overrides). Validation messages exchanged with the model are still Korean. A Korean README is not written yet.

---

## 1. Getting Started

Requires Python 3.11+, Git, and a UTF-8 terminal. Python's `curses` module and venv support are needed. On Linux or macOS, clone the repository and run once from the repo root:

```bash
cd /path/to/ContextTrail
./install.sh
contexttrail --version
```

`install.sh` installs the package into `~/.local/share/contexttrail/venv`, links `~/.local/bin/contexttrail` and (where possible) the `project` command, and registers Codex/Claude Code agent commands. Existing user command files are preserved; files created by ContextTrail are updated on reinstall. Package repository access may be required. If `~/.local/bin` is not in `PATH`, follow the instructions printed during installation. Running `pip install` or `pip install git+...` directly does not register agent commands — run `contexttrail install-commands` afterward.

After installation, running `contexttrail` with no arguments in a project directory opens its saved flow view — equivalent to `project view .`. AI is called only when you press `R` in the TUI. To open a different directory, pass it as the first argument: `contexttrail /path/to/project`.

```bash
cd /path/to/project
contexttrail
```

In a non-Git directory, state is stored in `.projectflow/`. In a Git repository, it goes under `.git/projectflow/`.

Code was tested on Python 3.13.5. Python 3.11 and 3.12 compatibility is pending verification. If another program already uses the `project` command, use `contexttrail` or `python -m projectflow` instead.

### Running the demo without AI or credentials

```bash
project demo --path /tmp/projectflow-demo
```

Specify a new or empty directory. This generates synthetic Codex/Claude logs and a demo project **without calling any external model** — it uses a test-only Mock Runner that handles a fixed set of cases. This Mock cannot be selected as the analyzer for a real project.

Example flow:

```text
[01] Adopt JSON storage / adopted
├── follow → [02] Propose SQLite alternative / proposed
│   └── follow → [05] Decide to switch to SQLite / adopted
│       └── follow → [06] Report SQLite migration done / report·unverified
└── follow → [03] Implement JSON storage / report·unverified
    └── follow → [04] Concurrent-write test fails / observed failure
        └── motivates → ↗ [05] (merges/returns)
```

Viewing saved results and re-running without changes:

```bash
project view /tmp/projectflow-demo/sample-project
project analyze /tmp/projectflow-demo/sample-project --no-tui
# noop if no new records — 0 runner calls
```

---

## 2. Applying to a Real Project

### Setting Up a Runner and Diagnosing

**Install and log in to the Codex or Claude Code CLI yourself** before use. ContextTrail does not handle token issuance, copying, or login. On Linux, real AI execution requires `bubblewrap` (`bwrap`) and available user namespaces. On macOS, `sandbox-exec` and file-based CLI credentials are required. If isolation tools or required CLI options are missing, execution is blocked. The macOS path uses a deny-default Seatbelt profile with a temporary HOME; it has passed Codex `doctor --smoke` and a small live-segment analysis. Blocking of malicious tools and config is not yet validated.

```bash
project doctor --runner codex
project doctor --runner claude
```

The default `doctor` run checks CLI version, help output, and isolation feasibility — no model calls. **The presence of a credential file does not guarantee successful authentication.** The following commands make explicit real-account calls, sending a small synthetic input (not your project data):

```bash
project doctor --runner codex --smoke --yes
project doctor --runner claude --smoke --yes
```

This smoke test is a simple structured JSON round-trip. **It does not substitute for full integration validation of mixed-log analysis, all ReadRequests, or permission blocking.** Per-CLI flags and unvalidated items are in [Implementation Status](docs/reports/IMPLEMENTATION_STATUS.md); isolation constraints are in [Security](docs/SECURITY.md).

Current alpha authentication constraints:

- Only file-based credentials are supported: Codex's `auth.json` and Claude's `.credentials.json` are bind-mounted read-only into the CLI's isolated environment. The program does not read or copy their contents.
- Keyring-only auth, per-org user settings, custom API providers, proxies, and certificate inheritance are not supported.
- If credential renewal requires a write, the run may fail. Log in or refresh via the original CLI, then retry. Permissions are not escalated to work around failures.

### Confirming Input Scope → Analyzing

```bash
# Check relevant record count, linked worktrees, and limitations — no AI calls
project scan /path/to/project

# Read logs from both sources and analyze with one chosen runner
project analyze /path/to/project --runner codex
# or
project analyze /path/to/project --runner claude
```

Replace `/path/to/project` with the actual path on your server. The first analysis prompts for transmission consent. **Conversation turns, tool outputs, and code fragments may be sent to the model service of the chosen CLI.** This is not an automatic secret redactor — verify your project's transmission policy before proceeding with sensitive material.

The chosen runner, settings, and consent are stored in the per-scope local DB. Analysis proceeds oldest-first through **work units**. Each run first shows a summary such as "1,480 units pending · 15 this run (≤30 AI calls) · ~2.39M input tokens · ~120 min (estimated)" along with token/time projections for 5, 15, and 30 units, then prompts for the number of units to process (Enter = shown count, `n` = cancel, `--yes` = no prompt). Use `--units N` to specify in advance. Once units are set, the per-unit AI call cap is 6; `--max-calls` overrides that. Both values apply only to the current run and are not saved. Estimates are calibrated from actual input tokens and timing if ≥3 units with usage records exist. Use `project scan` to preview the plan without sending anything (`plan_text`, `plan_choices`).

**Output language.** Event titles and summaries are written in one language per project, determined by the dominant language of user messages during the first analysis run. Script-distinct languages (Korean, Japanese, etc.) are identified directly; for Latin-script languages (English, Spanish, etc.) the system locale is used as a tiebreaker. Source text in other languages is still summarized in the chosen language. Code names, filenames, commands, and quotations remain verbatim. Use `--language Korean` to change; `--language auto` re-detects. Already-analyzed events are not re-translated unless re-analyzed.

**Speed.** While integrating one work unit, extraction for the next unit begins in parallel (with one extraction worker). Token cost is identical; only wall-clock time is reduced. For read-only tool calls (file reads, listings, searches), only the first 400 and last 200 characters are sent; the model may request the rest via a ReadRequest.

`--session <session-id>` prioritizes that session and its sub-agents over older records. `--session current` refers to the Codex/Claude Code conversation that invoked this command. Events added this way are marked "out-of-order analysis" — relationships to earlier unanalyzed records may be missing. Use `project analyze /path/to/project` for subsequent runs. Runner does not switch silently; completed segments are not re-analyzed just because settings changed.

**Analysis model and reasoning level.** ContextTrail always specifies the model by name because the isolation environment does not carry the user's CLI config file (`~/.codex/config.toml`, etc.). Defaults: Codex `gpt-6-sol`, Claude `sonnet`; override with `--model`. Per-stage reasoning level defaults to `medium` for all stages (lowered from `high` for integrate/re-review on 2026-09-27). Adjust with `--extract-effort`, `--integrate-effort`, `--escalation-effort` (`low`·`medium`·`high`·`xhigh`·`max`). Every call logs the requested model and reasoning level in the local call ledger (`project ops --details`) and evaluation reports. Claude records the responding model; Codex does not report it in its response.

**Change re-review.** If integration results contain unlinked execution results, unlinked fixes, or modifications to existing events/relationships, the same runner reviews the proposed changes one more time. This adds one call to that work unit. Use `--no-review` to skip for the current run only.

For non-interactive use, consent can be given with explicit `--yes`. The following command goes beyond a simple example and allows real transmission — confirm scope first:

```bash
project analyze /path/to/project --runner codex --yes --no-tui
```

Default log paths: `CODEX_HOME` or `~/.codex` (`sessions/`, `archived_sessions/`) and `CLAUDE_CONFIG_DIR` or `~/.claude` (`projects/`). Override when paths differ per server:

```bash
project scan /path/to/project \
  --codex-home /path/to/codex-home \
  --claude-home /path/to/claude-home
```

Only the project currently being worked on is analyzed. If logs are on a local PC and the project is on a Linux server, the server does not automatically collect logs from the PC. Cross-machine/repo auto-merge is not a feature of this version.

Extraction input contains the source text for the work unit plus limited surrounding context. Up to 4 conversation/Git records from the same worktree within 15 minutes are placed at the front of the surrounding context and additional read list. **Temporal proximity alone does not imply causation.** Long Git diffs are split into citable fragments of ≤32,000 characters, the same as logs. Commits whose Git command output exceeds the 4 MB safety limit are deferred while the next commit continues — check `project scan` for limitations.

Work units are first grouped by session and worktree. Within a session, summary compression, gaps ≥3 hours, and date changes with sufficient gaps are used as boundaries. When the input budget is reached, breaks are made at user-turn boundaries where possible; tool calls/results and source fragments are kept together. Work unit boundaries do not correspond to event node boundaries in the final graph. Already-analyzed segments are not re-chunked in incremental analysis.

To preview a small fixed evaluation fixture before model calls, run `project eval --fixture /tmp/case.json --preview --output /tmp/case-preview`. The output `input-preview.html` shows WorkUnit boundaries and source text, the common system instruction, the extraction instruction, `new_records`/`context_only`, existing events, evidence, manifest, the response JSON Schema, the full task JSON, and the generation path of each part. **The first unit's input is the exact initial request; subsequent units' existing-graph sections are estimates that depend on earlier model responses.** Preview includes private source text; no AI calls are made.

After `project eval`, `review.html` is generated automatically. It shows event/relationship candidates, citation strings vs. actual source lines, and validation errors for each model call side by side. Eval runs also save failed structured responses to `call-review/` with private user permissions. You can regenerate HTML for earlier evals with `project review /path/to/eval-output`, but source model response text not saved at the time cannot be recovered. This local review view makes no AI calls or external transmissions.

---

## 3. Using the Terminal UI

`project view`, `project analyze`, and `project graph` share the same screen. When user messages exist in the graph, the left panel shows a **request list**; the center **event flow** panel renders only the span of the selected request — from that message to the next message in the same session. Use `[`/`]` to navigate requests, and `A` to toggle between full-flow and per-request views. Connections to out-of-span events are annotated inline as `← [02] motivates`. Events before any recorded request are grouped under "Events without request record."

The **event flow** panel draws events as boxes with labeled arrows. The left column lists events in record order; execution results that verified a change but do not continue elsewhere are attached to the right of that change as `──verified──▶`. Results that lead to the next story beat (e.g. a failure motivating a fix) remain in the column, so `failure → motivates → fix` reads downward. Adjacent boxes connect with downward arrows; distant boxes connect via a margin line. The margin line notes the source box number and relationship as `[02] motivates ▶` before the destination, so the origin is visible without scrolling back. Unrendered connections are annotated on both boxes as `→ [06] motivates` / `← [05] motivates`. Events with no relationships show "No connected events." Every user message becomes a `request` box; events in the same session span with no other relationships are connected from that request as `follow-up` — this reflects conversational structure, not causal claims. Groups of events sharing no session or relationship (e.g. independent features from separate sessions) are rendered under `══ Flow 1/2 ══` headings. Titles and status are never truncated — they wrap. The selected box is drawn with a bold border; only its connected lines, annotations, and boxes are highlighted. When a panel is too narrow for boxes, it falls back to a branch list.

Status markers: `✓` confirmed (verified change, observed result, answered request), `!` needs confirmation (unverified change, completion report, result without a verification target, unanswered request), `✗` failed.

The right **selected event** panel shows the description, the result that verified this change (or the change this result confirmed), connected events, and source evidence. Results and connected events are numbered 1–9; press the digit to navigate and Backspace to return. Evidence is shown with record type, source, timestamp, and line position. Single-line tool-call arguments with escaped newlines (`\n`-escaped patches etc.) are reflowed visually **on screen only** — stored evidence is verbatim. At ≥180 columns, all three panels are side-by-side; at ≥150 columns, the flow and detail panels are side-by-side; below that, they stack vertically.

| Key / Option | Action |
| --- | --- |
| ↑↓ / `j` `k` | Previous/next event. In per-request view, wraps to the next request at the end of a span |
| ←→ / `h` `l` | Jump to connected box in adjacent panel (change ↔ right-side result). Horizontal scroll in the narrow branch-list view |
| PgUp·PgDn / Space | Scroll one screen |
| `g` `G` / Home·End | First/last event |
| `[` `]` | Previous/next request |
| 1–9 / Backspace | Navigate to numbered connection in the selected-event panel / return to previous event |
| `!` | Jump to next event needing attention (`!`·`✗`); wraps from end to start |
| `/` → `n` `N` | Search titles, summaries, and source evidence; next/previous result. Matching box titles are underlined |
| Tab / Enter | Toggle focus between the event flow and selected-event panels |
| `J` `K` | Scroll the selected-event panel without changing focus |
| `e` | Toggle full evidence expansion ↔ 8 lines per piece |
| `z` | Zoom current panel to full screen ↔ restore |
| `A` | Toggle full-flow / per-request-flow view |
| Esc | Close search or zoom; return from selected-event panel to flow panel |
| `?` | Show full keyboard help |
| Mouse | Click to select a box, request, or numbered connection (clicking a line or `[02] motivates ▶` annotation jumps to the connected box); scroll wheel navigates events/requests and scrolls detail |
| `y` | Copy a reference (`contexttrail:ev_6226b954@v12`) to clipboard. Paste into a Claude Code or Codex conversation — the agent reads that event with its evidence. Uses `pbcopy` locally on macOS, OSC 52 over SSH; if neither works, select the text from the status line |
| R | Explicit incremental analysis. Shows pending volume and choices, prompts for unit count, then runs while keeping the previous graph visible (`view`·`analyze`) |
| B | Print the browser URL and SSH forwarding instructions for the current version / selected event (`view`·`analyze`, optional feature) |
| Q | Quit. During analysis, confirms cancellation and cleans up child processes |
| `--ascii` | Replace box-drawing characters, arrows, and status markers with ASCII (`v` `!` `x`). Non-ASCII titles are preserved |
| `--no-tui` | Print the annotated flow and status without interactive UI |
| `--no-color` | Disable status colors. Markers (✓ ! ✗) are preserved. Also respects the `NO_COLOR` env var |
| `--no-mouse` | Disable mouse capture and restore terminal text selection |

Keys also work when a Korean IME is active (e.g. `ㅂ` → Q, `ㅓ` → J). If the IME is mid-composition when the terminal delays sending, a second keypress may be needed.

The UI does not depend on terminal image protocols or specific emulators. Mouse is used when available, but every action is accessible via keyboard. Real branches, merges, and back-edges are rendered; already-shown nodes appear as references. Long graphs scroll; deeply nested branches are collapsed to references.

**Compatibility targets:** standard SSH, Windows Terminal, IDE terminals, tmux, screen. Tested: `xterm-256color`, `screen-256color`, `tmux-256color`, `linux`, `vt100` TERM values, and resize on a Linux PTY. This does not imply complete screen/keyboard compatibility verification on each product. `TERM`, CJK width, and font issues must be verified on real servers separately.

In `TERM=dumb` or when output is piped, the UI falls back to plain text. The `project graph` text output follows the flow with per-event description, verification, connections, and evidence — the same content as the right panel. ContextTrail agent commands installed into Codex/Claude Code read this output. If curses initialization fails on an unknown terminfo, a clear error is shown; use `--no-tui` to view saved results.

The **analysis tokens** counter at the bottom of the screen is the sum of calls where the runner reported both input and output tokens in this project's local LLM call ledger. If some calls lack usage data, `+ (confirmed calls/total calls)` is appended. It does not include tokens used in the original Codex/Claude conversations or estimated costs.

### Developer Execution Tracing

LangSmith tracing (`--langsmith`) is a developer tool for inspecting analysis structure and is not a production feature. It does not appear in command help; see [LLM Ops](docs/guides/LLM_OPS.md) for usage.

### Viewing Analysis Runs in Studio (Developer)

`project analyze` and `project eval` invoke the **same LangGraph execution graph** as Studio's `contexttrail_analysis`. The LangGraph runtime is included in the base install; the optional `studio` install adds a development server. You can inspect extraction input preparation, model calls, candidate evidence validation, integration input preparation, graph change summaries, and SQLite publishing in live execution nodes. `extract_input.request` and `integrate_input.request` in node state are the exact tasks to be sent; `candidate_audit` and `graph_change_audit` contain source citations and reflected events/relationships. Additional read and repair calls appear in sub-traces. Claude can be read as log input but only Codex is used as the model runner. Project paths and eval fixtures for live mode are fixed via server environment variables, not Studio input.

```bash
python -m pip install -e '.[dev,studio]'
# Use only the LangSmith key from your private .env, with a separate tracing project
dotenv -f /path/to/private/.env run -- env -u OPENAI_API_KEY \
  LANGSMITH_TRACING=true LANGSMITH_PROJECT=contexttrail-dev \
  langgraph dev --no-browser --host 127.0.0.1 --port 2025
```

Replace `/path/to/private/.env` with your own private file path. In the **Studio UI** URL printed by the server, select `contexttrail_analysis` and run with empty input `{}` to practice with synthetic logs and the Mock Runner. To see a real Codex analysis, stop the server and restart with the project and fixture paths fixed as environment variables:

```bash
dotenv -f /path/to/private/.env run -- env -u OPENAI_API_KEY \
  CONTEXTTRAIL_STUDIO_SCOPE=/path/to/project \
  CONTEXTTRAIL_STUDIO_EVAL_FIXTURE=/path/to/reviewed-small-fixture.json \
  LANGSMITH_TRACING=true LANGSMITH_PROJECT=contexttrail-dev \
  langgraph dev --no-browser --host 127.0.0.1 --port 2025
```

Studio input `{"mode":"eval","confirm_live":true,"max_units":1,"max_calls":10}` analyzes the reviewed small fixture with **real Codex** and saves to a separate temporary state. `{"mode":"live","confirm_live":true,"max_units":1,"max_calls":10}` scans the full project and publishes to the existing ContextTrail state DB. Only one work unit is processed by default; if units remain, the run ends with `partial`. Actual model input/output is visible in the `Codex structured response` sub-trace in Studio. Records from `project analyze` run separately in the terminal do not appear retroactively in Studio UI (see [LLM Ops](docs/guides/LLM_OPS.md) for developer tracing). The Studio dev server has no user authentication — bind only to `127.0.0.1` and shut down after use. See [Studio Architecture](docs/guides/STUDIO_ARCHITECTURE.md) for details.

---

## 4. Browser Detail View

Press `B` in the TUI, or run in a separate terminal:

```bash
project serve /path/to/project --port 8765
```

The server binds to `127.0.0.1` only by default. Set up SSH port forwarding from your PC (replace `user@server` with your actual connection details):

```bash
ssh -L 127.0.0.1:8765:127.0.0.1:8765 user@server
```

Open the **full URL including the token** printed in the server terminal in your PC browser. If a port conflict causes a different port to be selected, use the actual port shown. The URL contains a private access token for your records — do not share it externally.

Select a graph node to inspect event/relationship/evidence levels, preserved source excerpts, and diff identification at the time of the event. F5, page load, and node selection do not call AI. Analysis must be explicitly requested via the **"Analyze Changes" button and confirmation dialog**. The token becomes invalid when the server stops.

No external CDNs or web fonts are used. The browser graph is a custom SVG layout generated from a safe internal Mermaid subset. **This is not a full Mermaid engine bundle.** Layout and actual browser rendering for complex graphs are pending further validation.

---

## 5. Saving and Exporting

To open the saved event flow directly in the terminal:

```bash
project graph /path/to/project
project graph /path/to/eval-output
```

In a terminal, this opens an interactive view with the event flow and selected event's description and evidence side by side. Use arrow keys to select events, `Tab` to focus the detail panel, and `Q` to close. When a screen cannot be opened (e.g. when piped or run from a coding agent), the same command prints event summaries, relationships, and evidence excerpts to stdout. No AI calls or external transmissions are made.

The same command works on `project eval` output directories. Stdout may contain private source citations — review before sharing.

To find or inspect individual events:

```bash
project find "install script"        # events with all words in title, description, or source evidence — newest first
project find                         # recent events and open items
project show ev_6226b954             # status, connections, and source evidence for one event
project show contexttrail:ev_6226b954@v12   # copied reference; notifies if event changed since v12
```

Output is brief and machine-friendly by default; `--json` is also supported. No AI calls are made — reads only from stored results.

### Installing Codex / Claude Code Commands

`./install.sh` automatically registers two commands as personal skills in the current user's Codex and Claude Code installations. If you installed via `pip` directly, run `contexttrail install-commands` once. Files created by ContextTrail are updated on reinstall; separately authored files are not overwritten. Use `contexttrail install-commands --force` only if you want to replace those too. If the agent does not recognize the new commands, start a new session.

| Task | Codex | Claude Code |
| --- | --- | --- |
| Update graph | `$contexttrail-update [N units \| current session]` | `/contexttrail-update [N units \| current session]` |
| Load context from saved graph | `$contexttrail-context [reference \| search term]` | `/contexttrail-context [reference \| search term]` |

The legacy slash-style variants `/prompts:contexttrail-update` and `/prompts:contexttrail-context` for Codex CLI/IDE are also installed for compatibility; Codex recommends the skill form. Both commands invoke the `contexttrail` CLI from the shell — no MCP server required.

- **Update** runs only when the user calls it by name. Claude Code uses `disable-model-invocation`; Codex uses `allow_implicit_invocation: false` in `agents/openai.yaml` to prevent the agent from calling it on its own. It first shows pending units and per-choice token/time projections, then prompts for unit count. Providing a count (e.g. `/contexttrail-update 5`) skips the prompt; `current session` prioritizes the active conversation's session. Analysis uses the **Codex Runner** only (`analyze --no-tui --brief --units N`, printing a few-line summary instead of the full flow). Since it can take a long time, the agent runs it in the background where possible and reports progress as `unit done k/N` lines. Interrupted runs resume from the last completed unit. Running inside Codex may be blocked by Codex's own isolation — the agent will guide you to run the command in a separate terminal.
- **Context loading** reads only stored results; no AI calls. The agent may invoke it proactively when the user asks about past decisions, attempts, or verifications. The agent uses `contexttrail find "<query>"` (or recent events and open items if no query) and `contexttrail show <event>` (description, connections, cited source evidence). Both support `--json`. Output and skill instructions note that source evidence is a quotation from past records and should be treated as reference — not instruction.
- **Attaching events as evidence:** Press `y` in the TUI or click "Copy agent reference" in the browser to copy a reference like `contexttrail:ev_6226b954@v12`. Paste it into a Claude Code or Codex conversation — the agent reads that event and its evidence with `show`. If the event was re-analyzed and changed or removed since the copy, `show` will say so.
- Using context loading in Claude Code sends cited Codex/Claude records to the model in that conversation (Anthropic).

### Export

```bash
project export /path/to/project --format md --output ./project-flow.md
project export /path/to/project --format mmd --output ./project-flow.mmd
project export /path/to/project --format json --output ./project-flow.json
```

All formats are generated from the same stored graph — no AI calls. Markdown includes events, relationships, source evidence, and limitations. JSON includes the graph and cited evidence. MMD is Mermaid code. Use `--force` to overwrite an existing file. Export to DB, raw logs, or Git internal files is blocked.

**Storage locations:** Git repos use `<git-common-dir>/projectflow/<scope-key>/`; plain directories use `<folder>/.projectflow/`. The same repo root/worktree set shares state; different specified sub-paths get separate scopes. Full source text is not permanently copied — only metadata, digests, cited excerpts, and stage results are stored.

Permissions: state directory `0700`, DB and new exports `0600`. This does not imply disk encryption or automatic secret redaction. SQLite locking on network filesystems (NFS, etc.) is not validated.

---

## 6. Explicit Limitations of This Alpha

1. **Analysis quality is measured only on two small fixtures and this repository's own logs.** Both CLIs have run end to end (Codex on Linux and macOS, Claude on macOS), but a reconstruction of a large, unfamiliar project has not been checked by a person. The known failure modes are events split or merged differently between runs, and two-hop `verifies` links (a test run that covers the code it calls) that cheap models do not draw.
2. **Not all historical records and files are read without limit.** The default Git commit scope is the 50 most recent per worktree, plus tracked staged/unstaged diffs. Untracked content, all merge parents, and an arbitrary-revision comparison UI are out of scope. Sub-agents whose parent linkage cannot be verified are excluded.
3. **Long source text is split while preserving semantic lineage.** The parser splits into stable fragments of ≤32,000 characters and preserves `fragment_of/index/count`. Binary/Base64 payloads (e.g. images) are excluded from text model input; only media type, size, and SHA-256 marker are kept. The total input budget per work unit is still limited — very large context combinations may be deferred.
4. **Historical context retrieval is also limited.** Defaults: 24 relevant events, 200 indexed events + 250 source records + 100 pinned files, up to 2 additional evidence reads, and at most 1 format/reference repair. After repair, invalid candidates are discarded; discarded candidates remain in the graph's limitation list by title. If selected context exceeds the budget, the work unit is deferred/failed. Large-project tuning remains to be done.
5. **Evidence presence checks do not guarantee semantic correctness.** Citation scope, actor source, and execution evidence presence are checked in code, but whether the source text actually supports a given interpretation or causal claim requires AI and user evaluation.
6. **Not all MVP acceptance criteria are complete.** Safe mixed-source analysis with both real runners, semantic regression evaluation, and real terminal/browser validation are outstanding. Explicit historical re-analysis commands and user graph editing are also not yet provided.

Budget tuning example (review increased data transmission and model limit effects together):

```bash
project analyze /path/to/project --history-limit 100 --record-chars 64000 --unit-chars 80000
```

The constraint `record_chars ≤ unit_chars < task_chars` must hold. `task_chars` and detailed context values are adjusted in the Python `AnalysisConfig`. Code or config changes alone do not trigger automatic re-analysis of completed records.

---

## 7. Development and Regression Testing

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

Or simply `scripts/test.sh -q`, which picks the right interpreter. On most current Linux and macOS systems there is no bare `python`, so prefer `python3` or an explicit virtualenv.

This reproducible command prevents unnecessary external pytest plugins from auto-activating. Tests create synthetic logs and Git fixtures in temporary directories; HTTP tests use a loopback port; TUI tests use a PTY. No real AI accounts are called.

**CI runs this suite on Linux and macOS across Python 3.11, 3.12, and 3.13** — see [`.github/workflows/test.yml`](.github/workflows/test.yml). Linux is included because it is the platform README claims as primary; a green badge there is a real signal, not a formality.

The mouse assertion in `tests/test_cli_ui.py` checks the portable invariant — with `--no-mouse` no mouse-reporting enable sequence may appear, and any enable that does appear must have a matching disable before exit — rather than one terminfo-specific escape sequence, so it does not depend on the local terminfo database. A separate test in the same file renders across `xterm-256color`, `screen-256color`, `tmux-256color`, `linux`, and `vt100`.

Structure:

```text
src/projectflow/
  sources/           local log adapters
  runners/           Codex·Claude CLI / bubblewrap isolation
  analysis.py        incremental planning, ReadRequest, extract/integrate
  schema.py          JSON contract, evidence validation, GraphDelta application
  git_context.py     scope/worktree, pinned Git evidence
  store.py           SQLite, lock, ledger, atomic publish
  render.py          Mermaid subset, character graph, SVG, export
  ui.py              curses TUI
  webview.py         loopback detail server
  prompts/           SPEC-based analysis instructions
  assets/            local HTML·JS·CSS
  demo.py            Mock Runner for synthetic fixtures only
```

See [Documentation Guide](docs/README.md) for the public document list. Design basis: [PRD](docs/PRD.md) and [SPEC](docs/SPEC.md). Also read [Implementation Status](docs/reports/IMPLEMENTATION_STATUS.md), [Test Report](docs/reports/TEST_REPORT.md), and [Security & Execution Constraints](docs/SECURITY.md).

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) for the invariants every change must keep and how model-visible changes are evaluated, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md) for private vulnerability reporting. Changes by date are in [CHANGELOG.md](CHANGELOG.md).

Distributed under the MIT License ([LICENSE](LICENSE)). External packages installed alongside this tool and their licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
