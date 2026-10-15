from pca.compare import compare, default_candidates, rationale

from test_verifier import CAT, CN, NOW, client


def test_compare_marks_suitability_with_reasons_and_caps():
    c = client(level=2)
    rows = {r["id"]: r for r in compare(c, CAT, ["CN-BOND-GOV", "CN-FUND-TECH", "CN-DEP-12M"], CN, NOW)}
    assert rows["CN-BOND-GOV"]["suitable"] and rows["CN-BOND-GOV"]["max_amount"] == 150_000  # 30% of 500k
    tech = rows["CN-FUND-TECH"]
    assert not tech["suitable"] and tech["max_amount"] == 0
    assert any(r["rule_id"] == "CN-12-1" for r in tech["reasons"])
    assert rows["CN-DEP-12M"]["deposit_insured"] and rows["CN-DEP-12M"]["liquidity"] == "1 year"


def test_rationale_figures_match_the_plan():
    c = client(level=3)  # 500k investable, 8k/month expenses
    r = rationale(c, CAT, {"CN-WM-CASH": 100_000, "CN-FUND-DIV": 100_000})
    f = r["figures"]
    assert f["total"] == 200_000 and f["share_invested"] == 0.4
    assert f["max_risk"] == 3 and f["products"] == 2
    assert f["cushion"] == 400_000 and f["cushion_months"] == 50.0  # cash-like product counts as cushion
    assert any("not a forecast" in p for p in r["points"])


def test_rationale_empty_plan():
    assert rationale(client(level=3), CAT, {}) == {"points": [], "figures": {}}


def test_default_candidates_include_plan_request_and_an_alternative():
    c = client(level=3)
    ids = default_candidates(c, CAT, {"CN-FUND-DIV": 100_000}, ["CN-FUND-TECH"])
    assert ids[:2] == ["CN-FUND-DIV", "CN-FUND-TECH"] and len(ids) == 3
    assert CAT[ids[2]].type == CAT["CN-FUND-DIV"].type and CAT[ids[2]].effective_risk <= 3
