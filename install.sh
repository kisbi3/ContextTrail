#!/bin/sh
# Install ContextTrail and its local coding-agent commands from a source checkout.
set -eu

install_repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
install_python=${CONTEXTTRAIL_PYTHON:-python3}
install_venv=${CONTEXTTRAIL_VENV:-"$HOME/.local/share/contexttrail/venv"}
install_bin=${CONTEXTTRAIL_BIN_DIR:-"$HOME/.local/bin"}

case "$install_venv:$install_bin" in
  /*:/*) ;;
  *) printf '%s\n' 'CONTEXTTRAIL_VENV and CONTEXTTRAIL_BIN_DIR must be absolute paths.' >&2; exit 1 ;;
esac

if ! "$install_python" -c 'import curses, sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  printf '%s\n' 'ContextTrail needs Python 3.11+ with curses. Set CONTEXTTRAIL_PYTHON to a suitable interpreter.' >&2
  exit 1
fi

if [ -e "$install_bin/contexttrail" ] || [ -L "$install_bin/contexttrail" ]; then
  if [ ! -L "$install_bin/contexttrail" ] || [ "$(readlink "$install_bin/contexttrail")" != "$install_venv/bin/contexttrail" ]; then
    printf 'Existing command was preserved: %s\n' "$install_bin/contexttrail" >&2
    exit 1
  fi
fi

mkdir -p "$(dirname -- "$install_venv")" "$install_bin"

# `python -m venv` fails with a raw ensurepip error on Debian/Ubuntu hosts that
# do not ship python3-venv, which is the default on Ubuntu 24.04. That message
# does not tell the reader what to run, and a failed install with no next step
# is where a first-time user stops. Check first and say the exact command.
if ! "$install_python" -c 'import ensurepip' >/dev/null 2>&1; then
  install_version=$("$install_python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '')
  printf '%s\n' "ContextTrail needs venv support, which is a separate package on Debian/Ubuntu." >&2
  printf 'Install it, then run this script again:\n\n' >&2
  if [ "$(id -u)" = 0 ]; then
    printf '    apt install python%s-venv\n\n' "$install_version" >&2
  else
    printf '    sudo apt install python%s-venv\n\n' "$install_version" >&2
  fi
  printf 'Already have it? Set CONTEXTTRAIL_PYTHON to an interpreter whose venv works.\n' >&2
  exit 1
fi

"$install_python" -m venv "$install_venv"
"$install_venv/bin/python" -m pip install --upgrade "$install_repo"

if [ ! -e "$install_bin/contexttrail" ] && [ ! -L "$install_bin/contexttrail" ]; then
  ln -s "$install_venv/bin/contexttrail" "$install_bin/contexttrail"
fi

if [ -e "$install_bin/project" ] || [ -L "$install_bin/project" ]; then
  if [ ! -L "$install_bin/project" ] || [ "$(readlink "$install_bin/project")" != "$install_venv/bin/project" ]; then
    printf 'Another program owns %s; use contexttrail instead.\n' "$install_bin/project" >&2
  fi
else
  ln -s "$install_venv/bin/project" "$install_bin/project"
fi

"$install_venv/bin/contexttrail" install-commands
printf '\nInstalled ContextTrail at %s\n' "$install_venv"
printf 'Run: %s graph /path/to/project\n' "$install_bin/contexttrail"
case ":$PATH:" in
  *":$install_bin:"*) ;;
  *) printf 'Add %s to PATH to use the contexttrail command directly.\n' "$install_bin" ;;
esac
