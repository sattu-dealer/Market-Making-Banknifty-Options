"""Markdown rendering for results, with the labelling rules enforced here.

Every table this module emits carries its data tier, and :func:`headline`
refuses to render a claim from a tier that cannot support one. That is the
mechanical form of ``BRIEFING.md``'s non-negotiable constraint: real captured
data and synthetic data must never be presented as though they were the same
thing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .results import HEADLINE_TIERS, PnlGrid, Queue, Regime, Tier


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """A padded GitHub-flavoured markdown table.

    Padded rather than minimal so the raw markdown stays readable in a diff --
    these files are reviewed as text as often as they are rendered.
    """
    rows = [list(r) for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
        for i, h in enumerate(headers)
    ]
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    out = ["|" + "|".join(f" {h:<{w}} " for h, w in zip(headers, widths)) + "|", sep]
    out += [
        "|" + "|".join(f" {c:<{w}} " for c, w in zip(row, widths)) + "|" for row in rows
    ]
    return "\n".join(out)


def tier_caption(tier: Tier) -> str:
    """The caption that must accompany any figure from ``tier``."""
    if tier is Tier.C_SYNTHETIC:
        return (
            f"**{tier.label}.** Not a market-data result. Numbers here come from a "
            "flow generator and are reported to exercise the pipeline, not to "
            "describe BANKNIFTY."
        )
    return f"**{tier.label}.**"


def grid_table(grid: PnlGrid) -> str:
    """Render a :class:`PnlGrid` as a 2x2 with its tier caption."""
    headers = ["", *(f"{q.value} queue" for q in Queue)]
    rows = [
        [
            f"**{regime.value}**",
            *(f"{grid.unit} {grid.get(regime, q):,.2f}" for q in Queue),
        ]
        for regime in Regime
    ]
    parts = [
        f"*{grid.label}*",
        "",
        markdown_table(headers, rows),
        "",
        tier_caption(grid.tier),
    ]
    if grid.sign_flips:
        flipped = ", ".join(f"`{r.value}/{q.value}`" for r, q in grid.negative_corners())
        parts += [
            "",
            f"**The sign is not robust across the grid** (negative at {flipped}). "
            "The result is therefore determined by the modelling assumption as much "
            "as by the data, and is reported as such rather than averaged.",
        ]
    return "\n".join(parts)


def paired_columns(profiles: Sequence[str]) -> list[str]:
    """Column-header suffixes for a table reported under several regimes."""
    return [f"({p})" for p in profiles]


def headline(grid: PnlGrid) -> str:
    """The README's top-line claim. Refuses tiers that cannot support one.

    Raises
    ------
    ValueError
        If ``grid.tier`` is not in :data:`~bnfmm.analysis.results.HEADLINE_TIERS`.
        Tier C is synthetic; Tier B is real but 1-minute bars cannot evidence a
        queue-position claim. Only real captured depth can.
    """
    if grid.tier not in HEADLINE_TIERS:
        allowed = ", ".join(sorted(t.value for t in HEADLINE_TIERS))
        raise ValueError(
            f"refusing to render a headline from {grid.tier.label}: only tier(s) "
            f"{allowed} can support one. Report this grid in the body with its "
            "tier caption instead."
        )
    return grid.describe()
