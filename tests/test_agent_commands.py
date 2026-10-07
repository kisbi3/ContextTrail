from pathlib import Path

import pytest

from contexttrail.agent_commands import install_agent_commands
from contexttrail.util import FlowError


def test_install_agent_commands_preserves_existing_user_commands(tmp_path):
    python = Path('/test environment/bin/python')
    paths = install_agent_commands(tmp_path, python)
    assert len(paths) == 11
    note = tmp_path / '.claude/skills/contexttrail-note/SKILL.md'
    # The agent picks the note skill itself while it works; it never runs an analysis.
    assert 'disable-model-invocation' not in note.read_text() and 'note --kind <kind>' in note.read_text()
    assert 'analyze' not in note.read_text().split('---', 2)[2]
    assert (tmp_path / '.agents/skills/contexttrail-note/SKILL.md').is_file()
    assert not (tmp_path / '.agents/skills/contexttrail-note/agents/openai.yaml').exists()
    update = tmp_path / '.agents/skills/contexttrail-update/SKILL.md'
    claude = tmp_path / '.claude/skills/contexttrail-context/SKILL.md'
    prompt = tmp_path / '.codex/prompts/contexttrail-update.md'
    opencode = tmp_path / '.config/opencode/commands/contexttrail-update.md'
    assert 'scan . --json' in update.read_text()
    assert 'analyze . --runner <runner> --yes --no-tui --brief --units N' in update.read_text()
    assert 'Codex only' not in update.read_text() and 'opencode is a log source, never a runner' in update.read_text()
    assert 'Do not run `analyze` yourself' in claude.read_text()
    assert '`/contexttrail-update` (Claude Code, opencode) or `$contexttrail-update` (Codex)' in claude.read_text()
    assert 'name: contexttrail-update' not in prompt.read_text()
    assert opencode.read_text().startswith('---\ndescription: "') and '`$ARGUMENTS`' in opencode.read_text()
    assert 'name: contexttrail-update' not in opencode.read_text() and 'disable-model-invocation' not in opencode.read_text()
    context = (tmp_path / '.config/opencode/commands/contexttrail-context.md').read_text()
    assert 'the user can run `/contexttrail-update`.' in context
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


def test_the_plugin_bundle_in_the_repository_matches_the_generated_skills():
    from contexttrail import __version__
    from contexttrail.agent_commands import plugin_files
    root = Path(__file__).resolve().parent.parent
    for relative, content in plugin_files(__version__).items():
        assert (root / relative).read_text(encoding="utf-8") == content, f"{relative}: run scripts/build_plugin.py"
    note = (root / "plugins/contexttrail/skills/note/SKILL.md").read_text()
    assert note.startswith("---\nname: note\n") and "`contexttrail note --kind <kind>" in note
    assert "disable-model-invocation" in (root / "plugins/contexttrail/skills/update/SKILL.md").read_text()
