# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11+ package under `src/projectflow/`. `analysis.py` coordinates incremental analysis; `sources/` reads local transcripts; `runners/` integrates the Codex and Claude CLIs; `schema.py` validates evidence and graph changes; `store.py` manages SQLite state. The terminal and browser interfaces live in `ui.py`, `webview.py`, and `assets/`. Prompts are in `prompts/`, tests in `tests/`, runnable examples in `examples/`, and design and security notes in `docs/`. Start with `README.md`, `docs/SPEC.md`, and `docs/SECURITY.md` when changing behavior.

## Build, Test, and Development Commands

- `python -m pip install -e '.[dev]'`: install the package and pytest for local development.
- `scripts/project demo --path /tmp/projectflow-demo`: create a synthetic demo without a live AI account.
- `scripts/project view /tmp/projectflow-demo/sample-project`: inspect saved results locally.
- `scripts/test.sh -q`: run the pytest suite with external pytest plugins disabled. It picks `python` or `python3` automatically; override with the `PYTHON` environment variable. Equivalent: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`. CI runs the same command on Linux and macOS across Python 3.11-3.13.
- `python scripts/prelive_walkthrough.py --output /tmp/pf-prelive-walkthrough`: exercise the scripted prelive flow and write artifacts outside the repository.

## Coding Style & Naming Conventions

Use four-space indentation and follow the existing Python style: `snake_case` for modules, functions, and variables; `PascalCase` for classes; `UPPER_CASE` for constants. Keep type annotations where surrounding code uses them. There is no configured formatter or linter; keep changes consistent with nearby code and avoid unrelated reformatting.

## Testing Guidelines

Tests use pytest. Add focused `tests/test_*.py` cases named `test_<behavior>` for changed behavior, using fixtures in `tests/conftest.py` when useful. Prefer synthetic transcripts and temporary directories; the suite is designed to avoid real AI accounts. Run `scripts/test.sh -q` before submitting. No coverage threshold is configured.

## Commit & Pull Request Guidelines

This checkout has no Git history, so no established commit convention can be verified. Use short, imperative commit subjects that identify the change. Pull requests should explain the behavior changed, link a relevant issue or design note, include test results, and attach screenshots for UI changes.

## Security & Configuration

Treat transcripts, evidence exports, and browser access links as sensitive. Preserve the read-only handling of project and Git inputs, and validate model output before storing or rendering it. Review `docs/SECURITY.md` before changing CLI isolation, authentication, or browser endpoints.
