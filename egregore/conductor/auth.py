"""Shared party-password auth (Architecture §2.8, PRD D-1/D-3).

One password, shared by every guest device, is the whole access model: the
join flow is "open a URL, enter a password, go fullscreen" (PRD D-1). There
is no per-user identity and nothing server-side to expire -- a session is a
cookie whose value is ``HMAC-SHA256(key=password, message="egregore-party")``.
Anyone who knows the password can compute the same digest, so possessing the
cookie is equivalent to having typed the password once; the server verifies
by recomputing and comparing in constant time rather than keeping any kind
of session table.

The password itself comes from the environment variable named by
``ServingConfig.password_env`` (see ``egregore/config/schema.py``), or a
generated random word pair the integration layer prints once at party start
when unset. Either way, resolving *that* is the integration layer's job --
this module and :func:`egregore.conductor.app.create_app` only ever accept
the resolved password string.

If ``password`` is empty or ``None``, auth is disabled entirely: every
request and every WS handshake passes. This is the documented LAN-trusted
default (PRD D-2, a private venue network with no public tunnel); an
operator who exposes the Conductor beyond the LAN (D-3, e.g. via Cloudflare
Tunnel) is expected to set a password.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status

COOKIE_NAME = "egregore_party"
_MESSAGE = b"egregore-party"


def sign(password: str) -> str:
    """The cookie value for ``password``: HMAC-SHA256 hex digest, keyed by
    the password, over the fixed message ``"egregore-party"``."""
    return hmac.new(password.encode("utf-8"), _MESSAGE, hashlib.sha256).hexdigest()


def verify_cookie(password: str | None, cookie_value: str | None) -> bool:
    """True if ``cookie_value`` is a valid session for ``password``.

    Always true when auth is disabled (falsy ``password``). Comparison is
    constant-time so a bad guess can't be narrowed down byte-by-byte via
    response timing.
    """
    if not password:
        return True
    if not cookie_value:
        return False
    return hmac.compare_digest(sign(password), cookie_value)


def check_password(password: str | None, candidate: str) -> bool:
    """True if ``candidate`` is the configured party password.

    Always true when auth is disabled. Constant-time comparison.
    """
    if not password:
        return True
    return hmac.compare_digest(password, candidate)


class RequireParty:
    """FastAPI HTTP dependency guarding ``/api/*`` and ``/clips/*``.

    401s a request whose ``egregore_party`` cookie doesn't verify against
    the configured password. A no-op (always passes) when auth is disabled.
    """

    def __init__(self, password: str | None) -> None:
        self._password = password

    def __call__(self, request: Request) -> None:
        cookie = request.cookies.get(COOKIE_NAME)
        if not verify_cookie(self._password, cookie):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="join the party first")


def ws_authorized(password: str | None, cookie_value: str | None) -> bool:
    """Same check as :class:`RequireParty`, for a WS handshake's cookie jar.

    The caller (``app.py``) is expected to close the socket with code 4401
    when this returns false, before accepting.
    """
    return verify_cookie(password, cookie_value)
