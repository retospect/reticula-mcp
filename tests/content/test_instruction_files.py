"""Acceptance line 5: the shipped instruction files are short and only name
tools and parameters that exist in the frozen tool list."""

from __future__ import annotations

import json
import re
from pathlib import Path

from reticula_mcp.content import bootstrap

CONTRACT = json.loads(
    (Path(__file__).parents[1] / "contract" / "tools.json").read_text(encoding="utf-8")
)
INSTRUCTIONS_DIR = (
    Path(__file__).parents[2] / "src" / "reticula_mcp" / "content" / "instructions"
)
SPAN = re.compile(r"`([^`\n]+)`")
CALL = re.compile(r"^(\w+)\((.*)\)$")


def _argument_names(inner: str) -> list[str]:
    """Names in ``a, b=1, c="x, y"``; placeholders and values are skipped."""
    inner = re.sub(r'"[^"]*"', "S", inner)
    inner = re.sub(r"<[^>]*>", "X", inner)
    inner = re.sub(r"\[[^\]]*\]", "L", inner)
    names = []
    for token in inner.split(","):
        token = token.strip()
        if token in ("", "..."):
            continue
        names.append(token.split("=")[0].strip())
    return names


def test_there_is_a_starter_and_a_few_how_tos():
    slugs = [i.slug for i in bootstrap.shipped_instructions()]
    assert "starter" in slugs
    # a few: six since tagging, onboarding and finding a person (0.3.0);
    # each one more is a note an assistant has to find, so a seventh needs
    # a reason
    assert 2 <= len([s for s in slugs if s.startswith("howto-")]) <= 6


def test_every_instruction_file_is_under_80_lines():
    files = sorted(INSTRUCTIONS_DIR.glob("*.md"))
    assert files
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 80, path.name
        assert lines[0].startswith("# "), f"{path.name} needs a '# Title' first line"


def test_instructions_only_mention_tools_and_parameters_that_exist():
    checked = 0
    for item in bootstrap.shipped_instructions():
        for span in SPAN.findall(item.text):
            call = CALL.match(span)
            if not call:
                continue
            name, inner = call.groups()
            assert name in CONTRACT, f"{item.slug}: unknown tool {name!r} in `{span}`"
            for arg in _argument_names(inner):
                assert arg in CONTRACT[name]["properties"], (
                    f"{item.slug}: `{span}` uses {arg!r}, not a parameter of {name}"
                )
            checked += 1
    assert checked >= 12


def test_instruction_files_use_plain_punctuation():
    for path in INSTRUCTIONS_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "—" not in text and "–" not in text, path.name
        assert all(ord(ch) < 0x2000 for ch in text), f"{path.name}: non-text symbol"
