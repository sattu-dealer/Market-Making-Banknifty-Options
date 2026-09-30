"""The holdout split, enforced in the loader rather than by convention.

``BRIEFING.md`` §18.5 names the one way the route to breakeven can destroy the
project's credibility: five knobs swept on a handful of days until one prints
positive. The defence is a split frozen *before* the sweeping starts, plus two
records a third party can check afterwards:

* ``reports/holdout_log.md`` -- every time a holdout day is read, with the
  reason given, the protocol hash and the command line. Appended, never edited.
* ``reports/config_log.jsonl`` -- every configuration run on any day, so each
  result can be quoted "from N configurations".

:func:`require_access` refuses a holdout day unless the caller passes an
explicit unlock with a reason. It fails *loudly*, which is the property §5 asks
of every structural guarantee: a default can never satisfy it.

The protocol file's sha256 is stamped on every log line. The repo has no
commits yet, so a git SHA is not available; the content hash is what makes an
edited protocol visible.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_PATH = _REPO_ROOT / "config" / "frozen" / "protocol.yaml"
HOLDOUT_LOG = _REPO_ROOT / "reports" / "holdout_log.md"
CONFIG_LOG = _REPO_ROOT / "reports" / "config_log.jsonl"


class HoldoutLocked(RuntimeError):
    """A holdout day was requested without an explicit, reasoned unlock."""


class ProtocolError(ValueError):
    """The frozen protocol file is malformed or self-contradictory."""


@dataclass(frozen=True, slots=True)
class Inclusion:
    max_gap_s: float
    min_depth_coverage: float
    min_feed_coverage: float


@dataclass(frozen=True, slots=True)
class Protocol:
    develop: tuple[date, ...]
    holdout: tuple[date, ...]
    excluded: dict[date, str]
    expiries: dict[date, date]
    default_expiry: date
    inclusion: Inclusion
    baseline: dict = field(default_factory=dict)
    sha256: str = ""
    path: Path = PROTOCOL_PATH

    @property
    def days(self) -> tuple[date, ...]:
        """Every analysable day, develop first then holdout, in date order."""
        return tuple(sorted(self.develop + self.holdout))

    def expiry_for(self, day: date) -> date:
        return self.expiries.get(day, self.default_expiry)

    def is_holdout(self, day: date) -> bool:
        return day in self.holdout


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def load_protocol(path: Path | str = PROTOCOL_PATH) -> Protocol:
    path = Path(path)
    raw_bytes = path.read_bytes()
    raw = yaml.safe_load(raw_bytes)
    develop = tuple(sorted(_as_date(d) for d in raw["develop"]))
    holdout = tuple(sorted(_as_date(d) for d in raw["holdout"]))
    excluded = {_as_date(k): str(v).strip() for k, v in (raw.get("excluded") or {}).items()}

    overlap = (set(develop) & set(holdout)) | ((set(develop) | set(holdout)) & set(excluded))
    if overlap:
        raise ProtocolError(f"days in more than one set: {sorted(overlap)}")
    if develop and holdout and max(develop) >= min(holdout):
        raise ProtocolError("the holdout must be strictly later than every development day")

    exp = dict(raw.get("expiry") or {})
    if "default" not in exp:
        raise ProtocolError("expiry map needs a 'default' entry")
    default_expiry = _as_date(exp.pop("default"))
    inc = raw["inclusion"]
    return Protocol(
        develop=develop,
        holdout=holdout,
        excluded=excluded,
        expiries={_as_date(k): _as_date(v) for k, v in exp.items()},
        default_expiry=default_expiry,
        inclusion=Inclusion(
            max_gap_s=float(inc["max_gap_s"]),
            min_depth_coverage=float(inc["min_depth_coverage"]),
            min_feed_coverage=float(inc["min_feed_coverage"]),
        ),
        baseline=dict(raw.get("baseline") or {}),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        path=path,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_access(
    protocol: Protocol,
    days: Iterable[date],
    *,
    unlock_reason: str | None = None,
    log_path: Path | str = HOLDOUT_LOG,
    argv: list[str] | None = None,
) -> list[date]:
    """Return ``days`` if they may be read, else raise.

    Unknown and excluded days raise :class:`ProtocolError`. Holdout days raise
    :class:`HoldoutLocked` unless ``unlock_reason`` is a non-empty string, in
    which case the unlock is appended to ``log_path`` before returning.
    """
    days = sorted(set(days))
    for d in days:
        if d in protocol.excluded:
            raise ProtocolError(f"{d} is excluded: {protocol.excluded[d]}")
        if d not in protocol.develop and d not in protocol.holdout:
            raise ProtocolError(f"{d} is in neither the develop nor the holdout set")
    locked = [d for d in days if protocol.is_holdout(d)]
    if not locked:
        return days
    if not (unlock_reason and unlock_reason.strip()):
        raise HoldoutLocked(
            f"holdout day(s) {[str(d) for d in locked]} requested without an unlock. "
            f"Pass --unlock-holdout 'REASON'; the unlock is logged to {Path(log_path).name}."
        )
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.exists()
    with open(log_path, "a", encoding="utf-8") as fh:
        if new:
            fh.write(
                "# Holdout unlock log\n\n"
                "Appended by `bnfmm.analysis.holdout.require_access` every time a holdout "
                "day is read. Never edit by hand. Protocol: `config/frozen/protocol.yaml`.\n\n"
                "| When (UTC) | Days | Reason | Protocol sha256 | Command |\n"
                "|---|---|---|---|---|\n"
            )
        cmd = " ".join(argv if argv is not None else sys.argv).replace("|", "\\|")
        fh.write(
            f"| {_now()} | {', '.join(str(d) for d in locked)} | "
            f"{unlock_reason.strip().replace('|', '/')} | `{protocol.sha256[:12]}` | `{cmd}` |\n"
        )
    return days


def log_config(
    protocol: Protocol,
    record: dict,
    *,
    log_path: Path | str = CONFIG_LOG,
) -> int:
    """Append one configuration record; return the running count N."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": _now(), "protocol_sha256": protocol.sha256, **record}
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    return count_configs(log_path)


def count_configs(log_path: Path | str = CONFIG_LOG) -> int:
    """Distinct configurations tried, ignoring which days they were run on."""
    log_path = Path(log_path)
    if not log_path.exists():
        return 0
    seen = set()
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            seen.add(json.dumps(json.loads(line).get("config"), sort_keys=True))
    return len(seen)
