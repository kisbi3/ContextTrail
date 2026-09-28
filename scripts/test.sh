#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"

# Pick an interpreter that can actually import the package and pytest. A bare
# `python` is absent on most current Linux and macOS systems, and one that
# exists may be a different environment without the dev dependencies installed.
# Override with PYTHON=... when several are usable.
usable() {
  "$1" -c 'import pytest, projectflow' >/dev/null 2>&1
}

PY=""
if [ -n "${PYTHON:-}" ]; then
  # An explicit override is never silently ignored: falling back would run a
  # different interpreter than the one the caller asked for.
  if ! usable "$PYTHON"; then
    echo "PYTHON=$PYTHON cannot 'import pytest, projectflow'." >&2
    echo "Create one and run:  python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
    exit 1
  fi
  PY="$PYTHON"
else
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && usable "$candidate"; then
      PY="$candidate"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "No interpreter can 'import pytest, projectflow'." >&2
  echo "Create one and run:  python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
  echo "then:                PYTHON=.venv/bin/python $0 \$@" >&2
  exit 1
fi

exec "$PY" -m pytest "$@"
