"""/results, "How well it works": rendered only from web/results.json, which
RETICULA4 writes from its benchmark (arms: Reticula, all notes pasted, no
memory; memory sizes; models).

Every number on the page is a value of the file, printed as it is there
(integers with thousands separators). The charts have no numeric axis: each
point carries its own value, and a cell that does not fit is a label, not a
point. With the stub (status "not measured yet") the page says so and shows
no number.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from reticula_mcp.web.html import e

MEASURED = "measured"
# one colour and dash per arm, in the order of the file
_ARM_STYLE = (
    ("#2B7A78", ""),
    ("#A34E15", ' stroke-dasharray="10 6"'),
    ("#4A5568", ' stroke-dasharray="3 5"'),
)


def load(text: str | None = None) -> dict[str, Any]:
    if text is None:
        text = (resources.files("reticula_mcp.web") / "results.json").read_text(
            encoding="utf-8"
        )
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _num(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return e(value)


def _cell(
    data: dict[str, Any], arm: str, size: Any, model: str
) -> dict[str, Any] | None:
    for c in data.get("cells", []):
        if c.get("arm") == arm and c.get("size") == size and c.get("model") == model:
            return c
    return None


def _tokens(cell: dict[str, Any]) -> list[Any] | None:
    median = cell.get("median") or {}
    if "input_tokens" not in median or "output_tokens" not in median:
        return None
    return [median["input_tokens"], median["output_tokens"]]


def _chart(data: dict[str, Any], model: str) -> str:
    """Tokens per question by memory size, one line per arm, for one model.
    The height of a point is input + output tokens; its label says both."""
    sizes = data.get("sizes", [])
    arms = data.get("arms", [])
    width, height, left, top, bottom = 900, 420, 60, 30, 70
    if not sizes:
        return ""
    step = (width - left - 40) / max(len(sizes) - 1, 1)
    totals = []
    for arm in arms:
        for size in sizes:
            c = _cell(data, arm["id"], size, model)
            t = _tokens(c) if c and c.get("fits", True) else None
            if t:
                totals.append(t[0] + t[1])
    top_total = max(totals) if totals else 1
    parts = [
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - 20}"'
        f' y2="{height - bottom}" stroke="#C9C4B6" stroke-width="2"/>'
    ]
    for i, size in enumerate(sizes):
        x = left + i * step
        parts.append(
            f'<text x="{x}" y="{height - bottom + 30}" text-anchor="middle"'
            f' font-size="18" fill="#4A5568">{_num(size)} notes</text>'
        )
    legend_y = height - 14
    for n, arm in enumerate(arms):
        colour, dash = _ARM_STYLE[n % len(_ARM_STYLE)]
        points = []
        for i, size in enumerate(sizes):
            x = left + i * step
            c = _cell(data, arm["id"], size, model)
            if c is not None and c.get("fits") is False:
                parts.append(
                    f'<text x="{x}" y="{top + 20 + n * 24}" text-anchor="middle"'
                    f' font-size="16" fill="{colour}">does not fit</text>'
                )
                continue
            t = _tokens(c) if c else None
            if not t:
                continue
            y = (height - bottom) - (t[0] + t[1]) / top_total * (
                height - bottom - top - 40
            )
            points.append((x, y))
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{colour}"/>')
            parts.append(
                f'<text x="{x + 8:.1f}" y="{y - 8:.1f}" font-size="15" fill="{colour}">'
                f"{_num(t[0])} in, {_num(t[1])} out</text>"
            )
        if len(points) > 1:
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            parts.append(
                f'<polyline points="{d}" fill="none" stroke="{colour}"'
                f' stroke-width="3"{dash}/>'
            )
        lx = left + n * 280
        parts.append(
            f'<line x1="{lx}" y1="{legend_y - 6}" x2="{lx + 30}" y2="{legend_y - 6}"'
            f' stroke="{colour}" stroke-width="3"{dash}/>'
            f'<text x="{lx + 38}" y="{legend_y}" font-size="16" fill="#13212E">'
            f"{e(arm.get('label', arm['id']))}</text>"
        )
    title = f"Tokens per question by memory size, {model}"
    return (
        f'<svg class="diagram" viewBox="0 0 {width} {height}" role="img"'
        f' aria-label="{e(title)}" font-family="Arial, sans-serif">'
        f"<title>{e(title)}</title>{''.join(parts)}</svg>"
    )


def _table(data: dict[str, Any]) -> str:
    """Right-answer share per arm and model, a column per memory size."""
    sizes = data.get("sizes", [])
    head = "".join(f"<th>{_num(s)} notes</th>" for s in sizes)
    rows = []
    for model in data.get("models", []):
        for arm in data.get("arms", []):
            cells = []
            for size in sizes:
                c = _cell(data, arm["id"], size, model)
                if c is None:
                    cells.append("<td>not run</td>")
                elif c.get("fits") is False:
                    cells.append("<td>does not fit</td>")
                else:
                    cells.append(
                        f"<td>{_num(c.get('right_share', ''))}"
                        f' <span class="small">({_num(c.get("runs", ""))} runs)</span></td>'
                    )
            rows.append(
                f"<tr><th>{e(model)}</th><th>{e(arm.get('label', arm['id']))}</th>"
                f"{''.join(cells)}</tr>"
            )
    return (
        '<table class="results"><thead><tr><th>Model</th><th>Memory</th>'
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def body(data: dict[str, Any] | None = None) -> str:
    data = load() if data is None else data
    intro = (
        "<h1>How well it works</h1>\n"
        "<p>A benchmark of the same questions asked with different memory: the"
        " Reticula connector, all notes pasted into the prompt, and no"
        " memory.</p>\n"
    )
    if data.get("status") != MEASURED or not data.get("cells"):
        return intro + "<p>Not measured yet.</p>"
    charts = "".join(
        f"<h2>Tokens per question, {e(m)}</h2>{_chart(data, m)}"
        for m in data.get("models", [])
    )
    about = [
        f"Measured on commit <code>{e(data.get('commit', ''))}</code> on"
        f" {e(data.get('date', ''))}. The number of runs of each cell is in the"
        " table.",
        "Each point is the median over runs of the tokens the model read (in)"
        " and wrote (out) for one question.",
    ]
    if data.get("notes") == "synthetic":
        about.append("The notes are synthetic.")
    if data.get("scoring"):
        about.append(e(data["scoring"]))
    if data.get("setting"):
        about.append(f"Setting: {e(data['setting'])}.")
    return (
        intro
        + charts
        + "<h2>Right answers</h2>"
        + "<p>The share of answers judged right, per model, memory and size.</p>"
        + _table(data)
        + '<p class="small">'
        + " ".join(about)
        + "</p>"
    )
