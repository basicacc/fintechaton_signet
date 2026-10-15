"""Rule packs.

Each rule turns (client, catalog, as_of) into constraints over the decision
variables ``amt:<product_id>`` (whole RMB purchased) and, at execution stage,
procedure variables ``proc:<field>``. Client and product facts are baked in as
constants: the solver may move money between products, but it never searches
over a client's assessment answers (doing so would amount to coaching the
client, which NFRA Art. 13(1) prohibits).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable, Iterable

from .expr import BoolConst, Expr, Var, all_of, any_of, implies, total
from .models import DISCLOSURE_ITEMS, Capacity, Client, InvestorType, Market, Product


class Kind(StrEnum):
    LAW = "law"  # binding regulation
    HOUSE = "house"  # institution's own policy, labelled as such
    PRODUCT = "product"  # product terms (e.g. minimum investment)


class Stage(StrEnum):
    PROPOSAL = "proposal"  # must hold before advice is shown to the client
    EXECUTION = "execution"  # must additionally hold before an order is placed


@dataclass(frozen=True)
class Constraint:
    rule_id: str
    expr: Expr
    message: str
    product_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.rule_id}@{self.product_id}" if self.product_id else self.rule_id


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    citation: str
    kind: Kind
    stage: Stage
    build: Callable[["Context"], Iterable[Constraint]]


@dataclass(frozen=True)
class RulePack:
    id: str
    version: str
    title: str
    markets: frozenset[Market]
    rules: tuple[Rule, ...]


def amt(pid: str) -> Var:
    return Var(f"amt:{pid}")


def proc(name: str, is_bool: bool = True) -> Var:
    return Var(f"proc:{name}", is_bool=is_bool)


@dataclass
class Context:
    client: Client
    catalog: dict[str, Product]
    as_of: datetime

    def products(self, market: Market | None = None) -> list[Product]:
        return [p for p in self.catalog.values() if market is None or p.market == market]

    def total_purchases(self, market: Market | None = None) -> Expr:
        return total(amt(p.id) for p in self.products(market))

    @property
    def wealth(self) -> int:
        """Client's investable wealth: uninvested assets plus existing holdings."""
        return self.client.investable_assets + sum(self.client.holdings.values())

    @property
    def level(self) -> int:
        """Assessed level, lowered by any integrity cap (never raised)."""
        a = self.client.current_assessment
        level = a.level if a else 0
        return min(level, self.client.level_cap) if self.client.level_cap else level


# ---------------------------------------------------------------- helpers

def _buys(pid: str) -> Expr:
    return amt(pid) > 0


def _any_purchase(ctx: Context, market: Market | None = None) -> Expr:
    return ctx.total_purchases(market) > 0


def _assessments_within_limits(c: Client, now: datetime) -> bool:
    day = [a for a in c.assessments if now - a.at <= timedelta(days=1)]
    year = [a for a in c.assessments if now - a.at <= timedelta(days=365)]
    return len(day) <= 2 and len(year) <= 8


def _level_changed(c: Client) -> bool:
    return len(c.assessments) >= 2 and c.assessments[-1].level != c.assessments[-2].level


# ---------------------------------------------------------------- CN (NFRA)

def _cn_assessment_valid(ctx: Context):
    valid = ctx.client.assessment_valid_at(ctx.as_of)
    yield Constraint(
        "CN-29-3",
        implies(_any_purchase(ctx, Market.CN), BoolConst(valid)),
        "A valid risk assessment (≤12 months old, no reported change) is required before any sale.",
    )


def _cn_assessment_limits(ctx: Context):
    ok = _assessments_within_limits(ctx.client, ctx.as_of)
    yield Constraint(
        "CN-29-1",
        implies(_any_purchase(ctx, Market.CN), BoolConst(ok)),
        "Risk assessment was taken more than 2 times in a day or 8 times in 12 months.",
    )


def _cn_level_reconfirmed(ctx: Context):
    if _level_changed(ctx.client):
        yield Constraint(
            "CN-29-2",
            implies(_any_purchase(ctx, Market.CN), proc("level_change_confirmed")),
            "Risk level changed since the last assessment; the client must re-confirm it.",
        )


def _cn_capacity(ctx: Context):
    c = ctx.client
    if c.capacity == Capacity.NONE:
        yield Constraint("CN-19", ctx.total_purchases().eq(0),
                         "No products may be sold to a person without civil capacity.")
    elif c.capacity == Capacity.LIMITED:
        yield Constraint("CN-19", implies(_any_purchase(ctx), BoolConst(c.guardian_consent)),
                         "Limited civil capacity: guardian consent is required.")
        for p in ctx.products():
            if p.effective_risk > 2:
                yield Constraint("CN-19", amt(p.id).eq(0),
                                 "Limited civil capacity: only low-risk (R1–R2) products.", p.id)


def _cn_suitability(ctx: Context):
    if ctx.client.investor_type == InvestorType.PROFESSIONAL:
        return
    for p in ctx.products(Market.CN):
        if p.effective_risk > ctx.level:
            yield Constraint(
                "CN-12-1",
                amt(p.id).eq(0),
                f"Product risk R{p.effective_risk} exceeds client risk tolerance C{ctx.level}. "
                "An advisor may not recommend it (Art. 13(3)); for investment products there is no "
                "override (Art. 12).",
                p.id,
            )


def _cn_financial_capacity(ctx: Context):
    yield Constraint(
        "CN-12-2",
        ctx.total_purchases() <= ctx.client.investable_assets,
        "Total purchases exceed the client's investable assets.",
    )


def _cn_private(ctx: Context):
    if ctx.client.qualified_private:
        return
    for p in ctx.products(Market.CN):
        if p.private:
            yield Constraint("CN-30", amt(p.id).eq(0),
                             "Private product: client does not meet the qualified-investor standard.", p.id)


def _cn_disclosure(ctx: Context):
    shown = all_of(*(proc(f"disclosure:{d}") for d in DISCLOSURE_ITEMS))
    yield Constraint("CN-31", implies(_any_purchase(ctx), shown),
                     "All pre-sale disclosures (basic info, risk level, principal loss, fees, "
                     "matching opinion) must be shown before execution.")


def _cn_elderly(ctx: Context):
    if ctx.client.age < 65:  # Art. 46: "以上" is inclusive, so 65 counts
        return
    high = [p for p in ctx.products() if p.effective_risk >= 4]
    if not high:
        return
    buys_high = any_of(*(_buys(p.id) for p in high))
    care = all_of(proc("enhanced_warning"), proc("consideration_hours", is_bool=False) >= 24,
                  proc("follow_up_scheduled"))
    yield Constraint("CN-18", implies(buys_high, care),
                     "Client aged 65+ buying a high-risk (R4+) product: enhanced warning, at least "
                     "24h to consider, and a follow-up visit are required.")


def _client_confirmed(ctx: Context):
    yield Constraint("CN-5", implies(_any_purchase(ctx), proc("client_confirmed")),
                     "The client must make the final decision themselves.")


def _min_investment(ctx: Context):
    for p in ctx.products():
        if p.min_investment > 0:
            yield Constraint("P-MIN", any_of(amt(p.id).eq(0), amt(p.id) >= p.min_investment),
                             f"Minimum investment for this product is ¥{p.min_investment:,}.", p.id)


CN_NFRA = RulePack(
    id="cn-nfra-2025-7",
    version="2026.02.01",
    title="NFRA 金融机构产品适当性管理办法 (Order No. 7 of 2025)",
    markets=frozenset({Market.CN}),
    rules=(
        Rule("CN-29-3", "Assessment must be valid", "NFRA Art. 29 para 2", Kind.LAW, Stage.PROPOSAL, _cn_assessment_valid),
        Rule("CN-29-1", "Assessment frequency limits", "NFRA Art. 29 para 1", Kind.LAW, Stage.PROPOSAL, _cn_assessment_limits),
        Rule("CN-29-2", "Re-confirm a changed risk level", "NFRA Art. 29 para 1", Kind.LAW, Stage.EXECUTION, _cn_level_reconfirmed),
        Rule("CN-19", "Civil capacity", "NFRA Art. 19", Kind.LAW, Stage.PROPOSAL, _cn_capacity),
        Rule("CN-12-1", "Risk level matching (no override)", "NFRA Art. 12(1), Art. 13(3)", Kind.LAW, Stage.PROPOSAL, _cn_suitability),
        Rule("CN-12-2", "Financial capacity", "NFRA Art. 12(2)", Kind.LAW, Stage.PROPOSAL, _cn_financial_capacity),
        Rule("CN-30", "Private products", "NFRA Art. 30", Kind.LAW, Stage.PROPOSAL, _cn_private),
        Rule("P-MIN", "Minimum investment", "Product terms", Kind.PRODUCT, Stage.PROPOSAL, _min_investment),
        Rule("CN-31", "Pre-sale disclosure", "NFRA Art. 31", Kind.LAW, Stage.EXECUTION, _cn_disclosure),
        Rule("CN-18", "Special care for clients aged 65+", "NFRA Art. 18, Art. 46", Kind.LAW, Stage.EXECUTION, _cn_elderly),
        Rule("CN-5", "Client decides", "NFRA Art. 5", Kind.LAW, Stage.EXECUTION, _client_confirmed),
    ),
)


# ---------------------------------------------------------------- HK / WMC Southbound

WMC_QUOTA = 3_000_000  # 广东银发〔2024〕5号 Art. 46 (RMB 1.5M per channel if both bank and securities channels are used;
                       # this prototype models the bank channel only)


def _wmc_investor_eligible(c: Client) -> bool:
    """广东银发〔2024〕5号 (WMC implementation rules, in force 2024-02-26), Art. 16.

    Full civil capacity; GBA residency (household registration, or 2+ years of social
    security or income tax in the 9 mainland GBA cities); AND 2+ years of investment
    experience; AND one of: net household financial assets >= RMB 1M (last 3 months),
    household financial assets >= RMB 2M (last 3 months), or average annual personal
    income >= RMB 400k over the last 3 years.
    """
    wealth_or_income = (
        c.net_household_financial_assets >= 1_000_000
        or c.household_financial_assets >= 2_000_000
        or c.min_annual_income_3y >= 400_000  # field holds the 3-year average annual income
    )
    return (c.capacity == Capacity.FULL and c.gba_resident and c.investment_experience_years >= 2
            and wealth_or_income)


def _wmc_eligibility(ctx: Context):
    ok = _wmc_investor_eligible(ctx.client)
    yield Constraint("HK-WMC-INV", implies(_any_purchase(ctx, Market.HK), BoolConst(ok)),
                     "Client is not an eligible Southbound investor: needs full civil capacity, GBA residency, 2+ years "
                     "of investment experience, and the wealth or income test.")


def _wmc_products(ctx: Context):
    for p in ctx.products(Market.HK):
        if not p.wmc_eligible or p.complex or p.effective_risk > 4:
            yield Constraint("HK-WMC-PROD", amt(p.id).eq(0),
                             "Southbound only allows non-complex products rated low to medium-high.", p.id)


def _wmc_quota(ctx: Context):
    yield Constraint("HK-WMC-QUOTA",
                     ctx.total_purchases(Market.HK) + ctx.client.wmc_quota_used <= WMC_QUOTA,
                     f"Southbound individual quota is ¥{WMC_QUOTA:,}.")


def _sfc_suitability(ctx: Context):
    if ctx.client.investor_type == InvestorType.PROFESSIONAL:
        return
    for p in ctx.products(Market.HK):
        if p.effective_risk > ctx.level:
            yield Constraint("HK-SFC-5.2", amt(p.id).eq(0),
                             f"Product risk {p.effective_risk} is not suitable for client risk tolerance {ctx.level}.",
                             p.id)


def _hk_assessment(ctx: Context):
    valid = ctx.client.assessment_valid_at(ctx.as_of)
    yield Constraint("HK-SFC-KYC", implies(_any_purchase(ctx, Market.HK), BoolConst(valid)),
                     "An up-to-date client risk profile is required.")


HK_WMC = RulePack(
    id="hk-wmc-2.0",
    version="2024.02.26",
    title="HK SFC Code of Conduct 5.2 + Cross-boundary WMC 2.0 (Southbound)",
    markets=frozenset({Market.HK}),
    rules=(
        Rule("HK-SFC-KYC", "Up-to-date client profile", "SFC Code of Conduct 5.1A/5.2", Kind.LAW, Stage.PROPOSAL, _hk_assessment),
        Rule("HK-WMC-INV", "Southbound investor eligibility", "广东银发〔2024〕5号 Art. 16", Kind.LAW, Stage.PROPOSAL, _wmc_eligibility),
        Rule("HK-WMC-PROD", "Southbound product scope", "广东银发〔2024〕5号 Art. 40; HK Gov LCQ18 (2024)", Kind.LAW, Stage.PROPOSAL, _wmc_products),
        Rule("HK-WMC-QUOTA", "Southbound quota", "广东银发〔2024〕5号 Art. 46", Kind.LAW, Stage.PROPOSAL, _wmc_quota),
        Rule("HK-SFC-5.2", "Suitability", "SFC Code of Conduct 5.2", Kind.LAW, Stage.PROPOSAL, _sfc_suitability),
        Rule("P-MIN", "Minimum investment", "Product terms", Kind.PRODUCT, Stage.PROPOSAL, _min_investment),
    ),
)


# ---------------------------------------------------------------- House policy

def _concentration(ctx: Context):
    # Diversification limits apply to products that can lose value (R2+). Applying them
    # to R1 deposits would push conservative clients into riskier products.
    for p in ctx.products():
        if p.effective_risk <= 1:
            continue
        held = ctx.client.holdings.get(p.id, 0)
        yield Constraint("H-CONC", implies(_buys(p.id), 100 * (amt(p.id) + held) <= 30 * ctx.wealth),
                         "No single R2+ product may exceed 30% of investable wealth.", p.id)


def _emergency_reserve(ctx: Context):
    non_cash = total(amt(p.id) for p in ctx.products() if not p.is_cash_like)
    reserve = 6 * ctx.client.monthly_expenses
    yield Constraint("H-LIQ", implies(non_cash > 0, ctx.client.investable_assets - non_cash >= reserve),
                     f"Keep at least 6 months of expenses (¥{reserve:,}) in cash-like products.")


def _horizon(ctx: Context):
    for p in ctx.products():
        if p.liquidity_days > ctx.client.horizon_years * 365:
            yield Constraint("H-HORIZON", amt(p.id).eq(0),
                             "Product lock-up is longer than the client's investment horizon.", p.id)


def _elderly_cap(ctx: Context):
    if ctx.client.age < 65:
        return
    risky = [p for p in ctx.products() if p.effective_risk >= 4]
    if not risky:
        return
    high = total(amt(p.id) + ctx.client.holdings.get(p.id, 0) for p in risky)
    buys_high = total(amt(p.id) for p in risky) > 0
    yield Constraint("H-ELDER", implies(buys_high, 100 * high <= 20 * ctx.wealth),
                     "Clients aged 65+: high-risk (R4+) products capped at 20% of investable wealth.")


HOUSE = RulePack(
    id="house-default",
    version="1.0",
    title="House policy (not legal requirements)",
    markets=frozenset({Market.CN, Market.HK}),
    rules=(
        Rule("H-CONC", "Concentration limit 30% (R2+)", "House policy", Kind.HOUSE, Stage.PROPOSAL, _concentration),
        Rule("H-LIQ", "Emergency reserve", "House policy", Kind.HOUSE, Stage.PROPOSAL, _emergency_reserve),
        Rule("H-HORIZON", "Lock-up within horizon", "House policy", Kind.HOUSE, Stage.PROPOSAL, _horizon),
        Rule("H-ELDER", "High-risk cap for 65+", "House policy", Kind.HOUSE, Stage.PROPOSAL, _elderly_cap),
    ),
)

PACKS: dict[str, RulePack] = {p.id: p for p in (CN_NFRA, HK_WMC, HOUSE)}
