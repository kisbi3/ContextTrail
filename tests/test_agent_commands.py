from pathlib import Path

import pytest

from contexttrail.agent_commands import install_agent_commands
from contexttrail.util import FlowError


def test_install_agent_commands_preserves_existing_user_commands(tmp_path):
    python = Path('/test environment/bin/python')
    paths = install_agent_commands(tmp_path, python)
    assert len(paths) == 7
    update = tmp_path / '.agents/skills/contexttrail-update/SKILL.md'
    claude = tmp_path / '.claude/skills/contexttrail-context/SKILL.md'
    prompt = tmp_path / '.codex/prompts/contexttrail-update.md'
    assert 'scan .' in update.read_text()
    assert 'analyze . --runner codex --yes --no-tui --brief --units N' in update.read_text()
    assert 'Do not run `analyze` yourself' in claude.read_text()
    assert 'name: contexttrail-update' not in prompt.read_text()
    assert all(path.stat().st_mode & 0o077 == 0 for path in paths)
    install_agent_commands(tmp_path, Path('/new environment/bin/python'))
    assert '/new environment/bin/python' in update.read_text()
    prompt.write_text('my custom prompt')
    with pytest.raises(FlowError, match='덮어쓰지 않았습니다'):
        install_agent_commands(tmp_path, python)
    assert prompt.read_text() == 'my custom prompt'
    install_agent_commands(tmp_path, python, force=True)
    assert 'scan .' in prompt.read_text()


def test_install_agent_commands_rejects_symlinked_skill_directory(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / '.agents').symlink_to(outside, target_is_directory=True)
    with pytest.raises(FlowError, match='symlink'):
        install_agent_commands(tmp_path)
    assert list(outside.iterdir()) == []


def test_install_agent_commands_keeps_virtualenv_interpreter_path(tmp_path):
    base = tmp_path / 'python-base'
    base.write_text('')
    virtual = tmp_path / 'venv/bin/python'
    virtual.parent.mkdir(parents=True)
    virtual.symlink_to(base)
    install_agent_commands(tmp_path / 'home', virtual)
    skill = (tmp_path / 'home/.agents/skills/contexttrail-context/SKILL.md').read_text()
    assert str(virtual) in skill
    assert str(base) not in skill
