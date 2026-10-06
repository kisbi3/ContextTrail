# Contributing to ContextTrail

Thank you for your interest. ContextTrail reads local Codex and Claude Code transcripts plus Git history and reconstructs an evidence-linked project history. Because it handles people's private conversation logs and calls their own paid model accounts, contributions are held to a few rules that keep the tool read-only, isolated and honest about what it has measured.

## Before you start

- Read [`AGENTS.md`](AGENTS.md) (repository guidelines), [`docs/SPEC.md`](docs/SPEC.md) (analysis and storage contract) and [`docs/SECURITY.md`](docs/SECURITY.md) (isolation and trust boundary). `CLAUDE.md` is the same material in the form coding agents read.
- Open an issue first for anything that changes the model-visible input (prompts, task payload, context selection). Such changes need a measured evaluation before they are merged; see "Evaluations" below.

## Development setup

```bash
git clone https://github.com/kisbi3/ContextTrail.git
cd ContextTrail
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
PYTHON=.venv/bin/python scripts/test.sh -q
```

`scripts/test.sh` runs pytest with external plugins disabled. The suite uses synthetic transcripts, temporary directories, a loopback HTTP port and a PTY. It never calls a real AI account, and it must stay that way.

Useful commands that make zero model calls:

```bash
scripts/contexttrail demo --path /tmp/contexttrail-demo --no-tui    # synthetic data + Mock Runner
scripts/contexttrail scan /path/to/project                          # input selection diagnostics
scripts/contexttrail eval --fixture demo --runner mock --output /tmp/pf-eval
```

Write demo, eval and walkthrough output outside the repository.

## Invariants every change must keep

- **Read-only toward inputs.** Never modify the analysed project, transcripts, or Git refs, index or config. Git is queried with argument arrays, `--no-ext-diff`, `--no-textconv` and the pager off.
- **No model call without an explicit request.** Viewing, exporting, page loads and node selection never call a model.
- **Validate before storing.** Model output is untrusted structured data. Every cited quote must exist in the source; only slips with a single possible reading are corrected in code, and each correction is audited.
- **Explicit partial or failed states beat fabricated completeness.** A change never carries a success badge of its own; proximity in time is not causality.
- **Sandboxed runners only.** The Codex and Claude CLIs run inside bubblewrap (Linux) or a deny-default `sandbox-exec` profile (macOS). There is no unsandboxed fallback, and the tool never reads or copies credential contents.
- **Sensitive by default.** State directories are `0700`, databases and exports `0600`. Do not commit transcripts, evidence exports, access links, real project names or machine-specific paths.

## Making a change

1. Branch from `main`. Keep each pull request to one behaviour change.
2. Add or update tests in `tests/test_*.py`, named `test_<behavior>`, using the fixtures in `tests/conftest.py`. Changes to the analysis flow usually touch both `analysis.py` (primitives) and `studio_graph.py` (LangGraph node wiring).
3. Run `scripts/test.sh -q` locally. CI runs the same command on Linux and macOS across Python 3.11, 3.12 and 3.13.
4. Update the documents that describe the behaviour you changed (`README.md`, `CLAUDE.md`, the relevant `docs/` page). Record design decisions in `docs/DECISIONS.md` with a date.
5. Use a short imperative commit subject that names the behaviour. In the pull request, explain what changed and why, link the issue or design note, and include the test result. Attach a screenshot or terminal capture for UI changes.

## Evaluations

Anything that changes what the model sees needs an evaluation on a fixture before and after, with the numbers recorded in `docs/plans/PERFORMANCE_PLAN.md` or `docs/DECISIONS.md`. Evaluations against real CLIs send data to a model service and use the account of whoever runs them, so:

- never run `analyze`, `eval --runner codex|claude` or `doctor --smoke` in CI or on someone else's account;
- publish only aggregate results (scores, calls, tokens, durations). `calls.json` and the eval `state/` directory contain raw transcripts and stay private;
- say what was measured and what was not. A number that was not measured is not reported.

## Prompts and language

Prompts in `src/contexttrail/prompts/` are written in English. Validation messages returned to the model are Korean, and the repair step matches them by pattern, so change both the message and the matcher together. Titles and summaries are written in the project's detected `output_language`; quotes and identifiers stay as written.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Please do not open a public issue for a vulnerability.

## Releases

Maintainers cut releases as described in [`docs/guides/RELEASING.md`](docs/guides/RELEASING.md): a version bump, a tag, and the `publish` workflow, which uploads to PyPI through Trusted Publishing.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
