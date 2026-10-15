import hashlib
import time

import pytest
from hypothesis import given, settings, strategies as st
from web3 import EthereumTesterProvider, Web3
from eth_tester.exceptions import TransactionFailed
from web3.exceptions import ContractLogicError

REVERT = (ContractLogicError, TransactionFailed)  # real node vs in-process EVM

from pca.anchor import Anchor, merkle_root_and_proofs, verify_proof


def digests(n: int, seed: str = "") -> list[str]:
    return [hashlib.sha256(f"{seed}{i}".encode()).hexdigest() for i in range(n)]


@pytest.fixture(scope="module")
def chain():
    w3 = Web3(EthereumTesterProvider())
    w3.eth.default_account = w3.eth.accounts[0]
    return w3, Anchor(w3)


@settings(max_examples=50, deadline=None)
@given(st.integers(1, 70))
def test_merkle_proofs_verify_for_any_batch_size(n):
    ds = digests(n, "p")
    root, proofs = merkle_root_and_proofs(ds)
    assert all(verify_proof(d, p, root) for d, p in zip(ds, proofs))
    assert not verify_proof(digests(1, "other")[0], proofs[0], root)


def test_anchor_and_verify_on_chain(chain):
    w3, anchor = chain
    ds = digests(37, "a")
    receipts = anchor.anchor_batch(ds)
    assert len({r.tx_hash for r in receipts}) == 1  # one transaction for the whole batch
    assert all(anchor.is_anchored(r) for r in receipts)


def test_tampered_digest_not_anchored(chain):
    w3, anchor = chain
    r = anchor.anchor_batch(digests(5, "b"))[2]
    r.digest = digests(1, "evil")[0]
    assert not anchor.is_anchored(r)


def test_duplicate_root_rejected(chain):
    _, anchor = chain
    ds = digests(3, "dup")
    anchor.anchor_batch(ds)
    with pytest.raises(REVERT):
        anchor.anchor_batch(ds)


def test_only_anchorers_can_anchor(chain):
    w3, anchor = chain
    from pca.anchor import merkle_root_and_proofs as mr
    root, _ = mr(digests(2, "x"))
    with pytest.raises(REVERT):
        anchor.contract.functions.anchorBatch(root, 2).transact({"from": w3.eth.accounts[3]})


def test_regulator_flag(chain):
    w3, anchor = chain
    regulator = w3.eth.accounts[1]
    d = digests(1, "flag")[0]
    with pytest.raises(REVERT):
        anchor.flag(d, "mis-selling", regulator)  # not yet a regulator
    anchor.set_regulator(regulator)
    anchor.flag(d, "mis-selling", regulator)
    assert anchor.flag_of(d) != b"\x00" * 32


def test_ten_thousand_certificates_in_one_transaction(chain):
    _, anchor = chain
    t = time.perf_counter()
    receipts = anchor.anchor_batch(digests(10_000, "big"))
    assert len({r.tx_hash for r in receipts}) == 1
    assert anchor.is_anchored(receipts[9_999])
    assert max(len(r.proof) for r in receipts) == 14  # ceil(log2(10000))
    assert time.perf_counter() - t < 30
