"""E-mail verification: designed in, switched OFF.

Today an account is created ``active`` with ``verified = false`` and works
completely; nothing is sent and nothing is blocked.

Intended behaviour once switched on (set ``ENABLED = True`` and fill in the two
marked places below):

* an unverified account is *provisional*. Private use works at once: it can
  read, search and write its own notes. Joining a shared hub, publishing to one,
  and inviting other people wait until the address is verified.
* a verification mail carries a single-use, expiring link; following it calls
  ``accounts.set_verified(conn, account, True)``, which appends the new signed
  identity version.
* an account that is still unverified after about a week lapses: it is set to
  ``disabled`` (its notes stay signed in the log; signing in is refused with a
  message saying how to get a new link).
* the gate is enforced where the action happens (the graph lane's invite,
  join and publish paths call :func:`require_verified`); this module only
  holds the rule so it lives in one place.
"""

from __future__ import annotations

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import FORBIDDEN, ReticulaError

ENABLED = False

#: Unverified accounts lapse after this many days (when ENABLED).
LAPSE_AFTER_DAYS = 7


def after_signup(conn: Conn, account: Account, settings: Settings) -> None:
    """VERIFICATION SWITCH-POINT 1: called inside the sign-up transaction.

    When ``ENABLED``: mark the account provisional if needed and send the
    verification mail here (``settings.base_url`` gives the link's origin).
    Disabled: does nothing, sends no mail.
    """
    if not ENABLED:
        return
    raise NotImplementedError("e-mail verification is not built yet")


def require_verified(account: Account, action: str) -> None:
    """VERIFICATION SWITCH-POINT 2: the provisional gate.

    ``action`` is one of ``"join"``, ``"publish"``, ``"invite"``. Refuses with
    ``Forbidden`` for an unverified account when ``ENABLED``; a no-op while
    verification is off.
    """
    if not ENABLED or account.verified:
        return
    raise ReticulaError(
        FORBIDDEN,
        f"verify your e-mail address before you {action}",
        hint="open the link in the verification mail",
    )
