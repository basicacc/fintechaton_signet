"""Compliance certificates.

A certificate records exactly what was checked: the client facts (pseudonymised),
the facts of every purchased product, the purchases, the procedure, the rule-pack
versions plus a hash of the rule source, and the result. It's signed with Ed25519.

Only ``digest(cert)`` ever goes on-chain. The certificate itself contains personal
data and stays in the institution's records (PIPL; NFRA Art. 17 retention).

Verification needs no solver. It re-evaluates the rules with the plain evaluator,
so a regulator can check certificates without trusting our Z3 search code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .models import Client, Procedure, Product, Proposal
from .rules import PACKS, RulePack, Stage
from .verifier import check

SCHEMA = "pca.cert/1"

# What a certificate does and doesn't establish. Signed into every certificate so the
# limits of the proof travel with it.
SCOPE = {
    "verified": "Every encoded rule in the listed rule packs holds for the recorded facts and purchases "
                "(product risk vs client level, affordability, concentration, liquidity, horizon, private and "
                "Southbound eligibility, and, at execution, disclosures, client confirmation and waiting periods).",
    "pattern_checked": "The advisor's message (English or Chinese) was screened for guarantee promises, deposit "
                       "confusion, performance figures, risk levels, product names or stated totals not matching the "
                       "catalogue and the plan, missing loss warnings, promotion of unsuitable or unchecked products, "
                       "and coaching on the risk assessment. Screening is pattern-based: paraphrases can slip through.",
    "not_checked": [
        "Whether the facts the client confirmed are true",
        "Whether the product data (ratings, fees, returns) is correct",
        "Rules not encoded in the listed rule packs",
        "The overall meaning or tone of the advice beyond the screened patterns",
        "Whether the investment will perform well",
    ],
}


# ---------------------------------------------------------------- canonical form

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def rules_source_hash() -> str:
    """Hash of the rule definitions, so a certificate pins the exact rule code."""
    src = resources.files("pca").joinpath("rules.py").read_bytes()
    return sha256(src)


def pack_refs(packs: Sequence[RulePack]) -> list[dict]:
    return [{"id": p.id, "version": p.version} for p in packs]


# ---------------------------------------------------------------- keys

@dataclass
class Signer:
    key: Ed25519PrivateKey
    key_id: str

    @classmethod
    def generate(cls) -> "Signer":
        k = Ed25519PrivateKey.generate()
        return cls(k, _key_id(k.public_key()))

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        if path.exists():
            k = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(k, Ed25519PrivateKey)
            return cls(k, _key_id(k.public_key()))
        s = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(s.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        path.chmod(0o600)
        return s

    def public_pem(self) -> str:
        return self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _key_id(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sha256(raw)[:16]


def load_public_key(pem: str) -> Ed25519PublicKey:
    k = serialization.load_pem_public_key(pem.encode())
    assert isinstance(k, Ed25519PublicKey)
    return k


# ---------------------------------------------------------------- issue

def pseudonym(client_id: str, salt: bytes) -> str:
    return hmac.new(salt, client_id.encode(), hashlib.sha256).hexdigest()[:24]


def issue(client: Client, catalog: dict[str, Product], proposal: Proposal, packs: Sequence[RulePack],
          stage: Stage, signer: Signer, salt: bytes, message: str = "",
          speech: dict | None = None, prev: str | None = None) -> dict:
    """Check the proposal and, only if it passes, return a signed certificate."""
    result = check(client, catalog, proposal, packs, stage)
    if not result.ok:
        raise ValueError(f"cannot certify a non-compliant proposal: {[v.rule_id for v in result.violations]}")
    facts = client.model_dump(mode="json", exclude={"id"})
    body = {
        "schema": SCHEMA,
        "cert_id": str(uuid.uuid4()),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage.value,
        "as_of": proposal.as_of.isoformat(),
        "rule_packs": pack_refs(packs),
        "rules_hash": rules_source_hash(),
        "client_ref": pseudonym(client.id, salt),
        "client_facts": facts,
        "products": {pid: catalog[pid].model_dump(mode="json") for pid in proposal.purchases},
        "purchases": dict(proposal.purchases),
        "procedure": proposal.procedure.model_dump(mode="json"),
        "result": {"ok": True, "checked": result.checked, "rules": result.rules},
        "scope": SCOPE,
        "message_sha256": sha256(message.encode()),
        "speech_check": speech,
        "prev": prev,
    }
    sig = signer.key.sign(canonical(body))
    return {**body, "signature": {"alg": "ed25519", "key_id": signer.key_id,
                                  "sig": base64.b64encode(sig).decode()}}


def digest(cert: dict) -> str:
    """The value anchored on-chain: hash of the full signed certificate."""
    return sha256(canonical(cert))


# ---------------------------------------------------------------- verify

@dataclass
class VerifyReport:
    ok: bool
    signature_ok: bool
    rules_match: bool  # certificate was made with the same rule code as this verifier
    recheck_ok: bool  # re-evaluating the facts still passes
    problems: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _reconstruct(cert: dict) -> tuple[Client, dict[str, Product], Proposal]:
    client = Client.model_validate({"id": cert["client_ref"], **cert["client_facts"]})
    products = {pid: Product.model_validate(p) for pid, p in cert["products"].items()}
    proposal = Proposal(client_id=cert["client_ref"], purchases=cert["purchases"],
                        as_of=datetime.fromisoformat(cert["as_of"]),
                        procedure=Procedure.model_validate(cert["procedure"]))
    return client, products, proposal


def verify(cert: dict, public_keys: dict[str, Ed25519PublicKey],
           packs: Sequence[RulePack] | None = None) -> VerifyReport:
    """Check signature, rule version, and re-evaluate the facts.

    ``packs`` defaults to the packs named in the certificate. Pass different packs
    to ask "would this advice still be compliant under the new rules?".
    """
    problems: list[str] = []
    body = {k: v for k, v in cert.items() if k != "signature"}
    sig = cert.get("signature", {})

    signature_ok = False
    key = public_keys.get(sig.get("key_id", ""))
    if key is None:
        problems.append("unknown signing key")
    else:
        try:
            key.verify(base64.b64decode(sig["sig"]), canonical(body))
            signature_ok = True
        except (InvalidSignature, KeyError, ValueError):
            problems.append("signature invalid: certificate was altered or not issued by this key")

    rules_match = cert.get("rules_hash") == rules_source_hash()
    if not rules_match:
        problems.append("certificate was issued under different rule code")

    if packs is None:
        try:
            packs = [PACKS[r["id"]] for r in cert["rule_packs"]]
        except KeyError as e:
            problems.append(f"unknown rule pack {e}")
            return VerifyReport(False, signature_ok, rules_match, False, problems)

    violations: list[str] = []
    try:
        client, products, proposal = _reconstruct(cert)
        r = check(client, products, proposal, packs, Stage(cert["stage"]))
        recheck_ok = r.ok
        violations = [f"{v.rule_id}{'@' + v.product_id if v.product_id else ''}" for v in r.violations]
        if not recheck_ok:
            problems.append("facts do not satisfy the rules")
    except Exception as e:  # malformed certificate
        recheck_ok = False
        problems.append(f"malformed certificate: {e}")

    return VerifyReport(signature_ok and rules_match and recheck_ok, signature_ok, rules_match,
                        recheck_ok, problems, violations)
