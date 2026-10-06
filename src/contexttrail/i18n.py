"""The language of what the person sees on the screen and in the terminal: Korean or English.

It is chosen once per process (`set_language`) from, in order: an explicit `--language`, the
`CONTEXTTRAIL_LANGUAGE` environment variable, the language saved for the project (the one the
person's own messages are written in), and the terminal locale; English otherwise. Text the
model reads (prompts, validation messages, read denials) and text written into the graph
(titles, summaries, rationales, limitations) follow the analysis contract, not this choice.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Iterator

LANGUAGES = ("ko", "en")
ENV = "CONTEXTTRAIL_LANGUAGE"
_KO = {"ko", "kor", "korean", "한국어"}
_EN = {"en", "eng", "english", "c", "posix"}
_language: str | None = None


def code(name: str | None) -> str | None:
    """'ko' or 'en' for a language name or locale ('Korean', 'ko_KR.UTF-8', 'en'); None otherwise."""
    text = (name or "").strip().lower()
    head = text.split(".")[0].split("@")[0].split("_")[0].split("-")[0]
    if head in _KO:
        return "ko"
    if head in _EN:
        return "en"
    return None


def resolve(explicit: str | None = None, saved: str | None = None, environ: Mapping | None = None) -> str:
    """The screen language from the explicit choice, the environment, the project's saved language, the locale."""
    env = os.environ if environ is None else environ
    locale = next((code(env.get(name)) for name in ("LC_ALL", "LC_MESSAGES", "LANG") if code(env.get(name))), None)
    return code(explicit) or code(env.get(ENV)) or code(saved) or locale or "en"


def set_language(language: str | None) -> str:
    global _language
    _language = code(language) or "en"
    return _language


def language() -> str:
    """The current screen language; resolved from the environment alone until something sets it."""
    global _language
    if _language is None:
        _language = resolve()
    return _language


def tr(ko: str, en: str) -> str:
    """The Korean or the English text, by the current screen language."""
    return ko if language() == "ko" else en


class Labels(Mapping):
    """A read-only mapping whose values follow the screen language at the time of each lookup."""

    def __init__(self, ko: Mapping[str, str], en: Mapping[str, str]):
        missing = set(ko) ^ set(en)
        if missing:
            raise ValueError(f"labels without both languages: {sorted(missing)}")
        self._ko, self._en = dict(ko), dict(en)

    def _table(self) -> dict[str, str]:
        return self._ko if language() == "ko" else self._en

    def __getitem__(self, key: str) -> str:
        return self._table()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._ko)

    def __len__(self) -> int:
        return len(self._ko)

    def __repr__(self) -> str:
        return f"Labels({self._table()!r})"
