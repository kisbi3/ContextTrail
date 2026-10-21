# ContextTrail

[![test](https://github.com/kisbi3/ContextTrail/actions/workflows/test.yml/badge.svg)](https://github.com/kisbi3/ContextTrail/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/contexttrail)](https://pypi.org/project/contexttrail/)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)](https://github.com/kisbi3/ContextTrail/blob/main/.github/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/kisbi3/ContextTrail/blob/main/LICENSE)

**Turns the Codex, Claude Code and opencode sessions already on your machine into an evidence-linked history of a project: what was asked, what was tried, what failed, what was decided, and what was actually verified.**

ContextTrail reads the transcripts the three tools keep locally (Codex and Claude Code JSONL files, the opencode SQLite database), plus the project's Git history, and asks the Codex or Claude CLI you already have installed (sandboxed, read-only) to reconstruct the flow. Every event and every arrow carries a quote from the source, and code checks each quote against the record before anything is stored. You get a terminal view, a local browser view, `find`/`show`/`status` commands, and agent skills so Codex, Claude Code and opencode can answer "why did we drop X?" from the saved graph instead of from memory.

Development alpha `0.1.0a10`. Linux and macOS, Python 3.11+. [What has been measured](#what-has-been-measured) is further down; nothing on this page is an estimate presented as a result.

![The terminal story view of the synthetic demo: two requests, a proposal, a failed check and an unverified report as coloured boxes, with the selected report's status, links and quoted evidence below](https://raw.githubusercontent.com/kisbi3/ContextTrail/main/docs/images/tui-story-en.png)

## Try it in a minute, without an AI account

```bash
pipx install contexttrail                               # or: pip install contexttrail
contexttrail demo --path /tmp/contexttrail-demo        # synthetic logs + a deterministic mock runner
```

Give it a new or empty directory. The demo writes a small Codex log and a small Claude Code log for one folder, runs the whole pipeline with a mock in place of the model, and opens the result; `contexttrail view /tmp/contexttrail-demo/sample-project` reopens it later. It shows what the tool builds and how it reads; it says nothing about real model accuracy. The mock can be chosen only for the demo and evaluation fixtures, never to analyse a real project.

## What goes in, what comes out

The demo's input is twelve records in two logs, the kind of thing the CLIs write all day:

| Source | Role | Record |
| --- | --- | --- |
| Codex session | user | Let's store it as a JSON file for now. |
| Codex session | assistant | SQLite is also worth considering. |
| Codex session | tool call → result | patch `store.py` → Patch applied: JSON storage code. |
| Codex session | tool call → result | `pytest tests/test_store.py -q` → concurrent write test: FAILED — JSONDecodeError (1 failed, 3 passed) |
| Claude Code session | user | Concurrent writes break it, so let's switch to SQLite. |
| Claude Code session | tool call → result | Edit `store.py` → The file has been updated. |
| Claude Code session | tool call → result | `pytest tests/test_store.py -q` → SQLite concurrent write test: 4 passed |
| Claude Code session | assistant | Switched to SQLite and the tests pass too. |

Out of that, code finds the work in the tool calls (two edits, two test runs, and that each run tested the file just edited), one model call per stretch of work writes the story over it (the alternative proposed, the report, which reply answered which request), and code checks every claim against the records before it publishes eight events and their relations.

The screen opens on the **story view** above: one box per request, proposal, decision, finding or failure, with the work folded into a line of its box (`↳ 1 step: Edit store.py`). `v` (or the button on the browser page, shown here) shows **every step**, the same graph with each edit and check as its own box:

![The browser's every-step view of the demo: each change with an arrow labelled verifies to the test run that checked it, the first failed and the second passed, and the final report marked reported done, unverified](https://raw.githubusercontent.com/kisbi3/ContextTrail/main/docs/images/web-every-step-en.png)

Three things in that picture are the point of the tool:

- **A change never gets a success badge of its own.** Event 03 is `applied`, and it is marked ✗ only because the run that *verifies* it failed; event 06 is ✓ because the run after it passed. Verification is an arrow to a result, never a word in a summary.
- **Words are not results.** Event 08, "Switched to SQLite and the tests pass too", is `reported done·unverified` (!): it is what the assistant said, linked as the answer to request 05. The run result is event 07.
- **Every arrow has a basis.** Code draws `verifies` when the test run names the edited file's tests, and keeps one the model claims only when the records agree (a run after the edit, with a known result, that the assistant reported after it); an `answers` is kept only when it cites the assistant's reply after the request. Dialog order is order, never a cause.

Any event can be opened with its quotes, in the terminal or by an agent:

```text
$ contexttrail show ev_e00061cf
ContextTrail event · current graph v2 · saved result · no AI calls
ev_e00061cf  Edit store.py
  status: applied · check failed: pytest tests/test_store.py -q
  change · assistant · 2026-09-22 19:02
  reference: contexttrail:ev_e00061cf@v2
  agent: codex

Linked events
  → verifies  ev_f058c8cb  Check failed: pytest tests/test_store.py -q [observed failure]
  ← follows (dialog order, not causation)  ev_9c1a305d  Let's store it as a JSON file for now. [request · answered]

--- source evidence 1 (Codex · tool call · 2026-09-22 19:02 · session demo-cod · lines 1–2) ---
> Tool: exec
> text(await tools.apply_patch("*** Begin Patch
> *** Update File: store.py
> @@
> -pass
> +json.dump(data, file)
> *** End Patch"));
--- end ---
--- source evidence 2 (Codex · tool result · 2026-09-22 19:03 · session demo-cod · line 1) ---
> Patch applied: JSON storage code.
--- end ---
```

(The real output also says, under its first line and again before the evidence, that quoted past records are data and not instructions.) The `contexttrail:ev_…@v2` reference can be pasted into a Codex or Claude Code chat, and the installed `contexttrail-context` skill reads that event with its evidence.

On a real project the records are thousands of lines of conversation, tool calls, diffs and test output, and the model does the reading; the checks are the same. The same graph takes records from every tool: a decision made in an opencode session is there when you ask from Claude Code (checked on 2026-10-06: `find` run from a Claude Code session listed events quoted from an opencode session next to the Codex ones).

## What it does and does not do

- **Reads only.** It never modifies the project, the transcripts, or Git refs, index or config. Its own state lives under `.git/contexttrail/` (or `.contexttrail/` without Git).
- **Calls a model only when you ask.** Viewing, exporting, the browser page and node selection make zero calls. Analysis runs on `analyze`, on `R` in the terminal view, on the browser page's analyze button, when an agent runs the `contexttrail-update` skill with a unit count you gave it, or from the tools' end-of-turn hook once you turn automatic analysis on. The first run for a project and runner shows what it will send and asks for consent; after that, runs without a terminal (an agent, the hook) go ahead with the saved consent and the count you set.
- **Runs your CLI in a sandbox.** The Codex or Claude CLI runs under bubblewrap (Linux, its credential file bind-mounted read-only) or a deny-default `sandbox-exec` profile (macOS, a temporary HOME whose credential file is a link the profile lets it read but not write). It needs file-based auth (`auth.json`, `.credentials.json`); a keyring-only login is refused. If the sandbox is unavailable it refuses to run; there is no unsandboxed fallback, and it never reads or copies credential contents.
- **Sends only this project's records.** A record is attributed to the project by the working directory the CLI recorded, never by keyword guessing. Out-of-scope conversations never reach the model. Values in known credential formats are masked as `[secret hidden]` when transcripts are read (not a guarantee: [SECURITY](https://github.com/kisbi3/ContextTrail/blob/main/docs/SECURITY.md)).
- **Checks before it stores.** Every quote must exist in the source at the cited lines. Slips with one possible reading are corrected in code and audited; everything else goes back to the model for one repair round, and what still fails is dropped and named in the graph's limitations.
- **Costs tokens from your own account.** One model call per work unit (two when its answer needs a repair, at most three), on a light model by default (Codex `gpt-luna`, Claude `haiku`). The plan shown before a run estimates calls, tokens and minutes for 5, 15 and 30 units, and you choose how many.
- **Is incremental.** Finished work units are never re-sent. Re-running with no new records makes zero calls.

## Install

Requires Python 3.11+, Git, and a UTF-8 terminal with Python's `curses` module.

```bash
pipx install contexttrail          # or: pip install contexttrail
contexttrail install-commands      # adds the Codex, Claude Code and opencode commands; existing user files are kept
ct --version                       # ct is the short alias of contexttrail
```

Or from a clone, which does both steps into `~/.local/share/contexttrail/venv` and links `contexttrail` and `ct` into `~/.local/bin`:

```bash
git clone https://github.com/kisbi3/ContextTrail.git && cd ContextTrail
./install.sh
```

The repository is also a Claude Code plugin marketplace with the skills and hooks (the CLI above is still needed): `/plugin marketplace add kisbi3/ContextTrail`, then `/plugin install contexttrail@contexttrail`. From the plugin, the commands are `/contexttrail:context` and `/contexttrail:update`. Use either the plugin or `install-commands`, not both.

## Use it on a project

```bash
cd /path/to/project
contexttrail                                    # open the saved flow (same as: contexttrail view .); never calls AI
contexttrail doctor --runner claude             # CLI, sandbox and flags; no model call
contexttrail scan                               # what would be read and sent: records, worktrees, homes, plan; no model call
contexttrail analyze --runner claude --units 5  # or --runner codex; shows the plan and asks before sending
```

`contexttrail /path/to/project` opens another folder, and `python -m contexttrail` works as well. **Log in to the Codex or Claude Code CLI yourself first**; ContextTrail only runs it. Analysis goes oldest first, a few work units at a time, and the graph is usable after any run: the rest waits for the next one. For a big project, `--recent N` or `--since 7d` analyzes the latest sessions first; `--session current` the conversation you are in. `contexttrail refresh` brings a graph made by an older version up to today's rules with no model call.

| | |
| --- | --- |
| **Terminal** | Story view by default, `v` every step, `[` `]` between your requests, `!` the next thing that needs a look, `/` search, `y` copies an event reference for an agent chat, `R` analyzes, `?` lists every key |
| **Browser** | `contexttrail serve` (or `B` in the terminal) starts a loopback-only page with an access token; the same story and every-step views, request navigation, search and each event's evidence |
| **Agents** | `/contexttrail-context` (`$contexttrail-context` in Codex) answers from the saved graph; `/contexttrail-update 5` analyzes five units, only when you call it |
| **Notes** (opt-in) | The working agent records its own decisions, changes and test results as it goes, checked against its transcript, with no model call: `contexttrail note --enable` |
| **Memory** (opt-in) | A short summary of the graph is put into each new session: `contexttrail context --enable` |
| **Automatic** (opt-in) | The tools' end-of-turn hooks start a small analysis with your saved runner and limits: `contexttrail auto-update --enable --runner claude --units 2` |
| **Programs** | JSON with a schema name on every command's output (`export` names its `format`), `capabilities`, distinct exit codes, `export --since-version N`: [HOST.md](https://github.com/kisbi3/ContextTrail/blob/main/docs/HOST.md) |

The hooks behind Notes, Memory and Automatic are registered once with `contexttrail install-hooks --claude` (and/or `--codex`, `--opencode`), or come with the Claude Code plugin.

![The browser's story view of the demo: request, proposal, failed check, second request and unverified report as boxes with coloured borders](https://raw.githubusercontent.com/kisbi3/ContextTrail/main/docs/images/web-story-en.png)

Every option, key and hook is in the **[usage guide](https://github.com/kisbi3/ContextTrail/blob/main/docs/USAGE.md)**: runner setup and authentication limits, consent and what is sent, models and reasoning levels, where transcripts are read, failed units, the terminal keys, the browser over SSH, export formats, the agent commands, notes and audits, session memory, automatic analysis, storage and permissions.

**Languages:** the screens are in English or Korean (`--language`, else `CONTEXTTRAIL_LANGUAGE`, else the project's saved output language, else the locale). Event titles and summaries are written in the language of your own messages (or `--language`). The instructions the model reads are English; the records are sent as written.

## What has been measured

Both real CLIs have been measured end to end on fixtures and on this repository's own logs. Analysis quality is still a draft to check, not a record. Below is every number actually measured. The rows dated before 2026-10-20 were measured with the earlier pipeline (separate extract and integrate calls per unit); the one-call pipeline that replaced it was compared with it on the same ten units of a real research project, both with Claude Haiku: about 50 seconds and 12,000–15,000 input tokens per unit instead of 118 seconds and 146,000 (a fifth of the API-equivalent cost), 12 and 11 of 14 requests linked to their answers instead of 9 and 8, and 31 and 29 of 43 changes linked to the check that tried them instead of 30 and 25 ([PIPELINE_V2 §8.4–8.5](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PIPELINE_V2.md)).

| | Measured | Not yet done |
| --- | --- | --- |
| Real model calls | First real runs (2026-09-23 to 10-02): **Codex CLI 12 analysis calls** (8 on this repository's own logs, 4 on the Linux external project, both `gpt-6-sol`) plus 2 `doctor --smoke` round-trips; **20 calls** across the 6 scored fixture runs ([Linux](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/artifacts/linux-live-eval-2026-09-28.json) · [table](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/NEXT_STEPS.md)). **Claude Code CLI: 42 eval runs** on macOS with `claude-sonnet-5-5` (2 installer smoke runs, 40 scored runs on the `repairfix-v2` fixture), 3–4 calls each ([record](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PERFORMANCE_PLAN.md) §9–14). Later runs on fixtures and real projects with Codex `gpt-6-luna`/`gpt-6-sol`/`gpt-6.1-sol` and Claude Haiku and Sonnet are recorded in PERFORMANCE_PLAN §17–24 and [PIPELINE_V2 §8](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PIPELINE_V2.md) | Claude Runner on Linux: 0 calls (that host has no Claude CLI) |
| Real analysis scale | Linux: 5,282 in-scope records → 8 events / 9 edges, 4 runner calls, read-only check passed. macOS self-analysis: 4,199 records → 17 events / 15 edges ([Linux](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/artifacts/linux-live-eval-2026-09-28.json) · [macOS](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/artifacts/self-analysis-a4.json)) | — |
| Cost per work unit | One-call pipeline on ten units of a research project: Claude Haiku about 50 s, 12,000–15,000 input tokens and $0.006 API-equivalent per unit; Codex `gpt-luna` (medium) $0.002 ([§8.4–8.5](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PIPELINE_V2.md)). Earlier pipeline, Claude Sonnet on `repairfix-v2`: 1.5–2.4 minutes per run of 2 units ([§12–14](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PERFORMANCE_PLAN.md)) | The one-call pipeline on a large project end to end: not measured |
| Archive parse audit | **156 files** (Claude 40, Codex 116), **31,748 records** selected, `limitations: 0` as of the a4 parser ([audit](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/artifacts/archive-audit-a4.json) · [report](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/PRELIVE_AUDIT.md)) | **Stale for this parser.** The record-type split in `sources/local.py` now reports types that audit did not. Re-run needed |
| Semantic quality | Codex: **79/111 expectations met (71%)** across 6 runs of 2 fixtures. Claude Sonnet on `repairfix-v2`: **11.8/18 mean** over 5 runs (11, 11, 13, 13, 11) ([§14](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PERFORMANCE_PLAN.md)). One-call pipeline: reading-comprehension questions fixed before the run and graded blind, 5.5/10 from the drawing alone and 7.0/10 with event details ([§8.2](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PIPELINE_V2.md)) | **2** human-checked WorkUnits total. Not a rate |
| Reproducibility | Five same-configuration Claude Sonnet runs of `repairfix-v2` scored 11, 11, 13, 13, 11; the spread comes from events being split or merged differently between runs, not from relations ([§11, §14](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PERFORMANCE_PLAN.md)). Codex on the earlier pipeline, three same-configuration runs of `repairfix-v2`: 13, 15, 12 with `gpt-6-sol` and 16, 14, 13 with `gpt-6.1-sol` (PERFORMANCE_PLAN §17). In the one-call comparison, the same drawing graded twice differed by up to 2 points, and Haiku's linked checks varied from 21 to 34 of 43 in three runs of the same code | No repeat on a large project |
| Platform validation | **Linux/Ubuntu 24.04 + bubblewrap: 1 end-to-end run on an external project** ([report](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/LINUX_LIVE_EVAL_2026-09-28.md)) · macOS: `sandbox-exec` + Codex smoke + self-analysis + the 42 Claude eval runs above | Claude Runner under bubblewrap: 0 calls (no Claude CLI on the Linux host) |
| Tool-denial tests | Canary escape probe at startup on macOS (a file outside the sandbox must be neither readable nor writable, also through a temp-home link); on Linux, startup checks that bubblewrap runs the CLI, with no escape probe ([SECURITY](https://github.com/kisbi3/ContextTrail/blob/main/docs/SECURITY.md)) | `~`, `.ssh`, project tree, and real credential write-blocking unverified on both |
| Tests | **790** in the suite, all passing (2026-10-22); CI runs them on Linux and macOS × Python 3.11–3.13 | 0 real model calls in CI, by design |

Evidence behind these numbers is published, not summarized: [design & evaluation history](https://github.com/kisbi3/ContextTrail/blob/main/docs/DECISIONS.md) · [one-call pipeline](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PIPELINE_V2.md) · [performance plan and Claude measurements](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/PERFORMANCE_PLAN.md) · [pre-live audit](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/PRELIVE_AUDIT.md) · [real-CLI evaluation](https://github.com/kisbi3/ContextTrail/blob/main/docs/reports/TWO_CALL_LIVE_EVAL_2026-09-25.md) · [what is left](https://github.com/kisbi3/ContextTrail/blob/main/docs/plans/NEXT_STEPS.md). Real project names, session UUIDs and machine-specific paths in those files are replaced with placeholders; counts, token totals, durations and archive digests are kept.

> The quality row is the honest one: extraction is good enough to be useful on a project you remember well, and there is no measurement showing it is stable across repeated runs. Treat a reconstructed flow as a draft to check, not a record.

## Limitations of this alpha

1. **Analysis quality is measured only on small fixtures, ten units of one research project and this repository's own logs.** A reconstruction of a large, unfamiliar project has not been checked by a person. The known failure modes are events split or merged differently between runs, and checks a light model does not link to the change they tried.
2. **Not every record is read without limit.** The default Git commit scope is the 50 most recent per worktree, plus tracked staged/unstaged diffs. Untracked content, all merge parents, and an arbitrary-revision comparison UI are out of scope. Sub-agents whose parent linkage cannot be verified are excluded.
3. **Long source text is split, and a unit's input is bounded.** Records are split into fragments of ≤32,000 characters; binary payloads (images) are replaced by a media type, size and SHA-256 marker. A unit whose request is over the input budget fails and is skipped; `status` counts it among the units skipped after a failure.
4. **Evidence checks do not guarantee meaning.** Citation scope, who wrote a record and whether a run happened are checked in code; whether the text supports an interpretation or a cause is the model's reading, and yours.
5. **Only the machine you run it on.** Logs on a laptop are not merged with a project on a server. There is no graph editing and no command to analyse finished work units again (`refresh` re-derives only what code works out).

## Development

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
scripts/test.sh -q        # sets PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 and picks the interpreter
```

Tests create synthetic logs and Git fixtures in temporary directories; HTTP tests use a loopback port; TUI tests use a PTY. No real AI accounts are called. CI runs the suite on Linux and macOS across Python 3.11, 3.12 and 3.13 ([workflow](https://github.com/kisbi3/ContextTrail/blob/main/.github/workflows/test.yml)).

```text
src/contexttrail/
  sources/           Codex, Claude Code and opencode readers
  runners/           Codex·Claude CLI in bubblewrap / sandbox-exec
  analysis.py        incremental planning, the story call, publishing
  light.py           the work code finds in the tool calls, the story request and answer
  blocks.py          what was written, cut into citable blocks
  schema.py          JSON contract, evidence validation, GraphDelta application
  git_context.py     scope/worktree, pinned Git evidence
  store.py           SQLite, lock, ledger, atomic publish
  diagram.py         the story view and the flow as boxes, for the terminal and as SVG
  render.py          labels, character graph, export
  ui.py              curses TUI
  webview.py         loopback detail server
  prompts/           the story call's instructions
  assets/            local HTML·JS·CSS
  demo.py            Mock Runner for synthetic fixtures only
```

Design: [PRD](https://github.com/kisbi3/ContextTrail/blob/main/docs/PRD.md) and [SPEC](https://github.com/kisbi3/ContextTrail/blob/main/docs/SPEC.md); the document list is in [docs/README.md](https://github.com/kisbi3/ContextTrail/blob/main/docs/README.md) (Korean). Contributions are welcome: see [CONTRIBUTING.md](https://github.com/kisbi3/ContextTrail/blob/main/CONTRIBUTING.md) for the invariants every change must keep and how model-visible changes are evaluated, [CODE_OF_CONDUCT.md](https://github.com/kisbi3/ContextTrail/blob/main/CODE_OF_CONDUCT.md), and [SECURITY.md](https://github.com/kisbi3/ContextTrail/blob/main/SECURITY.md) for private vulnerability reporting. Changes by date are in [CHANGELOG.md](https://github.com/kisbi3/ContextTrail/blob/main/CHANGELOG.md).

Distributed under the MIT License ([LICENSE](https://github.com/kisbi3/ContextTrail/blob/main/LICENSE)). External packages installed alongside this tool and their licenses are listed in [THIRD_PARTY_NOTICES.md](https://github.com/kisbi3/ContextTrail/blob/main/THIRD_PARTY_NOTICES.md).
