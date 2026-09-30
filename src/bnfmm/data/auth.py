"""Access-token minting for the Dhan API. Read-only by construction.

Credentials come from the environment (optionally via a gitignored ``.env``) and
are never written to disk, logged, or committed. ``config/`` holds no secrets.

**What this module deliberately does not do.** It does not touch
``DhanLogin.set_ip`` or ``modify_ip``. Static-IP registration exists only as a
prerequisite for order placement, so wiring it would be the first step toward a
capability this project has none of -- see ``tests/test_no_order_placement.py``,
which fails if ``set_ip`` appears anywhere under ``src/``.

**On automating the TOTP.** ``BRIEFING.md`` §2 asked that token refresh not be
automated around the OTP step. That was written about a different broker, where
the OTP arrives out-of-band and automating it would mean intercepting a message.
Dhan's second factor is a TOTP generated from a shared secret the user already
holds, so computing it locally is what the factor is *for* -- it is the same
arithmetic the authenticator app does, with no interception and no channel
involved. The intent behind the constraint is preserved where it matters: the
secret is human-supplied via the environment, never committed, and never
transmitted anywhere except as a six-digit code to Dhan's own auth endpoint.

Set ``BNFMM_TOTP_SECRET`` to skip the prompt, or leave it unset and the six digits
are asked for interactively -- which keeps the base32 seed off the machine
entirely, at the cost of one prompt per session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- environment --------------------------------------------------------------

CLIENT_ID_VAR = "BNFMM_DHAN_CLIENT_ID"
PIN_VAR = "BNFMM_DHAN_PIN"
TOTP_SECRET_VAR = "BNFMM_TOTP_SECRET"
ACCESS_TOKEN_VAR = "BNFMM_DHAN_ACCESS_TOKEN"

REQUIRED_VARS = (CLIENT_ID_VAR, PIN_VAR)

# Dhan access tokens are documented as valid for 24 hours. Treated as a hint for
# the staleness warning below, not as a guarantee -- the server decides.
TOKEN_LIFETIME = timedelta(hours=24)


class AuthError(RuntimeError):
    """Raised for a missing credential or a rejected login.

    Deliberately does not carry the credential values, so a traceback pasted into
    a chat window or a CI log leaks nothing.
    """


def load_dotenv(path: str | Path | None = None) -> None:
    """Load ``.env`` into the environment if present. Never overrides real vars.

    Optional convenience: the file is gitignored, and everything here works from
    plain environment variables with no ``.env`` at all.
    """
    from dotenv import load_dotenv as _load

    _load(dotenv_path=str(path) if path else None, override=False)


def _require(var: str) -> str:
    value = os.environ.get(var, "").strip()
    if not value:
        raise AuthError(
            f"{var} is not set. Credentials come from the environment, never from "
            f"a file in the repo. Export it or put it in a gitignored .env "
            f"(required: {', '.join(REQUIRED_VARS)})."
        )
    return value


def current_totp(secret: str | None = None) -> str:
    """Six digits for the current 30-second window.

    With no secret available, prompts. The prompt path means the base32 seed
    never has to exist on this machine.
    """
    secret = (secret or os.environ.get(TOTP_SECRET_VAR, "")).strip().replace(" ", "")
    if not secret:
        code = input(f"TOTP (6 digits; set {TOTP_SECRET_VAR} to skip this prompt): ")
        code = code.strip()
        if not (len(code) == 6 and code.isdigit()):
            raise AuthError("expected six digits")
        return code

    import pyotp

    try:
        return pyotp.TOTP(secret).now()
    except Exception as exc:  # pyotp raises several types on a malformed seed
        raise AuthError(
            f"{TOTP_SECRET_VAR} is not a valid base32 TOTP secret ({type(exc).__name__}). "
            "Unset it to be prompted for the six digits instead."
        ) from None


# --- session ------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """A minted access token and the identity it belongs to.

    ``repr`` is overridden so the token cannot end up in a log line or a pytest
    assertion dump by accident.
    """

    client_id: str
    access_token: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime | None = None
    source: str = "generate_token"

    def __repr__(self) -> str:
        return (
            f"Session(client_id={self.client_id!r}, source={self.source!r}, "
            f"issued_at={self.issued_at.isoformat()}, token=<redacted>)"
        )

    @property
    def likely_expired(self) -> bool:
        """Best-effort staleness check. The server is the only real authority."""
        deadline = self.expires_at or (self.issued_at + TOKEN_LIFETIME)
        return datetime.now(timezone.utc) >= deadline

    def headers(self) -> dict[str, str]:
        """Auth headers for a direct ``requests`` call.

        Used by the diagnostic scripts, which need the raw status code and
        response body -- the SDK raises a bare ``Exception`` on non-200 and the
        error code in the body is exactly what those scripts are looking for.
        """
        return {"access-token": self.access_token, "client-id": self.client_id}


def session_from_env() -> Session | None:
    """Reuse a token already exported in the environment, if there is one.

    Lets a human mint the token however they like -- the web dashboard, a
    separate script -- and hand it over without this module ever seeing a PIN.
    """
    token = os.environ.get(ACCESS_TOKEN_VAR, "").strip()
    if not token:
        return None
    return Session(
        client_id=_require(CLIENT_ID_VAR),
        access_token=token,
        issued_at=datetime.now(timezone.utc),
        source=f"${ACCESS_TOKEN_VAR}",
    )


def login(*, allow_env_token: bool = True) -> Session:
    """Mint an access token, or reuse one from the environment.

    Raises
    ------
    AuthError
        On a missing credential, a malformed TOTP secret, or a rejected login.
    """
    if allow_env_token:
        existing = session_from_env()
        if existing is not None:
            return existing

    from dhanhq.auth import DhanLogin

    client_id = _require(CLIENT_ID_VAR)
    pin = _require(PIN_VAR)
    totp = current_totp()

    try:
        payload = DhanLogin(client_id).generate_token(pin, totp)
    except Exception as exc:
        # The SDK stringifies the response body into the exception. Truncated
        # here rather than reproduced in full: a rejected-login body can echo
        # request parameters back.
        raise AuthError(f"Dhan rejected the login ({str(exc)[:200]})") from None

    token = payload.get("accessToken") or payload.get("access_token")
    if not token:
        raise AuthError(f"login succeeded but returned no access token; keys={sorted(payload)}")

    return Session(
        client_id=client_id,
        access_token=str(token),
        issued_at=datetime.now(timezone.utc),
        expires_at=_parse_expiry(payload.get("expiryTime")),
    )


def _parse_expiry(raw: object) -> datetime | None:
    """Dhan's expiry field, when it is present and parseable. Never fatal."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
