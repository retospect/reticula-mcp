"""The one page: sign in or create an account. Plain HTML, no scripts, no assets.

Works on a phone (viewport meta, fluid column, large tap targets) and without
JavaScript (the two modes are two links to the same URL). Everything dynamic is
HTML-escaped; there is no inline script and the only inline style is the
constant below, allowed by hash in the Content-Security-Policy.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
from html import escape
from urllib.parse import quote, urlsplit

from starlette.responses import HTMLResponse

from reticula_mcp import refusal

STYLE = """
:root{color-scheme:light dark;--fg:#1b1f23;--bg:#fff;--mut:#59636e;--line:#c9d1d9;\
--acc:#0b57d0;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--bg:#0d1117;--mut:#9aa4af;\
--line:#30363d;--acc:#7aa7ff;--err:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);\
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:26rem;margin:0 auto;padding:1.5rem 1rem 3rem}
h1{font-size:1.4rem;margin:.2rem 0 1rem}
p{margin:.6rem 0}
.mut{color:var(--mut);font-size:.9rem}
nav{display:flex;gap:.5rem;margin:1.2rem 0 1rem}
nav a{flex:1;text-align:center;padding:.6rem;border:1px solid var(--line);\
border-radius:.5rem;color:var(--fg);text-decoration:none}
nav a[aria-current=page]{border-color:var(--acc);color:var(--acc);font-weight:600}
label{display:block;margin:.9rem 0 .25rem;font-weight:600}
input[type=email],input[type=password],input[type=text]{width:100%;\
padding:.7rem;font:inherit;color:inherit;background:transparent;\
border:1px solid var(--line);border-radius:.5rem}
input:focus{outline:2px solid var(--acc);outline-offset:1px}
button{width:100%;margin-top:1.2rem;padding:.8rem;font:inherit;font-weight:600;\
color:#fff;background:var(--acc);border:0;border-radius:.5rem;cursor:pointer}
.err{color:var(--err);border:1px solid var(--err);border-radius:.5rem;\
padding:.6rem .8rem;margin:1rem 0}
code{font-size:.9em;word-break:break-all}
.warn{border:2px solid var(--err);border-radius:.5rem;padding:.7rem .9rem;margin:1rem 0}
.warn strong{color:var(--err)}
"""

_STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()

#: No scripts, no images, no fonts, no framing. ``form-action`` is left out on
#: purpose: Chrome applies it to the redirect that follows a form POST, so
#: ``form-action 'self'`` would block the redirect back to the assistant.
CSP = (
    f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

HEADERS = {
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "interest-cohort=()",
}

MESSAGES = {
    "bad_credentials": "That e-mail and password do not match an account.",
    "throttled": "Too many attempts. Wait a few minutes and try again.",
    "closed": "New accounts are not open on this instance. Ask its owner for one.",
    "signup_rate": "Too many accounts were created from your network. Try again later.",
    "expired": "This page has expired. Reload it and try again.",
    "generic": "Something went wrong. Try again.",
}


def _shell(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def respond(html: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(html, status_code=status, headers=HEADERS)


def gone_page() -> HTMLResponse:
    """The parked request is unknown, used or expired."""
    refusal.note("signin: link_gone")
    return respond(
        _shell(
            "Link expired",
            "<h1>Reticula</h1><p>This sign-in link has expired or was already used.</p>"
            '<p class="mut">Go back to your assistant and connect again.</p>',
        ),
        400,
    )


def expired_page(handle: str) -> HTMLResponse:
    """CSRF check failed: ask for a reload (a fresh GET sets a fresh token)."""
    link = f"/signin?req={quote(handle, safe='')}"
    refusal.note("signin: page_expired")
    return respond(
        _shell(
            "Reload",
            "<h1>Reticula</h1>"
            f'<p class="err" role="alert">{escape(MESSAGES["expired"])}</p>'
            f'<p><a href="{escape(link)}">Reload</a></p>',
        ),
        403,
    )


def sign_in_page(
    *,
    handle: str,
    csrf: str,
    mode: str,
    client_name: str,
    redirect_uri: str,
    signups_open: bool,
    known_hosts: tuple[str, ...] = (),
    error: str | None = None,
    email: str = "",
    handle_value: str = "",
    status: int = 200,
) -> HTMLResponse:
    signup = mode == "signup"
    q = quote(handle, safe="")
    tabs = (
        f'<nav aria-label="Mode">'
        f'<a href="/signin?req={q}&amp;mode=signin"'
        f"{' aria-current=page' if not signup else ''}>Sign in</a>"
        f'<a href="/signin?req={q}&amp;mode=signup"'
        f"{' aria-current=page' if signup else ''}>Create account</a></nav>"
    )
    banner = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
    if signup and not signups_open:
        form = (
            f'<p class="err" role="alert">{escape(MESSAGES["closed"])}</p>'
            '<p class="mut">If you already have an account, use Sign in.</p>'
        )
    else:
        extra = (
            '<label for="handle">Handle (optional)</label>'
            '<input id="handle" name="handle" type="text" maxlength="32" '
            'autocomplete="nickname" autocapitalize="none" spellcheck="false" '
            f'value="{escape(handle_value)}">'
            '<p class="mut">Letters, digits, - and _ (3-32). Chosen for you if left '
            "empty.</p>"
            if signup
            else ""
        )
        hint = '<p class="mut">At least 10 characters.</p>' if signup else ""
        form = (
            '<form method="post" action="/signin">'
            f'<input type="hidden" name="req" value="{escape(handle)}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            f'<input type="hidden" name="mode" value="{"signup" if signup else "signin"}">'
            '<label for="email">E-mail</label>'
            '<input id="email" name="email" type="email" required maxlength="254" '
            'autocomplete="username" autocapitalize="none" spellcheck="false" '
            f'value="{escape(email)}">'
            '<label for="password">Password</label>'
            '<input id="password" name="password" type="password" required '
            f'maxlength="1024"{" minlength=10" if signup else ""} '
            f'autocomplete="{"new-password" if signup else "current-password"}">'
            f"{hint}{extra}"
            f'<button type="submit">{"Create account and connect" if signup else "Sign in and connect"}</button>'
            "</form>"
        )
    who, warning = _receiver(redirect_uri, known_hosts)
    body = (
        "<h1>Reticula</h1>"
        f"<p>After you sign in, {who} gets access to your notes.</p>"
        f'<p class="mut">It calls itself \u201c{escape(client_name)}\u201d (that name '
        "is chosen by the application, not checked by this server).</p>"
        f"{warning}"
        '<p class="mut">You should only see this page right after you add this '
        "server to your assistant.</p>"
        f"{tabs}{banner}{form}"
        '<p class="mut">You can revoke the connection later.</p>'
    )
    return respond(_shell("Connect to Reticula", body), status)


def _receiver(redirect_uri: str, known_hosts: tuple[str, ...]) -> tuple[str, str]:
    """Who gets access (HTML) and a warning block when it is an unfamiliar host.

    The redirect URI is where the code goes, so its host is the one fact about
    the receiver that a registering client cannot misstate.
    """
    parts = urlsplit(redirect_uri)
    host = (parts.hostname or "").lower()
    if _is_loopback(host):
        port = f" (port {parts.port})" if parts.port else ""
        return f"<strong>an application on this computer</strong>{port}", ""
    who = f"<strong>{escape(host)}</strong>"
    if host in known_hosts:
        return who, ""
    warning = (
        '<div class="warn" role="alert"><p><strong>Unfamiliar address.</strong> '
        f"This server does not know {escape(host)}. Continue only if you yourself "
        "started this connection from your assistant a moment ago, and never "
        "because of a link someone sent you.</p></div>"
    )
    return who, warning


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
