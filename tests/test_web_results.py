"""/results renders only from web/results.json: with the stub it says "Not
measured yet." and shows no number; with a sample file every number in the
text of the page is a value of the file."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from reticula_mcp.web import results

SAMPLE: dict[str, Any] = {
    "status": "measured",
    "commit": "abc1234",
    "date": "2026-11-27",
    "setting": "chat application system prompt; arm reticula adds the standing line",
    "notes": "synthetic",
    "scoring": "An answer is right when it names the fact the question asks for.",
    "models": ["claude-haiku-5-5", "claude-sonnet-5-5"],
    "sizes": [66, 1000, 10000],
    "arms": [
        {"id": "reticula", "label": "Reticula connector, standing line"},
        {"id": "pasted", "label": "All notes pasted into the prompt"},
        {"id": "none", "label": "No memory"},
    ],
    "questions": [{"id": "q1", "kind": "exact", "text": "When was sample 14 fired?"}],
    "cells": [],
}


def _fill() -> dict[str, Any]:
    data = json.loads(json.dumps(SAMPLE))
    n = 0
    for model in data["models"]:
        for arm in data["arms"]:
            for size in data["sizes"]:
                n += 1
                if arm["id"] == "pasted" and size == 10000:
                    data["cells"].append(
                        {"arm": "pasted", "size": size, "model": model, "fits": False}
                    )
                    continue
                data["cells"].append(
                    {
                        "arm": arm["id"],
                        "size": size,
                        "model": model,
                        "fits": True,
                        "runs": 12 + n,
                        "right_share": round(0.5 + n / 100, 2),
                        "median": {
                            "input_tokens": 1000 * n + 7,
                            "output_tokens": 30 + n,
                            "tool_calls": 3,
                            "reticula_reply_tokens": 400 + n,
                            "cost_usd": 0.0012,
                        },
                    }
                )
    return data


def _numbers_in_text(html: str) -> set[str]:
    """Numbers in what a reader sees: tags (and SVG coordinates) removed,
    thousands separators dropped."""
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def _numbers_in(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= _numbers_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= _numbers_in(v)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        found.add(str(value))
    elif isinstance(value, str):
        found |= set(re.findall(r"\d+(?:\.\d+)?", value))
    return found


STUB = '{"status": "not measured yet", "notes": "synthetic", "cells": []}'


def test_the_stub_says_not_measured_and_shows_no_number() -> None:
    data = results.load(STUB)
    body = results.body(data)
    assert "Not measured yet." in body
    assert _numbers_in_text(body) == set()


def test_the_shipped_file_loads_and_every_number_is_from_it() -> None:
    """Whatever RETICULA4 has written into web/results.json."""
    data = results.load()
    assert data.get("status") in ("measured", "not measured yet")
    body = results.body(data)
    extra = _numbers_in_text(body) - _numbers_in(data)
    assert not extra, sorted(extra)


def test_a_sample_file_renders_and_every_number_is_from_the_file() -> None:
    data = _fill()
    body = results.body(data)
    assert body.count("<svg") == 2  # one chart per model
    assert (
        body.count("does not fit") == 2 * 2
    )  # the chart label and the table, per model
    assert "Not measured yet" not in body
    assert "abc1234" in body and "2026-11-27" in body
    assert "The notes are synthetic." in body
    assert data["scoring"] in body
    assert "<script" not in body
    extra = _numbers_in_text(body) - _numbers_in(data)
    assert not extra, sorted(extra)


def test_a_cell_that_does_not_fit_is_a_label_not_a_point() -> None:
    data = _fill()
    chart = results._chart(data, "claude-haiku-5-5")
    # pasted has points at two sizes and a label at the third
    assert chart.count(">does not fit</text>") == 1
    assert chart.count("<circle") == 3 * 3 - 1


def test_a_file_not_marked_measured_shows_no_number() -> None:
    data = _fill()
    data["status"] = "not measured yet"
    assert "Not measured yet." in results.body(data)
