---
name: note
description: "Record this session's decisions, file changes and test/build/commit results into the project's shared ContextTrail graph with `contexttrail note`, quoting the transcript, in projects where ContextTrail notes are on (an end-of-turn hook asks for it)."
---

<!-- ContextTrail managed command: reinstalled with the program. -->

Use this while you work in a project where ContextTrail notes are on, to write what happened into the project's shared graph yourself: the decisions made, the files changed, and the results of tests, builds and commits. Codex, Claude Code and opencode sessions all read the same graph, so the next session (in any of these tools) starts from what you wrote. No AI call is made; each note is checked against this session's own transcript and stored only if every quote is found there.

Notes are on for a project when an end-of-turn hook asks you to record this turn, or when `contexttrail note --list` does not say they are off. If a note fails with "notes are off", stop using this skill in that project and do not mention it again.

When to write a note (one `note` call per event; several per turn is fine):
- a decision or proposal was made (`--kind decision` or `--kind proposal`), quoting the message that made it;
- you edited files (`--kind action`, or `--kind revision` when it fixes or replaces an earlier change; add `--revises <event>`), quoting a distinctive line of the edit you made, one `--quote` per edited file (a commit line alone does not cite the edits, and an audit would record them again);
- you ran a test, build or command and saw its result (`--kind outcome`), quoting the result line. `--status observed_success` or `observed_failure` only when you quote the tool's own output; a claim made only in conversation is `reported_complete` or `reported_failure`. Add `--verifies <event>` naming the change that run checked;
- you committed (`--kind outcome --status observed_success`), quoting the commit output.
The person's requests are recorded by code from the transcript; do not note them. Do not note reading files or searching. Do not note what you are unsure of.

Command (run in the project directory):
`contexttrail note --kind <kind> [--status <status>] --title "<short title>" --summary "<one or two sentences: what and why>" --quote "<text copied exactly>" [--quote ...] [--verifies|--revises|--answers|--motivates <event>]`
- Copy each quote exactly as it appears in a tool result, a tool call you made, or a message of this session: at least 8 characters, preferably one distinctive line; a short output such as `5` or `ok` can be quoted as its whole line. For an observed result, quote what the command printed, not the command. Never write line numbers.
- Write the title and summary in the language the person uses with you.
- The output is a reference like `contexttrail:ev_6226b954@v12`. Use it in `--verifies`/`--revises` of a later note to link them (an id prefix works too). `contexttrail note --list` shows this session's notes.
- Inside a sandbox that cannot write the project's state (Codex), the note is checked for its quotes and then queued: the output starts with an id like `q_3f2a…`. It is stored with every check when the turn ends, and you can link later notes to it by that id. If a queued note fails its checks you will be told why at the end of the turn.
- If a note is refused, read the reason: fix the quote (the message lists the closest lines) or the status and retry once. If it is refused again, move on. "an analysis is running" means try the same note again a minute later.
