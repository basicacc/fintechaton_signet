from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings, strategies as st

from pca.catalog import load_catalog
from pca.models import Assessment, Capacity, Client, Market, Procedure, Proposal, DISCLOSURE_ITEMS
from pca.rules import CN_NFRA, HK_WMC, HOUSE, Stage
from pca.verifier import check, check_z3, feasible, max_amount, minimal_fix, required_procedure

NOW = datetime(2026, 10, 14, 10, 0)
CAT = load_catalog()
CN = [CN_NFRA, HOUSE]
BOTH = [CN_NFRA, HK_WMC, HOUSE]


def client(level=2, age=35, assessed=NOW - timedelta(days=30), **kw) -> Client:
    defaults = dict(investable_assets=500_000, monthly_expenses=8_000, horizon_years=3)
    defaults.update(kw)
    return Client(id="c1", age=age, assessments=[Assessment(level=level, at=assessed)], **defaults)


def prop(purchases, **kw) -> Proposal:
    return Proposal(client_id="c1", purchases=purchases, as_of=NOW, **kw)


def ids(result):
    return {v.rule_id for v in result.violations}


def full_procedure(**kw) -> Procedure:
    return Procedure(disclosures_shown=set(DISCLOSURE_ITEMS), client_confirmed=True, **kw)


# ---------------------------------------------------------------- basic suitability

def test_conservative_portfolio_passes():
    c = client(level=2)
    p = prop({"CN-WM-CASH": 100_000, "CN-WM-FI90": 100_000, "CN-BOND-GOV": 100_000})
    r = check(c, CAT, p, CN)
    assert r.ok, r.violations
    assert check_z3(c, CAT, p, CN) == (True, [])


def test_risk_above_tolerance_blocked():
    c = client(level=2)
    r = check(c, CAT, prop({"CN-FUND-TECH": 50_000}), CN)
    assert not r.ok
    assert "CN-12-1" in ids(r)


def test_higher_of_issuer_and_distributor_rating_applies():
    # CN-WM-MIX: issuer R3, distributor R2 -> effective R3 (NFRA Art. 22)
    r = check(client(level=2), CAT, prop({"CN-WM-MIX": 50_000}), CN)
    assert "CN-12-1" in ids(r)
    assert check(client(level=3), CAT, prop({"CN-WM-MIX": 50_000}), CN).ok


def test_expired_assessment_blocks_everything():
    c = client(level=5, assessed=NOW - timedelta(days=400))
    r = check(c, CAT, prop({"CN-WM-CASH": 10_000}), CN)
    assert "CN-29-3" in ids(r)


def test_reported_change_requires_reassessment():
    c = client(level=4, reported_change=True)
    assert "CN-29-3" in ids(check(c, CAT, prop({"CN-WM-CASH": 10_000}), CN))


def test_assessment_frequency_limit():
    c = client(level=4)
    c.assessments = [Assessment(level=2, at=NOW - timedelta(hours=h)) for h in (5, 3, 1)]
    assert "CN-29-1" in ids(check(c, CAT, prop({"CN-WM-CASH": 10_000}), CN))


def test_no_civil_capacity_blocks_all():
    c = client(level=3, capacity=Capacity.NONE)
    assert "CN-19" in ids(check(c, CAT, prop({"CN-DEP-01": 1_000}), CN))


def test_limited_capacity_low_risk_with_guardian_only():
    c = client(level=3, capacity=Capacity.LIMITED, guardian_consent=True)
    assert check(c, CAT, prop({"CN-WM-FI90": 10_000}), CN).ok
    assert "CN-19" in ids(check(c, CAT, prop({"CN-FUND-DIV": 10_000}), CN))


def test_private_product_requires_qualified_investor():
    c = client(level=5, investable_assets=10_000_000)
    assert "CN-30" in ids(check(c, CAT, prop({"CN-PRIV-QUANT": 1_000_000}), CN))
    c.qualified_private = True
    assert "CN-30" not in ids(check(c, CAT, prop({"CN-PRIV-QUANT": 1_000_000}), CN))


def test_financial_capacity():
    c = client(level=3, investable_assets=100_000, monthly_expenses=0)
    assert "CN-12-2" in ids(check(c, CAT, prop({"CN-WM-CASH": 150_000}), CN))


def test_min_investment():
    c = client(level=5, investable_assets=10_000_000, qualified_private=True, horizon_years=5)
    assert "P-MIN" in ids(check(c, CAT, prop({"CN-PRIV-QUANT": 500_000}), CN))


# ---------------------------------------------------------------- house policy

def test_concentration_limit():
    c = client(level=3)
    r = check(c, CAT, prop({"CN-FUND-DIV": 200_000, "CN-WM-CASH": 100_000}), CN)
    assert "H-CONC" in ids(r)


def test_emergency_reserve():
    c = client(level=3, investable_assets=100_000, monthly_expenses=10_000)
    r = check(c, CAT, prop({"CN-FUND-DIV": 25_000, "CN-WM-FI90": 25_000, "CN-BOND-GOV": 25_000}), CN)
    assert "H-LIQ" in ids(r)  # leaves 25k, needs 60k


def test_horizon_lockup():
    c = client(level=3, horizon_years=0)
    assert "H-HORIZON" in ids(check(c, CAT, prop({"CN-DEP-12M": 10_000}), CN))


# ---------------------------------------------------------------- elderly (Art. 18)

def test_elderly_high_risk_needs_special_care_at_execution():
    c = client(level=4, age=70)
    p = prop({"CN-FUND-300": 50_000, "CN-WM-CASH": 125_000, "CN-WM-FI90": 125_000})
    assert check(c, CAT, p, CN).ok  # proposal stage is fine
    assert required_procedure(c, CAT, p.purchases, NOW, CN) == ["CN-18", "CN-31", "CN-5"]
    p.procedure = full_procedure()
    assert "CN-18" in ids(check(c, CAT, p, CN, Stage.EXECUTION))
    p.procedure = full_procedure(enhanced_warning=True, consideration_hours=24, follow_up_scheduled=True)
    assert check(c, CAT, p, CN, Stage.EXECUTION).ok


def test_elderly_age_boundary_is_inclusive():
    # Art. 46: "以上" includes the number itself, so 65 counts.
    c = client(level=4, age=65)
    assert "CN-18" in required_procedure(c, CAT, {"CN-FUND-300": 10_000}, NOW, CN)
    c64 = client(level=4, age=64)
    assert "CN-18" not in required_procedure(c64, CAT, {"CN-FUND-300": 10_000}, NOW, CN)


def test_elderly_high_risk_cap():
    c = client(level=5, age=70)
    assert "H-ELDER" in ids(check(c, CAT, prop({"CN-FUND-300": 120_000, "CN-WM-CASH": 180_000}), CN))


def test_level_change_requires_reconfirmation():
    c = client(level=3)
    c.assessments.insert(0, Assessment(level=2, at=NOW - timedelta(days=200)))
    p = prop({"CN-WM-CASH": 10_000}, procedure=full_procedure())
    assert "CN-29-2" in ids(check(c, CAT, p, CN, Stage.EXECUTION))
    p.procedure.level_change_confirmed = True
    assert check(c, CAT, p, CN, Stage.EXECUTION).ok


def test_execution_needs_disclosures_and_confirmation():
    c = client(level=3)
    p = prop({"CN-WM-CASH": 10_000})
    assert {"CN-31", "CN-5"} <= ids(check(c, CAT, p, CN, Stage.EXECUTION))


# ---------------------------------------------------------------- HK / WMC

def wmc_client(**kw):
    base = dict(level=4, gba_resident=True, investment_experience_years=3,
                net_household_financial_assets=1_500_000, investable_assets=1_000_000)
    base.update(kw)
    return client(**base)


def test_hk_products_out_of_scope_without_hk_pack():
    assert "SCOPE" in ids(check(wmc_client(), CAT, prop({"HK-FUND-MMF": 10_000}), CN))


def test_wmc_eligible_investor_can_buy_eligible_fund():
    r = check(wmc_client(), CAT, prop({"HK-FUND-MMF": 100_000, "HK-FUND-BOND": 100_000}), BOTH)
    assert r.ok, r.violations


def test_wmc_ineligible_investor():
    c = wmc_client(investment_experience_years=1, net_household_financial_assets=500_000)
    assert "HK-WMC-INV" in ids(check(c, CAT, prop({"HK-FUND-MMF": 10_000}), BOTH))
    ok, core = feasible(c, CAT, NOW, BOTH, market=Market.HK)
    assert not ok and core == ["HK-WMC-INV"]


def test_wmc_complex_product_excluded():
    c = wmc_client(level=5)
    assert "HK-WMC-PROD" in ids(check(c, CAT, prop({"HK-FUND-LEV": 20_000}), BOTH))


def test_wmc_quota():
    c = wmc_client(investable_assets=5_000_000, wmc_quota_used=2_950_000, monthly_expenses=0)
    assert "HK-WMC-QUOTA" in ids(check(c, CAT, prop({"HK-FUND-MMF": 100_000}), BOTH))


# ---------------------------------------------------------------- search

def test_minimal_fix_replaces_unsuitable_fund_with_closest_suitable_one():
    c = client(level=3)
    p = prop({"CN-FUND-TECH": 100_000, "CN-WM-CASH": 100_000})
    fix = minimal_fix(c, CAT, p, CN)
    assert fix is not None
    assert sum(fix.purchases.values()) == 200_000
    assert "CN-FUND-TECH" not in fix.purchases
    assert check(c, CAT, prop(fix.purchases), CN).ok
    # the replacement should be a fund at the highest allowed risk (R3)
    assert any(CAT[pid].type.value == "fund" and CAT[pid].effective_risk == 3 for pid in fix.added)


def test_minimal_fix_keeps_compliant_proposal_unchanged():
    c = client(level=2)
    want = {"CN-WM-CASH": 100_000, "CN-WM-FI90": 50_000}
    fix = minimal_fix(c, CAT, prop(want), CN)
    assert fix.purchases == want and not fix.removed and not fix.added


def test_max_amount_respects_concentration():
    c = client(level=3)
    p = prop({"CN-WM-CASH": 140_000})
    m = max_amount(c, CAT, p, CN, "CN-FUND-DIV")
    # 100*x <= 30 * 500k wealth  ->  x <= 150k
    assert m == 150_000
    assert check(c, CAT, prop({"CN-WM-CASH": 140_000, "CN-FUND-DIV": m}), CN).ok
    assert not check(c, CAT, prop({"CN-WM-CASH": 140_000, "CN-FUND-DIV": m + 1}), CN).ok


def test_max_amount_zero_for_unsuitable():
    assert max_amount(client(level=2), CAT, prop({}), CN, "CN-FUND-TECH") == 0


def test_feasible_reports_core_when_nothing_is_possible():
    c = client(level=3, capacity=Capacity.NONE)
    ok, core = feasible(c, CAT, NOW, CN)
    assert not ok and core == ["CN-19"]


# ---------------------------------------------------------------- differential: evaluator vs Z3

PIDS = sorted(CAT)


@st.composite
def scenarios(draw):
    c = Client(
        id="r",
        age=draw(st.integers(18, 90)),
        capacity=draw(st.sampled_from(list(Capacity))),
        guardian_consent=draw(st.booleans()),
        assessments=[Assessment(level=draw(st.integers(1, 5)),
                                at=NOW - timedelta(days=draw(st.integers(0, 500))))],
        investable_assets=draw(st.integers(0, 3_000_000)),
        monthly_expenses=draw(st.integers(0, 50_000)),
        horizon_years=draw(st.integers(0, 10)),
        qualified_private=draw(st.booleans()),
        gba_resident=draw(st.booleans()),
        investment_experience_years=draw(st.integers(0, 10)),
        net_household_financial_assets=draw(st.integers(0, 3_000_000)),
        wmc_quota_used=draw(st.integers(0, 3_000_000)),
    )
    chosen = draw(st.lists(st.sampled_from(PIDS), max_size=5, unique=True))
    purchases = {pid: draw(st.integers(0, 1_500_000)) for pid in chosen}
    procedure = Procedure(
        disclosures_shown=set(draw(st.lists(st.sampled_from(DISCLOSURE_ITEMS), unique=True))),
        enhanced_warning=draw(st.booleans()),
        consideration_hours=draw(st.integers(0, 48)),
        follow_up_scheduled=draw(st.booleans()),
        client_confirmed=draw(st.booleans()),
    )
    packs = draw(st.sampled_from([CN, BOTH, [CN_NFRA], [HK_WMC, HOUSE]]))
    stage = draw(st.sampled_from(list(Stage)))
    return c, Proposal(client_id="r", purchases=purchases, as_of=NOW, procedure=procedure), packs, stage


@settings(max_examples=300, deadline=None)
@given(scenarios())
def test_evaluator_and_z3_agree(s):
    c, p, packs, stage = s
    r = check(c, CAT, p, packs, stage)
    ok, core = check_z3(c, CAT, p, packs, stage)
    assert r.ok == ok
    violated = {(v.rule_id, v.product_id) for v in r.violations}
    for label in core:
        rid, _, pid = label.partition("@")
        assert (rid, pid or None) in violated


@settings(max_examples=60, deadline=None)
@given(scenarios())
def test_minimal_fix_is_always_compliant(s):
    c, p, packs, _ = s
    fix = minimal_fix(c, CAT, p, packs)
    if fix is not None:
        assert check(c, CAT, prop(fix.purchases), packs).ok


def test_concentration_does_not_apply_to_r1_deposits():
    c = client(level=2)
    assert check(c, CAT, prop({"CN-DEP-12M": 400_000}), CN).ok



def test_wmc_experience_is_required_on_every_route():
    # 广东银发〔2024〕5号 Art. 16: 2+ years of experience AND one of the wealth/income tests
    rich_novice = wmc_client(investment_experience_years=1, net_household_financial_assets=0,
                             household_financial_assets=5_000_000, min_annual_income_3y=900_000)
    assert "HK-WMC-INV" in ids(check(rich_novice, CAT, prop({"HK-FUND-MMF": 10_000}), BOTH))
    experienced_high_income = wmc_client(investment_experience_years=3, net_household_financial_assets=0,
                                         min_annual_income_3y=450_000)
    assert "HK-WMC-INV" not in ids(check(experienced_high_income, CAT, prop({"HK-FUND-MMF": 10_000}), BOTH))


def test_wmc_requires_full_civil_capacity():
    c = wmc_client(capacity=Capacity.LIMITED, guardian_consent=True)
    assert "HK-WMC-INV" in ids(check(c, CAT, prop({"HK-FUND-MMF": 10_000}), BOTH))
