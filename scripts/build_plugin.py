"""Rewrite the Claude Code plugin bundle and marketplace files from the skill text in agent_commands."""
from pathlib import Path

from contexttrail import __version__
from contexttrail.agent_commands import plugin_files

ROOT = Path(__file__).resolve().parent.parent

for relative, content in plugin_files(__version__).items():
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(relative)
