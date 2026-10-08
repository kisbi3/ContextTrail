from __future__ import annotations

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
    text = "".join(c for c in text if (c == "\n" and multiline) or
                   (unicodedata.category(c) not in {"Cc", "Cf", "Cs"}))
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
