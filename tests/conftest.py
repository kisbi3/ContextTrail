from pathlib import Path

import pytest

from projectflow.analysis import AnalysisConfig, Engine
from projectflow.git_context import Scope
from projectflow.model import Snapshot, SourceRecord
from projectflow.store import Store


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
