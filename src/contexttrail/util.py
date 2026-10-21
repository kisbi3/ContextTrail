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


class UsageError(FlowError):
    """A command line or option value the program cannot act on (exit 64, as an argparse error)."""


class Busy(FlowError):
    """Another run holds the project (its analysis lock, or the state database's write lock) (exit 75)."""


class Refused(FlowError):
    """Not done for want of what only the person gives: consent, a runner choice, notes turned on (exit 77)."""


class Declined(Cancelled, Refused):
    """The person said no at the consent prompt: recorded as a cancelled run, reported as a refusal."""


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
# `model.segment_record`), so they are not stored, sent to a model or exported; the store masks what it gives back
# (`Store.graph`, `Store.evidence`: `mask_data`, `mask_evidence`) for states stored by earlier versions. Not a
# promise that nothing secret is left (`docs/SECURITY.md`): a format it does not know passes as recorded. Masking
# keeps every line break (a private key block becomes one masked line per line), so citations keep their line
# numbers; it is idempotent; and it never cuts a backslash escape or a quote of a JSON string, so a tool call's
# arguments stay readable JSON. Every pattern is bounded (values are matched by possessive runs, which never backtrack; a private
# key block at most `PRIVATE_KEY_MAX` characters to its END line or `PRIVATE_KEY_LINES` lines without one), so the
# work stays linear in the text.
SECRET_HIDDEN = "[secret hidden]"
PRIVATE_KEY_MAX, PRIVATE_KEY_LINES = 16_384, 100


def _token_start(literal: str) -> str:
    """`literal` at a word start: after no word character, or right after a JSON escape (`\n`, `\t`, `\r`) whose
    letter would otherwise count as one. The literal comes first, so `re` can skip ahead to it."""
    return rf"{literal}(?:(?<!\w{literal})|(?<=\\[nrt]{literal}))"


_SECRET_PATTERNS = (
    re.compile(_token_start("sk-") + r"(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{20,}"),
    re.compile(_token_start("gh[pousr]_") + r"[A-Za-z0-9]{30,}"),
    re.compile(_token_start("github_pat_") + r"[A-Za-z0-9_]{30,}"),
    re.compile(_token_start("AKIA") + r"[0-9A-Z]{16}\b"),
    re.compile(_token_start("xox[abposr]-") + r"[A-Za-z0-9-]{10,}"),
    re.compile(_token_start("AIza") + r"[0-9A-Za-z_-]{35}\b"),
)
# A private key block: to its END line within `PRIVATE_KEY_MAX` characters, never past a quote (a key in a JSON
# string has `\n` escapes, and code that names the header quotes it) or another BEGIN; one cut short keeps to its
# base64 and header lines, at most `PRIVATE_KEY_LINES` of them.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:"
    rf"(?:(?!-----BEGIN )[^\"'`\\]|\\[nrt]){{0,{PRIVATE_KEY_MAX}}}?-----END [A-Z ]{{0,40}}PRIVATE KEY-----"
    r"|(?:(?:\r?\n|(?:\\r)?\\n)(?:[A-Za-z0-9+/=]{1,256}+(?=\r?\n|\\[rn]|[\"'`]|\Z)"
    rf"|[A-Za-z-]{{1,64}}+: [^\n\"'`\\]{{0,256}}+)){{0,{PRIVATE_KEY_LINES}}})")
_LINE_TEXT = re.compile(r"[^\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]+")  # what `str.splitlines` keeps of a line
_BEARER = re.compile("(" + _token_start("(?i:bearer)") + r"[ \t]+)[A-Za-z0-9._~+/-]{20,}=*")
# NAME_KEY=value, "api_token": "value", PASSWORD: value — the name stays, the value goes. Found from the name, so a
# separator inside a URL or another assignment (`https://…?access_token=…`, `user:pw@…?password=…`) never claims
# the run before the secret one. A quoted value runs to its closing quote; an unquoted one is one word ended by a
# space, a quote, a backslash or punctuation, so `token = os.environ["X"]`, `get_token()` or `api_key=self._key`
# is no secret, and neither is a number (`max_tokens=128000`).
_VALUE_END = r"(?=[\s\"'`,;&\\)\]}>]|\Z)"
_NOT_SECRET = (r"(?![0-9.]+\b)"  # a number
               rf"(?![a-z_]+(?:\.[a-z_][a-z0-9_]*)+{_VALUE_END})")  # a dotted name, `self._key`
_ASSIGNED_VALUE = re.compile(
    # The value may sit on the next line (`password:` then an indented value); a quoted one ends at its quote, or
    # where the text does (a cut tool output); a value of any length is masked whole (possessive runs: linear).
    r"(?P<head>(?i:api[_-]?key|secret|token|passw(?:or)?d)[A-Za-z0-9_.-]{0,40}+(?:\\?[\"'])?[ \t]*[:=][ \t]*"
    r"(?:\r?\n[ \t]*)?)(?:"
    rf"(?P<quote>\\?[\"']){_NOT_SECRET}[^\s\"'`\\]{{8,}}+(?:(?=(?P=quote))|{_VALUE_END})"
    rf"|{_NOT_SECRET}[^\s\"'`,;&\\()\[\]{{}}<>]{{8,}}+{_VALUE_END})")
# A text holding none of these holds nothing the patterns above mask (a quick test before the patterns).
_MAYBE_SECRET = re.compile(r"(?i)sk-|gh[pousr]_|github_pat_|akia|xox[abposr]-|aiza|bearer|-----begin "
                           r"|api[_-]?key|secret|token|passw")


def _hide_lines(match: re.Match) -> str:
    return _LINE_TEXT.sub(lambda line: SECRET_HIDDEN if line.group().strip() else line.group(), match.group())


def mask_secrets(text: str) -> str:
    """The text with credential-shaped values replaced by `SECRET_HIDDEN`, line for line."""
    if not text or not _MAYBE_SECRET.search(text):
        return text
    if "-----BEGIN " in text:
        text = _PRIVATE_KEY.sub(_hide_lines, text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(SECRET_HIDDEN, text)
    text = _BEARER.sub(lambda m: m.group(1) + SECRET_HIDDEN, text)
    return _ASSIGNED_VALUE.sub(lambda m: m["head"] + (m["quote"] or "") + SECRET_HIDDEN, text)


def mask_data(value: Any) -> Any:
    """A copy of JSON-shaped data with every string masked (`mask_secrets`; keys stay)."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {key: mask_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_data(item) for item in value]
    return value


def mask_record(record: Any) -> Any:
    """A `SourceRecord` with its content and the strings of its `lineage` and `git` masked (both are in the content
    hash, so a scan and a note agree); the same object when nothing matched."""
    changes = {name: masked for name, masked in (("content", mask_secrets(record.content)),
                                                 ("lineage", mask_data(record.lineage)), ("git", mask_data(record.git)))
               if masked != getattr(record, name)}
    return dataclasses.replace(record, **changes) if changes else record


# Evidence fields that identify or fingerprint the stored text: never masked, so they stay what was stored.
_EVIDENCE_KEYS = ("id", "source_id", "source_version", "content_hash", "quote_hash")


def mask_evidence(item: dict) -> dict:
    """A stored evidence item as it is given out (`Store.evidence`): its strings masked (`mask_data`), its IDs and
    hashes as stored. When its quote changed this way its `focus` offsets (into the stored text) are dropped and
    `masked: true` says so."""
    if not isinstance(item, dict):
        return item
    shown = mask_data(item)
    shown.update({key: item[key] for key in _EVIDENCE_KEYS if key in item})
    if shown.get("quote") != item.get("quote"):
        shown.pop("focus", None)
        shown["masked"] = True
    return shown
