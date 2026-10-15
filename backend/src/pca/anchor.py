"""On-chain anchoring of certificate digests.

Works with any chain exposing the Ethereum JSON-RPC: an in-process EVM (tests),
a local node, a public testnet, or FISCO BCOS 3.x through its Web3 RPC.

Certificates are anchored in Merkle batches: one transaction per batch, with a
per-certificate inclusion proof that anyone can check, including on-chain via
``CertificateRegistry.isAnchored``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from eth_utils import keccak

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "CertificateRegistry.sol"
SOLC_VERSION = "0.8.26"
EVM_VERSION = "paris"  # no PUSH0, for compatibility with FISCO BCOS and older EVMs


# ---------------------------------------------------------------- Merkle tree (sorted pairs)

def leaf(digest_hex: str) -> bytes:
    return keccak(bytes.fromhex(digest_hex))


def _pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a < b else keccak(b + a)


def merkle_root_and_proofs(digests: list[str]) -> tuple[bytes, list[list[bytes]]]:
    if not digests:
        raise ValueError("empty batch")
    level = [leaf(d) for d in digests]
    proofs: list[list[bytes]] = [[] for _ in digests]
    positions = list(range(len(digests)))  # index of each leaf's ancestor in the current level
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_pair(level[i], level[i + 1]))
            else:
                nxt.append(level[i])  # odd node is promoted unchanged
        for k, pos in enumerate(positions):
            sib = pos ^ 1
            if sib < len(level):
                proofs[k].append(level[sib])
            positions[k] = pos // 2
        level = nxt
    return level[0], proofs


def verify_proof(digest_hex: str, proof: list[bytes], root: bytes) -> bool:
    h = leaf(digest_hex)
    for p in proof:
        h = _pair(h, p)
    return h == root


# ---------------------------------------------------------------- connection

def connect(url: str, accounts: list, consortium: bool = True):
    """Connect to an EVM JSON-RPC endpoint and sign transactions locally.

    ``consortium=True`` handles PBFT/PoA chains such as FISCO BCOS: long block
    ``extraData`` and zero gas price.
    """
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware, SignAndSendRawMiddlewareBuilder

    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
    if consortium:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        w3.eth.set_gas_price_strategy(lambda _w3, _tx: 0)
    w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(accounts), layer=0)
    w3.eth.default_account = accounts[0].address
    return w3


# ---------------------------------------------------------------- contract

@cache
def compiled() -> dict:
    import solcx

    solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_files([str(CONTRACT_PATH)], output_values=["abi", "bin"],
                              solc_version=SOLC_VERSION, evm_version=EVM_VERSION, optimize=True)
    return next(v for k, v in out.items() if k.endswith(":CertificateRegistry"))


@dataclass
class AnchorReceipt:
    digest: str
    root: str
    proof: list[str]
    batch_index: int
    tx_hash: str
    block_number: int
    chain_id: int
    contract: str


class Anchor:
    """Thin client for CertificateRegistry. ``w3`` must have a default account set."""

    def __init__(self, w3, address: str | None = None):
        self.w3 = w3
        info = compiled()
        if address is None:
            factory = w3.eth.contract(abi=info["abi"], bytecode=info["bin"])
            receipt = w3.eth.wait_for_transaction_receipt(factory.constructor().transact())
            address = receipt.contractAddress
        self.contract = w3.eth.contract(address=address, abi=info["abi"])
        self.address = address

    def anchor_batch(self, digests: list[str]) -> list[AnchorReceipt]:
        root, proofs = merkle_root_and_proofs(digests)
        tx = self.contract.functions.anchorBatch(root, len(digests)).transact()
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx)
        if rcpt.status != 1:
            raise RuntimeError(f"anchor transaction failed: {tx.hex()}")
        ev = self.contract.events.BatchAnchored().process_receipt(rcpt)[0]
        chain_id = self.w3.eth.chain_id
        return [AnchorReceipt(d, "0x" + root.hex(), ["0x" + p.hex() for p in pr], ev.args["index"],
                              "0x" + rcpt.transactionHash.hex().removeprefix("0x"), rcpt.blockNumber,
                              chain_id, self.address)
                for d, pr in zip(digests, proofs)]

    def is_anchored(self, r: AnchorReceipt) -> bool:
        return self.contract.functions.isAnchored(bytes.fromhex(r.digest), [bytes.fromhex(p[2:]) for p in r.proof],
                                                  bytes.fromhex(r.root[2:])).call()

    def root_on_chain(self, root_hex: str) -> bool:
        return self.contract.functions.batchIndexPlusOne(bytes.fromhex(root_hex[2:])).call() > 0

    def set_regulator(self, account: str, allowed: bool = True) -> str:
        tx = self.contract.functions.setRegulator(account, allowed).transact()
        return "0x" + self.w3.eth.wait_for_transaction_receipt(tx).transactionHash.hex().removeprefix("0x")

    def flag(self, digest: str, reason: str, sender: str) -> str:
        tx = self.contract.functions.flag(bytes.fromhex(digest), keccak(text=reason)).transact({"from": sender})
        return "0x" + self.w3.eth.wait_for_transaction_receipt(tx).transactionHash.hex().removeprefix("0x")

    def flag_of(self, digest: str) -> bytes:
        return self.contract.functions.flagOf(bytes.fromhex(digest)).call()
