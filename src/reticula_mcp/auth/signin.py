"""``/signin``: the one page where a person signs in or creates an account.

Flow: ``/authorize`` (SDK) -> provider parks the request -> browser lands here
with ``?req=<handle>`` -> GET sets a CSRF nonce (cookie + hidden field, digest
stored on the parked request) -> POST authenticates or creates the account,
consumes the parked request and redirects (303) to the client's registered
redirect URI with a single-use code.

Hostile-input rules: the body is size-capped before parsing, ``mode`` is a
closed set, the parked request is looked up by digest, the redirect target
comes only from the parked request and is re-checked against the client's
registered URIs, failures answer with fixed text (sign-in never says whether an
e-mail exists), failed sign-ins and sign-ups are counted per address and per
e-mail, and the unknown-e-mail path costs the same argon2 time as a real one
(``accounts.authenticate``).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs

import anyio.to_thread
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from reticula_mcp import accounts, refusal
from reticula_mcp.auth import config, page, throttle, verification
from reticula_mcp.auth.netaddr import caller_bucket
from reticula_mcp.auth.provider import PendingInvalid, ReticulaProvider, sha256_hex
from reticula_mcp.auth.web import read_limited
from reticula_mcp.config import Settings, parse_rate
from reticula_mcp.db.pool import Database
from reticula_mcp.errors import CONFLICT, ReticulaError

log = logging.getLogger("reticula_mcp.auth")

COOKIE_PATH = "/signin"
COOKIE_MAX_AGE = int(config.PENDING_TTL.total_seconds())


def cookie_name(handle: str) -> str:
    return "rc_" + sha256_hex(handle)[:16]


def _form(raw: bytes) -> dict[str, str] | None:
    """First value of each field (2 KiB per value), or None for a malformed form.

    Malformed: more than 12 fields, or a NUL byte anywhere (PostgreSQL text
    cannot hold one, and no honest browser sends one).
    """
    text = raw.decode("utf-8", "replace")
    try:
        parsed = parse_qs(text, max_num_fields=12)
    except ValueError:
        return None
    if any("\x00" in k or any("\x00" in v for v in vs) for k, vs in parsed.items()):
        return None
    return {k: v[0][:2048] for k, v in parsed.items() if v}


def _valid(form_value: str, expected: str | None) -> bool:
    return (
        bool(form_value)
        and expected is not None
        and hmac.compare_digest(form_value.encode(), expected.encode())
    )


def routes(settings: Settings, db: Database, provider: ReticulaProvider) -> list[Route]:
    secure_cookie = settings.base_url.startswith("https://")
    signup_limit, signup_window = parse_rate(settings.signup_rate)

    def _render(
        pending: dict[str, Any],
        handle: str,
        csrf: str,
        mode: str,
        *,
        error: str | None = None,
        email: str = "",
        handle_value: str = "",
        status: int = 200,
    ) -> Response:
        client = OAuthClientInformationFull.model_validate(pending["client_info"])
        return page.sign_in_page(
            handle=handle,
            csrf=csrf,
            mode=mode,
            client_name=client.client_name or "an assistant",
            redirect_uri=pending["params"]["redirect_uri"],
            signups_open=settings.signup == "open",
            known_hosts=config.known_clients(settings),
            error=error,
            email=email,
            handle_value=handle_value,
            status=status,
        )

    # --- GET -----------------------------------------------------------------

    def get_sync(handle: str, mode: str) -> Response:
        with db.connection() as conn:
            pending = provider.find_pending(conn, handle)
            if pending is None:
                return page.gone_page()
            nonce = secrets.token_urlsafe(24)
            provider.set_csrf(conn, handle, nonce)
        response = _render(pending, handle, nonce, mode)
        response.set_cookie(
            cookie_name(handle),
            nonce,
            max_age=COOKIE_MAX_AGE,
            path=COOKIE_PATH,
            secure=secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return response

    # --- POST ----------------------------------------------------------------

    def post_sync(form: dict[str, str], cookie: str | None, bucket: str) -> Response:
        handle = form.get("req", "")
        csrf = form.get("csrf", "")
        with db.connection() as conn:
            pending = provider.find_pending(conn, handle)
            if pending is None:
                return page.gone_page()
            stored = pending["csrf_hash"]
            if not (_valid(csrf, cookie) and _valid(sha256_hex(csrf), stored)):
                return page.expired_page(handle)
            signup = form.get("mode") == "signup"
            email = form.get("email", "").strip()
            password = form.get("password", "")
            chosen = form.get("handle", "").strip()
            mode = "signup" if signup else "signin"

            def fail(key: str, status: int) -> Response:
                refusal.note(f"signin: {key}")
                return _render(
                    pending,
                    handle,
                    csrf,
                    mode,
                    error=page.MESSAGES[key],
                    email=email[: config.MAX_EMAIL_LENGTH],
                    handle_value=chosen[: config.MAX_HANDLE_LENGTH],
                    status=status,
                )

            if signup:
                return _signup(
                    conn, pending, handle, csrf, bucket, email, password, chosen, fail
                )
            return _signin(conn, pending, handle, bucket, email, password, fail)

    def _signin(
        conn: Any,
        pending: dict[str, Any],
        handle: str,
        bucket: str,
        email: str,
        password: str,
        fail: Any,
    ) -> Response:
        # The counter is per (e-mail, address): someone elsewhere cannot lock
        # the owner out by guessing. The attempt is recorded BEFORE the argon2
        # check, under advisory locks on both keys, and committed, so parallel
        # guesses see each other (check-then-verify-then-record let 24 parallel
        # requests through a limit of 6). A success takes its own record back.
        # The pooled connection is held during the argon2 check exactly as
        # accounts.authenticate needs it; the locks are not (transaction ends here).
        email_key = f"{email.lower()[: config.MAX_EMAIL_LENGTH]}|{bucket}"
        provisional = throttle.acquire_many(
            conn,
            [
                (
                    throttle.SIGNIN_EMAIL,
                    email_key,
                    config.FAILURES_PER_EMAIL,
                    config.FAILURE_WINDOW,
                ),
                (
                    throttle.SIGNIN_IP,
                    bucket,
                    config.FAILURES_PER_IP,
                    config.FAILURE_WINDOW,
                ),
            ],
        )
        conn.commit()
        if provisional is None:
            return fail("throttled", 429)
        account = None
        if email and password and len(password) <= config.MAX_PASSWORD_LENGTH:
            account = accounts.authenticate(conn, email, password)
        if account is None:
            throttle.purge(conn)
            return fail("bad_credentials", 401)
        throttle.forget(conn, provisional)
        conn.execute(
            "DELETE FROM auth_event WHERE kind = %s AND subject = %s",
            (throttle.SIGNIN_EMAIL, email_key),
        )
        try:
            with conn.transaction():
                url = provider.finish_authorization(conn, handle, pending, account.id)
        except PendingInvalid:
            return page.gone_page()
        return _redirect(url)

    def _signup(
        conn: Any,
        pending: dict[str, Any],
        handle: str,
        csrf: str,
        bucket: str,
        email: str,
        password: str,
        chosen: str,
        fail: Any,
    ) -> Response:
        if settings.signup != "open":
            return fail("closed", 403)
        if not throttle.signup_allowed(
            conn, bucket, email, signup_limit, signup_window
        ):
            return fail("signup_rate", 429)
        if (
            not config.MIN_PASSWORD_LENGTH
            <= len(password)
            <= config.MAX_PASSWORD_LENGTH
        ):
            refusal.note("signin: password_length")
            return _render(
                pending,
                handle,
                csrf,
                "signup",
                error=f"Choose a password of at least {config.MIN_PASSWORD_LENGTH}"
                " characters.",
                email=email,
                handle_value=chosen,
                status=400,
            )
        try:
            with conn.transaction():
                account = accounts.create_account(conn, email, password, chosen or None)
                verification.after_signup(conn, account, settings)
                url = provider.finish_authorization(conn, handle, pending, account.id)
        except ReticulaError as exc:
            # the code only: the message can repeat the e-mail address or handle
            refusal.note(f"signin: {exc.code}")
            return _render(
                pending,
                handle,
                csrf,
                "signup",
                error=exc.message[:200],
                email=email,
                handle_value=chosen,
                status=409 if exc.code == CONFLICT else 400,
            )
        except PendingInvalid:
            return page.gone_page()
        except Exception:
            log.exception("sign-up failed")
            return fail("generic", 500)
        return _redirect(url)

    def _redirect(url: str) -> Response:
        return RedirectResponse(url, status_code=303, headers=page.HEADERS)

    # --- ASGI endpoints ------------------------------------------------------

    async def signin(request: Request) -> Response:
        if request.method == "GET":
            handle = request.query_params.get("req", "")
            mode = (
                "signup" if request.query_params.get("mode") == "signup" else "signin"
            )
            return await anyio.to_thread.run_sync(get_sync, handle, mode)
        raw = await read_limited(request, config.MAX_FORM_BODY)
        if raw is None:
            refusal.note("signin: too_large")
            return page.respond(
                "<!doctype html><title>Too large</title><p>Request too large.</p>", 413
            )
        if "application/x-www-form-urlencoded" not in request.headers.get(
            "content-type", ""
        ):
            refusal.note("signin: content_type")
            return page.respond(
                "<!doctype html><title>Bad request</title><p>Bad request.</p>", 415
            )
        form = _form(raw)
        if form is None:
            refusal.note("signin: bad_form")
            return page.respond(
                "<!doctype html><title>Bad request</title><p>Bad request.</p>", 400
            )
        cookie = request.cookies.get(cookie_name(form.get("req", "")))
        bucket = caller_bucket(request, settings)

        return await anyio.to_thread.run_sync(post_sync, form, cookie, bucket)

    return [Route("/signin", endpoint=signin, methods=["GET", "POST"])]
