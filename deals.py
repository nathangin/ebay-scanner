"""Deal scoring shared by the scanners.

    fair_value = NM market price x multiplier
    cost       = item price + shipping (shipping only when known up front)
    discount % = (fair_value - cost) / fair_value x 100

The multiplier comes from config.json: ``grade_multipliers`` for graded cards
(e.g. "PSA 10": 3.0) and ``condition_multipliers`` for raw cards (e.g. "LP": 0.70).
A listing is a deal when its discount is at least the configured threshold.

With a ``resale`` table (config.json), the score also estimates the profit from
reselling the card at fair value on eBay, after eBay's fees and the postage you pay:

    fees          = fair_value x (1 + sales tax) x fee % + per-order fee
    resale profit = fair_value - fees - postage - cost

and a deal must also clear ``min_profit`` dollars of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# Selling costs for the resale estimate, overridable in config.json under "resale".
# eBay's final value fee for trading cards is 13.25-13.6% (sources disagree; 13.6% is the
# conservative end) for sellers without a store, charged on everything the buyer pays,
# sales tax included, plus a per-order fee. Check eBay's selling-fees page for your account.
DEFAULT_RESALE: dict[str, float | None] = {
    "fee_pct":         13.6,   # final value fee, % of what the buyer pays
    "sales_tax_pct":    7.0,   # typical US sales tax; the fee is charged on it too
    "order_fee":        0.40,  # per order over $10
    "order_fee_small":  0.30,  # per order of $10 or less
    "postage_raw":      1.00,  # mailing a raw card (envelope, toploader), free shipping to the buyer
    "postage_graded":   5.00,  # tracked shipping for a slab
    "min_profit":       5.00,  # a deal must make at least this; None = judge on discount alone
}


@dataclass(frozen=True)
class DealScore:
    fair_value: float
    discount_pct: float
    is_deal: bool
    resale_profit: float | None = None   # None when no resale table was given


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


def resale_profit(sale_price: float, cost: float, graded: bool,
                  resale: Mapping[str, Any] | None = None) -> float:
    """Profit from reselling at ``sale_price`` on eBay with free shipping, after fees and postage."""
    r = {**DEFAULT_RESALE, **(resale or {})}
    fees = sale_price * (1 + r["sales_tax_pct"] / 100) * r["fee_pct"] / 100
    fees += r["order_fee_small"] if sale_price <= 10 else r["order_fee"]
    postage = r["postage_graded"] if graded else r["postage_raw"]
    return round(sale_price - fees - postage - cost, 2)


def score_deal(nm_price: float, multiplier: float, cost: float, threshold: float,
               graded: bool = False, resale: Mapping[str, Any] | None = None) -> DealScore:
    """Fair value, discount and deal flag for one listing (cost = price + shipping).

    With ``resale``, also the resale profit, and the listing must make ``min_profit`` to be a deal.
    """
    fair_value = round(nm_price * multiplier, 2)
    discount   = discount_pct(fair_value, cost)
    is_deal    = discount >= threshold
    if resale is None:
        return DealScore(fair_value, discount, is_deal)
    profit = resale_profit(fair_value, cost, graded, resale)
    min_profit = {**DEFAULT_RESALE, **resale}["min_profit"]
    if min_profit is not None:
        is_deal = is_deal and profit >= min_profit
    return DealScore(fair_value, discount, is_deal, profit)
