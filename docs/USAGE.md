# ContextTrail usage guide

The [README](../README.md) shows what ContextTrail is and how to start. This page is the reference: every option the README only names, the terminal keys, the browser view, the agent integrations, export and storage, and the developer tools.

- [Runner setup and diagnosis](#runner-setup-and-diagnosis)
- [Analysing a project](#analysing-a-project)
- [The terminal view](#the-terminal-view)
- [The browser view](#the-browser-view)
- [Finding, showing and exporting](#finding-showing-and-exporting)
- [Agent commands](#agent-commands)
- [Notes: the agent writes the graph as it works](#notes-the-agent-writes-the-graph-as-it-works)
- [Project memory at session start](#project-memory-at-session-start)
- [Automatic analysis from the tools' hooks](#automatic-analysis-from-the-tools-hooks)
- [Driving ContextTrail from a program](#driving-contexttrail-from-a-program)
- [Storage and permissions](#storage-and-permissions)
- [Budgets and evaluation tools](#budgets-and-evaluation-tools)
- [Developer tools: Studio and tracing](#developer-tools-studio-and-tracing)

## Runner setup and diagnosis

**Install and log in to the Codex or Claude Code CLI yourself** before use. ContextTrail does not handle token issuance, copying, or login. On Linux, real AI execution requires `bubblewrap` (`bwrap`) and available user namespaces. On macOS, `sandbox-exec` and file-based CLI credentials are required. If isolation tools or required CLI options are missing, execution is blocked. The macOS path uses a deny-default Seatbelt profile with a temporary HOME; it has passed Codex `doctor --smoke` and a small live-segment analysis. Blocking of malicious tools and config is not yet validated.

```bash
contexttrail doctor --runner codex
contexttrail doctor --runner claude
```

The default `doctor` run checks CLI version, help output, and isolation feasibility — no model calls. **The presence of a credential file does not guarantee successful authentication.** The following commands make explicit real-account calls, sending a small synthetic input (not your project data):

```bash
contexttrail doctor --runner codex --smoke --yes
contexttrail doctor --runner claude --smoke --yes
```

This smoke test is a simple structured JSON round-trip. **It does not substitute for full integration validation of mixed-log analysis or permission blocking.** Per-CLI flags and unvalidated items are in [Implementation Status](reports/IMPLEMENTATION_STATUS.md); isolation constraints are in [Security](SECURITY.md).

Current alpha authentication constraints:

- Only file-based credentials are supported: Codex's `auth.json` and Claude's `.credentials.json` are bind-mounted read-only into the CLI's isolated environment. The program does not read or copy their contents.
- Keyring-only auth, per-org user settings, custom API providers, proxies, and certificate inheritance are not supported.
- If credential renewal requires a write, the run may fail. Log in or refresh via the original CLI, then retry. Permissions are not escalated to work around failures.

## Analysing a project

```bash
# Check relevant record count, linked worktrees, and limitations — no AI calls
contexttrail scan /path/to/project

# Read logs from every source and analyze with one chosen runner
contexttrail analyze /path/to/project --runner codex
# or
contexttrail analyze /path/to/project --runner claude
```

The first analysis prompts for transmission consent. **Conversation turns, tool outputs, and code fragments may be sent to the model service of the chosen CLI.** Values in known credential formats (`sk-…`, `ghp_…`, `AKIA…`, private key blocks, `NAME_KEY=value` and the like) are masked as `[secret hidden]` when transcripts are read, so they are not stored, sent, or exported; this is not a guarantee — formats it does not know pass as written, so verify your project's transmission policy before proceeding with sensitive material.

The chosen runner, settings, and consent are stored in the per-scope local DB. Analysis proceeds oldest-first through **work units**. Each run first shows a summary such as "1,480 units pending · 30 this run (AI calls ≤30) · input ≈2,390k tokens · ≈120 min (estimate)" along with token/time projections for 5, 15, and 30 units, then prompts for the number of units to process (Enter = shown count, `n` = cancel, `--yes` = no prompt). Use `--units N` to specify in advance. Without a unit count a run makes at most 30 AI calls; with one, at most 3 per unit (the story call, its repair, a retry of an unreadable answer); `--max-calls` overrides either. Both values apply only to the current run and are not saved. Estimates are calibrated from actual input tokens and timing if ≥3 units with usage records exist. Use `contexttrail scan` to preview the plan without sending anything (`plan_text`, `plan_choices`).

For non-interactive use, consent can be given with explicit `--yes`. The following command allows real transmission — confirm scope first:

```bash
contexttrail analyze /path/to/project --runner codex --yes --no-tui
```

**One call per unit.** Code finds the work in the tool calls (edits, the checks after them and their results, commits, other runs); the model reads only what the person and the assistant wrote, cut into numbered blocks, plus an outline of that work, and writes the story over it: what was asked and where it was answered, goals, decisions and their stated reasons, findings, changes of course, and which later run tried each edit. It cites block IDs, so code puts the exact source lines in the graph, and code keeps a claimed check or answer only when the records agree. A unit takes one call, two when its answer needs a repair; extraction of several units can run side by side with `--workers`. On ten units of a real research project this read about a tenth of the input of the earlier extract-and-integrate pipeline, linked more requests to their answers and as many changes to their checks ([measurements](plans/PIPELINE_V2.md) §8).

**Output language.** Event titles and summaries are written in one language per project, determined by the dominant language of user messages during the first analysis run. Script-distinct languages (Korean, Japanese, etc.) are recognised from the messages; a message in Latin letters counts for the system locale's language (English when the locale names none). Source text in other languages is still summarized in the chosen language. Code names, filenames, commands, and quotations remain verbatim. Use `--language Korean` to change; `--language auto` re-detects. Already-analyzed events are not re-translated. The screens (terminal, TUI, browser, CLI help) are in English or Korean, chosen from `--language`, `CONTEXTTRAIL_LANGUAGE`, the project's saved output language, or the locale. Everything the model reads is English.

**Analysis model and reasoning level.** ContextTrail always specifies the model by name because the isolation environment does not carry the user's CLI config file (`~/.codex/config.toml`, etc.). Defaults: Codex `gpt-luna`, Claude `haiku` (the light models, since 2026-10-17; `gpt-sol` and `sonnet` are what they are compared against); override with `--model`. On Codex, `gpt-sol` reads the records more reliably than `gpt-luna` (more requests linked to their answers, more changes linked to the checks that tried them) at about twenty times the price per token. Codex accepts only versioned names, so a name without a version (`gpt-sol`, `gpt-luna`) is resolved before the first call to the newest listed model of that family in the catalog shipped with the installed Codex (`codex debug models --bundled`, run inside the sandbox; today `gpt-6-luna` and `gpt-6.1-sol`), and calls record the resolved name. The reasoning level defaults to `medium`; adjust it with `--extract-effort` (`low`·`medium`·`high`·`xhigh`·`max`), and pick the analysis model apart from the runner's with `--extract-model`. Every call logs the requested model and reasoning level in the local call ledger (`contexttrail ops --details`) and evaluation reports. Claude records the responding model; Codex does not report it in its response. The runner never switches silently; completed segments are not re-analyzed just because settings changed.

**One session first.** `--session <session-id>` prioritizes that session and its sub-agents over older records. `--session current` refers to the Codex/Claude Code conversation that invoked this command. Events added this way are marked "out-of-order analysis" — relationships to earlier unanalyzed records may be missing. Use `contexttrail analyze /path/to/project` for subsequent runs.

**Recent sessions first (large projects).** The default order stays oldest first, because relations point from earlier events to later ones. For a first analysis of a big project, `--recent N` (the N most recent sessions) and `--since WHEN` (sessions that ended after an age such as `7d`, `36h`, `2w`, or a date such as `2026-10-01`) analyze only those sessions and their sub-agents, oldest first within them, and mark what they add as out-of-order analysis while older records still wait. A session is as recent as the latest record of it and its sub-agents; sessions with notes are left out and not counted. The consent screen names the sessions it will send first; `contexttrail scan --recent 5` previews that with no model call. The other sessions are filled in by a later ordinary run (the project stays `partial` until then). `--units` and `--max-calls` work as usual; the options cannot be combined with `--session`, `--audit` or the automatic hook. Design: [RECENT_SESSIONS_FIRST](plans/RECENT_SESSIONS_FIRST.md).

**Bringing an older graph up to today's rules.** Some of what the graph holds is worked out by code from the records, and a graph published by an earlier version keeps what that version worked out. `contexttrail refresh /path/to/project` scans (no model call, no consent needed) and reads it again: who wrote each event (a Codex sub-agent first stored under its random nickname, such as `Dirac`, is named by its task, such as `plots`), what step it is (a check, a documentation edit, a delegation), which sub-agent work belongs to which delegation, and which requests were not the person's under the current rules (harness text, the Codex app's plugin list, `/compact`, the `[Image: original …]` note Claude Code writes after the agent reads an image file, a queued message, a sub-agent's brief): those are set aside as withdrawn, keep their relations and are no longer drawn or counted as requests, and the work that hung only from them is hung where a fresh analysis would hang it. An answer to the agent's own question is kept: when it picks one of the options the agent offered it becomes the person's decision. Titles, summaries, check results and the relations a model wrote are left as they are (a choice made a decision is titled by the option picked, as a fresh analysis titles it). It publishes one new graph version, or nothing when nothing changed, and is refused while an analysis runs (`--json` for the counts). On this machine's copies it renamed 43 events and set 13 steps on priceimpact and set aside 24 of 157 requests there, and 60 of 415 on this repository. `status` and `find` do not say whether a graph needs it.

**Where the transcripts are read.** Default log paths: `CODEX_HOME` or `~/.codex` (`sessions/`, `archived_sessions/`), `CLAUDE_CONFIG_DIR` or `~/.claude` (`projects/`), and the opencode data directory `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode` (every `opencode*.db`, opened read-only; only the `session`, `message` and `part` tables are read, never the credential tables or `auth.json`). A session belongs to the project by its recorded working directory only. Override when paths differ per server (`--opencode-home` for opencode):

```bash
contexttrail scan /path/to/project \
  --codex-home /path/to/codex-home \
  --claude-home /path/to/claude-home
```

Only the project currently being worked on is analyzed. If logs are on a local PC and the project is on a Linux server, the server does not automatically collect logs from the PC. Cross-machine/repo auto-merge is not a feature of this version.

`--sources codex,claude,opencode` reads only the named tools (saved by `analyze`, like the homes). A home is taken from the command's flag, else the value `analyze` saved for the project, else `CODEX_HOME` / `CLAUDE_CONFIG_DIR` / `$XDG_DATA_HOME`, else the default; `scan` lists each home it used, where it came from and what it found there. Transcript files that are symbolic links are skipped. A linked directory is followed when it resolves inside the tool's home or inside the transcript directory's own target; one leading elsewhere is not read, and `scan` counts and names it as a limitation (the run then reads partial). Inherited `GIT_*` variables are ignored on purpose: give ContextTrail a real checkout.

**What a unit holds.** The story call is shown what the person and the assistant wrote in the unit, cut into numbered blocks, an outline of the work code found in its tool calls (the runs after an edit with their command and last lines; no other tool output), the person's requests, and a few existing events with limited surrounding context. **Temporal proximity alone does not imply causation.** Long Git diffs are split into citable fragments of ≤32,000 characters, the same as logs. A commit whose Git output exceeds the 4 MB safety limit is skipped with a limitation and the next commit is read — check `contexttrail scan` for limitations.

Work units are first grouped by session and worktree. Within a session, summary compression, gaps ≥3 hours, and date changes with sufficient gaps are used as boundaries. When the input budget is reached, the break goes at the most meaningful boundary that keeps a fair share of the unit: after a successful commit, else before a person's message, else after a run, test, commit or vcs result. A tool call and its result are split only when no such boundary fits, and a limitation then says so. Work unit boundaries do not correspond to event node boundaries in the final graph. Already-analyzed segments are not re-chunked in incremental analysis.

**When a unit fails.** A unit that fails for its own reasons (the model's answer still invalid after the one repair round, or a request over the input budget) is skipped, not the whole run: the run goes on with the next unit and ends `partial` (or `failed` when no unit completed). A skipped unit is not sent again while its records and the analysis settings are unchanged, so a rerun with nothing else to do is a no-op; `contexttrail status` counts such units apart and `analyze --retry-failed` sends them again. If the unit is sent again after later units were integrated, its events are marked out-of-order. A runner error, an unknown error, or an unreadable answer that is still unreadable after one retry stops the run. A run does not start a unit with fewer calls left than it needs; with none completed it ends `call_limit` (exit 2, like partial; the automatic hook does not count it as a failure). The runner's CLI, sandbox and login are checked (no model call) after you agree to the plan and before the first unit.

## The terminal view

`contexttrail view`, `contexttrail analyze`, and `contexttrail graph` share the same screen. It opens on the **story view**: one box per request, goal, decision, change of course, finding the assistant stated, and failure nothing came of, each with its title only (the reason and every step are in the selected-event panel). The work (edits, checks, runs) folds into the box it belongs to, and a sub-agent's work becomes one line in the box that handed it the task, `↳ plots: smoke run on one market passed`, with what that agent last said; the box's own folded steps are one dim line, `↳ 4 steps: Check passed: pytest -q · ✗ 1 · ✓ 1` (how many, the latest result, the failed and passed checks). A box's border colour is its status, and its header carries the status's mark after the kind (`[04] result ✗`), so it reads without colour. Work no box takes (a sub-agent whose delegation is not analysed yet) becomes a plain box of its own, per session; a sub-agent line with no delegation behind it says `(no delegation linked)`. `v` switches to every step and back; following a link, a search hit or `!` to a step the story view folded switches to every step by itself. On one priceimpact request (64 events, at 100 columns) the story view drew 12 boxes in 73 rows where every step took 426.

When user messages exist in the graph, the screen opens on the latest request and shows one request's turn at a time — from that message to the next message in the same session; at ≥180 columns a **request list** is on the left. Use `[`/`]` to navigate requests, and `A` to toggle between full-flow and per-request views. Connections to out-of-span events are annotated inline as `← [02] motivates`. Events that belong to no recorded request's turn are grouped under "events with no recorded request".

**Every step** (`v`) draws events as boxes with labeled arrows. The left column lists events in the order they happened; execution results that verified a change but do not continue elsewhere are attached to the right of that change as `──verifies──▶`. Results that lead to the next story beat (e.g. a failure motivating a fix) remain in the column, so `failure → motivates → fix` reads downward. Adjacent boxes connect with downward arrows; a box at most three boxes away connects via a margin line, which notes the source box number and relationship as `[02] motivates ▶` before the destination, so the origin is visible without scrolling back. A connection farther away, or one with no room for a line, is annotated on both boxes as `→ [06] motivates` / `← [05] motivates`. Every user message becomes a `request` box; the work after it in its session belongs to its turn, which the column's order shows (the selected-event panel lists it as dialog order, not a causal claim). A sub-agent's boxes stand beside the delegation that handed it the task, its name once at the top of its stack. Groups of events sharing no session or relationship (e.g. independent features from separate sessions) are rendered under `══ flow 1/2 ══` headings. Titles and status are never truncated — they wrap. The selected box is drawn with a bold border; only its connected lines, annotations, and boxes are highlighted. When a panel is too narrow for boxes, it falls back to a branch list.

Status markers: `✓` confirmed (verified change, observed result, answered request), `!` needs confirmation (unverified change, completion report, result without a verification target), `✗` failed.

The **selected event** panel shows the description, the result that verified this change (or the change this result confirmed), connected events, and source evidence. Results and connected events are numbered 1–9; press the digit to navigate and Backspace to return. Evidence is shown with record type, source, timestamp, and line position. Single-line tool-call arguments with escaped newlines (`\n`-escaped patches etc.) are reflowed visually **on screen only** — stored evidence is verbatim. At ≥180 columns, all three panels are side-by-side; at ≥150 columns, the flow and detail panels are side-by-side; below that, they stack vertically.

| Key / Option | Action |
| --- | --- |
| ↑↓ / `j` `k` | Previous/next event. In per-request view, wraps to the next request at the end of a span |
| ←→ / `h` `l` | Jump to connected box in adjacent panel (change ↔ right-side result). Horizontal scroll in the narrow branch-list view |
| PgUp·PgDn / Space | Move five events in the flow; scroll the selected-event panel by a page |
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
| `v` | Toggle the story view (requests, decisions, findings; work and sub-agents a line in their box) / every step |
| Esc | Close search or zoom; return from selected-event panel to flow panel |
| `?` | Show full keyboard help |
| Mouse | Click to select a box, request, or numbered connection (clicking a line or `[02] motivates ▶` annotation jumps to the connected box); scroll wheel navigates events/requests and scrolls detail |
| `y` | Copy a reference (`contexttrail:ev_6226b954@v12`) to clipboard. Paste into a Claude Code or Codex conversation — the agent reads that event with its evidence. Uses `pbcopy` locally on macOS, OSC 52 over SSH; if neither works, select the text from the status line |
| `R` | Explicit incremental analysis. Shows pending volume and choices, prompts for unit count, then runs while keeping the previous graph visible (`view`·`analyze`) |
| `B` | Print the browser URL and SSH forwarding instructions for the current version / selected event (`view`·`analyze`) |
| `Q` | Quit. During analysis, confirms cancellation and cleans up child processes |
| `--ascii` | Replace box-drawing characters, arrows, and status markers with ASCII (`v` `!` `x`). Non-ASCII titles are preserved |
| `--no-tui` | Print the annotated flow and status without interactive UI |
| `--no-color` | Disable status colors. Markers (✓ ! ✗) are preserved. Also respects the `NO_COLOR` env var |
| `--no-mouse` | Disable mouse capture and restore terminal text selection |

Keys also work when a Korean IME is active (e.g. `ㅂ` → q, `ㅓ` → j, `ㄲ` → R); other capital keys (`J`, `K`, `G`, `A`, `N`) need Latin input. If the IME is mid-composition when the terminal delays sending, a second keypress may be needed.

The UI does not depend on terminal image protocols or specific emulators. Mouse is used when available, but every action is accessible via keyboard.

**Compatibility targets:** standard SSH, Windows Terminal, IDE terminals, tmux, screen. Tested: `xterm-256color`, `screen-256color`, `tmux-256color`, `linux`, `vt100` TERM values, and resize on a Linux PTY. This does not imply complete screen/keyboard compatibility verification on each product. `TERM`, CJK width, and font issues must be verified on real servers separately.

In `TERM=dumb` or when output is piped, the UI falls back to plain text. The `contexttrail graph` text output follows the flow with per-event description, verification, connections, and evidence — the same content as the selected-event panel. If curses initialization fails on an unknown terminfo, a clear error is shown; use `--no-tui` to view saved results.

The **analysis tokens** counter at the bottom of the screen is the sum of calls where the runner reported both input and output tokens in this project's local LLM call ledger. If some calls lack usage data, `+ (confirmed calls/total calls)` is appended. It does not include tokens used in the original Codex/Claude conversations.

## The browser view

Press `B` in the TUI, or run in a separate terminal:

```bash
contexttrail serve /path/to/project --port 8765
```

The server binds to `127.0.0.1` only. Set up SSH port forwarding from your PC (replace `user@server` with your actual connection details):

```bash
ssh -L 127.0.0.1:8765:127.0.0.1:8765 user@server
```

Open the **full URL including the token** printed in the server terminal in your PC browser. If a port conflict causes a different port to be selected, use the actual port shown. The URL contains a private access token for your records — do not share it externally. The token becomes invalid when the server stops.

The page opens on the **story view**, as the terminal does: one column of boxes for the requests, decisions, goals, changes of course, findings and failures nothing came of, each with its title, a line per sub-agent handed work there (what it last said) and a line for its own folded steps, its status the border's colour and a mark after its kind. The button above the graph switches to every step, drawn as the same boxes and arrows the terminal shows, on a fixed character grid. Buttons above the graph move between your requests (◀ ▶, latest, with each request's date; `[` `]` keys), a search box finds events by words of their title or summary (`/`), and the detail shows an event's own evidence first, its relations as links you can click. Both drawings are SVG made by ContextTrail itself and checked against an element whitelist in the page; this is not a Mermaid engine. No external CDNs or web fonts are used.

Page load, refresh and node selection do not call AI. Analysis must be explicitly requested via the **"Analyze Changes" button and confirmation dialog**, and only with a runner chosen and consented to in a terminal first.

## Finding, showing and exporting

To open the saved event flow directly in the terminal:

```bash
contexttrail graph /path/to/project
contexttrail graph /path/to/eval-output
```

In a terminal, this opens the same interactive view. When a screen cannot be opened (e.g. when piped or run from a coding agent), the same command prints event summaries, relationships, and evidence excerpts to stdout. No AI calls or external transmissions are made. Stdout may contain private source citations — review before sharing.

To find or inspect individual events:

```bash
contexttrail find "install script"        # events with all words in title, description, or source evidence — newest first
contexttrail find                         # recent events and open items
contexttrail show ev_6226b954             # status, connections, and source evidence for one event
contexttrail show contexttrail:ev_6226b954@v12   # copied reference; notifies if event changed since v12
contexttrail status                       # how far the graph is behind the transcripts
```

Output is brief and machine-friendly by default; `--json` is also supported. No AI calls are made — reads only from stored results.

```bash
contexttrail export /path/to/project --format md --output ./project-flow.md
contexttrail export /path/to/project --format mmd --output ./project-flow.mmd
contexttrail export /path/to/project --format json --output ./project-flow.json
```

All formats are generated from the same stored graph — no AI calls. `--graph-version N` exports an older version; `--since-version N` (json) writes only the events, relations and open items added, updated or invalidated since version N, with their evidence. Markdown includes events, relationships, source evidence, and limitations. JSON includes the graph and cited evidence. MMD is Mermaid code. Use `--force` to overwrite an existing file. Export to DB, raw logs, or Git internal files is blocked.

## Agent commands

`./install.sh` automatically registers two commands as personal skills in the current user's Codex and Claude Code installations and as custom commands in opencode. If you installed via `pip` directly, run `contexttrail install-commands` once. Files created by ContextTrail are updated on reinstall; separately authored files are not overwritten. Use `contexttrail install-commands --force` only if you want to replace those too. If the agent does not recognize the new commands, start a new session.

| Task | Codex | Claude Code / opencode |
| --- | --- | --- |
| Update graph | `$contexttrail-update [N units \| current session]` | `/contexttrail-update [N units \| current session]` |
| Load context from saved graph | `$contexttrail-context [reference \| search term]` | `/contexttrail-context [reference \| search term]` |

The legacy slash-style variants `/prompts:contexttrail-update` and `/prompts:contexttrail-context` for Codex CLI/IDE are also installed for compatibility; Codex recommends the skill form. All of them invoke the `contexttrail` CLI from the shell — no MCP server required. The same graph answers in every tool: a decision made in an opencode session is there when you ask from Claude Code, and the first line of `find` says whether the graph is behind the transcripts (`contexttrail status` has the detail).

- **Update** runs only when the user calls it by name. Claude Code uses `disable-model-invocation`; Codex uses `allow_implicit_invocation: false` in `agents/openai.yaml` to prevent the agent from calling it on its own; opencode's custom commands are never chosen by the model, but opencode also reads the skills in `~/.claude/skills` and `~/.agents/skills` and has no explicit-only flag for a skill, so `install-commands` prints the `permission.skill` line to add to `~/.config/opencode/opencode.json` if you want the update skill to need your approval there (the file is not edited for you). It first shows pending units and per-choice token/time projections, then prompts for unit count. Providing a count (e.g. `/contexttrail-update 5`) skips the prompt; `current session` prioritizes the active conversation's session. The analysis runs with the runner saved for the project; if none is saved, the agent asks whether to use **Codex** or **Claude** (`analyze --runner codex|claude --no-tui --brief --units N`, printing a few-line summary instead of the full flow). opencode is a log source, never a runner. Since it can take a long time, the agent runs it in the background where possible and reports progress as `unit done k/N` lines. Interrupted runs resume from the last completed unit. Running inside Codex may be blocked by Codex's own isolation — the agent will guide you to run the command in a separate terminal.
- **Context loading** reads only stored results; no AI calls. The agent may invoke it proactively when the user asks about past decisions, attempts, or verifications. The agent uses `contexttrail find "<query>"` (or recent events and open items if no query) and `contexttrail show <event>` (description, connections, cited source evidence). Both support `--json`. Output and skill instructions note that source evidence is a quotation from past records and should be treated as reference — not instruction.
- **Attaching events as evidence:** Press `y` in the TUI or click "Copy agent reference" in the browser to copy a reference like `contexttrail:ev_6226b954@v12`. Paste it into a Claude Code or Codex conversation — the agent reads that event and its evidence with `show`. If the event was re-analyzed and changed or removed since the copy, `show` will say so.
- Using context loading in Claude Code sends cited Codex/Claude/opencode records to the model in that conversation (Anthropic); in opencode, to whichever model that session uses.

**Claude Code plugin.** The repository is also a Claude Code plugin marketplace with the three skills and the hooks (auto-update, note reminder, project memory): `/plugin marketplace add kisbi3/ContextTrail`, then `/plugin install contexttrail@contexttrail`. The CLI is still a separate `pipx install contexttrail`; the hooks do nothing while it is missing. Use either the plugin or `install-commands`/`install-hooks`, not both.

## Notes: the agent writes the graph as it works

Opt-in, per project. Analysis has a second model read a session that the working agent already understood. With notes on, the agent that did the work writes the events itself, with no model call:

```bash
contexttrail note --enable                       # in the project folder, once
contexttrail install-hooks --claude --codex --opencode   # the end-of-turn reminder (and auto-update)
```

The `contexttrail-note` skill (installed with the other two; Claude Code and Codex pick it themselves) tells the agent to record decisions, file changes and test, build and commit results:

```bash
contexttrail note --kind outcome --status observed_failure --title "Two-writer test failed" \
  --quote "test_two_writers - JSONDecodeError" --verifies contexttrail:ev_6e4b75dd@v23
```

Each `--quote` is text the agent copies from its own session; ContextTrail finds it in that session's transcript (the agent never writes line numbers) and stores the event only if every quote is there and the usual rules hold: `observed_success`/`observed_failure` need a quoted tool result, `verifies` links a change to an observed result, and so on. A refused note stores nothing and says why, with the closest lines when a quote was not found. The person's own messages become request events from the transcript, as in analysis. A session with notes leaves analysis (`analyze` and automatic analysis skip its records), so nothing is written twice; past sessions are still for `analyze`.

To check such a session for what the agent left out, run `contexttrail analyze --audit` (explicit only, never by the automatic hook; `--session ID|current` limits it to one session, `--units N` to a number of work units, `scan --audit` previews the plan with no model call): it re-reads the records no analysis has covered, shows the model the existing notes, and adds only events, relations and open items they do not record. It never changes an existing event, and the events it adds carry `origin: "audit"`.

The end-of-turn hook (`contexttrail note --hook`, synchronous) sends the agent back once when a turn edited files or ran checks after its last note, and its message names those calls (at most five, newest, each as a kind — edit, test, commit, vcs or run — and a short target: the file edited or the first 80 characters of the command; never their output); in projects without notes it does nothing. In opencode the plugin also passes the session's ID to the tool shell (`OPENCODE_SESSION_ID`) and sends the same reminder when the session goes idle. `contexttrail note --list` shows the session's notes, `--disable` turns notes off. Inside Codex's default sandbox, which keeps `.git` (where the state lives) read-only, a note is checked for its quotes and queued under a `q_…` ID; the end-of-turn hook stores it with every check.

Checked on 2026-10-07: in this repository a Claude Code session wrote five notes (two changes, two test results verifying them, one decision), and the hook asked for a note after a test run and stayed quiet once it was answered; in a scratch project, opencode (through `opencode serve`; `opencode run` exits before the idle reminder can be sent) and Codex (`codex exec`) each received the reminder, loaded the skill and wrote notes, Codex through the queue.

## Project memory at session start

Opt-in, per project. The `contexttrail-context` skill only helps when the agent decides to use it. With project memory on, the tool's start-of-session hook puts a short summary of the saved graph into the session itself, with no model call:

```bash
contexttrail context --enable                            # in the project folder, once
contexttrail install-hooks --claude --codex --opencode   # SessionStart hooks next to the Stop hooks
contexttrail context                                     # preview the text a session would get
```

The summary is at most 2,000 characters: the most recent decisions and changes, the open items, the latest failed attempts (not those a later fix revised), how many records the last scan left unanalyzed (counted from the store, plus the count an earlier `find`/`status` kept; nothing is re-parsed), and how to look further (`contexttrail find`, `contexttrail show <reference>`). Every item carries a `contexttrail:ev_…@vN` reference. It is written in English for the agent whatever the screen language is. What the graph holds was written by a model from past conversations, so it is treated as data: titles are cut to one line and quoted inside a fenced block that is announced as past records, not instructions; summaries and quotes are not included ([SECURITY](SECURITY.md)). In a project where it is off, or that was never opened with ContextTrail, the hook prints nothing, creates nothing and exits 0; it reads the saved state read-only and takes a fraction of a second (0.14 s to build the text from a synthetic project of 60,000 records).

How each tool receives it: Claude Code and Codex run a `SessionStart` command whose JSON answer is `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": …}}` (format taken from Claude Code's hooks documentation and, for Codex, from the schema inside the Codex 0.162.1 binary); the Claude Code plugin includes the hook too. opencode's plugin posts the same text on `session.created` as a message that asks for no reply (`noReply`; checked against the `@opencode-ai/plugin` 1.19.0 types, top-level sessions only). Not yet run in the real tools: see [SESSION_START_CONTEXT](plans/SESSION_START_CONTEXT.md) §7 for what to check locally.

## Automatic analysis from the tools' hooks

Opt-in, per project. Analysis still runs only on request. If you want a project's graph to follow your sessions without typing `/contexttrail-update`, enable it for that project, in your own terminal, and install the hooks once:

```bash
cd /path/to/project
contexttrail auto-update --enable --runner claude --units 2 --cooldown 15m --max-runs-per-day 8
contexttrail install-hooks --claude --codex --opencode     # merges into settings.json under CLAUDE_CONFIG_DIR or ~/.claude (Stop, async),
                                                          # hooks.json under CODEX_HOME or ~/.codex (Stop), plugins/contexttrail.ts under $XDG_CONFIG_HOME/opencode or ~/.config/opencode (session.idle)
contexttrail auto-update --status
```

Enabling is the explicit request: it records your consent for that runner and the project. Every hook runs the same command, `contexttrail auto-update`, which exits 0 at once in every case and never blocks the tool. It starts a detached `analyze … --units N` (output in the state directory's `auto-update.log`) only when the project has it enabled, no analysis is running, records are pending, the cooldown has passed and the daily cap is not reached; in other projects the hook does nothing and creates nothing. Runs started this way carry `trigger: hook` in the local run ledger. `contexttrail auto-update --disable` turns it off; `--force` on `install-hooks` refreshes a hook ContextTrail installed earlier, and hooks you wrote yourself are kept.

The hook's log in the state directory has one line per quiet decision (`hook: cooldown`, `lock_held`, `nothing_pending`) so "why did nothing happen?" has an answer. The detached child records how each run ended; after three failed runs in a row (a runner that cannot start, for example) the hooks stop starting new ones, `contexttrail auto-update --status` says so with the log path, and `--enable` again restarts them.

Checked live on 2026-10-06 in this repository: the opencode plugin fired on `session.idle` and started a one-unit analysis (graph v8); Claude Code ran the `Stop` hook in an interactive session (in `claude -p` the async hook is registered and then cancelled when the process exits, as its docs say); Codex ran its `Stop` hook after the "Hooks need review" prompt in its TUI was answered with trust (a new `hooks.json` is not run until then).

## Driving ContextTrail from a program

Every JSON output carries a `schema` name (`contexttrail.scan.v1`, …), `contexttrail capabilities` lists the version, the outputs, the readable sources and every command's options, `analyze --json` prints the run's result as one object, and the exit codes are distinct: 0 done, 2 partial, 64 usage error, 75 busy, 77 refused, 130 interrupted, 1 other errors. `export --format json --since-version N` gives what changed since graph version N.

A program that keeps its own copy of a project can point ContextTrail at the sessions recorded where the folder used to be (`--previous-path PATH`, repeatable; `analyze` saves the list) and keep the state outside the project (`--state-dir DIR`, absolute, never saved). The whole contract is in [HOST.md](HOST.md).

## Storage and permissions

Git repos use `<git-common-dir>/contexttrail/<scope-key>/`; plain directories use `<folder>/.contexttrail/` (an existing `.git/projectflow/` from the tool's earlier name is still used). The same repo root/worktree set shares state; different specified sub-paths get separate scopes. Full source text is not permanently copied — only metadata, digests, cited excerpts, and stage results are stored.

Permissions: state directory `0700`, DB and new exports `0600`. This does not imply disk encryption. Known credential formats are masked when read (see above), which is not a guarantee that nothing secret is stored. SQLite locking on network filesystems (NFS, etc.) is not validated.

## Budgets and evaluation tools

Budget tuning example (review increased data transmission and model limit effects together):

```bash
contexttrail analyze /path/to/project --history-limit 100 --unit-chars 70000
```

These are saved for the project like the other options. The constraint `record_chars ≤ unit_chars < task_chars` must hold. Records are already cut to ≤32,000 characters, and a unit is also bounded by an estimated payload of 60% of `task_chars`, so a `--unit-chars` above about 75,000 has no effect unless `task_chars` is raised; `task_chars` and detailed context values are adjusted in the Python `AnalysisConfig`. Code or config changes alone do not trigger automatic re-analysis of completed records.

To preview a small fixed evaluation fixture before model calls, run `contexttrail eval --fixture /tmp/case.json --preview --output /tmp/case-preview`. The output `input-preview.html` shows WorkUnit boundaries and source text, the story call's system instruction, the full request JSON, the response JSON Schema, and the generation path of each part. **The first unit's input is the exact initial request; subsequent units' existing-graph sections are estimates that depend on earlier model responses.** Preview includes private source text; no AI calls are made.

After `contexttrail eval`, `review.html` is generated automatically. It shows event/relationship candidates, citation strings vs. actual source lines, and validation errors for each model call side by side. Eval runs also save failed structured responses to `call-review/` with private user permissions. You can regenerate HTML for earlier evals with `contexttrail review /path/to/eval-output`, but source model response text not saved at the time cannot be recovered. This local review view makes no AI calls or external transmissions. `python scripts/compare_models.py` runs the same units with several models, one state copy each (real model calls, needs consent).

## Developer tools: Studio and tracing

LangSmith tracing (`--langsmith`) is a developer tool for inspecting analysis structure and is not a user feature. It does not appear in command help; see [LLM Ops](guides/LLM_OPS.md).

`contexttrail analyze` and `contexttrail eval` run the **same LangGraph graph** as Studio's `contexttrail_analysis` (`langgraph.json` → `studio_graph.py:graph`): `prepare_run → scan_sources → classify_steps → plan_work_units → select_unit → prepare_extract_input → extract_model_and_validate → validate_candidates → publish_draft → link_request_turns → summarize_graph_changes → publish_result → advance_unit → finish_run`. `prepare_extract_input` shows the exact story request of a unit. The LangGraph runtime is part of the base install; the optional `studio` extra adds a development server:

```bash
python -m pip install -e '.[dev,studio]'
# Use only the LangSmith key from your private .env, with a separate tracing project
dotenv -f /path/to/private/.env run -- env -u OPENAI_API_KEY \
  LANGSMITH_TRACING=true LANGSMITH_PROJECT=contexttrail-dev \
  langgraph dev --no-browser --host 127.0.0.1 --port 2025
```

In the Studio UI printed by the server, select `contexttrail_analysis` and run with empty input `{}` to practice with synthetic logs and the Mock Runner. A live run uses the Codex runner and needs the project or fixture fixed in the server's environment (`CONTEXTTRAIL_STUDIO_SCOPE`, `CONTEXTTRAIL_STUDIO_EVAL_FIXTURE`) and `"confirm_live": true` in the input, for example `{"mode":"eval","confirm_live":true,"max_units":1,"max_calls":10}` (a reviewed small fixture into a separate temporary state) or `{"mode":"live",…}` (the project's own state). The Studio dev server has no user authentication — bind only to `127.0.0.1` and shut it down after use. See [Studio Architecture](guides/STUDIO_ARCHITECTURE.md).
