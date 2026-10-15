import copy
from datetime import timedelta

import pytest
from web3 import EthereumTesterProvider, Web3

from pca.anchor import Anchor
from pca.certificate import Signer, digest, issue
from pca.gateway import AGENT_MAX_TIER, Gateway, PermissionDenied, SandboxBroker, Tier, authorize
from pca.ledger import Ledger
from pca.rules import Stage

from test_verifier import CAT, CN, NOW, client, full_procedure, prop


@pytest.fixture(scope="module")
def chain():
    w3 = Web3(EthereumTesterProvider())
    w3.eth.default_account = w3.eth.accounts[0]
    a = Anchor(w3)
    a.set_regulator(w3.eth.accounts[1])
    return w3, a


@pytest.fixture
def env(chain):
    w3, anchor = chain
    signer = Signer.generate()
    ledger = Ledger()
    gw = Gateway(SandboxBroker(ledger), anchor, {signer.key_id: signer.key.public_key()}, CN, ledger)
    return w3, anchor, signer, ledger, gw


def make(signer, anchor, purchases=None, c=None, **proc):
    c = c or client(level=3)
    p = prop(purchases or {"CN-WM-CASH": 100_000, "CN-FUND-DIV": 100_000}, procedure=full_procedure(**proc))
    cert = issue(c, CAT, p, CN, Stage.EXECUTION, signer, b"s")
    receipt = anchor.anchor_batch([digest(cert)])[0]
    return cert, receipt


def test_happy_path_executes_and_logs(env):
    _, anchor, signer, ledger, gw = env
    cert, r = make(signer, anchor)
    rep = gw.execute(cert, r, CAT, NOW)
    assert rep.ok and len(rep.orders) == 2
    acct = gw.broker.accounts[cert["client_ref"]]
    assert acct.positions == {"CN-FUND-DIV": 100_000, "CN-WM-CASH": 100_000}
    assert acct.cash == 300_000
    assert ledger.verify() == (True, None)
    assert [e.type for e in ledger.events][-1] == "execution_done"


def test_double_execution_blocked(env):
    _, anchor, signer, _, gw = env
    cert, r = make(signer, anchor)
    assert gw.execute(cert, r, CAT, NOW).ok
    rep = gw.execute(cert, r, CAT, NOW)
    assert not rep.ok and "already executed" in rep.reasons[0]


def test_unanchored_certificate_blocked(env):
    _, anchor, signer, _, gw = env
    cert, r = make(signer, anchor)
    other, _ = make(signer, anchor)
    fake = copy.copy(r)
    fake.digest = digest(other)  # receipt doesn't belong to this certificate
    assert not gw.execute(cert, fake, CAT, NOW).ok


def test_tampered_certificate_blocked(env):
    _, anchor, signer, _, gw = env
    cert, r = make(signer, anchor)
    cert["purchases"]["CN-FUND-DIV"] = 150_000
    rep = gw.execute(cert, r, CAT, NOW)
    assert not rep.ok and any("signature" in x for x in rep.reasons)


def test_proposal_stage_certificate_cannot_execute(env):
    _, anchor, signer, _, gw = env
    c = client(level=3)
    cert = issue(c, CAT, prop({"CN-WM-CASH": 10_000}), CN, Stage.PROPOSAL, signer, b"s")
    r = anchor.anchor_batch([digest(cert)])[0]
    assert not gw.execute(cert, r, CAT, NOW).ok


def test_regulator_flag_blocks_and_escalates(env):
    w3, anchor, signer, _, gw = env
    cert, r = make(signer, anchor)
    anchor.flag(digest(cert), "under investigation", w3.eth.accounts[1])
    rep = gw.execute(cert, r, CAT, NOW)
    assert not rep.ok and rep.escalated and gw.compliance_queue


def test_consideration_period_enforced(env):
    _, anchor, signer, _, gw = env
    c = client(level=4, age=70)
    cert, r = make(signer, anchor, {"CN-FUND-300": 50_000, "CN-WM-CASH": 100_000}, c,
                   enhanced_warning=True, consideration_hours=24, follow_up_scheduled=True)
    rep = gw.execute(cert, r, CAT, NOW + timedelta(hours=2))
    assert not rep.ok and "consideration period" in rep.reasons[0]
    assert gw.execute(cert, r, CAT, NOW + timedelta(hours=25)).ok


def test_product_rerated_after_certification_is_blocked(env):
    _, anchor, signer, _, gw = env
    cert, r = make(signer, anchor)  # C3 client, CN-FUND-DIV is R3
    rerated = dict(CAT)
    rerated["CN-FUND-DIV"] = CAT["CN-FUND-DIV"].model_copy(update={"distributor_risk": 4})
    rep = gw.execute(cert, r, rerated, NOW)
    assert not rep.ok and rep.escalated
    assert any("CN-12-1@CN-FUND-DIV" in x for x in rep.reasons)


def test_private_product_cooling_off_and_cancel(env):
    _, anchor, signer, ledger, gw = env
    c = client(level=5, investable_assets=5_000_000, qualified_private=True, horizon_years=5, monthly_expenses=0)
    cert, r = make(signer, anchor, {"CN-PRIV-QUANT": 1_000_000}, c)
    rep = gw.execute(cert, r, CAT, NOW)
    assert rep.ok and rep.orders[0].status == "pending_cooling_off"
    gw.broker.cancel(rep.orders[0].order_id, NOW + timedelta(hours=10))
    assert gw.broker.accounts[cert["client_ref"]].cash == 5_000_000


def test_private_product_settles_after_cooling_off(env):
    _, anchor, signer, _, gw = env
    c = client(level=5, investable_assets=5_000_000, qualified_private=True, horizon_years=5, monthly_expenses=0)
    cert, r = make(signer, anchor, {"CN-PRIV-QUANT": 1_000_000}, c)
    oid = gw.execute(cert, r, CAT, NOW).orders[0].order_id
    assert gw.broker.settle(NOW + timedelta(hours=25))
    with pytest.raises(ValueError):
        gw.broker.cancel(oid, NOW + timedelta(hours=26))


def test_agent_cannot_execute_or_use_forbidden_tools(env):
    _, anchor, signer, ledger, gw = env
    cert, r = make(signer, anchor)
    with pytest.raises(PermissionDenied):
        gw.execute(cert, r, CAT, NOW, actor="advisor-llm", actor_tier=AGENT_MAX_TIER)
    for tool in ("override_verifier", "edit_rules", "transfer_out", "change_client_assessment", "made_up_tool"):
        with pytest.raises(PermissionDenied):
            authorize("advisor-llm", tool, Tier.T2_EXECUTE, ledger)
    assert sum(1 for _ in ledger.of_type("permission_denied")) == 6


def test_ledger_tampering_detected(env):
    _, anchor, signer, ledger, gw = env
    cert, r = make(signer, anchor)
    gw.execute(cert, r, CAT, NOW)
    ev = ledger.events[1]
    ledger.events[1] = ev.__class__(**{**ev.__dict__, "data": {**ev.data, "amount": 1}})
    assert ledger.verify() == (False, 1)


def test_redemption_is_immediate_for_liquid_products_and_delayed_otherwise():
    ledger = Ledger()
    b = SandboxBroker(ledger)
    b.open_account("r", 100_000)
    b.place("o1", "r", CAT["CN-WM-CASH"], 20_000, NOW)
    b.place("o2", "r", CAT["CN-WM-FI90"], 30_000, NOW)
    b.place("o3", "r", CAT["CN-DEP-12M"], 10_000, NOW)
    assert b.redeem("s1", "r", CAT["CN-WM-CASH"], 5_000, NOW).status == "redeemed"
    assert b.redeem("s3", "r", CAT["CN-DEP-12M"], 10_000, NOW).status == "redeemed"  # early withdrawal
    slow = b.redeem("s2", "r", CAT["CN-WM-FI90"], 30_000, NOW)
    assert slow.status == "pending_redemption" and slow.settles_at == NOW + timedelta(days=90)
    acct = b.accounts["r"]
    assert acct.cash == 40_000 + 5_000 + 10_000 and acct.positions == {"CN-WM-CASH": 15_000}
    with pytest.raises(ValueError):
        b.redeem("s4", "r", CAT["CN-WM-CASH"], 15_001, NOW)  # more than held
    b.settle(NOW + timedelta(days=91))
    assert acct.cash == 85_000 and slow.status == "redeemed"
    assert {e.type for e in ledger.events} >= {"redemption", "redemption_settled"}
