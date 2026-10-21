from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wcwidth import wcwidth, wcswidth

from .i18n import tr


class FlowError(Exception):
    """A user-facing operational error; messages must never contain credentials."""


class Cancelled(FlowError):
    pass


class BrokenOutput(FlowError):
    """The model's final answer was not readable JSON; a call may be tried once more."""


class InputBudgetExceeded(FlowError):
    """One unit's request is over the input budget and nothing is left to trim; the unit's fault, not the run's."""


class CallLimitReached(FlowError):
    """The run's cap on model calls was reached; a planned stop, not a failure."""


def input_tokens(usage: dict | None) -> int | None:
    """All input tokens one model call read, or None when the call reported none.

    Claude reports the cached part apart (`cache_creation_input_tokens`, `cache_read_input_tokens`), with
    `input_tokens` only what was neither written to nor read from the cache, often 2; Codex's `input_tokens`
    already holds its `cached_input_tokens`."""
    def count(key: str) -> int | None:
        value = (usage or {}).get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    base = count("input_tokens")
    if base is None:
        return None
    return base + (count("cache_creation_input_tokens") or 0) + (count("cache_read_input_tokens") or 0)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else (value if isinstance(value, str) else dumps(value)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def ident(prefix: str, *parts: Any) -> str:
    return prefix + digest(parts)[:24]


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


# Remove whole escape sequences, not just ESC: OSC may otherwise leave hostile URLs.
_ESC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


def merge_focus(*ranges: list | None) -> list[list[int]]:
    """Union of [start, end) character ranges cited within one evidence quote."""
    spans = sorted([int(s), int(e)] for group in ranges for s, e in (group or []))
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def safe_text(value: Any, *, multiline: bool = True) -> str:
    text = _ESC.sub("", str(value))
    # U+FFFE and U+FFFF are no characters XML can carry: one would make a whole SVG view unreadable.
    text = "".join(c for c in text if (c == "\n" and multiline) or
                   (unicodedata.category(c) not in {"Cc", "Cf", "Cs"} and c not in "\ufffe\uffff"))
    return text if multiline else " ".join(text.split())


def cell_slice(text: str, start: int, width: int) -> str:
    """Slice terminal cells without splitting a wide CJK glyph."""
    result, position = [], 0
    for char in text:
        size = max(0, wcwidth(char))
        if position >= start + width:
            break
        if position >= start and position + size <= start + width:
            result.append(char)
        elif position < start < position + size:
            result.append(" " * (position + size - start))
        position += size
    return "".join(result)


def ellipsis(text: str, width: int) -> str:
    text = safe_text(text, multiline=False)
    return text if wcswidth(text) <= width else cell_slice(text, 0, max(0, width - 3)) + "..."


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise FlowError(tr(f"상태 디렉터리 symlink는 허용하지 않습니다: {path}", f"A state directory that is a symlink is not allowed: {path}"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise FlowError(tr("상태 디렉터리는 현재 사용자 소유여야 합니다.", "The state directory must be owned by the current user."))
    os.chmod(path, 0o700)


# Values shaped like credentials, masked where transcripts and Git are read (`mask_record`, through
# `model.segment_record`), so they are not stored, sent to a model or exported; screens and exports mask them again
# for states stored by earlier versions. Not a promise that nothing secret is left (`docs/SECURITY.md`): a format it
# does not know passes as recorded. Masking keeps every line break (a private key block becomes one masked line per
# line), so citations keep their line numbers; it is idempotent; and it never cuts a backslash escape or a quote of a
# JSON string, so a tool call's arguments stay readable JSON.
SECRET_HIDDEN = "[secret hidden]"
# Each starts with its literal (a word boundary is checked after it), which lets `re` skip ahead fast.
_SECRET_PATTERNS = (
    re.compile(r"sk-(?<!\wsk-)(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_(?<!\wgh[pousr]_)[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_(?<!\wgithub_pat_)[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA(?<!\wAKIA)[0-9A-Z]{16}\b"),
    re.compile(r"xox[abposr]-(?<!\wxox[abposr]-)[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza(?<!\wAIza)[0-9A-Za-z_-]{35}\b"),
)
# A private key block: to its END line, never past a quote (a key in a JSON string has `\n` escapes, and code that
# names the header quotes it); one cut short keeps to its base64 and header lines.
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?:[^\"'`\\]|\\[nrt])*?-----END [A-Z ]*PRIVATE KEY-----"
                          r"|(?:(?:\r?\n|(?:\\r)?\\n)(?:[A-Za-z0-9+/=]+(?=\r?\n|\\[rn]|[\"'`]|\Z)|[A-Za-z-]+: [^\n\"'`\\]*))*)")
_LINE_TEXT = re.compile(r"[^\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]+")  # what `str.splitlines` keeps of a line
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{20,}=*")
# NAME_KEY=value, "api_token": "value", PASSWORD: value — the name stays, the value goes. A value is one word ended by
# a space, a quote, a backslash or punctuation, so `token = os.environ["X"]`, `get_token()` or `api_key=self._key`
# is no secret. Found by its `=` or `:` first and kept only when the name before it says secret (`_SECRET_NAME`):
# one pass, not a search from every word.
_ASSIGNED_VALUE = re.compile(r"[:=]\s*(?:\\?[\"'])?(?![0-9.]+\b)"  # a number (max_tokens=128000) is no secret
                             r"(?![a-z_]+(?:\.[a-z_][a-z0-9_]*)+(?=[\s\"'`,;&\\)\]}>]|\Z))"  # nor `self._key`
                             r"[^\s\"'`,;&\\()\[\]{}<>]{8,}(?=[\s\"'`,;&\\)\]}>]|\Z)")
_SECRET_NAME = re.compile(r"(?i)(?:api[_-]?key|secret|token|passw(?:or)?d)[A-Z0-9_.-]*(?:\\?[\"'])?\s*\Z")
_VALUE_START = re.compile(r"[:=]\s*(?:\\?[\"'])?")


def _hide_lines(match: re.Match) -> str:
    return _LINE_TEXT.sub(lambda line: SECRET_HIDDEN if line.group().strip() else line.group(), match.group())


def mask_secrets(text: str) -> str:
    """The text with credential-shaped values replaced by `SECRET_HIDDEN`, line for line."""
    if not text:
        return text
    if "-----BEGIN " in text:
        text = _PRIVATE_KEY.sub(_hide_lines, text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(SECRET_HIDDEN, text)
    text = _BEARER.sub(lambda m: m.group(1) + SECRET_HIDDEN, text)
    def value(m: re.Match) -> str:
        if not _SECRET_NAME.search(text, max(0, m.start() - 80), m.start()):
            return m.group()
        return _VALUE_START.match(m.group()).group() + SECRET_HIDDEN
    return _ASSIGNED_VALUE.sub(value, text)


def mask_record(record: Any) -> Any:
    """A `SourceRecord` with its content masked (`mask_secrets`); the same object when nothing matched."""
    content = mask_secrets(record.content)
    return record if content == record.content else dataclasses.replace(record, content=content)


def mask_data(value: Any) -> Any:
    """A copy of JSON-shaped data with every string masked (keys stay); for an export of a stored graph."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {key: mask_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_data(item) for item in value]
    return value
