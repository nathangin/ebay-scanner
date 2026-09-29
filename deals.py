"""Deal scoring shared by the scanners.

    fair_value = NM market price x multiplier
    cost       = item price + shipping (shipping only when known up front)
    discount % = (fair_value - cost) / fair_value x 100

The multiplier comes from config.json: ``grade_multipliers`` for graded cards
(e.g. "PSA 10": 3.0) and ``condition_multipliers`` for raw cards (e.g. "LP": 0.70).
A listing is a deal when its discount is at least the configured threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class DealScore:
    fair_value: float
    discount_pct: float
    is_deal: bool


def resolve_multiplier(
    is_graded: bool,
    grading_company: str | None,
    grade: str | None,
    condition: str | None,
    grade_mults: Mapping[str, float],
    cond_mults: Mapping[str, float],
) -> float:
    """Multiplier applied to the NM price for this card's grade or condition.

    Graded: exact "PSA 10" key, then a company-only key ("PSA"), else 1.0.
    Raw:    the condition key ("LP"), else the "Unknown" entry (0.70 default).
    """
    if is_graded:
        key = f"{grading_company} {grade}" if grade else (grading_company or "")
        return float(grade_mults.get(key, grade_mults.get(grading_company or "", 1.0)))
    return float(cond_mults.get(condition or "Unknown", cond_mults.get("Unknown", 0.70)))


def discount_pct(reference_price: float, cost: float) -> float:
    """Percent the cost is below the reference price (negative = above), 1 decimal."""
    if not reference_price or reference_price <= 0:
        return 0.0
    return round((reference_price - cost) / reference_price * 100, 1)


def score_deal(nm_price: float, multiplier: float, cost: float, threshold: float) -> DealScore:
    """Fair value, discount and deal flag for one listing (cost = price + shipping)."""
    fair_value = round(nm_price * multiplier, 2)
    discount   = discount_pct(fair_value, cost)
    return DealScore(fair_value, discount, discount >= threshold)
