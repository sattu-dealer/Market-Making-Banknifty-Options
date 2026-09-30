"""Tests for the reporting spine: paired grids and tier labelling.

These are guard tests, not arithmetic tests. They exist so that the two reporting
rules the project committed to -- every figure paired, every figure tiered -- fail
loudly when violated instead of quietly degrading into a single unlabelled number
somewhere in the README.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from bnfmm.analysis.report import (
    grid_table,
    headline,
    markdown_table,
    paired_columns,
    tier_caption,
)
from bnfmm.analysis.results import HEADLINE_TIERS, PnlGrid, Queue, Regime, Tier


def make_grid(**overrides) -> PnlGrid:
    kwargs = {
        "tier": Tier.A_REAL_L2,
        "label": "naive fill, 2026-08-24",
        "member_pessimistic": 1200.0,
        "member_proportional": 1500.0,
        "retail_pessimistic": 300.0,
        "retail_proportional": 600.0,
    }
    kwargs.update(overrides)
    return PnlGrid(**kwargs)  # type: ignore[arg-type]


# --- construction is total ----------------------------------------------------


def test_a_missing_corner_is_a_type_error() -> None:
    """The core guarantee: there is no way to build a three-corner grid.

    Python raises on the missing argument, so this is enforced by the dataclass
    rather than by a check that could be forgotten. The test pins the behaviour
    against a future refactor that gives the corners defaults.
    """
    with pytest.raises(TypeError):
        PnlGrid(  # type: ignore[call-arg]
            tier=Tier.A_REAL_L2,
            label="incomplete",
            member_pessimistic=1.0,
            member_proportional=2.0,
            retail_pessimistic=3.0,
        )


def test_corners_have_no_defaults() -> None:
    """Belt and braces: assert the absence of defaults directly.

    Without this, someone could add ``= 0.0`` to the corners and the test above
    would still pass while silently reporting zero for an unmeasured regime.
    """
    fields = {f.name: f for f in dataclasses.fields(PnlGrid)}
    for name in (
        "member_pessimistic",
        "member_proportional",
        "retail_pessimistic",
        "retail_proportional",
        "tier",
        "label",
    ):
        assert fields[name].default is dataclasses.MISSING, f"{name} must stay required"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_corners_are_rejected(bad: float) -> None:
    """A NaN corner is an unmeasured corner wearing a measured one's type."""
    with pytest.raises(ValueError, match="non-finite corner"):
        make_grid(retail_pessimistic=bad)


def test_empty_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="unciteable"):
        make_grid(label="")


def test_tier_must_be_a_tier() -> None:
    """A bare string would defeat the ``HEADLINE_TIERS`` check downstream."""
    with pytest.raises(TypeError, match="must be a Tier"):
        make_grid(tier="A")


def test_grid_is_immutable() -> None:
    grid = make_grid()
    with pytest.raises(dataclasses.FrozenInstanceError):
        grid.retail_pessimistic = 0.0  # type: ignore[misc]


# --- the interval the grid represents -----------------------------------------


def test_corners_cover_the_full_cross_product() -> None:
    corners = make_grid().corners
    assert set(corners) == {(r, q) for r in Regime for q in Queue}
    assert len(corners) == 4


def test_best_and_worst_bracket_every_corner() -> None:
    grid = make_grid()
    assert grid.worst == 300.0
    assert grid.best == 1500.0
    assert grid.span == pytest.approx(1200.0)
    assert all(grid.worst <= v <= grid.best for v in grid.corners.values())


def test_get_matches_the_named_fields() -> None:
    grid = make_grid()
    assert grid.get(Regime.RETAIL, Queue.PESSIMISTIC) == grid.retail_pessimistic
    assert grid.get(Regime.MEMBER, Queue.PROPORTIONAL) == grid.member_proportional


def test_an_all_positive_grid_does_not_flip() -> None:
    grid = make_grid()
    assert not grid.sign_flips
    assert grid.negative_corners() == ()
    assert "SIGN FLIPS" not in grid.describe()


def test_a_sign_flip_is_detected_and_named() -> None:
    """The case the project expects and the one it must not average away.

    Profitable for an exchange member on an optimistic queue, loss-making for a
    retail account on a pessimistic one. The honest report names the corner.
    """
    grid = make_grid(retail_pessimistic=-450.0)
    assert grid.sign_flips
    assert grid.negative_corners() == ((Regime.RETAIL, Queue.PESSIMISTIC),)
    assert "SIGN FLIPS" in grid.describe()
    assert "retail/pessimistic" in grid.describe()


def test_an_all_negative_grid_does_not_flip() -> None:
    """Uniformly negative is a robust conclusion, not an ambiguous one."""
    grid = make_grid(
        member_pessimistic=-1.0,
        member_proportional=-2.0,
        retail_pessimistic=-3.0,
        retail_proportional=-4.0,
    )
    assert not grid.sign_flips
    assert len(grid.negative_corners()) == 4


# --- tier labelling -----------------------------------------------------------


def test_every_tier_has_a_label() -> None:
    for tier in Tier:
        assert tier.label
        assert tier.label != tier.value


def test_only_tier_c_is_synthetic() -> None:
    assert not Tier.C_SYNTHETIC.is_real
    assert Tier.A_REAL_L2.is_real
    assert Tier.B_REAL_1MIN.is_real


def test_synthetic_caption_says_so_in_words() -> None:
    """BRIEFING.md §2: the label must be unmissable, not a footnote."""
    caption = tier_caption(Tier.C_SYNTHETIC)
    assert "SYNTHETIC" in caption
    assert "Not a market-data result" in caption
    assert "BANKNIFTY" in caption


def test_real_tier_captions_do_not_claim_synthetic() -> None:
    for tier in (Tier.A_REAL_L2, Tier.B_REAL_1MIN):
        assert "SYNTHETIC" not in tier_caption(tier)
        assert "real" in tier_caption(tier)


def test_headline_accepts_only_captured_depth() -> None:
    assert HEADLINE_TIERS == frozenset({Tier.A_REAL_L2})
    assert headline(make_grid(tier=Tier.A_REAL_L2))


@pytest.mark.parametrize("tier", [Tier.B_REAL_1MIN, Tier.C_SYNTHETIC])
def test_headline_refuses_a_tier_that_cannot_support_one(tier: Tier) -> None:
    """The mechanical form of the briefing's non-negotiable constraint.

    Tier C is synthetic. Tier B is real, but 1-minute bars contain no queue at
    all, so a queue-position claim built on them would be fiction with real
    provenance -- arguably the worse failure of the two.
    """
    with pytest.raises(ValueError, match="refusing to render a headline"):
        headline(make_grid(tier=tier))


def test_headline_error_names_the_tier_and_the_remedy() -> None:
    with pytest.raises(ValueError) as excinfo:
        headline(make_grid(tier=Tier.C_SYNTHETIC))
    message = str(excinfo.value)
    assert "SYNTHETIC" in message
    assert "tier caption" in message, "the error should say what to do instead"


# --- rendering ----------------------------------------------------------------


def test_grid_table_shows_all_four_corners_and_both_regimes() -> None:
    grid = make_grid()
    table = grid_table(grid)
    for regime in Regime:
        assert regime.value in table
    for queue in Queue:
        assert queue.value in table
    for value in grid.corners.values():
        assert f"{value:,.2f}" in table
    assert grid.label in table
    assert grid.tier.label in table


def test_grid_table_warns_when_the_sign_is_not_robust() -> None:
    table = grid_table(make_grid(retail_pessimistic=-450.0))
    assert "sign is not robust" in table
    assert "`retail/pessimistic`" in table
    assert "averaged" in table, "must say explicitly that averaging is not the answer"


def test_grid_table_does_not_warn_when_the_sign_is_robust() -> None:
    assert "sign is not robust" not in grid_table(make_grid())


def test_markdown_table_is_rectangular() -> None:
    table = markdown_table(["a", "bbbb"], [["1", "2"], ["333", "4"]])
    lines = table.splitlines()
    assert len(lines) == 4, "header, separator, two rows"
    assert len({len(line) for line in lines}) == 1, "columns must be aligned"
    assert set(lines[1]) <= {"|", "-"}


def test_markdown_table_handles_no_rows() -> None:
    table = markdown_table(["a", "b"], [])
    assert len(table.splitlines()) == 2


def test_paired_columns_labels_every_profile() -> None:
    assert paired_columns(["member", "dhan"]) == ["(member)", "(dhan)"]
