"""Settings, read from the environment (no config files)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass

from reticula_mcp.errors import BAD_INPUT, ReticulaError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_rate(text: str) -> tuple[int, int]:
    """``"5/hour"`` -> ``(5, 3600)``: (count, window in seconds)."""
    try:
        count_text, unit = text.strip().split("/")
        count = int(count_text)
        seconds = _UNITS[unit.strip().rstrip("s")]
    except (ValueError, KeyError) as exc:
        raise ReticulaError(
            BAD_INPUT, f"bad rate {text!r}", hint="use e.g. 5/hour, 30/minute"
        ) from exc
    if count < 0:
        raise ReticulaError(BAD_INPUT, f"bad rate {text!r}")
    return count, seconds


@dataclass(frozen=True)
class Settings:
    """Everything configurable. Build with :meth:`from_env`."""

    database_url: str | None = None
    public_url: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    embedder: str = "auto"  # auto | hash | fastembed
    signup: str = "open"  # open | closed
    signup_rate: str = "5/hour"  # per IP
    register_rate: str = "20/hour"  # OAuth client registrations per IP
    # Peers whose X-Forwarded-For is believed (IPs or CIDRs); empty = trust nobody.
    trusted_proxies: tuple[str, ...] = ()
    # Hosts an assistant's connector returns to that the sign-in page does not warn about.
    known_clients: tuple[str, ...] = ("claude.ai", "claude.com")
    embed_model: str | None = None  # model name for the fastembed embedder
    # Stateless streamable HTTP: no server-side MCP sessions, so a restart or a
    # second worker never strands a connected assistant.
    stateless_http: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def get(name: str, default: str | None = None) -> str | None:
            value = e.get(f"RETICULA_{name}", "").strip()
            return value or default

        settings = cls(
            database_url=get("DATABASE_URL"),
            public_url=(get("PUBLIC_URL") or "").rstrip("/") or None,
            host=get("HOST", DEFAULT_HOST) or DEFAULT_HOST,
            port=int(get("PORT", str(DEFAULT_PORT)) or DEFAULT_PORT),
            embedder=get("EMBEDDER", "auto") or "auto",
            signup=get("SIGNUP", "open") or "open",
            signup_rate=get("SIGNUP_RATE", "5/hour") or "5/hour",
            register_rate=get("REGISTER_RATE", "20/hour") or "20/hour",
            trusted_proxies=tuple(
                part.strip()
                for part in (get("TRUSTED_PROXIES") or "").split(",")
                if part.strip()
            ),
            known_clients=tuple(
                part.strip().lower()
                for part in (get("KNOWN_CLIENTS", "claude.ai,claude.com") or "").split(
                    ","
                )
                if part.strip()
            ),
            embed_model=get("EMBED_MODEL"),
            stateless_http=(get("STATELESS_HTTP", "true") or "true").lower()
            not in ("0", "false", "no", "off"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.embedder not in ("auto", "hash", "fastembed"):
            raise ReticulaError(
                BAD_INPUT, "RETICULA_EMBEDDER must be auto|hash|fastembed"
            )
        if self.signup not in ("open", "closed"):
            raise ReticulaError(BAD_INPUT, "RETICULA_SIGNUP must be open|closed")
        parse_rate(self.signup_rate)
        parse_rate(self.register_rate)
        for proxy in self.trusted_proxies:
            try:
                ipaddress.ip_network(proxy, strict=False)
            except ValueError as exc:
                raise ReticulaError(
                    BAD_INPUT,
                    f"RETICULA_TRUSTED_PROXIES: {proxy!r} is not an IP or CIDR",
                ) from exc

    @property
    def base_url(self) -> str:
        """External base URL (no trailing slash): the OAuth issuer/resource root."""
        return self.public_url or f"http://{self.host}:{self.port}"

    @property
    def resource_url(self) -> str:
        """The MCP endpoint URL clients are given."""
        return f"{self.base_url}/mcp"

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ReticulaError(
                BAD_INPUT,
                "RETICULA_DATABASE_URL is not set.",
                hint="e.g. postgresql://user:pass@localhost:5432/reticula",
            )
        return self.database_url
