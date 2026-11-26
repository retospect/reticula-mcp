"""The image job of check.yml renders the production compose files. Every
variable those files require (``${NAME:?...}``) must be set in that step, or
the job fails at release time with nothing else wrong (0.4.2's first tag)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_compose_check_sets_every_required_variable() -> None:
    required: set[str] = set()
    for name in ("compose.yaml", "compose.source.yaml"):
        text = (ROOT / "deploy" / name).read_text()
        required |= set(re.findall(r"\$\{([A-Z0-9_]+):\?", text))
    check = (ROOT / ".github" / "workflows" / "check.yml").read_text()
    step = check[check.index("The production compose files are valid") :]
    step = step[: step.index("- name:", 1)]
    missing = sorted(v for v in required if f"{v}=" not in step)
    assert not missing, f"set these in the compose check of check.yml: {missing}"
