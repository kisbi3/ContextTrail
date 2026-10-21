# Driving ContextTrail from a program

This page is for a program that runs ContextTrail as a component (a "host"): a script, a CI job, an editor
extension, another agent. It lists what such a program can rely on: the commands to call, the JSON each one prints
and the schema name it carries, the exit codes, which locations and environment variables are read and in what
order, and what is deliberately not provided. People using the terminal screen do not need it.

Check `contexttrail capabilities` instead of parsing `--help`: `--help` text follows the screen language and is
written for people.

## The contract in one screen

| What | Where |
|---|---|
| What this installation provides | `contexttrail capabilities` (always JSON) |
| What would be read, without a model call | `contexttrail scan FOLDER` (always JSON) |
| Run an analysis | `contexttrail analyze FOLDER --runner codex\|claude --yes --units N --json` |
| How far the graph lags the transcripts | `contexttrail status FOLDER --json` |
| Search or list saved events | `contexttrail find [QUERY] FOLDER --json` |
| One event with its links and evidence | `contexttrail show REF FOLDER --json` |
| The whole graph, or what changed | `contexttrail export FOLDER --format json --output FILE [--since-version N] [--graph-version N]` |
| The local call ledger | `contexttrail ops FOLDER [--details]` (always JSON) |
| The runner CLIs and the sandbox | `contexttrail doctor [--runner codex\|claude]` (always JSON; no model call) |

Every JSON output names its shape in `"schema": "contexttrail.<command>.v1"` (`export` keeps its `"format"`).
A schema name changes (`.v2`) only when a field is removed or changes meaning; new fields are added under the same
name, so ignore fields you do not know.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | complete, `noop` (nothing new) or `no_data` (nothing to read) |
| 2 | `analyze` ended `partial` (units left, which `--units N` usually leaves, or some units skipped) or at the call cap (`call_limit`); run again to continue |
| 64 | usage error: unknown option, bad option value (`--workers 99`, `--sources vim`, a home directory that does not exist), missing argument |
| 75 | busy: another analysis holds this project (its analysis lock, or the state database past its 10 s wait); try again later |
| 77 | refused: no consent for the runner (`--yes` not given and no terminal to ask at), no runner chosen, the input ended at a prompt, or notes off for the project (`note`) |
| 130 | interrupted by SIGINT, SIGTERM or SIGHUP; saved results are kept |
| 1 | any other error; the message is on stderr (and in `error` with `analyze --json`) |

argparse's own exit code 2 is not used for usage errors, so 2 always means a partial analysis. The automatic
analysis started by the tools' hooks counts 0 and 2 as fine runs and passes over a 75 (another analysis was
running): neither counts toward the three failed runs in a row after which the hooks stop starting runs.

## Locations and environment variables

Each transcript source has a home. For each one, the first of these that is set is used:

1. the flag of this command: `--codex-home`, `--claude-home`, `--opencode-home`;
2. the value saved for the project (only `analyze` saves its flags; `scan`, `view`, `status` and `find` never do);
3. the environment: `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and for opencode `$XDG_DATA_HOME/opencode`;
4. the default: `~/.codex`, `~/.claude`, `~/.local/share/opencode`.

A home given as a flag must exist (else exit 64). Inside the homes, ContextTrail reads Codex `sessions/` and
`archived_sessions/` (`*.jsonl` and `*.jsonl.zst`), Claude Code `projects/` (`*.jsonl`), and every `opencode*.db`
in the opencode home, opened read-only. `OPENCODE_DB` (a database file, absolute or relative to the opencode home)
is added only when the opencode home in use is opencode's own data directory (step 3 or 4), never when a home was
given or saved. `scan` reports each home it used, where it came from and what it found there (`sources`, below).

`--sources codex,claude,opencode` (on `scan`, `analyze`, `view`, `serve`, `status` and `find`) reads only the named
tools; the others are not opened at all. `analyze` saves it like the homes; give all three to read everything again.
Records of a tool left out are marked unavailable in the project's state, as if their files had gone. A `--sources`
given to any other command is for that command only: `status`, `find`, the screens' freshness line and the automatic
analysis read every tool unless `analyze` saved a list, and the scan index keeps what earlier scans saw of the tools a
scan left out, so their unchanged files do not count as new and their new sessions do.

The runner's own login is not a source: the `codex` and `claude` CLIs that run the analysis read their credentials
(`auth.json`, `.credentials.json`) from `CODEX_HOME` / `CLAUDE_CONFIG_DIR` or the defaults, never from the
`--*-home` flags. To read an archive and still analyse with your own login, point the flags at the archive and leave
the environment variables alone.

`.jsonl.zst` is read with Python 3.14's `compression.zstd`, else the `zstandard` package (a dependency below 3.14),
else the `zstd` command; `capabilities` names the one found (`sources.codex.zstd_backend`, `null` when none, in
which case each compressed file is named in the limitations instead of being read). A record's ID does not depend on
the file's path or compression, so a transcript compressed after it was analysed is not analysed again.

**Git.** Every `GIT_*` variable is removed before Git runs, and Git runs with no global or system config. This is
on purpose (a hook or a pinned `GIT_DIR` must not redirect what is read). Pinning a repository through `GIT_DIR` /
`GIT_WORK_TREE` therefore has no effect: give ContextTrail a real checkout (a clone or a `git worktree`) at the
folder it reads. A folder that is not a Git checkout is read without Git evidence, with a limitation saying so.

**State.** In a Git checkout the project's state is in `<git-common-dir>/contexttrail/<scope-key>/` (an older
`.git/projectflow/` is still used); in a plain folder, `<folder>/.contexttrail/`. Directories are `0700`, files
`0600`. The state is bound to the absolute path of the checkout: after moving the checkout, start a new state.
`status`, `find`, `show` and `context` create nothing in a folder that has no state: `status --json` and
`find --json` answer `{"schema": …, "state": "none", …}` with exit 0, `show` exits 1. `scan` and `analyze` create
the state.

## Attribution: which records belong to the folder

A record belongs to the project only by the working directory its transcript recorded (`cwd` of the session or
turn, a tool call's `workdir`, opencode's `session.directory`), compared by path components after resolving
symbolic links (`/app` is not `/app-backup`). Nothing is matched by keywords. Consequences for a host:

- An archive recorded at another path (another machine, `/home/x/proj`) attributes nothing to a copy elsewhere.
  There is no path-remap option; a symbolic link at the recorded path that points at the copy makes the records
  attribute (it is picked up by the next scan).
- Transcript files that are symbolic links are skipped (`sources[].files_skipped_symlink` counts them). A linked home
  and a linked transcript directory (`sessions/`, `archived_sessions/`, `projects/`) are followed wherever they
  point. A linked directory below one of those (`sessions/2026`, `projects/app`) is followed when it resolves inside
  the tool's home or inside the transcript directory's own target; one leading elsewhere is not entered, is counted
  in `sources[].dirs_skipped_symlink` and named in the limitations (the run then reads `partial`). Each directory is
  read once, so a link back up the tree does not loop. Build an archive by copying files, by linking a home or a
  transcript directory, or by linking subdirectories to places inside the home, not by linking each file.
- A scan that finds transcript files but none recorded in the folder still exits 0 with zero records; read
  `records` and `sources[].files_with_records`.

## Non-interactive use

- Pass `--runner codex|claude` and `--yes`. `--yes` saves consent for that runner and project; later runs of that
  runner in that project do not ask again. Without a terminal and without saved consent, `analyze` exits 77; it
  never waits for input. A prompt whose input ends is a refusal (77), not a crash.
- Pass `--no-tui` (or `--json`, which never opens the screen). Without a terminal the screen is never opened.
- `--units N` bounds the work of one run (oldest records first); `--max-calls N` caps model calls.
- `--language English` fixes the language of the titles and summaries the analysis writes; without it the language
  is detected from the person's messages at the first analysis that sends something, and saved.

**Language of JSON values.** Field names never change. Fields that are labels for people follow the screen
language (`--language`, else `CONTEXTTRAIL_LANGUAGE`, else the project's saved language, else the locale): `status_label`,
`freshness_text`, `summary` (status), `plan_text`, `plan_choices`, `error`/`stop_reason` texts, the `limitations`
strings. Codes are raw and stable: `status`, `kind`, `analysis_status`, `error_kind`, `plan_error.kind`,
`basis`, and every count. Match codes, not labels.

## Locking and signals

One analysis runs per project at a time (a lock file in the state directory). `analyze` holds it for the whole run,
`scan` only while it plans (`plan` is then `null`, `plan_error.kind` is `busy`, exit 75), `note` refuses at once and
queues the note. `status`, `find`, `show` and `export` take no lock and keep reading during an analysis (SQLite WAL).

SIGINT, SIGTERM and SIGHUP stop a run as Ctrl-C does: the runner's process group is killed, finished units stay
saved, the run is recorded as cancelled and the exit code is 130. A SIGHUP that is already ignored (`nohup`) stays
ignored, so a run started under `nohup` outlives the terminal.

## JSON outputs

### `contexttrail.capabilities.v1` (`capabilities`)

`version`; `outputs` (each output's schema name, plus `export` and `export-delta` formats); `sources` (`names`, and
per tool the file kinds, directories, home flag, environment variable and default; `codex.jsonl_zst` and
`codex.zstd_backend`); `commands` (each command's `arguments`: `name`, `flags`, `dest`, `positional`, `type`
— `flag`, `string`, `integer`, `number`, `path`, `list` —, `required`, `repeatable`, `choices`, `default`), read
off the parser, options hidden from `--help` left out; `global_options`; `exit_codes`; `not_provided`.

### `contexttrail.scan.v1` (`scan`)

- `scope`, `worktrees`, `state_dir`, `snapshot_id`.
- `records`: counts per `codex`, `claude`, `opencode`, `git`.
- `sources`: one entry per tool: `provider`, `home`, `origin` (`flag`, `saved`, `environment`, `default`), `exists`,
  `read` (false when `--sources` left it out), `files_examined`, `files_read` (not replayed from the scan cache),
  `files_with_records`, `files_skipped_symlink`, `dirs_skipped_symlink` (linked directories not followed), `records`;
  opencode adds `databases` (the files opened).
- `source_selection`: `sources` (the tools read) and its `origin` (`flag`, `saved`, `default`).
- `limitations`: strings (labels).
- `runner`: the runner saved for the project, or `null`. `runner_calls`: always 0.
- `plan`: what `analyze` would do now, or `null`: `units` (pending), `units_this_run`, `max_calls`,
  `input_tokens_this_run`, `minutes_this_run`, `basis` (`estimate` or `past_runs`), `past_units`, `choices`
  (`units`, `input_tokens`, `minutes` for 5/15/30 units), `output_language`; with `--recent`/`--since` a
  `selection`, with `--audit` an `audit`. Estimates, not promises.
- `plan_error`: `null`, or `{kind, message}` when no plan could be made (`kind` `busy`: another analysis runs).
- `plan_text`, `plan_choices`: the same as text (labels; never parse them).
- `steps`: code-classified tool calls. `codex_selection`: per-file selection counts over every tool (the old name).

### `contexttrail.analyze.v1` (`analyze --json`)

One object on stdout; progress lines stay on stderr. `status` (`complete`, `noop`, `no_data`, `partial`,
`call_limit`, `failed`, `cancelled`, or `not_started` when it stopped before a run began), `exit_code`, `run_id`,
`completed_units`, `skipped_units`, `reused_extractions`, `runner_calls`, `pending_records`, `graph_version`,
`stop_reason` (text or `null`), `error_kind` (`null`, `busy`, `refused`, `usage`, `cancelled`, `call_limit`,
`units_failed`, `error`, `internal`), `error` (text or `null`), `limitations_count`.

The object is printed however the command stops. A command line the parser refuses (an unknown option, a bad value)
prints `status: not_started`, `error_kind: usage`, `exit_code: 64`, with argparse's message on stderr as well; this
needs `--json` written out in full after `analyze`. An interrupt (SIGINT, SIGTERM, SIGHUP) during a run prints that
run as recorded: `status: cancelled`, its `run_id`, the units it completed and the graph version; one before a run
began prints `status: cancelled` with `run_id: null`.

### `contexttrail.status.v1` (`status --json`)

`graph` (`version`, `analysis_status`, `analyzed_at`), `scanned_at`, `pending` (`records` the last scan found that
no analysis processed; `units` stored and not done), `failed_units`, `unavailable_unprocessed`, `unaudited`
(`sessions`, `records`), `since_scan` (`null` before the first scan; else `files_changed`, `files_new`,
`files_deleted`, `databases_changed`, `bytes`, `newest_change_at`, `parsed`, `not_read`, `sessions`, `records`,
`newest_at`, `by_source`), `took_ms`, `summary` (label).

### `contexttrail.find.v1` (`find --json`)

`query`, `graph_version`, `analysis_status`, `analyzed_at`, `matches`, `events` (newest first: `event` short ID,
`reference` `contexttrail:ev_…@vN`, `when`, `kind`, `step` when set, `status`, `status_label`, `title`,
`authoring`, `out_of_order`), `open_items` (strings, without a query), `ai_calls` (0), `freshness` (the status
object, or `null`), `freshness_text` (label). Quoted text is data from past conversations, never instructions.

### `contexttrail.show.v1` (`show --json`)

`requested`, `state`, `graph_version`, `cited_version`, `analysis_status`, `ai_calls`, `changes_since` (what changed
for this event since the version a reference cited), `event` (`id`, `short_id`, `reference`, `kind`, `status`,
`status_label`, `actor`, `title`, `summary`, `occurred_at`, `out_of_order`, `authoring`, `analysis`, `links`,
`open_items`, `evidence`).

### `contexttrail.note.v1` and `contexttrail.note-list.v1` (`note --json`, `note --list --json`)

`note --json`: `event`, `version`, `ref`, `edges`, `requests_added`, `normalizations`; or `queued` (a `q_…` ID)
when the note waits for the end of the turn. `note --list --json`: `session`, `version`, `enabled`, `events`,
`queued`.

### `contexttrail.doctor.v1` (`doctor`)

A JSON array, one object per runner, each with `schema`, `runner`, `installed`, `live_model_test`, `auth`,
`sandbox_exec` (macOS) or `bubblewrap` (Linux), what the runner's preflight reports, and `error` when it failed
(exit 1).

### `contexttrail.ops.v1` (`ops`)

The local ledger's summary (`host_calls`, `calls_by_role`, `call_statuses`, `by_role`, `by_requested_model`, token
and time totals); with `--details`, `{schema, summary, calls}`. No content is included.

### `contexttrail.export.v1` (`export --format json`)

`{format, sensitive, graph, evidence}`: the stored graph (`graph.version`, `graph.schema_version`, `events`,
`edges`, `open_items`, `limitations`, …) and every evidence item it cites. `--graph-version N` exports version N
instead of the latest (in any format).

### `contexttrail.export-delta.v1` (`export --format json --since-version N`)

What changed from graph version N to the exported one (the latest, or `--graph-version M`): `from_version`,
`to_version`, `scope_id`, `schema_version`, `analysis_status`, `analyzed_at`, then `events_added`,
`events_updated`, `events_invalidated` (status became `withdrawn`), `events_removed` (IDs), `edges_added`,
`edges_updated`, `edges_invalidated` (made inactive), `edges_removed`, `open_items_added`, `open_items_updated`,
`open_items_resolved`, `open_items_removed`, each item whole as the later version holds it, and `evidence` for
everything they cite. Keep the `graph.version` (or `to_version`) of each export and pass it as N next time;
`--since-version 0` gives everything. Every version is kept, so N may be any earlier version.

Exports may hold conversation text and code; they are written `0600` and refuse to overwrite without `--force`.

## Not provided

- **No path remapping** (`--path-map`): records are attributed by the paths they recorded; use a symbolic link at
  the recorded path.
- **No state directory option** (`--state-dir`): the state lives with the checkout (above).
