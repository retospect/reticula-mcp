"""The account pages: sign in on the web, see and revoke connections, change
settings, export, delete the account. Plain HTML, no JavaScript.

Sessions are websession.py. Every POST needs:

- an Origin (or, without one, a Referer) of this server's public address;
- the form token: of the session (derived from its cookie), or before sign-in
  the nonce of a cookie set with the form (double submit);
- the session cookie is SameSite=Strict, HttpOnly, and Secure on https.

Passwords are checked by signin.check_password, inside signin.hash_slots and
after signin.email_delay: the same counters and limits as the connector's
sign-in page. Export and revoke need the password again within
websession.REAUTH_WINDOW; delete always needs it, and the handle typed.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from reticula_mcp import account_admin, accounts, refusal
from reticula_mcp import settings as account_settings
from reticula_mcp.accounts import Account
from reticula_mcp.auth import config, grants, page, signin, throttle, websession
from reticula_mcp.auth.netaddr import caller_bucket
from reticula_mcp.auth.web import read_limited
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database
from reticula_mcp.errors import ReticulaError
from reticula_mcp.web.html import e
from reticula_mcp.web.html import page as frame

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none';"
    " form-action 'self'; frame-ancestors 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
FORM_COOKIE = "reticula_account_form"
MAX_FORM = 8 * 1024


def _respond(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        frame(f"{title} · Reticula", body, path="/account"),
        status_code=status,
        headers=HEADERS,
    )


def _see(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303, headers=HEADERS)


def _form(raw: bytes) -> dict[str, str] | None:
    return signin._form(raw)  # the size, field-count and NUL rules of /signin


#: One export per account in this time, in seconds.
EXPORT_EVERY = 600
EXPORT_KIND = "export"
HSTS = "max-age=31536000"


def _origin_of(url: str) -> str | None:
    """scheme://host[:port] of ``url``, lowercase, the default port left out:
    how two origins are compared."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def routes(settings: Settings, db: Database) -> list[Route]:
    secure = settings.base_url.startswith("https://")
    cookie_name = "__Host-reticula_session" if secure else "reticula_session"
    origin = _origin_of(settings.base_url)

    def same_origin(request: Request) -> bool:
        sent = request.headers.get("origin")
        if sent is None:
            sent = request.headers.get("referer")
        return sent is not None and _origin_of(sent) == origin

    def set_session(response: Response, token: str, keep: bool) -> None:
        response.set_cookie(
            cookie_name,
            token,
            max_age=int(websession.KEEP.total_seconds()) if keep else None,
            path="/",
            secure=secure,
            httponly=True,
            samesite="strict",
        )

    def clear_session(response: Response) -> None:
        response.delete_cookie(
            cookie_name, path="/", secure=secure, httponly=True, samesite="strict"
        )

    # ------------------------------------------------------------ helpers

    async def body_of(request: Request) -> dict[str, str] | None:
        raw = await read_limited(request, MAX_FORM)
        if (
            raw is None
            or "application/x-www-form-urlencoded"
            not in request.headers.get("content-type", "")
        ):
            return None
        return _form(raw)

    def load(token: str | None) -> tuple[websession.Session, Account] | None:
        with db.connection() as conn:
            session = websession.load(conn, token)
            if session is None:
                conn.commit()
                return None
            account = accounts.get_account(conn, session.account_id)
            conn.commit()
        if account is None or account.status not in ("active", "provisional"):
            return None
        return session, account

    def checked(
        request: Request, form: dict[str, str] | None
    ) -> tuple[websession.Session, Account, str] | Response:
        """The session of a POST whose origin and form token are right."""
        if not same_origin(request):
            refusal.note("account: origin")
            return _respond(
                "Refused", "<p>Refused: this form came from elsewhere.</p>", 403
            )
        token = request.cookies.get(cookie_name)
        found = load(token)
        if found is None or token is None:
            return _see("/account/signin")
        if form is None or not websession.csrf_ok(token, form.get("csrf", "")):
            refusal.note("account: csrf")
            return _respond(
                "Expired",
                '<p>This form has expired. <a href="/account">Back to your account</a>.</p>',
                403,
            )
        return found[0], found[1], token

    async def password_ok(
        request: Request, account: Account, password: str
    ) -> Account | str:
        """The shared check: delay, hash slot, counters of /signin."""
        delay = await anyio.to_thread.run_sync(signin.email_delay, db, account.email)
        if delay:
            await signin._wait(delay)
        bucket = caller_bucket(request, settings)

        def run() -> Account | str:
            with signin.hash_slots, db.connection() as conn:
                found = signin.check_password(conn, account.email, password, bucket)
                conn.commit()
                return found

        return await anyio.to_thread.run_sync(run)

    def csrf_field(token: str) -> str:
        return (
            f'<input type="hidden" name="csrf" value="{e(websession.csrf_for(token))}">'
        )

    def password_field(session: websession.Session) -> str:
        if session.fresh_reauth:
            return ""
        return (
            '<p><label>Your password <input type="password" name="password"'
            ' autocomplete="current-password" required></label></p>'
        )

    # ------------------------------------------------------------ sign-in

    def signin_page(nonce: str, error: str | None = None, email: str = "") -> str:
        err = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
        return (
            "<h1>Sign in</h1>"
            f"{err}"
            '<form method="post" action="/account/signin">'
            f'<input type="hidden" name="form" value="{e(nonce)}">'
            '<p><label>E-mail <input type="email" name="email" autocomplete="username"'
            f' value="{e(email)}" required></label></p>'
            '<p><label>Password <input type="password" name="password"'
            ' autocomplete="current-password" required></label></p>'
            '<p><label><input type="checkbox" name="keep" value="yes"> Keep me'
            " signed in for 30 days</label></p>"
            "<p><button>Sign in</button></p></form>"
            '<p class="small">No account yet? Accounts are made when you connect an'
            ' assistant: see <a href="/connect">Connect</a>.</p>'
        )

    def form_cookie(response: Response, nonce: str) -> None:
        response.set_cookie(
            FORM_COOKIE,
            nonce,
            max_age=3600,
            path="/account",
            secure=secure,
            httponly=True,
            samesite="strict",
        )

    async def signin_get(request: Request) -> Response:
        nonce = secrets.token_urlsafe(24)
        response = _respond("Sign in", signin_page(nonce))
        form_cookie(response, nonce)
        return response

    async def signin_post(request: Request) -> Response:
        form = await body_of(request)
        nonce = request.cookies.get(FORM_COOKIE, "")
        if (
            not same_origin(request)
            or form is None
            or not nonce
            or not secrets.compare_digest(
                form.get("form", "").encode("utf-8", "replace"),
                nonce.encode("utf-8", "replace"),
            )
        ):
            refusal.note("account: signin_form")
            return _respond(
                "Expired",
                '<p>This page has expired. <a href="/account/signin">Sign in again</a>.</p>',
                403,
            )
        email = form.get("email", "").strip()[: config.MAX_EMAIL_LENGTH]
        password = form.get("password", "")
        keep = form.get("keep") == "yes"
        delay = await anyio.to_thread.run_sync(signin.email_delay, db, email)
        if delay:
            await signin._wait(delay)
        bucket = caller_bucket(request, settings)
        agent = "".join(
            c for c in request.headers.get("user-agent", "") if c.isprintable()
        )
        presented = request.cookies.get(cookie_name)

        def run() -> str | None:
            with signin.hash_slots, db.connection() as conn:
                found = signin.check_password(conn, email, password, bucket)
                if isinstance(found, str):
                    conn.commit()
                    known = (
                        accounts.get_account_by_email(conn, email) if email else None
                    )
                    if known is not None:
                        record(known.id, "web sign-in failed", "Failed")
                    return None if found == "bad_credentials" else found
                # the session this browser presented, if any, ends here
                if presented:
                    conn.execute(
                        "DELETE FROM web_session WHERE id_hash = %s",
                        (websession.digest(presented),),
                    )
                token = websession.create(
                    conn, found.id, keep=keep, agent=agent, bucket=bucket
                )
                conn.commit()
                record(found.id, "web sign-in")
                return "token:" + token

        result = await anyio.to_thread.run_sync(run)
        if result is None or not result.startswith("token:"):
            key = "throttled" if result == "throttled" else "bad_credentials"
            refusal.note(f"account: {key}")
            fresh = secrets.token_urlsafe(24)
            refused = _respond(
                "Sign in",
                signin_page(fresh, page.MESSAGES[key], email),
                429 if key == "throttled" else 401,
            )
            form_cookie(refused, fresh)
            return refused
        done = _see("/account")
        set_session(done, result[len("token:") :], keep)
        done.delete_cookie(FORM_COOKIE, path="/account")
        return done

    # ------------------------------------------------------------ the page

    def overview(session: websession.Session, account: Account, token: str) -> str:
        with db.connection() as conn:
            connections = [
                c for c in grants.list_connections(conn, account.id) if c.live
            ]
            tokens = [
                t
                for t in grants.list_operator_tokens(conn, account.id)
                if t.revoked_at is None
            ]
            listed = account_settings.listed(conn, account)
            sessions = websession.count(conn, account.id)
            activity = activity_lines(conn, account)
            conn.commit()
        csrf = csrf_field(token)
        until = f"{session.expires_at:%Y-%m-%d %H:%M} UTC"
        idle = "" if session.keep else ", or after 30 minutes without use"

        def used(c: grants.Connection) -> str:
            if c.last_used_at is None:
                return "not used yet"
            return f"last used {c.last_used_at:%Y-%m-%d %H:%M} UTC"

        rows = "".join(
            f'<li>{e(c.label)} <span class="small">connected'
            f" {c.created_at:%Y-%m-%d}, {used(c)}</span>"
            f' <a href="/account/revoke/connection/{c.id}">Revoke</a></li>'
            for c in connections
        )
        token_rows = "".join(
            f'<li>{e(t.label or "token")} <span class="small">made'
            f" {t.created_at:%Y-%m-%d}"
            + (
                f", expires {t.expires_at:%Y-%m-%d}"
                if t.expires_at
                else ", never expires"
            )
            + f'</span> <a href="/account/revoke/token/{t.id}">Revoke</a></li>'
            for t in tokens
        )
        activity_html = (
            "<h2>Activity</h2><ul>"
            + "".join(f"<li>{e(a)}</li>" for a in activity)
            + "</ul>"
            if activity
            else ""
        )
        return (
            f"<h1>@{e(account.handle)}</h1>"
            f"<p>E-mail: {e(account.email)} · person ID <code>{e(account.person_id)}</code></p>"
            f"<p>Signed in until {e(until)}{idle}.</p>"
            '<form method="post" action="/account/signout" class="inline">'
            f"{csrf}<button>Sign out</button></form> "
            '<form method="post" action="/account/signout-everywhere" class="inline">'
            f"{csrf}<button>Sign out everywhere ({sessions} signed in)</button></form>"
            "<h2>Connected assistants</h2>"
            + (f"<ul>{rows}</ul>" if rows else "<p>None.</p>")
            + "<h2>Tokens made on the server</h2>"
            + (f"<ul>{token_rows}</ul>" if token_rows else "<p>None.</p>")
            + "<h2>Settings</h2>"
            '<form method="post" action="/account/settings">'
            f"{csrf}<p>Listed in the directory of people:"
            f' <label><input type="radio" name="listed" value="yes"{" checked" if listed else ""}> yes</label>'
            f' <label><input type="radio" name="listed" value="no"{"" if listed else " checked"}> no</label>'
            "</p><p><button>Save</button></p></form>"
            "<h2>Your data</h2>"
            '<p><a href="/account/export">Export everything</a> ·'
            ' <a href="/account/delete">Delete the account</a></p>'
            f"{activity_html}"
        )

    async def account_get(request: Request) -> Response:
        token = request.cookies.get(cookie_name)
        found = await anyio.to_thread.run_sync(load, token)
        if found is None or token is None:
            return _see("/account/signin")
        session, account = found
        body = await anyio.to_thread.run_sync(overview, session, account, token)
        return _respond("Account", body)

    # ------------------------------------------------------------ actions

    def action(
        handler: Callable[
            [Request, websession.Session, Account, str, dict[str, str]],
            Awaitable[Response],
        ],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            form = await body_of(request)
            got = await anyio.to_thread.run_sync(checked, request, form)
            if isinstance(got, Response):
                return got
            session, account, token = got
            assert form is not None
            return await handler(request, session, account, token, form)

        return endpoint

    async def do_signout(
        request: Request,
        session: websession.Session,
        account: Account,
        token: str,
        form: dict[str, str],
    ) -> Response:
        def run() -> None:
            with db.connection() as conn:
                websession.end(conn, session)
                conn.commit()
            record(account.id, "sign out")

        await anyio.to_thread.run_sync(run)
        response = _see("/")
        clear_session(response)
        return response

    async def do_signout_everywhere(
        request: Request,
        session: websession.Session,
        account: Account,
        token: str,
        form: dict[str, str],
    ) -> Response:
        def run() -> None:
            with db.connection() as conn:
                websession.end_all(conn, account.id)
                conn.commit()
            record(account.id, "sign out everywhere")

        await anyio.to_thread.run_sync(run)
        response = _see("/account/signin")
        clear_session(response)
        return response

    async def do_settings(
        request: Request,
        session: websession.Session,
        account: Account,
        token: str,
        form: dict[str, str],
    ) -> Response:
        listed = form.get("listed")
        if listed not in ("yes", "no"):
            return _see("/account")

        def run() -> None:
            with db.connection() as conn:
                account_settings.change(conn, account, f"listed: {listed}")
                conn.commit()

        await anyio.to_thread.run_sync(run)
        return _see("/account")

    async def reauth(
        request: Request,
        session: websession.Session,
        account: Account,
        form: dict[str, str],
    ) -> str | None:
        """None when the password was given recently or now; else the key of
        the message to show."""
        if session.fresh_reauth:
            return None
        found = await password_ok(request, account, form.get("password", ""))
        if isinstance(found, str):
            return found

        def run() -> None:
            with db.connection() as conn:
                websession.reauthenticated(conn, session)
                conn.commit()

        await anyio.to_thread.run_sync(run)
        return None

    # revoke: a confirm page, then the POST
    def revoke_target(account: Account, kind: str, ident: int) -> str | None:
        if kind not in ("connection", "token"):
            return None
        with db.connection() as conn:
            if kind == "connection":
                found = [
                    c
                    for c in grants.list_connections(conn, account.id)
                    if c.id == ident and c.live
                ]
                return (
                    f"{found[0].label} (connected {found[0].created_at:%Y-%m-%d})"
                    if found
                    else None
                )
            found_t = [
                t
                for t in grants.list_operator_tokens(conn, account.id)
                if t.id == ident and t.revoked_at is None
            ]
            return (
                f"the token {found_t[0].label or ''} (made {found_t[0].created_at:%Y-%m-%d})"
                if found_t
                else None
            )

    def revoke_page(
        token: str,
        session: websession.Session,
        kind: str,
        ident: int,
        what: str,
        error: str | None = None,
    ) -> str:
        err = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
        then = (
            "Its assistant must connect and sign in again."
            if kind == "connection"
            else "Whatever uses it stops working."
        )
        return (
            f"<h1>Revoke {e(what)}?</h1>{err}<p>{then}</p>"
            f'<form method="post" action="/account/revoke/{kind}/{ident}">'
            f"{csrf_field(token)}{password_field(session)}"
            '<p><button>Revoke</button> <a href="/account">Cancel</a></p></form>'
        )

    async def revoke_get(request: Request) -> Response:
        kind = request.path_params["kind"]
        ident = int(request.path_params["ident"])
        token = request.cookies.get(cookie_name)
        found = await anyio.to_thread.run_sync(load, token)
        if found is None or token is None:
            return _see("/account/signin")
        session, account = found
        what = await anyio.to_thread.run_sync(revoke_target, account, kind, ident)
        if what is None:
            return _respond(
                "Not found",
                '<p>Nothing to revoke there. <a href="/account">Back</a>.</p>',
                404,
            )
        return _respond("Revoke", revoke_page(token, session, kind, ident, what))

    async def revoke_post(request: Request) -> Response:
        kind = request.path_params["kind"]
        ident = int(request.path_params["ident"])
        form = await body_of(request)
        got = await anyio.to_thread.run_sync(checked, request, form)
        if isinstance(got, Response):
            return got
        session, account, token = got
        assert form is not None
        what = await anyio.to_thread.run_sync(revoke_target, account, kind, ident)
        if what is None:
            return _see("/account")
        problem = await reauth(request, session, account, form)
        if problem:
            return _respond(
                "Revoke",
                revoke_page(token, session, kind, ident, what, page.MESSAGES[problem]),
                429 if problem == "throttled" else 401,
            )

        def run() -> None:
            with db.connection() as conn:
                if kind == "connection":
                    grants.revoke_connection(conn, account.id, ident)
                else:
                    grants.revoke_operator_token(conn, account.id, ident)
                conn.commit()
            record(account.id, f"revoke {kind}")

        await anyio.to_thread.run_sync(run)
        return _see("/account")

    # export
    def export_page(
        token: str, session: websession.Session, error: str | None = None
    ) -> str:
        err = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
        return (
            f"<h1>Export everything</h1>{err}"
            "<p>One zip file: every record you signed, with a note on checking the"
            " signatures, and your notes as Markdown, a folder per hub.</p>"
            f'<form method="post" action="/account/export">{csrf_field(token)}'
            f"{password_field(session)}<p><button>Download</button></p></form>"
        )

    async def export_get(request: Request) -> Response:
        token = request.cookies.get(cookie_name)
        found = await anyio.to_thread.run_sync(load, token)
        if found is None or token is None:
            return _see("/account/signin")
        return _respond("Export", export_page(token, found[0]))

    async def do_export(
        request: Request,
        session: websession.Session,
        account: Account,
        token: str,
        form: dict[str, str],
    ) -> Response:
        problem = await reauth(request, session, account, form)
        if problem:
            return _respond(
                "Export",
                export_page(token, session, page.MESSAGES[problem]),
                429 if problem == "throttled" else 401,
            )

        def run() -> bytes | Response:
            with account_admin.export_slot(account.id):
                with db.connection() as conn:
                    allowed = throttle.acquire(
                        conn, EXPORT_KIND, str(account.id), 1, EXPORT_EVERY
                    )
                    conn.commit()
                    if not allowed:
                        return _respond(
                            "Export",
                            "<p>One export every 10 minutes: try again later."
                            ' <a href="/account">Back</a>.</p>',
                            429,
                        )
                    rows = account_admin.export_rows(conn, account)
                    conn.commit()
                # the connection is back in the pool while the zip is built
                with account_admin.build_zip(account, rows) as spooled:
                    data = spooled.read()
            record(account.id, "export")
            return data

        try:
            result = await anyio.to_thread.run_sync(run)
        except ReticulaError as exc:
            if exc.code == account_admin.TOO_LARGE:
                return _respond(
                    "Export",
                    f"<p>{e(exc.message)}: too large for a download here."
                    f" {e(exc.hint or '')}.</p>",
                    413,
                )
            if exc.code == "Conflict":
                return _respond(
                    "Export",
                    "<p>An export of this account is running already.</p>",
                    409,
                )
            raise
        if isinstance(result, Response):
            return result
        return Response(
            result,
            media_type="application/zip",
            headers={
                **HEADERS,
                "Content-Disposition": f'attachment; filename="reticula-{account.handle}.zip"',
            },
        )

    # delete
    def delete_page(token: str, account: Account, error: str | None = None) -> str:
        err = f'<p class="err" role="alert">{e(error)}</p>' if error else ""
        return (
            f"<h1>Delete the account @{e(account.handle)}</h1>{err}"
            "<p>This erases at once, and cannot be undone: every note, hub and link"
            " you wrote, also in other people's hubs; every connection, token and"
            " sign-in; your e-mail address and password. Your handle stays taken,"
            " so nobody else can use it. Export first if you want a copy.</p>"
            f'<form method="post" action="/account/delete">{csrf_field(token)}'
            f"<p><label>Type your handle, {e(account.handle)}:"
            ' <input name="handle" autocomplete="off" required></label></p>'
            '<p><label>Your password <input type="password" name="password"'
            ' autocomplete="current-password" required></label></p>'
            "<p><button>Delete the account and everything in it</button>"
            ' <a href="/account">Cancel</a></p></form>'
        )

    async def delete_get(request: Request) -> Response:
        token = request.cookies.get(cookie_name)
        found = await anyio.to_thread.run_sync(load, token)
        if found is None or token is None:
            return _see("/account/signin")
        return _respond("Delete", delete_page(token, found[1]))

    async def do_delete(
        request: Request,
        session: websession.Session,
        account: Account,
        token: str,
        form: dict[str, str],
    ) -> Response:
        typed = form.get("handle", "").strip().lstrip("@").lower()
        if typed != account.handle.lower():
            return _respond(
                "Delete",
                delete_page(
                    token, account, "The handle you typed is not this account's."
                ),
                400,
            )
        found = await password_ok(request, account, form.get("password", ""))
        if isinstance(found, str):
            return _respond(
                "Delete",
                delete_page(token, account, page.MESSAGES[found]),
                429 if found == "throttled" else 401,
            )

        def run() -> dict[str, int]:
            with db.connection() as conn, conn.transaction():
                return account_admin.delete_everything(conn, account)

        try:
            counts = await anyio.to_thread.run_sync(run)
        except ReticulaError as exc:
            # the transaction rolled back: every step or none
            refusal.note(f"account: delete_{exc.code}")
            return _respond(
                "Delete",
                "<h1>Nothing was deleted</h1><p>One step of the deletion failed"
                f" ({e(exc.message)}), so none of it was done. Try again; if it"
                " fails again, ask the operator of this server.</p>",
                409,
            )
        response = _respond(
            "Deleted",
            f"<h1>The account is deleted</h1><p>{counts['notes']} notes and"
            f" {counts['hubs']} hubs were erased, and every connection was revoked.</p>",
        )
        clear_session(response)
        return response

    def hsts(
        endpoint: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        if not secure:
            return endpoint

        async def wrapped(request: Request) -> Response:
            response = await endpoint(request)
            response.headers["Strict-Transport-Security"] = HSTS
            return response

        return wrapped

    found_routes = [
        Route("/account", endpoint=account_get, methods=["GET", "HEAD"]),
        Route("/account/signin", endpoint=signin_get, methods=["GET", "HEAD"]),
        Route("/account/signin", endpoint=signin_post, methods=["POST"]),
        Route("/account/signout", endpoint=action(do_signout), methods=["POST"]),
        Route(
            "/account/signout-everywhere",
            endpoint=action(do_signout_everywhere),
            methods=["POST"],
        ),
        Route("/account/settings", endpoint=action(do_settings), methods=["POST"]),
        Route(
            "/account/revoke/{kind:str}/{ident:int}",
            endpoint=revoke_get,
            methods=["GET", "HEAD"],
        ),
        Route(
            "/account/revoke/{kind:str}/{ident:int}",
            endpoint=revoke_post,
            methods=["POST"],
        ),
        Route("/account/export", endpoint=export_get, methods=["GET", "HEAD"]),
        Route("/account/export", endpoint=action(do_export), methods=["POST"]),
        Route("/account/delete", endpoint=delete_get, methods=["GET", "HEAD"]),
        Route("/account/delete", endpoint=action(do_delete), methods=["POST"]),
    ]
    return [
        Route(r.path, endpoint=hsts(r.endpoint), methods=list(r.methods or ()))
        for r in found_routes
    ]


def record(account_id: int, event: str, outcome: str = "ok") -> None:
    """An event of these pages for RETICULA3's activity log
    (reticula_mcp.activity.record: queued, never blocks or raises); nothing
    until that module exists."""
    activity = account_admin._activity()
    if activity is not None:
        activity.record(account_id, event, outcome=outcome)


def activity_lines(conn: Any, account: Account) -> list[str]:
    """The Activity section: lines from RETICULA3's activity log, plain text
    (the page escapes them). Empty, and the section left out, until a module
    ``reticula_mcp.activity`` with ``lines(conn, account_id) -> list[str]``
    exists."""
    activity = account_admin._activity()
    if activity is None:
        return []
    return list(activity.lines(conn, account.id))
