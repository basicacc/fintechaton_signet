"""Demo sign-in and role-based authorization.

Three roles (client, bank, regulator) receive a signed, expiring session token. The
API checks the role on every protected endpoint, so, for example, only a regulator
can flag a certificate and only bank staff can resolve a review case.

A client token is also bound to one client (``subject``): it can only read and act on
that client's conversation, account and orders. Bank staff can open any client.

This is a *demo* sign-in: anyone can choose a role (and, as a client, which example
client they are), and there are no passwords. In production the token would come
from the bank's identity provider; the authorization checks on the API stay the same.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

ROLES = ("client", "bank", "regulator")
TTL_SECONDS = 8 * 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


_SUBJECT = re.compile(r"^[a-z0-9-]{1,64}$")


def issue(roles: set[str] | list[str] | tuple[str, ...], secret: bytes, ttl: int = TTL_SECONDS,
          now: float | None = None, subject: str | None = None) -> str:
    roles = sorted(set(roles))
    if not roles or any(r not in ROLES for r in roles):
        raise ValueError(f"roles must be from {ROLES}")
    if subject is not None and not _SUBJECT.match(subject):
        raise ValueError("invalid client id")
    exp = int((now or time.time()) + ttl)
    payload = f"{'+'.join(roles)}.{subject or '-'}.{exp}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def claims(token: str | None, secret: bytes, now: float | None = None) -> tuple[set[str], str | None]:
    """(roles, subject) of a valid, unexpired token; (empty set, None) otherwise."""
    if not token:
        return set(), None
    try:
        roles_part, sub, exp_part, sig = token.split(".")
        exp = int(exp_part)
    except ValueError:
        return set(), None
    expected = _b64(hmac.new(secret, f"{roles_part}.{sub}.{exp_part}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected) or exp < (now or time.time()):
        return set(), None
    roles = set(roles_part.split("+"))
    return (roles, None if sub == "-" else sub) if roles <= set(ROLES) else (set(), None)


def roles_of(token: str | None, secret: bytes, now: float | None = None) -> set[str]:
    """Roles granted by a valid, unexpired token; empty set otherwise."""
    return claims(token, secret, now)[0]
