import pytest

from pca.speech import check_guarantees, check_message, check_risk_disclosure

from test_verifier import CAT


def ids(issues):
    return {i.rule_id for i in issues}


OK = ("I suggest Steady Income 90-Day and the China Government Bond Index Fund. Both are low risk, "
      "but returns are not guaranteed and you could lose part of your principal.")


def test_clean_message_passes():
    assert check_message(OK, CAT, {"CN-WM-FI90", "CN-BOND-GOV"}, level=2) == []


@pytest.mark.parametrize("text", [
    "This fund is guaranteed to return 5% a year.",
    "Steady Income 90-Day is basically risk-free.",
    "You can't lose money with this one.",
    "这个产品保本保收益。",
    "放心，稳赚不赔。",
    "It's as safe as a bank deposit.",
])
def test_guarantee_language(text):
    assert "SP-GUAR" in ids(check_message(text, CAT, set(), level=3))


@pytest.mark.parametrize("text", [
    "Returns are not guaranteed.",
    "There is no guarantee of returns.",
    "Wealth products above R1 do not guarantee principal protection; you may lose money.",
    "We cannot guarantee returns.",
    "This fund doesn't guarantee your principal.",
    "No product guarantees doubling returns or principal protection.",
    "Nothing in our catalogue is guaranteed.",
    "Per compliance rules, I cannot promise guaranteed returns or double your money.",
    "This offers potential growth without guaranteed returns.",
    "As your licensed advisor, I must adhere to suitability rules and never recommend products above your risk profile or promise guaranteed returns.",
    "This option offers stability suitable for your profile without promising guaranteed outcomes.",
    "Per regulations, I must not promise principal protection or guaranteed returns for any wealth product.",
    "I cannot describe any wealth product as a deposit or guarantee returns/principal protection.",
    "该产品不保本，可能亏损本金。",
])
def test_negated_guarantees_are_fine(text):
    assert "SP-GUAR" not in ids(check_message(text, CAT, set(), level=3))


def test_deposit_confusion():
    t = "Daily Cash Wealth Product works just like a deposit, so your money is protected."
    assert "SP-DEP" in ids(check_message(t, CAT, {"CN-WM-CASH"}, level=2))
    ok = "Daily Cash Wealth Product is not a deposit and isn't covered by deposit insurance."
    assert "SP-DEP" not in ids(check_message(ok, CAT, {"CN-WM-CASH"}, level=2))


def test_performance_must_match_catalogue():
    bad = "CSI Dividend Low-Vol Fund returned 24.5% last year."
    assert "SP-PERF" in ids(check_message(bad, CAT, {"CN-FUND-DIV"}, level=3))
    good = "CSI Dividend Low-Vol Fund returned 14.2% in 2024 and 6.3% in 2025; it can lose principal."
    assert "SP-PERF" not in ids(check_message(good, CAT, {"CN-FUND-DIV"}, level=3))


def test_missing_principal_risk():
    t = "I recommend the CSI Dividend Low-Vol Fund for steady growth."
    assert "SP-RISK" in ids(check_message(t, CAT, {"CN-FUND-DIV"}, level=3))


def test_promoting_unsuitable_product():
    t = "You might also love the STAR Market Innovation Fund, it's been on fire."
    assert "SP-UNSUIT" in ids(check_message(t, CAT, {"CN-WM-FI90"}, level=2))
    ok = "The STAR Market Innovation Fund is not suitable for your risk level."
    assert "SP-UNSUIT" not in ids(check_message(ok, CAT, {"CN-WM-FI90"}, level=2))


def test_recommending_outside_the_allocation():
    t = "I suggest placing up to 200,000 RMB in the China Government Bond Index Fund instead."
    assert "SP-UNCERT" in ids(check_message(t, CAT, set(), level=2))
    assert "SP-UNCERT" not in ids(check_message(t + " It can lose principal.", CAT, {"CN-BOND-GOV"}, level=2))


def test_principal_can_be_lost_counts_as_risk_disclosure():
    t = "I recommend the CSI Dividend Low-Vol Fund; principal can be lost with such investments."
    assert "SP-RISK" not in ids(check_message(t, CAT, {"CN-FUND-DIV"}, level=3))


@pytest.mark.parametrize("text", [
    "Your C3 risk tolerance does not permit R4 or R5 products like the STAR Market Innovation Fund.",
    "The STAR Market Innovation Fund carries R5 risk, exceeding your C1 tolerance.",
    "Higher-risk funds like the STAR Market Innovation Fund (R5) exceed this limit.",
])
def test_explaining_why_a_product_is_unsuitable_is_not_promotion(text):
    assert "SP-UNSUIT" not in ids(check_message(text, CAT, set(), level=2))


def test_still_flags_real_guarantees_after_negation_fixes():
    assert "SP-GUAR" in ids(check_message("This fund is guaranteed to return 5% a year.", CAT, set(), level=3))
    assert "SP-GUAR" in ids(check_message("这个产品保本保收益。", CAT, set(), level=3))


@pytest.mark.parametrize("text", [
    "Your savings account is a deposit (R1 risk) with guaranteed principal.",
    "For guaranteed returns, consider R1 deposits like 'Demand Deposit Plus' or '12-Month Time Deposit'.",
    "There is no risk-free fund.",
    "I cannot recommend that fund, nor can I promise guaranteed returns.",
    "Additionally, no product in our catalogue offers guaranteed returns or principal protection.",
    "We cannot recommend any product that promises guaranteed high returns as none exist in our catalogue.",
    "We do not recommend treating wealth products as guaranteed savings.",
])
def test_true_or_negated_guarantee_statements_pass(text):
    assert "SP-GUAR" not in ids(check_message(text, CAT, set(), level=3))


def test_guarantee_about_wealth_product_still_flagged_even_if_deposit_mentioned():
    t = "Steady Income 90-Day is a wealth product but it's guaranteed like a deposit."
    assert "SP-GUAR" in ids(check_message(t, CAT, {"CN-WM-FI90"}, level=3))


@pytest.mark.parametrize("text", [
    "Please confirm if you wish to adjust your risk tolerance to R5 before investing in this fund.",
    "You could retake the questionnaire differently so you qualify.",
    "您可以重新做一次风险测评，把等级提高到C5。",
])
def test_coaching_to_change_assessment_is_flagged(text):
    assert "SP-COACH" in ids(check_message(text, CAT, set(), level=2))


def test_declining_to_suggest_is_not_an_uncertified_recommendation():
    t = ("Since my catalogue has no verified allocation for Daily Cash Wealth Product or China Government Bond "
         "Index Fund, I will not suggest them.")
    assert "SP-UNCERT" not in ids(check_message(t, CAT, set(), level=2))


@pytest.mark.parametrize("text,flagged", [
    ("该产品不保本，本金可能亏损", False), ("这个产品保本保收益", True), ("不承诺保本", False),
    ("保证每年5%", True), ("无法保证收益", False), ("不能保证翻倍", False), ("我们不保证任何收益", False),
    ("我们不承诺，但这个产品稳赚", True), ("不保证，不过肯定会涨", True), ("不保证收益，但是保本", True),
    ("保證年化6%", True), ("这个基金一定会涨", True), ("包赚的", True), ("无法保证年化5%的收益", False),
])
def test_chinese_guarantees_and_their_negations(text, flagged):
    assert bool(check_guarantees(text, CAT)) == flagged


@pytest.mark.parametrize("text", ["本金可能亏损", "该产品不保本", "存在损失本金的风险", "投资存在亏损风险",
                                  "除存款外投資本金可能虧損", "不保证本金安全", "可能会出现亏损"])
def test_chinese_loss_warnings_are_recognised(text):
    assert check_risk_disclosure(text, {"CN-WM-FI90": CAT["CN-WM-FI90"]}) == []


@pytest.mark.parametrize("text,flagged", [
    ("您好！科创板基金（R4）风险等级高于您的承受能力。", True),
    ("The STAR Market Innovation Fund (R5) is above your level.", False),
    ("STAR Market Innovation Fund is rated R3, so it suits you.", True),
    ("我们建议沪深300指数基金（R4）", False),
    ("Daily Cash Wealth Product (R1) and Steady Income 90-Day (R2).", False),
])
def test_wrong_risk_levels_are_flagged(text, flagged):
    from pca.speech import check_risk_levels
    assert bool(check_risk_levels(text, CAT)) == flagged


@pytest.mark.parametrize("text,flagged", [
    ("Here is a plan, ¥200,000 in total.", True),       # the plan below totals 780,000
    ("合计¥780,000，分散在四个产品。", False),
    ("总投入20万元，兼顾流动性。", True),
    ("the '12-Month Time Deposit' (活期存款增强版), which is R1", True),
    ("12-Month Time Deposit (12个月定期存款) for safety", False),
    ("Daily Cash Wealth Product (R1)", False),
])
def test_stated_totals_and_names_must_match_the_plan(text, flagged):
    from pca.speech import check_product_facts
    plan = {"CN-DEP-12M": 200_000, "CN-WM-CASH": 200_000, "CN-WM-FI90": 180_000, "CN-BOND-GOV": 200_000}
    assert bool(check_product_facts(text, CAT, plan)) == flagged


@pytest.mark.parametrize("text,flagged", [
    ("建议配置12个月定期存款10万元和天天现金理财5万元。", False),
    ("天天现金理财就像存款一样安全。", True),
    ("除存款外，天天现金理财和国债基金本金可能亏损。", False),
    ("天天现金理财是一种存款。", True),
    ("Put ¥50,000 in the 12-Month Time Deposit and ¥50,000 in Daily Cash Wealth Product.", False),
])
def test_deposit_confusion_in_chinese(text, flagged):
    from pca.speech import check_deposit_confusion
    from pca.catalog import load_catalog
    assert bool(check_deposit_confusion(text, load_catalog())) == flagged
