import copy
import time
from dataclasses import replace

import pytest
from hypothesis import given, settings

from pca.certificate import Signer, digest, issue, verify
from pca.models import Procedure
from pca.rules import CN_NFRA, HOUSE, Constraint, Kind, Rule, RulePack, Stage, amt
from pca.expr import implies
from pca.verifier import check

from test_verifier import CAT, CN, NOW, client, full_procedure, prop, scenarios

SALT = b"test-salt"


@pytest.fixture(scope="module")
def signer():
    return Signer.generate()


@pytest.fixture(scope="module")
def keys(signer):
    return {signer.key_id: signer.key.public_key()}


def good(signer, stage=Stage.PROPOSAL):
    c = client(level=3)
    p = prop({"CN-WM-CASH": 100_000, "CN-FUND-DIV": 100_000}, procedure=full_procedure())
    return issue(c, CAT, p, CN, stage, signer, SALT, message="Advice text")


def test_issue_and_verify(signer, keys):
    cert = good(signer)
    r = verify(cert, keys)
    assert r.ok, r.problems
    assert cert["client_ref"] != "c1"  # pseudonymised
    assert "id" not in cert["client_facts"]


def test_refuses_to_certify_non_compliant(signer):
    with pytest.raises(ValueError, match="CN-12-1"):
        issue(client(level=2), CAT, prop({"CN-FUND-TECH": 10_000}), CN, Stage.PROPOSAL, signer, SALT)


def test_tampered_amount_is_detected(signer, keys):
    cert = good(signer)
    bad = copy.deepcopy(cert)
    bad["purchases"]["CN-FUND-DIV"] = 400_000
    r = verify(bad, keys)
    assert not r.ok and not r.signature_ok


def test_tampered_facts_are_detected(signer, keys):
    cert = good(signer)
    bad = copy.deepcopy(cert)
    bad["client_facts"]["assessments"][-1]["level"] = 5
    assert not verify(bad, keys).signature_ok


def test_unknown_key_rejected(signer):
    cert = good(signer)
    other = Signer.generate()
    assert not verify(cert, {other.key_id: other.key.public_key()}).ok


def test_digest_changes_with_any_edit(signer):
    cert = good(signer)
    bad = copy.deepcopy(cert)
    bad["issued_at"] = "2020-01-01T00:00:00+00:00"
    assert digest(cert) != digest(bad)


def test_rule_change_impact(signer, keys):
    """A stricter rule published later flags past advice that no longer complies."""
    cert = good(signer)  # 100k of 500k in one fund = 20%

    def strict_conc(ctx):
        for p in ctx.products():
            if not p.is_cash_like:
                yield Constraint("H-CONC-15", implies(amt(p.id) > 0, 100 * amt(p.id) <= 15 * ctx.wealth),
                                 "New policy: max 15% per product.", p.id)

    stricter = RulePack("house-v2", "2.0", "House v2", HOUSE.markets,
                        HOUSE.rules + (Rule("H-CONC-15", "Concentration 15%", "House policy v2",
                                            Kind.HOUSE, Stage.PROPOSAL, strict_conc),))
    r = verify(cert, keys, packs=[CN_NFRA, stricter])
    assert r.signature_ok and not r.recheck_ok
    assert r.violations == ["H-CONC-15@CN-FUND-DIV"]


def test_bulk_verification_is_fast(signer, keys):
    certs = [good(signer) for _ in range(500)]
    t = time.perf_counter()
    assert all(verify(c, keys).ok for c in certs)
    per = (time.perf_counter() - t) / len(certs)
    assert per < 0.01, f"{per*1000:.2f} ms per certificate"


@settings(max_examples=150, deadline=None)
@given(scenarios())
def test_certificate_needs_only_purchased_products(s):
    """Invariant behind compact certificates: products bought in zero amount never
    change the result, so re-checking against just the purchased products gives the
    same answer as checking against the whole catalogue."""
    c, p, packs, stage = s
    full = check(c, CAT, p, packs, stage)
    subset = check(c, {pid: CAT[pid] for pid in p.purchases}, p, packs, stage)
    assert full.ok == subset.ok
    assert {(v.rule_id, v.product_id) for v in full.violations} == \
           {(v.rule_id, v.product_id) for v in subset.violations}


def test_scope_is_signed_into_the_certificate(signer, keys):
    cert = good(signer)
    assert cert["scope"]["not_checked"]
    bad = copy.deepcopy(cert)
    bad["scope"]["not_checked"] = []  # quietly dropping the limits breaks the signature
    assert not verify(bad, keys).signature_ok
