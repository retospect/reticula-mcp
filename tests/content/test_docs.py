"""The README and docs/operating.md only name settings, commands and tools that
exist in the code."""

from __future__ import annotations

import json
import re
from pathlib import Path

from reticula_mcp import config
from reticula_mcp.cli import build_parser

ROOT = Path(__file__).parents[2]
DOCS = [ROOT / "README.md", ROOT / "docs" / "operating.md"]
CONTRACT = json.loads((ROOT / "tests" / "contract" / "tools.json").read_text())
# Commands owned by lanes that may not be merged yet (AGENTS.md, seam 9).
OTHER_LANES = {"connections", "reindex", "rebuild"}
# Used by scripts/test, not by the server.
TEST_ONLY = {"RETICULA_TEST_DATABASE_URL"}
# Read by deploy/compose.yaml, not by the server.
COMPOSE_ONLY = {
    "RETICULA_DB_PASSWORD",
    "RETICULA_VERSION",
    "RETICULA_SUBNET",
    "RETICULA_DB_WAIT",
    "RETICULA_TAILSCALE_ADDRESS",
    "RETICULA_APP_MEMORY",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_documented_environment_variables_exist_in_config():
    known = {
        f"RETICULA_{name}"
        for name in re.findall(r'get\("([A-Z_]+)"', _text(Path(config.__file__)))
    }
    for path in DOCS:
        used = set(re.findall(r"RETICULA_[A-Z_]+", _text(path)))
        assert used <= known | TEST_ONLY | COMPOSE_ONLY, (path.name, used - known)
    readme = _text(DOCS[0])
    for name in known:
        assert f"`{name}`" in readme, f"{name} is missing from the README table"


def test_documented_commands_exist():
    parser = build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)
    for path in DOCS:
        for name in re.findall(r"reticula-mcp(?:@latest)? ([a-z][a-z-]*)", _text(path)):
            if name in ("is", "and", "migrate"):
                continue
            assert name in commands | OTHER_LANES, (path.name, name)
    assert {"serve", "migrate", "token", "account", "debug-notes"} <= commands


def test_readme_lists_every_tool_and_no_other():
    readme = _text(DOCS[0])
    section = readme.split("## The tools", 1)[1].split("\n## ", 1)[0]
    listed = re.findall(r"^\| `(\w+)` \|", section, flags=re.MULTILINE)
    assert sorted(listed) == sorted(CONTRACT)


def test_docs_use_plain_punctuation():
    for path in DOCS:
        text = _text(path)
        assert "—" not in text and "–" not in text, path.name
        assert all(ord(ch) < 0x2000 for ch in text), path.name
