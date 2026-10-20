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
def no_real_opencode_data(monkeypatch, tmp_path_factory):
    """The default opencode data directory is empty in tests, so no test reads a real opencode database."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    # The tools' config directories: an installer test that sets HOME must not reach the person's own.
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_host_process(monkeypatch):
    """The suite runs under whatever agent started it; a note's session comes from the test's environment."""
    from contexttrail import journal
    monkeypatch.setattr(journal, "host_tool", lambda pid=None: None)


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
