"""Result containers that make an under-qualified number unrepresentable.

Two labelling rules govern everything this project publishes, and both are the
kind that decay if they depend on remembering them:

1. **Every PnL figure is a 2x2**, over brokerage regime (member / retail) and
   cancellation convention (pessimistic / proportional). There is no single
   headline number, by the user's explicit decision -- see ``DECISIONS.md`` #13.
2. **Every figure carries its data tier.** ``BRIEFING.md`` calls conflating real
   and synthetic data the one thing that "damages credibility badly if probed",
   so :class:`Tier` is a required field, not an optional annotation.

So :class:`PnlGrid` cannot be constructed with a missing corner (Python raises
on the missing argument), with a non-finite corner (rejected below), or without
a tier. A caller who wants to report one number has to reach past the type to
do it, which is the point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Regime(str, Enum):
    """Brokerage regime. Both are always reported; neither is the headline.

    ``MEMBER`` is an exchange member trading own account: statutory charges
    only. ``RETAIL`` is Dhan's flat Rs 20 per executed order. The member case
    is the most favourable regime that could plausibly be modelled and is
    included as a bound, not as a realistic scenario.
    """

    MEMBER = "member"
    RETAIL = "retail"


class Queue(str, Enum):
    """Cancellation convention, which bounds the unobservable.

    Between two depth snapshots at one price level,
    ``dqty = adds - cancels - trades``. Trades are observable, so
    ``adds - cancels`` is recoverable but ``cancels`` alone is not -- and
    cancellations *ahead of* a resting order are exactly what decides whether
    it fills. Rather than pick a convention, report both bounds.

    ``PESSIMISTIC``: all queue shrinkage happens behind the order, which never
    advances on cancels. Lower bound on fills.
    ``PROPORTIONAL``: cancels distributed uniformly across the queue. Upper
    bound on fills.
    """

    PESSIMISTIC = "pessimistic"
    PROPORTIONAL = "proportional"


class Tier(str, Enum):
    """Provenance of the data behind a figure. Required, never inferred."""

    A_REAL_L2 = "A"
    B_REAL_1MIN = "B"
    C_SYNTHETIC = "C"

    @property
    def label(self) -> str:
        return _TIER_LABELS[self]

    @property
    def is_real(self) -> bool:
        return self is not Tier.C_SYNTHETIC


_TIER_LABELS = {
    Tier.A_REAL_L2: "Tier A - real captured 20-level depth",
    Tier.B_REAL_1MIN: "Tier B - real 1-minute bars",
    Tier.C_SYNTHETIC: "Tier C - SYNTHETIC, calibrated flow",
}

# Tiers whose provenance is strong enough to carry the README's headline claim.
# Tier B is real but 1-minute bars cannot support a queue-position result at
# all, and Tier C is synthetic, so both are excluded.
HEADLINE_TIERS = frozenset({Tier.A_REAL_L2})


@dataclass(frozen=True)
class PnlGrid:
    """A PnL figure over both brokerage regimes and both queue conventions.

    All four corners are required. The most useful thing the grid tells you is
    not any single corner but whether the *sign* survives all of them: if it
    does not, :attr:`sign_flips` is true and the honest headline names the
    corner where it changes rather than quoting an average.

    ``value`` units are rupees unless the caller says otherwise in ``unit``.
    """

    tier: Tier
    label: str
    member_pessimistic: float
    member_proportional: float
    retail_pessimistic: float
    retail_proportional: float
    unit: str = "Rs"

    def __post_init__(self) -> None:
        if not isinstance(self.tier, Tier):
            raise TypeError(f"tier must be a Tier, got {type(self.tier).__name__}")
        if not self.label:
            raise ValueError("label must be non-empty: an unlabelled grid is unciteable")
        for corner, value in self.corners.items():
            if not math.isfinite(value):
                regime, queue = corner
                raise ValueError(
                    f"{regime.value}/{queue.value} corner is {value!r}; a grid with a "
                    "non-finite corner is a partial result wearing a complete one's type"
                )

    @property
    def corners(self) -> dict[tuple[Regime, Queue], float]:
        return {
            (Regime.MEMBER, Queue.PESSIMISTIC): self.member_pessimistic,
            (Regime.MEMBER, Queue.PROPORTIONAL): self.member_proportional,
            (Regime.RETAIL, Queue.PESSIMISTIC): self.retail_pessimistic,
            (Regime.RETAIL, Queue.PROPORTIONAL): self.retail_proportional,
        }

    def get(self, regime: Regime, queue: Queue) -> float:
        return self.corners[(regime, queue)]

    @property
    def best(self) -> float:
        """The most flattering corner -- normally member/proportional."""
        return max(self.corners.values())

    @property
    def worst(self) -> float:
        """The least flattering corner -- normally retail/pessimistic."""
        return min(self.corners.values())

    @property
    def span(self) -> float:
        """Width of the interval the four corners imply."""
        return self.best - self.worst

    @property
    def sign_flips(self) -> bool:
        """True when the corners disagree about whether the result is positive.

        This is the finding, when it happens: it means the answer is determined
        by the modelling assumption rather than by the market.
        """
        return self.worst < 0.0 < self.best

    def negative_corners(self) -> tuple[tuple[Regime, Queue], ...]:
        return tuple(corner for corner, v in self.corners.items() if v < 0.0)

    def describe(self) -> str:
        """One line, safe to put in a log or a commit message."""
        body = (
            f"{self.label} [{self.tier.value}]: "
            f"{self.unit} {self.worst:,.2f} .. {self.unit} {self.best:,.2f}"
        )
        if self.sign_flips:
            flipped = ", ".join(f"{r.value}/{q.value}" for r, q in self.negative_corners())
            return f"{body} -- SIGN FLIPS (negative at {flipped})"
        return body
