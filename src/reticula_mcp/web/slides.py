"""/slides: the four slides of the deck (memory, graph, calls, sharing) as one
page. The deck's diagrams were absolutely placed boxes joined by connector
elements; here they are inline SVG with the same boxes, labels and lines.
Each slide's Mermaid source is under it in a <details>. No script."""

from __future__ import annotations

from reticula_mcp.web.html import HOME, e

# box styles: fill, stroke, text colour
_STYLE = {
    "client": ("#1E3344", "#6FC2BF", "#F6F4EE"),
    "hub": ("#2B7A78", "none", "#F6F4EE"),
    "store": ("#ECE9E0", "none", "#13212E"),
    "note": ("#FBFAF6", "#C9C4B6", "#13212E"),
    "call": ("#FBFAF6", "#C9C4B6", "#13212E"),
}
_LINE = {
    "flow": ("#6FC2BF", ""),
    "part": ("#6B7280", ""),
    "rel": ("#A34E15", ' stroke-dasharray="12 8"'),
    "teal": ("#2B7A78", ""),
}


def _box(
    x: int, y: int, w: int, h: int, kind: str, lines: list[tuple[str, bool]]
) -> str:
    """A box with lines of text, centred; (text, bold) per line."""
    fill, stroke, colour = _STYLE[kind]
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}"'
        f' stroke="{stroke}" stroke-width="2"/>'
    ]
    step = 32
    top = y + h / 2 - (len(lines) - 1) * step / 2 + 9
    for n, (text, bold) in enumerate(lines):
        weight = ' font-weight="600"' if bold else ""
        out.append(
            f'<text x="{x + w / 2}" y="{top + n * step}" text-anchor="middle"'
            f' fill="{colour}" font-size="{26 if bold else 23}"{weight}>{e(text)}</text>'
        )
    return "".join(out)


def _line(points: list[tuple[int, int]], kind: str, *, head: str = "end") -> str:
    colour, dash = _LINE[kind]
    d = " ".join(f"{x},{y}" for x, y in points)
    marker = {
        "end": f' marker-end="url(#a-{kind})"',
        "both": f' marker-end="url(#a-{kind})" marker-start="url(#s-{kind})"',
        "none": "",
    }[head]
    return (
        f'<polyline points="{d}" fill="none" stroke="{colour}" stroke-width="3"'
        f"{dash}{marker}/>"
    )


def _defs() -> str:
    marks = []
    for kind, (colour, _) in _LINE.items():
        marks.append(
            f'<marker id="a-{kind}" viewBox="0 0 10 10" refX="9" refY="5"'
            ' markerWidth="7" markerHeight="7" orient="auto">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
            f'<marker id="s-{kind}" viewBox="0 0 10 10" refX="1" refY="5"'
            ' markerWidth="7" markerHeight="7" orient="auto">'
            f'<path d="M10,0 L0,5 L10,10 z" fill="{colour}"/></marker>'
        )
    return f"<defs>{''.join(marks)}</defs>"


def _svg(w: int, h: int, title: str, parts: list[str]) -> str:
    return (
        f'<svg class="diagram" viewBox="-4 -4 {w + 8} {h + 8}" role="img"'
        f' aria-label="{e(title)}" font-family="Arial, sans-serif">'
        f"<title>{e(title)}</title>{_defs()}{''.join(parts)}</svg>"
    )


def _label(x: int, y: int, text: str, colour: str) -> str:
    return (
        f'<text x="{x}" y="{y}" text-anchor="middle" fill="{colour}"'
        f' font-size="24" font-weight="600">{e(text)}</text>'
    )


def _mermaid(source: str) -> str:
    return (
        "<details><summary>Mermaid source of the diagram</summary>"
        f"<pre><code>{e(source.strip())}</code></pre></details>"
    )


MEMORY_SVG = _svg(
    800,
    540,
    "Three kinds of client connect to your Reticula server, which keeps your notes",
    [
        _box(0, 0, 240, 110, "client", [("Claude Code", False)]),
        _box(
            280,
            0,
            240,
            110,
            "client",
            [("claude.ai and the", False), ("Claude apps", False)],
        ),
        _box(560, 0, 240, 110, "client", [("Any MCP client", False)]),
        _box(
            140,
            200,
            520,
            140,
            "hub",
            [
                ("Your Reticula server", True),
                ("MCP tools, sign-in, search", False),
                ("by words and by meaning", False),
            ],
        ),
        _box(
            140,
            400,
            520,
            140,
            "store",
            [
                ("Your notes", True),
                ("PostgreSQL with pgvector;", False),
                ("one dump is the whole backup", False),
            ],
        ),
        _line([(120, 110), (120, 150)], "flow", head="none"),
        _line([(400, 110), (400, 150)], "flow", head="none"),
        _line([(680, 110), (680, 150)], "flow", head="none"),
        _line([(120, 150), (680, 150)], "flow", head="none"),
        _line([(400, 150), (400, 198)], "flow"),
        _line([(400, 340), (400, 398)], "flow"),
    ],
)

GRAPH_SVG = _svg(
    1220,
    680,
    "Two hubs with notes filed in them, and typed links between notes",
    [
        _box(
            0, 0, 520, 100, "hub", [("Hub: Lab notebook", True), ("yours alone", False)]
        ),
        _box(
            720,
            0,
            500,
            100,
            "hub",
            [("Hub: AIxMAT 2026", True), ("shared with 12 people", False)],
        ),
        _box(0, 260, 240, 120, "hub", [("Hub: Furnace", True)]),
        _box(
            280,
            260,
            240,
            120,
            "note",
            [("Sample 14 sintered", False), ("at 1222 °C", False)],
        ),
        _box(
            720,
            260,
            240,
            120,
            "note",
            [("Talk: active learning", False), ("for oxides", False)],
        ),
        _box(
            980,
            260,
            240,
            120,
            "note",
            [("Panel: labs share", False), ("one memory", False)],
        ),
        _box(
            0,
            560,
            240,
            120,
            "note",
            [("Thermocouple reads", False), ("8 °C low", False)],
        ),
        _line([(260, 100), (260, 236)], "part", head="none"),
        _line([(120, 236), (400, 236)], "part", head="none"),
        _line([(120, 236), (120, 258)], "part"),
        _line([(400, 236), (400, 258)], "part"),
        _line([(970, 100), (970, 236)], "part", head="none"),
        _line([(840, 236), (1100, 236)], "part", head="none"),
        _line([(840, 236), (840, 258)], "part"),
        _line([(1100, 236), (1100, 258)], "part"),
        _line([(120, 380), (120, 558)], "part"),
        _line([(240, 620), (400, 620), (400, 382)], "rel"),
        _line([(520, 320), (718, 320)], "rel"),
        _label(492, 500, "qualifies", "#A34E15"),
        _label(620, 304, "supports", "#A34E15"),
    ],
)


def _call(x: int, tool: str, arg: str, desc: list[str]) -> str:
    fill, stroke, colour = _STYLE["call"]
    parts = [
        f'<rect x="{x}" y="0" width="296" height="320" rx="12" fill="{fill}"'
        f' stroke="{stroke}" stroke-width="2"/>',
        f'<text x="{x + 24}" y="56" fill="#2B7A78" font-size="32" font-weight="700"'
        f' font-family="Courier New, monospace">{e(tool)}</text>',
        f'<text x="{x + 24}" y="100" fill="#4A5568" font-size="21"'
        f' font-family="Courier New, monospace">{e(arg)}</text>',
    ]
    for n, line in enumerate(desc):
        parts.append(
            f'<text x="{x + 24}" y="{150 + n * 30}" fill="{colour}" font-size="22">'
            f"{e(line)}</text>"
        )
    return "".join(parts)


CALLS_SVG = _svg(
    1664,
    460,
    "The calls of a conversation: start, search, get, put, link, and back to start",
    [
        _call(
            0,
            "start",
            "start()",
            ["What Reticula is for,", "the person's hubs,", "and what is new."],
        ),
        _call(
            342,
            "search",
            'query="furnace"',
            ["By keywords and by", "meaning. One line per", "note, short id first."],
        ),
        _call(
            684,
            "get",
            'ref="542b1dbc", depth=2',
            ["The note, its hub path,", "and notes one or two", "link steps out."],
        ),
        _call(
            1026,
            "put",
            "text=…, hub=…",
            ["Save a note without", "being asked; say in one", "line what was saved."],
        ),
        _call(
            1368,
            "link",
            'rel="qualifies"',
            ["Say how two notes", "relate, with one of", "seven names."],
        ),
        _line([(296, 160), (340, 160)], "teal"),
        _line([(638, 160), (682, 160)], "teal"),
        _line([(980, 160), (1024, 160)], "teal"),
        _line([(1322, 160), (1366, 160)], "teal"),
        _line([(1516, 320), (1516, 400), (148, 400), (148, 322)], "teal"),
        f'<text x="832" y="446" text-anchor="middle" fill="#4A5568" font-size="23">'
        f"{e('The next conversation, on any model, begins with start again and finds the same notes.')}"
        "</text>",
    ],
)

SHARING_SVG = _svg(
    880,
    300,
    "Two servers pull each other's signed feeds, and both pull from an always-on peer",
    [
        _box(
            0,
            0,
            300,
            100,
            "note",
            [("Ada's server", True), ("Ada's notes, signed", False)],
        ),
        _box(
            580,
            0,
            300,
            100,
            "note",
            [("Ben's server", True), ("Ben's notes, signed", False)],
        ),
        _box(
            290,
            200,
            300,
            100,
            "hub",
            [("An always-on peer", True), ("any member's server", False)],
        ),
        _line([(302, 50), (578, 50)], "teal", head="both"),
        _line([(150, 102), (150, 250), (288, 250)], "teal", head="both"),
        _line([(730, 102), (730, 250), (592, 250)], "teal", head="both"),
        _label(440, 36, "pull signed feeds", "#2B7A78"),
    ],
)


def body() -> str:
    home = f'<p class="small"><a href="{HOME}">reticula.to</a></p>'
    return f"""
<h1>Slides</h1>
<p>Four slides on what Reticula is and how it works.</p>

<section class="slide dark" id="memory">
<p class="eyebrow">RETICULA MCP</p>
<h2>One memory that belongs to you</h2>
<p>Notes live on a server you run or choose, not inside the assistant.</p>
<p>Any assistant that speaks MCP reads the same notes. Change the model or the
service and keep what it knew.</p>
<p>The assistant saves decisions, facts and results without being asked, and
says in one line what it saved.</p>
<p>Every note is signed (Ed25519) and has a permanent address that names the
person, not the server.</p>
{MEMORY_SVG}
{home}
{
        _mermaid('''
flowchart TB
  A[Claude Code] --> S
  B[claude.ai and the Claude apps] --> S
  C[Any MCP client] --> S
  S[Your Reticula server] --> D[(Your notes, PostgreSQL)]
''')
    }
</section>

<section class="slide" id="graph">
<h2>Hubs hold notes; links say how notes relate</h2>
{GRAPH_SVG}
<h3>Filed in a hub</h3>
<p>A note is filed in one hub or several. A hub can sit inside another hub.</p>
<h3>Linked</h3>
<p>supports, explains, qualifies, generalises, derived-from, cites,
related-to. Links cross hubs and may run in a circle.</p>
<h3>Hubs never in a circle</h3>
<p>Filing Lab notebook under Furnace is refused: "that would put a hub inside
itself".</p>
<p class="small">Solid grey arrow: filed in. Dashed orange arrow: a typed link,
read as a sentence: the thermocouple note qualifies sample 14.</p>
{home}
{
        _mermaid('''
flowchart TB
  H1[Hub: Lab notebook] --> F[Hub: Furnace]
  H1 --> N1[Sample 14 sintered at 1222 °C]
  F --> N2[Thermocouple reads 8 °C low]
  H2[Hub: AIxMAT 2026, shared] --> N3[Talk: active learning for oxides]
  H2 --> N4[Panel: labs share one memory]
  N2 -. qualifies .-> N1
  N1 -. supports .-> N3
''')
    }
</section>

<section class="slide alt" id="calls">
<h2>A conversation is a handful of calls</h2>
{CALLS_SVG}
<p class="small">What <b>put</b> answers, short id and title first:</p>
<pre class="reply"><code>saved note 7c1e09aa "Sample 15 sintered at 1230 °C" (ret:…/…7c1e09aa) in hub 23b6d67b "Lab notebook" (ret:…/…23b6d67b)</code></pre>
{home}
{
        _mermaid('''
flowchart LR
  start --> search --> get --> put --> link
  link -. next conversation, any model .-> start
''')
    }
</section>

<section class="slide" id="sharing">
<h2>Sharing today, federation next</h2>
<h3>Today: one server</h3>
<p>A hub is shared person by person: <b>add</b> lets them read, add notes and
file corrections; <b>read</b> only reads; <b>none</b> takes it back from the
next call.</p>
<p>A correction is your own new note that supersedes the old one. Nobody edits
another person's note.</p>
<p>Search, get and links show only the notes you may read.</p>
<p class="problem">The problem: everyone needs an account on the same server.
The 12 AIxMAT participants sign in to one instance, and it is down when that
machine is.</p>
<h3>Next: federation, designed and not built</h3>
{SHARING_SVG}
<p class="small">Each server publishes its people's signed notes, and peers
pull what is new. There is no master copy: the signature, not the host, proves
who wrote a note.</p>
<p class="small">Any member's server can be the always-on peer, so a hub stays
reachable when a laptop is off. Each server embeds the notes it holds with its
own model.</p>
<p class="small">Every note has carried its address and signature since phase
one, so federation will not rewrite notes. ActivityPub may announce new public
notes; it is not the data layer.</p>
{home}
{
        _mermaid('''
flowchart LR
  A[Ada's server] <-- pull signed feeds --> B[Ben's server]
  A <--> C[An always-on peer]
  B <--> C
''')
    }
</section>
"""
