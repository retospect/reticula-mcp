"""The frame of every public page, and the headers it is sent with."""

from __future__ import annotations

from html import escape

from starlette.responses import HTMLResponse, Response

#: No script, no frame, no form, nothing from elsewhere; styles only from
#: /static/site.css. Inline SVG is markup, not a resource, and needs nothing.
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none';"
    " form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
REPO = "https://github.com/retospect/reticula-mcp"
HOME = "https://reticula.to"

NAV = (
    ("/", "Reticula"),
    ("/slides", "Slides"),
    ("/hubs", "Public hubs"),
    ("/connect", "Connect"),
    ("/vision", "Vision"),
    ("/privacy", "Privacy"),
    (REPO, "Source on GitHub"),
    (HOME, "reticula.to"),
)


def e(text: object) -> str:
    """Escaped for text and for attribute values in double quotes."""
    return escape(str(text), quote=True)


def page(title: str, body: str, *, path: str) -> str:
    """A whole page. ``body`` is HTML this package wrote; anything from the
    database in it was escaped with :func:`e` where it was put in."""
    links = " ".join(
        f'<a href="{e(href)}"{' aria-current="page"' if href == path else ""}>'
        f"{e(label)}</a>"
        for href, label in NAV
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{e(title)}</title>\n"
        '<link rel="stylesheet" href="/static/site.css">\n</head>\n<body>\n'
        f"<nav>{links}</nav>\n<main>\n{body}\n</main>\n"
        f'<footer><p><a href="{HOME}">reticula.to</a> · '
        f'<a href="{REPO}">source</a> · AGPL-3.0-or-later</p></footer>\n'
        "</body>\n</html>\n"
    )


def respond(text: str, *, cache_seconds: int = 300, status: int = 200) -> Response:
    headers = dict(HEADERS)
    headers["Cache-Control"] = f"public, max-age={cache_seconds}"
    return HTMLResponse(text, status_code=status, headers=headers)
