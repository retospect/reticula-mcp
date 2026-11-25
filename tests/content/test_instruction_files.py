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
    assert 2 <= len([s for s in slugs if s.startswith("howto-")]) <= 4


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
    assert checked >= 20


def test_the_starter_names_every_tool():
    starter = bootstrap.starter_instruction().text
    for tool in CONTRACT:
        assert f"`{tool}(" in starter, tool


def test_starter_signatures_match_the_tool_defaults():
    """Where the starter writes `name=value` for a tool parameter that has a
    default, the value is that default."""
    starter = bootstrap.starter_instruction().text
    signature_lines = [ln for ln in starter.splitlines() if ln.startswith("- `")]
    assert len(signature_lines) == len(CONTRACT)
    for line in signature_lines:
        span = SPAN.findall(line)[0]
        call = CALL.match(span)
        assert call and call.group(1) in CONTRACT, line
        props = CONTRACT[call.group(1)]["properties"]
        for token in call.group(2).split(", "):
            if "=" not in token:
                continue
            name, value = token.split("=", 1)
            if name in props and "default" in props[name] and "[" not in value:
                expected = json.dumps(props[name]["default"])
                shown = {"None": "null", "True": "true", "False": "false"}.get(
                    value, value.replace("'", '"')
                )
                assert shown == expected, f"`{span}`: {name} default is {expected}"


def test_instruction_files_use_plain_punctuation():
    for path in INSTRUCTIONS_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "—" not in text and "–" not in text, path.name
        assert all(ord(ch) < 0x2000 for ch in text), f"{path.name}: non-text symbol"
