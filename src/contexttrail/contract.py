"""What a program that drives ContextTrail (a host) can rely on: the schema name every JSON output carries, the exit
codes, and `contexttrail capabilities`, which lists them with the commands and options read off the parser itself,
so the list cannot drift from what the parser accepts. docs/HOST.md describes each output field by field.

A schema name changes (`.v2`) only when a field is removed or changes meaning; fields are added under the same name.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .util import Busy, CallLimitReached, Cancelled, FlowError, Refused, UsageError

EXIT_OK = 0            # complete, noop (nothing new), no_data (nothing to read)
EXIT_ERROR = 1         # any other error
EXIT_PARTIAL = 2       # analyze: partial (units left, or some skipped) or call cap reached before a unit completed
EXIT_USAGE = 64        # a command line or option value the program cannot act on (argparse's own 2 is not used)
EXIT_BUSY = 75         # another run holds the project's analysis lock (or its database), try again later
EXIT_REFUSED = 77      # not done for want of the person's consent, a runner choice, or notes turned on
EXIT_INTERRUPTED = 130  # stopped by SIGINT, SIGTERM or SIGHUP; the saved result is kept

EXIT_CODES = {
    EXIT_OK: "complete, noop or no_data",
    EXIT_ERROR: "error (any other failure; the message is on stderr, or in the JSON with analyze --json)",
    EXIT_PARTIAL: "analyze ended partial or at the call cap; run again to continue",
    EXIT_USAGE: "usage error: unknown option, bad option value, missing argument",
    EXIT_BUSY: "busy: another analysis holds this project; try again later",
    EXIT_REFUSED: "refused: no consent for the runner, no runner chosen, or notes off for the project",
    EXIT_INTERRUPTED: "interrupted by SIGINT, SIGTERM or SIGHUP; saved results are kept",
}

# The schema name of each JSON output, by command; `export --format json` keeps its own `format` field.
SCHEMAS = {
    "scan": "contexttrail.scan.v1",
    "status": "contexttrail.status.v1",
    "find": "contexttrail.find.v1",
    "show": "contexttrail.show.v1",
    "note": "contexttrail.note.v1",
    "note-list": "contexttrail.note-list.v1",
    "doctor": "contexttrail.doctor.v1",
    "ops": "contexttrail.ops.v1",
    "analyze": "contexttrail.analyze.v1",
    "capabilities": "contexttrail.capabilities.v1",
}
EXPORT_FORMAT = "contexttrail.export.v1"
EXPORT_DELTA_FORMAT = "contexttrail.export-delta.v1"


def schema(name: str) -> str:
    return SCHEMAS[name]


def error_kind(exc: BaseException) -> str:
    """A short, stable name for why something stopped, for JSON outputs and the exit code."""
    if isinstance(exc, Busy) or (isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()):
        return "busy"
    if isinstance(exc, Refused):
        return "refused"
    if isinstance(exc, (Cancelled, KeyboardInterrupt)):
        return "cancelled"
    if isinstance(exc, CallLimitReached):
        return "call_limit"
    if isinstance(exc, UsageError):
        return "usage"
    if isinstance(exc, FlowError):
        return "error"
    return "internal"


KIND_EXIT = {"busy": EXIT_BUSY, "refused": EXIT_REFUSED, "usage": EXIT_USAGE, "cancelled": EXIT_INTERRUPTED}


def exit_for(exc: BaseException) -> int:
    return KIND_EXIT.get(error_kind(exc), EXIT_ERROR)


def run_exit(result: dict) -> int:
    """The exit code of an analysis by its result: a refusal or a busy project first, then the status."""
    kind = result.get("error_kind")
    if kind in ("refused", "busy"):
        return KIND_EXIT[kind]
    return {"failed": EXIT_ERROR, "partial": EXIT_PARTIAL, "call_limit": EXIT_PARTIAL,
            "cancelled": EXIT_INTERRUPTED}.get(result.get("status"), EXIT_OK)


class Parser(argparse.ArgumentParser):
    """argparse with usage errors on `EXIT_USAGE`: its own 2 would read as an analysis that ended partial."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _value_type(action: argparse.Action) -> str:
    if action.nargs == 0:
        return "flag"
    kind = action.type
    if kind is int:
        return "integer"
    if kind is float:
        return "number"
    if kind is Path:
        return "path"
    if getattr(kind, "value_type", None):
        return kind.value_type
    return "string"


def _argument(action: argparse.Action) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": action.option_strings[-1] if action.option_strings else action.dest,
        "flags": list(action.option_strings), "dest": action.dest, "positional": not action.option_strings,
        "type": _value_type(action), "required": bool(action.required) or (not action.option_strings and action.nargs is None),
        "repeatable": isinstance(action, argparse._AppendAction),
        "choices": _json_value(list(action.choices)) if action.choices else None,
        "default": _json_value(action.default) if action.default is not argparse.SUPPRESS else None}
    return entry


def commands(root: argparse.ArgumentParser) -> dict[str, Any]:
    """Every command and its arguments, read off the parser; options hidden from `--help` are left out."""
    found: dict[str, Any] = {}
    for action in root._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            arguments = [_argument(item) for item in sub._actions
                         if not isinstance(item, argparse._HelpAction) and item.help is not argparse.SUPPRESS]
            found[name] = {"arguments": arguments}
    return found


def capabilities(root: argparse.ArgumentParser) -> dict[str, Any]:
    from .sources.local import COMPRESSED_SUFFIX, SOURCES, zstd_backend

    backend = zstd_backend()
    return {
        "schema": schema("capabilities"),
        "version": __version__,
        "outputs": {**SCHEMAS, "export": EXPORT_FORMAT, "export-delta": EXPORT_DELTA_FORMAT},
        "sources": {
            "names": list(SOURCES),
            "codex": {"files": [".jsonl", COMPRESSED_SUFFIX], "jsonl_zst": backend is not None, "zstd_backend": backend,
                      "directories": ["sessions", "archived_sessions"], "home_flag": "--codex-home",
                      "home_environment": "CODEX_HOME", "home_default": "~/.codex"},
            "claude": {"files": [".jsonl"], "directories": ["projects"], "home_flag": "--claude-home",
                       "home_environment": "CLAUDE_CONFIG_DIR", "home_default": "~/.claude"},
            "opencode": {"files": ["opencode*.db"], "home_flag": "--opencode-home",
                         "home_environment": "XDG_DATA_HOME (as $XDG_DATA_HOME/opencode)",
                         "home_default": "~/.local/share/opencode",
                         "database_environment": "OPENCODE_DB (read only when the opencode home in use is opencode's own data directory)"},
            "symlinked_transcript_files": "skipped (counted in scan's sources[].files_skipped_symlink)",
            "attribution": "by the working directory each record names; no path remapping",
        },
        "commands": commands(root),
        "global_options": [_argument(action) for action in root._actions
                           if action.option_strings and not isinstance(action, argparse._HelpAction)],
        "exit_codes": {str(code): text for code, text in EXIT_CODES.items()},
        "not_provided": ["--path-map (remapping recorded paths)", "--state-dir (state outside the project)"],
    }
