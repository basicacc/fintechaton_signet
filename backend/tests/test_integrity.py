from pca.integrity import assess_integrity
from pca.verifier import check

from test_verifier import CAT, CN, client, prop


def test_retirement_savings_caps_aggressive_profile():
    r = assess_integrity("This is my retirement savings, I can't afford to lose it. Put it in tech.", 4)
    assert r.capped and r.effective_level == 2 and not r.reported_change
    assert {s.kind for s in r.signals} >= {"loss_averse", "dependent_funds"}


def test_job_loss_is_a_reported_change():
    r = assess_integrity("I just lost my job but I want to invest my severance.", 3)
    assert r.reported_change


def test_chinese_statements():
    assert assess_integrity("这是我的养老钱，不能亏。", 4).effective_level == 2
    assert assess_integrity("我刚被裁了", 3).reported_change


def test_consistent_client_not_flagged():
    r = assess_integrity("I'd like balanced growth over five years.", 3)
    assert not r.flagged and r.effective_level == 3


def test_cap_never_raises_level():
    r = assess_integrity("I need the money in 3 months.", 2)
    assert r.effective_level == 2 and not r.capped


def test_level_cap_is_enforced_by_verifier():
    c = client(level=4)
    assert check(c, CAT, prop({"CN-FUND-300": 50_000}), CN).ok
    c.level_cap = 2
    assert "CN-12-1" in {v.rule_id for v in check(c, CAT, prop({"CN-FUND-300": 50_000}), CN).violations}


def test_chinese_warning_signs():
    from pca.integrity import pattern_signals
    kinds = lambda t: {s.kind for s in pattern_signals(t)}
    assert "loss_averse" in kinds("这是我的养老钱，亏不起")
    assert "short_horizon" in kinds("这钱是给孩子上学用的，明年就要用")
    assert "short_horizon" in kinds("三个月后要用这笔钱")
    assert kinds("我想长期投资，追求稳健增长") == set()
