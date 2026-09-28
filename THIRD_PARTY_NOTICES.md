# Third-party software

ContextTrail's own code is under the MIT License (see `LICENSE`). This repository does not
include third-party source code, fonts or browser libraries. The packages below are installed
separately from PyPI by `pip` or `install.sh` and stay under their own licenses.

## Python packages

| Package | Used for | License |
| --- | --- | --- |
| [jsonschema](https://pypi.org/project/jsonschema/) | Checking model output against the JSON contracts | MIT |
| [wcwidth](https://pypi.org/project/wcwidth/) | Terminal column widths (Korean and other wide characters) | MIT |
| [langgraph](https://pypi.org/project/langgraph/) | Running the analysis steps as one graph | MIT |

Optional extras:

| Package | Extra | License |
| --- | --- | --- |
| [pytest](https://pypi.org/project/pytest/) | `dev` | MIT |
| [langsmith](https://pypi.org/project/langsmith/) | `langsmith`, `studio` (developer tracing only) | MIT |
| [langgraph-cli](https://pypi.org/project/langgraph-cli/) | `studio` | MIT |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | `studio` | BSD-3-Clause |

Packages these depend on are also installed from PyPI. As installed on 2026-09-27 they were all
under permissive licenses: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF-2.0, and MPL-2.0 for
`certifi` and part of `orjson` (used unmodified). Check `pip show <package>` for the versions you have.

## Tools ContextTrail calls but does not include

- [OpenAI Codex CLI](https://github.com/openai/codex) and [Claude Code](https://claude.com/claude-code)
  are installed by the user. ContextTrail reads their local transcripts and runs the chosen CLI
  for analysis; their use is governed by their own terms.
- `git`, `bubblewrap` (Linux) and `sandbox-exec` (macOS) are system tools called as separate processes.
