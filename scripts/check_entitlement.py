#!/usr/bin/env python3
"""Probe which Dhan data surfaces this account can actually reach. Read-only.

The Data API is a paid add-on and the docs do not state which endpoints it gates.
Two error codes suggest it gates all of them -- WebSocket disconnect `806`
("Subscribe to Data APIs to continue") and REST `DH-902` ("User has not
subscribed to Data APIs") -- but that inference decides the whole build order, so
it is worth thirty seconds to check rather than assume.

    python scripts/check_entitlement.py

Prints one line per surface: whether it answered, and if not, the exact error code.
Nothing is written to disk. **No endpoint reached here can place, modify or cancel
an order**, and none carries a body other than the query parameters shown.

Credentials come from the environment; see `src/bnfmm/data/auth.py`. With none
set, the script explains what to export and exits 2 without contacting anything.

Exit codes: 0 = every probed surface answered; 1 = at least one is gated;
2 = could not authenticate, so nothing was learned.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from bnfmm.data import auth  # noqa: E402
from bnfmm.data import chain  # noqa: E402
from bnfmm.data import universe as uni  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.dhan.co/v2"
TIMEOUT = 20

# The error code that means "not entitled", as distinct from a bad request. Both
# spellings are checked because the REST body and the docs disagree on case.
GATED_CODES = ("DH-902", "DH902")


@dataclass
class Probe:
    """One endpoint, its verdict, and enough detail to act on it."""

    surface: str
    endpoint: str
    why_it_matters: str
    ok: bool = False
    gated: bool = False
    detail: str = "not attempted"

    @property
    def verdict(self) -> str:
        if self.ok:
            return "OK"
        return "GATED" if self.gated else "ERROR"


def _classify(probe: Probe, response: requests.Response) -> None:
    """Fill in a probe's verdict from the raw response.

    Deliberately reads the body rather than trusting the status code: Dhan returns
    the entitlement error inside a 400-class body, and the code is the whole point
    of running this.
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:200]

    blob = json.dumps(body) if not isinstance(body, str) else body
    if any(code in blob for code in GATED_CODES):
        probe.gated = True
        probe.detail = f"HTTP {response.status_code}, entitlement error in body"
        return

    if response.status_code == 200 and not (
        isinstance(body, dict) and body.get("status") == "failure"
    ):
        probe.ok = True
        probe.detail = f"HTTP 200, {_shape(body)}"
        return

    probe.detail = f"HTTP {response.status_code}: {blob[:160]}"


def _shape(body: Any) -> str:
    """A one-line description of a payload, without printing market data."""
    if isinstance(body, dict):
        data = body.get("data", body)
        if isinstance(data, list):
            return f"{len(data)} records"
        if isinstance(data, dict):
            return f"keys={sorted(data)[:6]}"
    if isinstance(body, list):
        return f"{len(body)} records"
    return type(body).__name__


def probe_profile(session: auth.Session) -> Probe:
    p = Probe(
        surface="profile",
        endpoint="GET /v2/profile",
        why_it_matters=(
            "token validity and account setup. Free, so a failure here is an auth "
            "problem rather than an entitlement one."
        ),
    )
    r = requests.get(f"{API_BASE}/profile", headers=session.headers(), timeout=TIMEOUT)
    _classify(p, r)
    return p


def probe_option_chain(session: auth.Session, underlying_scrip: int, expiry: date | str) -> Probe:
    """The cheapest way to settle Phase 0's one open question.

    `/optionchain` returns best bid/ask across every strike, so a handful of polls
    measures the real quoted spread against the Rs 2.37-Rs 3.94 breakeven without
    touching the depth WebSocket at all.

    ``UnderlyingScrip`` is the **index** security id (NIFTY BANK = 25) with
    ``UnderlyingSeg: IDX_I``. Passing the futures id instead -- which this script
    did until the subscription made it testable -- is the failure worth naming,
    because the endpoint answers with an empty-looking body rather than an error
    and the probe would report OK while tomorrow's poller recorded nothing.

    The body is built by `chain.chain_request`, the same function the live poller
    uses, so a probe that answers is evidence about the real request rather than
    about a second hand-written copy of it.
    """
    p = Probe(
        surface="option chain",
        endpoint="POST /v2/optionchain",
        why_it_matters="best bid/ask across all strikes -> the measured-spread question",
    )
    r = requests.post(
        f"{API_BASE}/optionchain",
        headers={**session.headers(), "Content-Type": "application/json"},
        json=chain.chain_request(underlying_scrip, chain.IDX_SEGMENT, expiry),
        timeout=TIMEOUT,
    )
    _classify(p, r)
    if p.ok:
        # An entitled-but-wrong request still returns HTTP 200, so count the legs.
        # Zero strikes means the scrip or segment is wrong, not that the market is
        # closed -- the chain is served from the last session either way.
        try:
            snapshot = chain.parse_chain(r.content, expiry=str(expiry))
        except Exception as exc:  # noqa: BLE001 - a parse failure is itself the finding
            p.ok = False
            p.detail = f"HTTP 200 but unparseable: {type(exc).__name__}: {exc}"
        else:
            p.detail = (
                f"HTTP 200, {len(snapshot.legs)} legs across {len(snapshot.strikes)} "
                f"strikes, underlying last {snapshot.last_price}"
            )
            if not snapshot.legs:
                p.ok = False
                p.detail += " -- EMPTY: check UnderlyingScrip is the index id, not the future"
    return p


def probe_expiry_list(session: auth.Session, underlying_scrip: int) -> Probe:
    """Cross-check the downloaded master against the expiries the vendor will answer for.

    Cheap, and it catches the one silent failure the master cannot: a stale local
    CSV whose front expiry the API no longer serves. Run before capture, not after.
    """
    p = Probe(
        surface="expiry list",
        endpoint="POST /v2/optionchain/expirylist",
        why_it_matters="validates the master's expiries against the vendor's own list",
    )
    r = requests.post(
        f"{API_BASE}/optionchain/expirylist",
        headers={**session.headers(), "Content-Type": "application/json"},
        json=chain.expiry_list_request(underlying_scrip, chain.IDX_SEGMENT),
        timeout=TIMEOUT,
    )
    _classify(p, r)
    if p.ok:
        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            data = data.get("data", [])
        listed = [str(x) for x in data] if isinstance(data, list) else []
        p.detail = f"HTTP 200, {len(listed)} expiries: {listed[:3]}"
        if not listed:
            p.ok = False
            p.detail += " -- EMPTY: wrong UnderlyingScrip or segment"
    return p


def probe_intraday(session: auth.Session, security_id: int) -> Probe:
    """Tier B. If this is gated, no historical modelling happens before paying."""
    p = Probe(
        surface="intraday history",
        endpoint="POST /v2/charts/intraday",
        why_it_matters="Tier B 1-minute bars -- the whole pre-capture modelling path",
    )
    today = date.today()
    r = requests.post(
        f"{API_BASE}/charts/intraday",
        headers={**session.headers(), "Content-Type": "application/json"},
        json={
            "securityId": str(security_id),
            "exchangeSegment": "NSE_FNO",
            "instrument": "FUTIDX",
            "interval": "1",
            "fromDate": str(today - timedelta(days=5)),
            "toDate": str(today),
        },
        timeout=TIMEOUT,
    )
    _classify(p, r)
    return p


def probe_expired_options(session: auth.Session) -> Probe:
    """Undocumented but present in the SDK, and exactly the right Tier B source.

    `dhanhq._historical_data.expired_options_data` posts to `/charts/rollingoption`
    with ATM-relative strikes and an expiry code, which is how you get five years
    of option bars without knowing historical strike ladders. It is absent from the
    public docs, so its availability is a genuine unknown worth probing.
    """
    p = Probe(
        surface="expired options (rolling)",
        endpoint="POST /v2/charts/rollingoption",
        why_it_matters="5y of ATM-relative option bars; undocumented, SDK-only",
    )
    today = date.today()
    r = requests.post(
        f"{API_BASE}/charts/rollingoption",
        headers={**session.headers(), "Content-Type": "application/json"},
        json={
            "exchangeSegment": "NSE_FNO",
            "underlying": "BANKNIFTY",
            "expiryFlag": "MONTH",  # BANKNIFTY has no weeklies
            "expiryCode": 0,
            "strike": 0,  # ATM-relative
            "drvOptionType": "CALL",
            "requiredData": ["close", "volume", "oi"],
            "interval": 5,
            "fromDate": str(today - timedelta(days=30)),
            "toDate": str(today),
        },
        timeout=TIMEOUT,
    )
    _classify(p, r)
    return p


def render(probes: list[Probe]) -> str:
    lines = ["", "Dhan data-surface entitlement", "=" * 60]
    for p in probes:
        lines += [
            f"  [{p.verdict:5}] {p.surface:26} {p.endpoint}",
            f"          {p.detail}",
            f"          why: {p.why_it_matters}",
            "",
        ]

    gated = [p for p in probes if p.gated]
    errored = [p for p in probes if not p.ok and not p.gated]

    lines.append("-" * 60)
    if not gated and not errored:
        lines += [
            "Every probed surface answered. The Data API is active on this account,",
            "so capture can start immediately and Tier B backfill is available now.",
        ]
    elif len(gated) == len([p for p in probes if p.surface != "profile"]):
        lines += [
            "All market-data surfaces are gated, including 1-minute history.",
            "",
            "This confirms the planning assumption and the build order that follows",
            "from it: build the decoder, writer and QA against synthetic fixtures",
            "first, then subscribe for one month and spend it capturing rather than",
            "debugging. See README Status.",
        ]
    else:
        lines.append("Mixed result -- the entitlement is finer-grained than assumed:")
        lines += [f"  gated:   {p.surface}" for p in gated]
        lines += [f"  errored: {p.surface} ({p.detail[:60]})" for p in errored]
        lines += [
            "",
            "Anything that answered can be used before subscribing. Record which,",
            "in DECISIONS.md -- it changes the phase ordering.",
        ]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help="capture.yaml, for the index scrip and the real front expiry",
    )
    parser.add_argument(
        "--security-id",
        type=int,
        default=None,
        help="skip instrument resolution and probe intraday history with this id",
    )
    parser.add_argument(
        "--underlying-scrip",
        type=int,
        default=None,
        help="index security id for the option chain (default: from capture.yaml)",
    )
    parser.add_argument("--expiry", default=None, help="YYYY-MM-DD; default: resolved front expiry")
    args = parser.parse_args()

    auth.load_dotenv(REPO_ROOT / ".env")

    try:
        session = auth.login()
    except auth.AuthError as exc:
        print(f"cannot authenticate: {exc}", file=sys.stderr)
        print(
            "\nNothing was contacted. Export these and retry:\n"
            f"  {auth.CLIENT_ID_VAR}, {auth.PIN_VAR}\n"
            f"  optionally {auth.TOTP_SECRET_VAR} (else you are prompted for 6 digits)\n"
            f"  or {auth.ACCESS_TOKEN_VAR} to reuse a token minted elsewhere",
            file=sys.stderr,
        )
        return 2

    print(f"authenticated: {session!r}", file=sys.stderr)

    config_path = Path(args.config or REPO_ROOT / "config" / "capture.yaml")
    security_id = args.security_id
    underlying_scrip = args.underlying_scrip
    expiry: date | str | None = args.expiry

    if security_id is None or underlying_scrip is None or expiry is None:
        # The *capture* resolver, not instruments.resolve(): the latter's
        # min_days_to_expiry filter would skip the front expiry that is 2 days out,
        # which is exactly the contract tomorrow's session records.
        config = uni.load_capture_config(config_path)
        universe = uni.resolve_capture_from_disk(config_path)
        if underlying_scrip is None:
            underlying_scrip = int(config.get("option_chain", {}).get("underlying_scrip", 25))
        if expiry is None:
            expiry = universe.front_expiry
        if security_id is None:
            # all_contracts() is keyed by security id, so iterate the values --
            # iterating the mapping itself yields ints and fails on `.instrument`.
            futures = [c for c in universe.all_contracts().values() if c.instrument == "FUTIDX"]
            if not futures:
                print("no futures contract resolved; pass --security-id", file=sys.stderr)
                return 2
            security_id = futures[0].security_id
            print(f"probing intraday with {futures[0].display_name} (id={security_id})", file=sys.stderr)
        print(
            f"probing the chain with UnderlyingScrip={underlying_scrip} "
            f"(IDX_I), expiry {expiry}",
            file=sys.stderr,
        )

    probes = [probe_profile(session)]
    if not probes[0].ok:
        print(render(probes))
        print("profile failed, so the remaining probes would be uninformative.", file=sys.stderr)
        return 2

    probes += [
        probe_expiry_list(session, underlying_scrip),
        probe_option_chain(session, underlying_scrip, expiry),
        probe_intraday(session, security_id),
        probe_expired_options(session),
    ]

    print(render(probes))
    return 0 if all(p.ok for p in probes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
