"""Execution gateway and sandbox broker.

The advisor agent never touches the broker. Every capability belongs to a
permission tier, and the gateway is the only component holding the T2 capability
(placing orders). T3 actions don't exist as code paths: requests for them are
refused and logged.

Before any order, the gateway requires:
  1. a valid signed execution-stage certificate (signature, rule code, re-evaluation)
  2. its digest anchored on-chain, and not flagged by the regulator
  3. the client's waiting period (``consideration_hours``) to have elapsed
  4. the advice to still comply under the *current* rules and product ratings
  5. that the certificate hasn't already been executed
Private products settle only after a 24h cooling-off period, during which the
client can cancel for a full refund.

Selling (redemption) needs no certificate: it reduces risk and is the client's own
instruction, not advice. It still goes through the gateway so it is authorized and
logged, and cash arrives after the product's liquidity period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Protocol, Sequence

from .anchor import AnchorReceipt
from .certificate import _reconstruct, digest, verify
from .ledger import Ledger
from .models import Product
from .rules import RulePack, Stage
from .verifier import check

COOLING_OFF = timedelta(hours=24)  # private wealth products (CBIRC 2018 wealth rules; article TODO)


class Tier(IntEnum):
    T0_READ = 0
    T1_PROPOSE = 1
    T2_EXECUTE = 2
    T3_FORBIDDEN = 3


TOOL_TIERS: dict[str, Tier] = {
    "get_catalog": Tier.T0_READ,
    "get_client_facts": Tier.T0_READ,
    "explain_rule": Tier.T0_READ,
    "propose_allocation": Tier.T1_PROPOSE,
    "execute_certificate": Tier.T2_EXECUTE,
    "redeem_on_client_instruction": Tier.T2_EXECUTE,
    "cancel_on_client_instruction": Tier.T2_EXECUTE,
    "override_verifier": Tier.T3_FORBIDDEN,
    "edit_rules": Tier.T3_FORBIDDEN,
    "transfer_out": Tier.T3_FORBIDDEN,
    "change_client_assessment": Tier.T3_FORBIDDEN,
}
AGENT_MAX_TIER = Tier.T1_PROPOSE  # the LLM agent can read and propose, nothing more


class AnchorCheck(Protocol):
    def is_anchored(self, r: AnchorReceipt) -> bool: ...
    def flag_of(self, digest: str) -> bytes: ...


class PermissionDenied(Exception):
    pass


def authorize(actor: str, tool: str, max_tier: Tier, ledger: Ledger) -> None:
    tier = TOOL_TIERS.get(tool, Tier.T3_FORBIDDEN)
    if tier == Tier.T3_FORBIDDEN or tier > max_tier:
        ledger.append("permission_denied", {"actor": actor, "tool": tool, "tier": tier.name})
        raise PermissionDenied(f"{actor} may not call {tool} ({tier.name})")


# ---------------------------------------------------------------- broker

@dataclass
class Order:
    order_id: str
    client_ref: str
    product_id: str
    amount: int
    status: str  # buy: "filled" | "pending_cooling_off" | "cancelled"; sell: "pending_redemption" | "redeemed"
    settles_at: datetime | None = None


@dataclass
class Account:
    cash: int
    positions: dict[str, int] = field(default_factory=dict)


class SandboxBroker:
    """Simulated broker. Units are priced at 1 RMB; this is a sandbox, not a market model."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.accounts: dict[str, Account] = {}
        self.orders: dict[str, Order] = {}

    def open_account(self, client_ref: str, cash: int) -> None:
        self.accounts.setdefault(client_ref, Account(cash))
        self.ledger.append("account_opened", {"client_ref": client_ref, "cash": cash})

    def place(self, order_id: str, client_ref: str, product: Product, amount: int, now: datetime) -> Order:
        acct = self.accounts[client_ref]
        if amount > acct.cash:
            raise ValueError("insufficient sandbox cash")
        acct.cash -= amount
        if product.private:
            o = Order(order_id, client_ref, product.id, amount, "pending_cooling_off", now + COOLING_OFF)
        else:
            acct.positions[product.id] = acct.positions.get(product.id, 0) + amount
            o = Order(order_id, client_ref, product.id, amount, "filled")
        self.orders[order_id] = o
        self.ledger.append("order", {**o.__dict__, "settles_at": o.settles_at})
        return o

    def cancel(self, order_id: str, now: datetime) -> Order:
        o = self.orders[order_id]
        if o.status != "pending_cooling_off" or (o.settles_at and now >= o.settles_at):
            raise ValueError("order is no longer cancellable")
        o.status = "cancelled"
        self.accounts[o.client_ref].cash += o.amount
        self.ledger.append("order_cancelled", {"order_id": order_id, "refund": o.amount})
        return o

    def redeem(self, order_id: str, client_ref: str, product: Product, amount: int, now: datetime) -> Order:
        acct = self.accounts.get(client_ref)
        held = acct.positions.get(product.id, 0) if acct else 0
        if amount <= 0 or amount > held:
            raise ValueError(f"you hold ¥{held:,} of this product")
        acct.positions[product.id] = held - amount
        if not acct.positions[product.id]:
            del acct.positions[product.id]
        days = 0 if product.type.value == "deposit" else product.liquidity_days  # deposits: early withdrawal
        if days <= 1:
            acct.cash += amount
            o = Order(order_id, client_ref, product.id, amount, "redeemed")
        else:
            o = Order(order_id, client_ref, product.id, amount, "pending_redemption",
                      now + timedelta(days=days))
        self.orders[order_id] = o
        self.ledger.append("redemption", {**o.__dict__, "settles_at": o.settles_at})
        return o

    def settle(self, now: datetime) -> list[Order]:
        done = []
        for o in self.orders.values():
            if o.status == "pending_redemption" and o.settles_at and now >= o.settles_at:
                o.status = "redeemed"
                self.accounts[o.client_ref].cash += o.amount
                self.ledger.append("redemption_settled", {"order_id": o.order_id})
                done.append(o)
            if o.status == "pending_cooling_off" and o.settles_at and now >= o.settles_at:
                o.status = "filled"
                acct = self.accounts[o.client_ref]
                acct.positions[o.product_id] = acct.positions.get(o.product_id, 0) + o.amount
                self.ledger.append("order_settled", {"order_id": o.order_id})
                done.append(o)
        return done


# ---------------------------------------------------------------- gateway

@dataclass
class ExecutionReport:
    ok: bool
    cert_id: str
    orders: list[Order] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    escalated: bool = False


class Gateway:
    def __init__(self, broker: SandboxBroker, anchor: AnchorCheck, public_keys: dict, packs: Sequence[RulePack],
                 ledger: Ledger):
        self.broker, self.anchor, self.keys, self.packs, self.ledger = broker, anchor, public_keys, list(packs), ledger
        self.executed: set[str] = set()
        self.compliance_queue: list[dict] = []

    def execute(self, cert: dict, receipt: AnchorReceipt, catalog: dict[str, Product], now: datetime,
                actor: str = "gateway", actor_tier: Tier = Tier.T2_EXECUTE) -> ExecutionReport:
        authorize(actor, "execute_certificate", actor_tier, self.ledger)
        cid = cert.get("cert_id", "?")
        reasons: list[str] = []

        if cert.get("stage") != Stage.EXECUTION.value:
            reasons.append("certificate is not an execution-stage certificate")
        report = verify(cert, self.keys)
        reasons += report.problems
        d = digest(cert)
        if receipt.digest != d:
            reasons.append("anchor receipt is for a different certificate")
        elif not self.anchor.is_anchored(receipt):
            reasons.append("certificate digest is not anchored on-chain")
        if any(self.anchor.flag_of(d)):
            reasons.append("certificate was flagged by the regulator on-chain")
        if cid in self.executed:
            reasons.append("certificate was already executed")

        waited = timedelta(hours=cert.get("procedure", {}).get("consideration_hours", 0))
        as_of = datetime.fromisoformat(cert["as_of"])
        if now < as_of + waited:
            reasons.append(f"client consideration period not over until {as_of + waited:%Y-%m-%d %H:%M}")

        if not reasons:  # still compliant under current rules and current product data?
            client, _, proposal = _reconstruct(cert)
            missing = [p for p in proposal.purchases if p not in catalog]
            if missing:
                reasons.append(f"products no longer offered: {missing}")
            else:
                current = check(client, {p: catalog[p] for p in proposal.purchases}, proposal, self.packs,
                                Stage.EXECUTION)
                reasons += [f"no longer compliant: {v.rule_id}{'@' + v.product_id if v.product_id else ''} "
                            f"({v.message})" for v in current.violations]

        if reasons:
            escalate = any(r.startswith(("no longer compliant", "products no longer", "certificate was flagged"))
                           for r in reasons)
            self.ledger.append("execution_blocked", {"cert_id": cid, "digest": d, "reasons": reasons})
            if escalate:
                self.compliance_queue.append({"cert_id": cid, "digest": d, "reasons": reasons, "at": now.isoformat()})
            return ExecutionReport(False, cid, reasons=reasons, escalated=escalate)

        client_ref = cert["client_ref"]
        if client_ref not in self.broker.accounts:
            self.broker.open_account(client_ref, cert["client_facts"]["investable_assets"])
        orders = [self.broker.place(f"{cid}:{i}", client_ref, catalog[pid], amt, now)
                  for i, (pid, amt) in enumerate(sorted(cert["purchases"].items()))]
        self.executed.add(cid)
        self.ledger.append("execution_done", {"cert_id": cid, "digest": d, "tx_hash": receipt.tx_hash,
                                              "orders": [o.order_id for o in orders]})
        return ExecutionReport(True, cid, orders)

    def redeem(self, client_ref: str, product: Product, amount: int, now: datetime, order_id: str) -> Order:
        """Sell on the client's own instruction (no certificate needed: it isn't advice)."""
        authorize("gateway", "redeem_on_client_instruction", Tier.T2_EXECUTE, self.ledger)
        return self.broker.redeem(order_id, client_ref, product, amount, now)

    def cancel(self, client_ref: str, order_id: str, now: datetime) -> Order:
        """Cancel a purchase during its cooling-off period, on the client's own instruction."""
        authorize("gateway", "cancel_on_client_instruction", Tier.T2_EXECUTE, self.ledger)
        o = self.broker.orders.get(order_id)
        if o is None or o.client_ref != client_ref:
            raise KeyError(order_id)
        return self.broker.cancel(order_id, now)
