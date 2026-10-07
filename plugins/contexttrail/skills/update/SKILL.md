---
name: update
description: "Add to this project's saved ContextTrail graph with a Codex or Claude analysis, a chosen number of work units at a time. Only when the user invokes it by name."
argument-hint: "[work units | this session]"
disable-model-invocation: true
---

<!-- ContextTrail managed command: reinstalled with the program. -->

The user explicitly invoked this command to add to the current project's saved ContextTrail graph. Run it only because they invoked it by name; never start an analysis on your own. Arguments: `$ARGUMENTS` (may be empty).

Analysis sends the selected transcript records and Git evidence of this project to the cloud model behind the chosen runner (the Codex CLI or the Claude CLI) and uses that account's quota. It runs oldest records first, a number of work units at a time.

1. Run `contexttrail scan . --json` (no AI calls; add `--session current` if the arguments ask for this session). Check that `scope` is the intended project. Note `runner`: the runner saved for this project (`codex` or `claude`), or null. Show the user `plan_text` and `plan_choices` (pending work units, estimated input tokens and minutes per choice) and say that records are sent to that runner's model.
2. Choose the runner: if `runner` is not null, use it. If it is null, ask the user whether to analyze with Codex (`--runner codex`, the `codex` CLI) or Claude (`--runner claude`, the `claude` CLI) and wait for the answer; the CLI must be installed and logged in on this machine. opencode is a log source, never a runner. The choice is saved for the next run.
3. Decide how many work units to process:
   - A number in the arguments is the unit count.
   - "this session", "이번 세션" or "current" in the arguments means `--session current`: only the session you are running in, and the sub-agents it started, ahead of older records. Its events are marked out of order (earlier relations may be missing). Mention that.
   - Otherwise ask the user how many units to process and wait for the answer. Never choose the number yourself, and do not run step 4 without it.
4. Run `contexttrail analyze . --runner <runner> --yes --no-tui --brief --units N` (plus `--session current` if chosen). `--yes` records the user's consent for this project and runner, which they gave by invoking this command and choosing N. A unit usually takes 5–15 minutes, so run it in the background if you can and report progress from its stderr lines (one per finished unit, `k/N`). If it is stopped, finished units stay saved and the next run continues from there.
5. When it ends, run `contexttrail find .` and report: the run status (complete, or partial with units still waiting, which is expected when N is less than the pending count), what was added, and any error. Do not claim a change succeeded unless its status says it was verified.

If the analysis fails because of a sandbox or network restriction of your own environment (for example inside the Codex sandbox), say so and give the user the exact command to run in their own terminal. Never pick a runner the user did not choose or save. Do not edit project files as part of this command.
