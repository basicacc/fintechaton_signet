"""Product comparison and allocation rationale.

Everything here is computed from the catalogue, the client's facts and the rule
packs. Nothing is written by the language model, so the figures and reasons
shown to the client are always consistent with what the verifier checked.
Return figures are the catalogue's illustrative history, not forecasts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .models import Client, Product, Proposal
from .rules import RulePack
from .verifier import check, max_amount

TYPE_LABEL = {"deposit": "Bank deposit", "wealth": "Wealth product", "fund": "Fund", "bond": "Bond",
              "insurance_investment": "Investment-linked insurance"}


def _level(client: Client) -> int:
    a = client.current_assessment
    level = a.level if a else 0
    return min(level, client.level_cap) if client.level_cap else level


def _liquidity_label(days: int) -> str:
    if days <= 1:
        return "Same or next day"
    if days <= 7:
        return f"{days} days"
    if days < 365:
        return f"About {round(days / 30)} months"
    return f"{days // 365} year{'s' if days >= 730 else ''}"


def compare(client: Client, catalog: dict[str, Product], product_ids: Sequence[str], packs: Sequence[RulePack],
            as_of: datetime) -> list[dict]:
    """Side-by-side facts for each product, plus whether it suits this client and why."""
    rows = []
    for pid in product_ids:
        p = catalog.get(pid)
        if p is None:
            continue
        probe = max(p.min_investment, 1_000)
        res = check(client, catalog, Proposal(client_id=client.id, purchases={pid: probe}, as_of=as_of), packs)
        blocking = [v for v in res.violations if v.product_id in (pid, None)]
        cap = max_amount(client, catalog, Proposal(client_id=client.id, purchases={}, as_of=as_of), packs, pid)
        returns = list(p.returns_pct.values())
        rows.append({
            "id": pid, "name": p.name, "type": TYPE_LABEL.get(p.type.value, p.type.value), "market": p.market.value,
            "risk": p.effective_risk, "issuer_risk": p.issuer_risk, "distributor_risk": p.distributor_risk,
            "annual_fee_pct": p.annual_fee_bps / 100, "liquidity_days": p.liquidity_days,
            "liquidity": _liquidity_label(p.liquidity_days), "min_investment": p.min_investment,
            "returns_pct": p.returns_pct,
            "best_year_pct": max(returns) if returns else None, "worst_year_pct": min(returns) if returns else None,
            "max_drawdown_pct": p.max_drawdown_pct, "deposit_insured": p.type.value == "deposit",
            "private": p.private, "complex": p.complex,
            "suitable": not blocking and cap > 0, "max_amount": cap,
            "reasons": [{"rule_id": v.rule_id, "message": v.message, "citation": v.citation} for v in blocking],
        })
    return rows


def rationale(client: Client, catalog: dict[str, Product], purchases: dict[str, int]) -> dict:
    """Why this allocation fits the client, as plain figures and sentences."""
    total = sum(purchases.values())
    if not total:
        return {"points": [], "figures": {}}
    wealth = client.investable_assets + sum(client.holdings.values())
    level = _level(client)
    prods = {pid: catalog[pid] for pid in purchases if pid in catalog}
    w = {pid: amt / total for pid, amt in purchases.items() if pid in prods}
    avg_risk = sum(prods[p].effective_risk * x for p, x in w.items())
    max_risk = max(prods[p].effective_risk for p in w)
    avg_fee = sum(prods[p].annual_fee_bps / 100 * x for p, x in w.items())
    non_cash = sum(a for p, a in purchases.items() if p in prods and not prods[p].is_cash_like)
    cushion = client.investable_assets - non_cash
    months = cushion / client.monthly_expenses if client.monthly_expenses else None
    largest_pid = max(w, key=w.get)
    largest_share = purchases[largest_pid] / wealth if wealth else 0
    longest = max(prods[p].liquidity_days for p in w)
    worst = sum((min(prods[p].returns_pct.values()) if prods[p].returns_pct else 0) * x for p, x in w.items())
    best = sum((max(prods[p].returns_pct.values()) if prods[p].returns_pct else 0) * x for p, x in w.items())
    insured = sum(a for p, a in purchases.items() if p in prods and prods[p].type.value == "deposit")

    points = [
        f"Invests ¥{total:,} of your ¥{wealth:,} ({total / wealth:.0%}); the rest stays available to you.",
        f"Highest risk level in the plan is R{max_risk}, within your level C{level}; the average is R{avg_risk:.1f}.",
    ]
    if months is not None:
        points.append(f"Keeps about {months:.0f} months of expenses (¥{cushion:,}) in cash or cash-like products "
                      f"for emergencies.")
    if len(w) > 1:
        points.append(f"Spread across {len(w)} products; the largest is {prods[largest_pid].name} at "
                      f"{largest_share:.0%} of your wealth.")
    points.append(f"Average annual fee {avg_fee:.2f}%.")
    if longest > 1:
        within = longest <= client.horizon_years * 365
        points.append(f"Longest lock-up is {_liquidity_label(longest).lower()}"
                      f"{', within your ' + str(client.horizon_years) + '-year horizon' if within else ''}.")
    if insured:
        points.append(f"¥{insured:,} is in bank deposits, covered by deposit insurance up to ¥500,000 per bank.")
    points.append(f"In past years this mix would have ranged from about {worst:+.1f}% to {best:+.1f}% a year "
                  "(illustrative history, not a forecast; returns are not guaranteed).")
    return {"points": points, "figures": {
        "total": total, "wealth": wealth, "share_invested": round(total / wealth, 3) if wealth else None,
        "cushion": cushion, "cushion_months": round(months, 1) if months is not None else None,
        "products": len(w), "largest_share": round(largest_share, 3), "max_risk": max_risk,
        "avg_risk": round(avg_risk, 2), "avg_fee_pct": round(avg_fee, 3), "longest_lockup_days": longest,
        "past_range_pct": [round(worst, 2), round(best, 2)], "deposit_insured": insured, "client_level": level}}


def default_candidates(client: Client, catalog: dict[str, Product], purchases: dict[str, int],
                       requested: Sequence[str] = ()) -> list[str]:
    """What to compare by default: the plan, what the client asked for, and a close alternative."""
    level = _level(client)
    chosen = list(dict.fromkeys([*purchases, *requested]))
    for pid in list(purchases):
        p = catalog[pid]
        alt = sorted((q for q in catalog.values() if q.id not in chosen and q.type == p.type
                      and q.effective_risk <= level and q.market == p.market and not q.private),
                     key=lambda q: (abs(q.effective_risk - p.effective_risk), q.annual_fee_bps))
        if alt:
            chosen.append(alt[0].id)
            break
    return chosen[:4]
