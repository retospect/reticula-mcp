"""Tunables of the authorization server (constants, plus one optional env var)."""

from __future__ import annotations

from datetime import timedelta

from reticula_mcp import accounts
from reticula_mcp.config import Settings, parse_rate

#: The one scope. Access is all-or-nothing per connected assistant.
SCOPE = "mcp"

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)  # slides: every rotation issues a fresh window
CODE_TTL = timedelta(minutes=2)
PENDING_TTL = timedelta(minutes=15)  # how long the sign-in page stays valid

MAX_REGISTER_BODY = 16 * 1024
MAX_FORM_BODY = 8 * 1024
MAX_REDIRECT_URIS = 10
MAX_URI_LENGTH = 2048
MAX_STATE_LENGTH = 2048
MAX_PENDING_PER_CLIENT = 500
MAX_PENDING_PER_ADDRESS = 100
CLIENT_NAME_LENGTH = 100

MIN_PASSWORD_LENGTH = accounts.MIN_PASSWORD_LENGTH
MAX_PASSWORD_LENGTH = accounts.MAX_PASSWORD_LENGTH
MAX_HANDLE_LENGTH = 32
MAX_EMAIL_LENGTH = 254

#: Failed sign-ins inside FAILURE_WINDOW that lock sign-in for the key.
FAILURES_PER_EMAIL = 6
FAILURES_PER_IP = 30
FAILURE_WINDOW = timedelta(minutes=15)


def register_rate(settings: Settings) -> tuple[int, int]:
    """Client registrations allowed per address and window (count, seconds)."""
    return parse_rate(settings.register_rate)


DEFAULT_KNOWN_CLIENTS = ("claude.ai", "claude.com")


def known_clients(settings: Settings) -> tuple[str, ...]:
    """Hosts the sign-in page treats as familiar assistant front ends.

    ``Settings.known_clients`` (``RETICULA_KNOWN_CLIENTS``) when the field
    exists, else the built-in default.
    """
    found = getattr(settings, "known_clients", DEFAULT_KNOWN_CLIENTS)
    return tuple(h.lower() for h in found)
