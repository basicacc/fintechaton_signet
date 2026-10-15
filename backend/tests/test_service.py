"""Service behaviour: persistence, review cases, notifications, inspection, rule builder."""

import pytest

from pca.demo import DemoLLM
from pca.profile import QUESTIONS
from pca.service import Service, build_rule

ACK = ["basic_info", "risk_level", "principal_loss", "fees", "matching_opinion"]


@pytest.fixture
def data(tmp_path):
    return tmp_path


def svc(data):
    return Service(llm=DemoLLM(), chain_url="http://127.0.0.1:1", data=data)


def new_client(s, **kw):
    body = dict(name="Lin Yu", age=40, investable_assets=500_000, monthly_expenses=8_000, horizon_years=3,
                answers={q.id: 2 for q in QUESTIONS})
    body.update(kw)
    return s.create_client(**body)["id"]


def buy(s, cid, request="Invest 200k for balanced growth"):
    r = s.advise(cid, request)
    conf = s.confirm(cid, r["certificate"]["cert_id"], ACK)
    return r, conf


def test_state_survives_restart(data):
    s = svc(data)
    cid = new_client(s)
    _, conf = buy(s, cid)
    assert s.execute(conf["cert_id"])["ok"]
    s2 = svc(data)  # same data dir, fresh process
    assert s2.personas[cid].name == "Lin Yu"
    assert conf["cert_id"] in s2.certs and conf["cert_id"] in s2.gateway.executed
    assert s2.client_view(cid)["account"]["positions"]
    assert not s2.execute(conf["cert_id"])["ok"]  # still can't execute twice after restart


def test_capped_profile_case_and_lift(data):
    s = svc(data)
    r = s.advise("wong-ka-yan", "This is my retirement money and I can't afford to lose it. Invest 300k.")
    assert r["integrity"]["capped"]
    case = next(c for c in s.list_cases() if c["type"] == "profile_capped")
    with pytest.raises(ValueError, match="reason"):
        s.resolve_case(case["id"], "lift_cap", "ok")
    s.resolve_case(case["id"], "lift_cap", "Called the client; questionnaire confirmed accurate.")
    assert s.personas["wong-ka-yan"].client.level_cap is None
    assert s.list_cases() == [] and s.list_cases("resolved")[0]["resolution"]["action"] == "lift_cap"
    assert s.notifications("wong-ka-yan")[-1]["title"].startswith("Your profile review")


def test_request_reassessment_pauses_sales_and_reassessing_closes_case(data):
    s = svc(data)
    s.advise("wong-ka-yan", "It's my pension, I can't lose anything. Invest 300k.")
    case = next(c for c in s.list_cases() if c["type"] == "profile_capped")
    s.resolve_case(case["id"], "request_reassessment", "Statements contradict the questionnaire.")
    assert s.advise("wong-ka-yan", "Invest 100k")["reassessment_required"]
    assert s.notifications("wong-ka-yan")[-1]["action"] == "reassess"
    s.reassess("wong-ka-yan", {q.id: 1 for q in QUESTIONS})
    assert not s.personas["wong-ka-yan"].client.reported_change


def test_rerating_notifies_holder_and_opens_case(data):
    s = svc(data)
    cid = new_client(s)
    _, conf = buy(s, cid)
    s.execute(conf["cert_id"])
    held = next(iter(s.client_view(cid)["account"]["positions"]))
    s.rerate(held, 5)
    note = s.notifications(cid)[-1]
    assert "R5" in note["title"] and note["kind"] == "warning"
    case = next(c for c in s.list_cases() if c["type"] == "rerating")
    s.resolve_case(case["id"], "offer_review", "Holder above level after re-rating.")
    assert s.notifications(cid)[-1]["title"].startswith("Would you like")
    assert svc(data).catalog[held].distributor_risk == 5  # rating persisted


def test_flag_blocks_order_notifies_client_and_withdraw_works(data):
    s = svc(data)
    cid = new_client(s)
    _, conf = buy(s, cid)
    out = s.flag(conf["cert_id"], "regulator review")
    assert out["flagged"]
    assert s.notifications(cid)[-1]["title"] == "A recommendation is on hold"
    ex = s.execute(conf["cert_id"])
    assert not ex["ok"] and any("flagged" in r for r in ex["reasons"])
    case = next(c for c in s.list_cases() if c["type"] == "regulator_flag")
    s.resolve_case(case["id"], "withdraw", "Regulator asked us to stop this sale.")
    assert s.certs[conf["cert_id"]].withdrawn
    assert "withdrawn" in s.execute(conf["cert_id"])["reasons"][0]


def test_inspect_lists_every_constraint(data):
    s = svc(data)
    cid = new_client(s)
    _, conf = buy(s, cid)
    rep = s.inspect(conf["cert_id"])
    assert rep["signature_ok"] and rep["rules_match"] and rep["recheck_ok"] and rep["anchored"]
    assert rep["constraints"] and all(c["passed"] for c in rep["constraints"])
    assert {"CN-12-1", "CN-31", "H-CONC"} & {c["rule_id"] for c in rep["constraints"]}


@pytest.mark.parametrize("template,params", [
    ("max_share_per_product", {"pct": 5, "min_risk": 1}),
    ("ban_risk_for_age", {"min_age": 18, "min_risk": 2}),
    ("max_total_risky", {"pct": 0, "min_risk": 2}),
    ("min_reserve", {"months": 36}),
])
def test_rule_builder_finds_affected_clients(data, template, params):
    s = svc(data)
    cid = new_client(s)
    buy(s, cid, "Invest 250k for balanced growth")
    s.seed_synthetic(200)
    r = s.rule_change(template, params)
    assert r["checked"] > 200 and r["affected_count"] > 0
    assert r["affected"][0]["real_client"]  # real clients listed first


def test_rule_builder_validates_parameters():
    with pytest.raises(ValueError):
        build_rule("max_share_per_product", {"pct": 500})
    with pytest.raises(ValueError):
        build_rule("nope", {})


def test_repeat_signals_update_the_open_case_instead_of_duplicating(data):
    s = svc(data)
    s.advise("wong-ka-yan", "It's my retirement money, I can't lose it. Invest 300k.")
    s.advise("wong-ka-yan", "Again: this is my pension, no losses please. Invest 200k.")
    cases = [c for c in s.list_cases() if c["type"] == "profile_capped"]
    assert len(cases) == 1 and len(cases[0]["details"]) >= 2


def test_reset_demo_restores_a_clean_state(data):
    s = svc(data)
    cid = new_client(s)
    _, conf = buy(s, cid)
    s.execute(conf["cert_id"])
    s.rerate("CN-WM-FI90", 5)
    s.reset_demo()
    assert set(s.personas) == {"chen-mei", "zhang-hao", "wong-ka-yan", "li-wen"}
    assert not s.certs and not s.list_cases("all") and not s.gateway.broker.accounts
    assert s.catalog["CN-WM-FI90"].distributor_risk == 2
    s2 = svc(data)  # and it stays clean after a restart
    assert not s2.certs and s2.catalog["CN-WM-FI90"].distributor_risk == 2
    assert s2.ledger.verify()[0]


def test_conversation_is_remembered_and_only_delivered_text_reaches_the_model(data):
    s = svc(data)
    cid = new_client(s)
    r = s.advise(cid, "Invest 200k for balanced growth")
    s.advise(cid, "Make it safer")
    chat = svc(data).chat(cid)  # survives a restart
    assert [t["request"] for t in chat] == ["Invest 200k for balanced growth", "Make it safer"]
    h = s._history(cid)
    assert h[0]["advisor"] == r["final"]["message"] and h[0]["plan"] == r["final"]["purchases"]
    blocked = [a["message"] for a in r["attempts"] if not a["ok"]]
    assert all(b not in str(h) for b in blocked if b)
    s.reset_demo()
    assert s.chat("chen-mei") == []


def test_review_and_order_progress_is_saved_with_the_conversation(data):
    s = svc(data)
    cid = new_client(s)
    r, conf = buy(s, cid)
    s.execute(conf["cert_id"])
    flow = svc(data).chat(cid)[-1]["flow"]
    assert flow["executionCert"] == conf["cert_id"] and flow["receipt"]["tx_hash"] == conf["receipt"]["tx_hash"]
    assert flow["exec"]["ok"] and flow["exec"]["orders"]


def test_verify_all_checks_anchors_on_the_chain_itself(data):
    s = svc(data)
    cid = new_client(s)
    buy(s, cid)
    assert s.verify_all()["unanchored"] == 0
    s.anchor = svc(data / "other").anchor  # a chain that never saw this anchor
    rep = s.verify_all()
    assert rep["unanchored"] == 1 and "anchor not found" in rep["failures"][0]["problems"][0]


def test_offline_demo_reanchors_on_the_fresh_simulated_chain(data):
    s = svc(data)
    cid = new_client(s)
    _, conf = buy(s, cid)
    s2 = svc(data)  # restart: the in-process EVM is empty again
    assert "re-recorded" in s2.chain["note"] and s2.verify_all()["unanchored"] == 0
    assert s2.execute(conf["cert_id"])["ok"]
    assert any(e.type == "fallback_chain_reanchored" for e in s2.ledger.events)


def test_holdings_follow_the_account_so_caps_count_what_the_client_already_owns(data):
    s = svc(data)
    cid = new_client(s)
    r, conf = buy(s, cid)
    s.execute(conf["cert_id"])
    c = s.personas[cid].client
    bought = r["final"]["purchases"]
    assert c.holdings == bought and c.investable_assets == 500_000 - sum(bought.values())
    from pca.models import Proposal
    from pca.verifier import check
    pid = max(bought, key=bought.get)  # buying the same product again must respect concentration overall
    cat = {k: v for k, v in s.catalog.items()}
    again = check(c, cat, Proposal(client_id=cid, purchases={pid: 150_000}, as_of=s.now()), s.packs_for(cid))
    assert any(v.rule_id == "H-CONC" for v in again.violations)


def test_client_can_sell_and_sees_orders(data):
    s = svc(data)
    cid = new_client(s)
    r, conf = buy(s, cid)
    s.execute(conf["cert_id"])
    pid, amt = next(iter(r["final"]["purchases"].items()))
    out = s.redeem(cid, pid, amt)
    assert out["order"]["status"] in ("redeemed", "pending_redemption")
    assert pid not in s.personas[cid].client.holdings
    assert any(o["order_id"] == out["order"]["order_id"] for o in s.client_view(cid)["orders"])
    assert any("You sold" in n["title"] for n in s.notifications(cid))
    with pytest.raises(ValueError):
        s.redeem(cid, pid, 1)  # nothing left
    with pytest.raises(KeyError):
        s.cancel_order(cid, "someone-elses-order")


def test_two_plans_certified_before_either_executed_are_rechecked_against_current_holdings(data):
    s = svc(data)
    cid = new_client(s)
    _, conf1 = buy(s, cid, "Invest 300k for balanced growth")
    _, conf2 = buy(s, cid, "Invest 300k for balanced growth")  # certified against the same empty holdings
    assert s.execute(conf1["cert_id"])["ok"]
    second = s.execute(conf2["cert_id"])
    assert not second["ok"] and second["escalated"]
    assert any(c["type"] == "execution_blocked" for c in s.list_cases())


def test_qualified_investor_buys_a_private_product_and_cancels_in_cooling_off(data):
    s = svc(data)
    r = s.advise("li-wen", "Put 1.2 million into the Quant Alpha Private Wealth No.3.")
    assert "CN-PRIV-QUANT" in r["final"]["purchases"]
    conf = s.confirm("li-wen", r["certificate"]["cert_id"], ACK)
    assert [o["status"] for o in s.execute(conf["cert_id"])["orders"]] == ["pending_cooling_off"]
    assert s.personas["li-wen"].client.holdings == {"CN-PRIV-QUANT": 1_200_000}  # committed money counts
    order = next(o for o in s.client_view("li-wen")["orders"] if o["cancellable"])
    assert s.cancel_order("li-wen", order["order_id"])["order"]["status"] == "cancelled"
    assert s.client_view("li-wen")["account"]["cash"] == 8_000_000 and not s.personas["li-wen"].client.holdings
    # an unqualified client can't get the private product at all
    r2 = s.advise("zhang-hao", "Put 100k into the Quant Alpha Private Wealth No.3.")
    assert "CN-PRIV-QUANT" not in r2["final"]["purchases"]
