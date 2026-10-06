from pathlib import Path

import pytest

from contexttrail import i18n
from contexttrail.analysis import AnalysisConfig, Engine
from contexttrail.git_context import Scope
from contexttrail.model import Snapshot, SourceRecord
from contexttrail.store import Store


@pytest.fixture(autouse=True)
def korean_screen(monkeypatch):
    """The suite reads the Korean screen text; a test of the English screen sets the language itself.

    The environment variable reaches the CLI processes tests start, whatever the host locale.
    """
    monkeypatch.setenv(i18n.ENV, "ko")
    previous = i18n._language
    i18n.set_language("ko")
    yield
    i18n._language = previous


@pytest.fixture(autouse=True)
def integrate_by_model(monkeypatch):
    """Most tests are about the integrate call itself; a test of draft publishing asks for it."""
    original = AnalysisConfig.__init__
    def init(self, *args, **kwargs):
        kwargs.setdefault("integrate_output", "full")
        original(self, *args, **kwargs)
    monkeypatch.setattr(AnalysisConfig, "__init__", init)


@pytest.fixture
def laboratory(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    scope = Scope.resolve(folder)
    store = Store(scope.state_dir, scope.id)
    engine = Engine(scope, store, AnalysisConfig(codex_home=tmp_path / "codex", claude_home=tmp_path / "claude"))
    records = []
    engine.scan = lambda: Snapshot(list(records))
    def make(text, key="s1", role="user", provider="claude", session="session-1"):
        return SourceRecord(key, provider, session, role, text, {"kind": "jsonl", "path": str(tmp_path / "log.jsonl"), "line": 1},
                            recorded_at="2026-09-22T10:00:00Z", cwd=str(folder), worktree_id=scope.worktree_for(str(folder)))
    return scope, store, engine, records, make
