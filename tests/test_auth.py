"""Tests for credential handling. No network, no credentials, no market hours.

Everything here is either pure logic or an assertion about what does *not* happen.
The two properties worth guarding are that a missing credential fails before
anything is contacted, and that a token cannot leak into a log line or a pytest
dump by accident -- both are the kind of thing that is only ever noticed after it
has already gone wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bnfmm.data import auth

from .conftest import REPO_ROOT

TOKEN = "eyJhbGciOiJIUzI1NiJ9.not-a-real-token"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no credentials, whatever the developer has exported.

    Without this, a machine with real credentials in the environment would run a
    different test suite from CI -- and the passing one would be the wrong one.
    """
    for var in (
        auth.CLIENT_ID_VAR,
        auth.PIN_VAR,
        auth.TOTP_SECRET_VAR,
        auth.ACCESS_TOKEN_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def make_session(**overrides) -> auth.Session:
    kwargs = {
        "client_id": "1100000000",
        "access_token": TOKEN,
        "issued_at": datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc),
    }
    kwargs.update(overrides)
    return auth.Session(**kwargs)  # type: ignore[arg-type]


# --- missing credentials fail early -------------------------------------------


def test_login_without_credentials_raises_before_any_network_call() -> None:
    """The failure must be local. A probe script exits 2 having contacted nothing."""
    with pytest.raises(auth.AuthError, match=auth.CLIENT_ID_VAR):
        auth.login()


def test_the_error_names_every_required_variable() -> None:
    with pytest.raises(auth.AuthError) as excinfo:
        auth.login()
    message = str(excinfo.value)
    for var in auth.REQUIRED_VARS:
        assert var in message


def test_missing_pin_is_reported_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.CLIENT_ID_VAR, "1100000000")
    with pytest.raises(auth.AuthError, match=auth.PIN_VAR):
        auth.login()


def test_a_blank_variable_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty export is the common shape of a half-filled .env."""
    monkeypatch.setenv(auth.CLIENT_ID_VAR, "   ")
    with pytest.raises(auth.AuthError, match=auth.CLIENT_ID_VAR):
        auth.login()


# --- reusing a token from the environment -------------------------------------


def test_a_token_in_the_environment_needs_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path that lets a human mint the token and hand it over.

    No PIN and no TOTP secret ever reach this process in that case.
    """
    monkeypatch.setenv(auth.CLIENT_ID_VAR, "1100000000")
    monkeypatch.setenv(auth.ACCESS_TOKEN_VAR, TOKEN)
    session = auth.login()
    assert session.access_token == TOKEN
    assert session.source == f"${auth.ACCESS_TOKEN_VAR}"


def test_session_from_env_is_none_without_a_token() -> None:
    assert auth.session_from_env() is None


def test_env_token_still_requires_a_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.ACCESS_TOKEN_VAR, TOKEN)
    with pytest.raises(auth.AuthError, match=auth.CLIENT_ID_VAR):
        auth.session_from_env()


def test_login_can_refuse_to_reuse_an_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`allow_env_token=False` forces a fresh mint, for a deliberate re-auth."""
    monkeypatch.setenv(auth.CLIENT_ID_VAR, "1100000000")
    monkeypatch.setenv(auth.ACCESS_TOKEN_VAR, TOKEN)
    with pytest.raises(auth.AuthError, match=auth.PIN_VAR):
        auth.login(allow_env_token=False)


# --- the token must not leak ---------------------------------------------------


def test_repr_redacts_the_token() -> None:
    """A traceback or an assertion dump must not carry a live credential."""
    session = make_session()
    rendered = repr(session)
    assert TOKEN not in rendered
    assert "redacted" in rendered
    assert session.client_id in rendered, "the identity is not the secret"


def test_str_also_redacts() -> None:
    """`str` falls through to `__repr__` on a dataclass -- pinned, because a later
    `__str__` that forgets would silently reintroduce the leak in f-strings."""
    assert TOKEN not in str(make_session())
    assert TOKEN not in f"{make_session()}"


def test_the_token_is_still_reachable_deliberately() -> None:
    """Redaction is about accidents, not access."""
    session = make_session()
    assert session.access_token == TOKEN
    assert session.headers()["access-token"] == TOKEN


def test_headers_carry_the_identity_and_nothing_else() -> None:
    headers = make_session().headers()
    assert set(headers) == {"access-token", "client-id"}


# --- staleness -----------------------------------------------------------------


def test_a_fresh_token_is_not_flagged_stale() -> None:
    now = datetime.now(timezone.utc)
    assert not make_session(issued_at=now).likely_expired


def test_a_day_old_token_is_flagged_stale() -> None:
    old = datetime.now(timezone.utc) - auth.TOKEN_LIFETIME - timedelta(minutes=1)
    assert make_session(issued_at=old).likely_expired


def test_an_explicit_expiry_overrides_the_default_lifetime() -> None:
    """The server's own expiry wins when it gives one; the 24h figure is a hint."""
    now = datetime.now(timezone.utc)
    session = make_session(issued_at=now, expires_at=now - timedelta(seconds=1))
    assert session.likely_expired


@pytest.mark.parametrize(
    ("raw", "expected_year"),
    [
        ("2026-08-24T09:15:00Z", 2026),
        ("2026-08-24T09:15:00+05:30", 2026),
        ("2026-08-24T09:15:00", 2026),
    ],
)
def test_expiry_parsing_accepts_the_shapes_dhan_might_send(
    raw: str, expected_year: int
) -> None:
    parsed = auth._parse_expiry(raw)
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.tzinfo is not None, "a naive deadline would compare wrongly"


@pytest.mark.parametrize("raw", [None, "", "not a date", 12345, {}])
def test_unparseable_expiry_is_never_fatal(raw: object) -> None:
    """A field this project does not depend on must not be able to break login."""
    assert auth._parse_expiry(raw) is None


# --- TOTP ----------------------------------------------------------------------


def test_totp_is_six_digits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.TOTP_SECRET_VAR, "JBSWY3DPEHPK3PXP")
    code = auth.current_totp()
    assert len(code) == 6 and code.isdigit()


def test_a_secret_with_spaces_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticator apps display the seed in space-separated groups."""
    monkeypatch.setenv(auth.TOTP_SECRET_VAR, "JBSW Y3DP EHPK 3PXP")
    assert len(auth.current_totp()) == 6


def test_a_malformed_secret_says_how_to_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.TOTP_SECRET_VAR, "not-base32-at-all!!")
    with pytest.raises(auth.AuthError, match="base32"):
        auth.current_totp()


def test_the_prompt_path_validates_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "12ab56")
    with pytest.raises(auth.AuthError, match="six digits"):
        auth.current_totp()


def test_the_prompt_path_accepts_six_digits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: " 123456 ")
    assert auth.current_totp() == "123456"


# --- repo hygiene --------------------------------------------------------------


def test_no_dotenv_is_committed() -> None:
    """`.env.example` documents the variables; `.env` must never be tracked."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert ".env" not in tracked
    assert not [p for p in tracked if p.startswith(".env") and p != ".env.example"]


def test_the_example_env_documents_every_variable() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for var in (
        auth.CLIENT_ID_VAR,
        auth.PIN_VAR,
        auth.TOTP_SECRET_VAR,
        auth.ACCESS_TOKEN_VAR,
    ):
        assert var in text, f"{var} is undiscoverable without an entry in .env.example"


def test_the_example_env_holds_no_values() -> None:
    """A filled-in example is a committed credential."""
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("BNFMM_"):
            assert line.endswith("="), f"example must be blank: {line!r}"
