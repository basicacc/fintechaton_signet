"""Application service: wires the verifier, advisor, certificates, chain and gateway.

State is persisted in SQLite (``state.db`` under ``PCA_DATA``, default ``backend/data``):
clients, certificates, review cases, client notifications, sandbox accounts and orders,
product ratings, the demo clock. Keys, chain config and the evidence ledger live next
to it. A demo clock can be fast-forwarded to show consideration and cooling-off periods.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from eth_account import Account

from .advisor import Advisor, AdvisorResult, Attempt, language
from .anchor import Anchor, AnchorReceipt, connect, verify_proof
from .catalog import load_catalog
from .compare import compare, default_candidates, rationale
from .certificate import SCOPE, Signer, _reconstruct, digest, issue, pseudonym, verify
from .expr import implies, total
from .gateway import Account as BrokerAccount, Gateway, Order, SandboxBroker
from .integrity import assess_integrity
from .ledger import Ledger
from .llm import LLM, OllamaLLM
from .models import DISCLOSURE_ITEMS, Assessment, Client, Market, Procedure, Product, Proposal
from .profile import AssessmentLimitError, assess
from .rules import CN_NFRA, HK_WMC, HOUSE, PACKS, Constraint, Kind, Rule, RulePack, Stage, amt
from .store import Store
from .verifier import check, default_packs, explain, required_procedure

DATA = Path(os.environ.get("PCA_DATA", Path(__file__).resolve().parents[2] / "data"))
CHAT_KEEP = 40  # turns kept per client for display; the model sees the last advisor.HISTORY_TURNS


# ---------------------------------------------------------------- personas

@dataclass
class Persona:
    client: Client
    name: str
    blurb: str
    markets: list[Market]
    answers: dict[str, int]  # questionnaire answers used to seed the assessment
    example: bool = True

    def to_doc(self) -> dict:
        return {"client": self.client.model_dump(mode="json"), "name": self.name, "blurb": self.blurb,
                "markets": [m.value for m in self.markets], "answers": self.answers, "example": self.example}

    @classmethod
    def from_doc(cls, d: dict) -> "Persona":
        return cls(Client.model_validate(d["client"]), d["name"], d["blurb"], [Market(m) for m in d["markets"]],
                   d["answers"], d["example"])


def _example_personas() -> list[Persona]:
    def c(id, **kw):
        return Client(id=id, **kw)

    return [
        Persona(c("chen-mei", age=71, investable_assets=800_000, monthly_expenses=5_000, horizon_years=2,
                  annual_income=60_000), "Chen Mei", "Retired teacher, 71, living on her pension savings", [Market.CN],
                {"age": 3, "income": 2, "share": 1, "experience": 1, "knowledge": 1, "horizon": 1, "goal": 1,
                 "loss": 1, "reaction": 1}),
        Persona(c("zhang-hao", age=28, investable_assets=200_000, monthly_expenses=6_000, horizon_years=2,
                  annual_income=150_000), "Zhang Hao", "Wants fast gains, but his questionnaire says cautious",
                [Market.CN],
                {"age": 0, "income": 1, "share": 1, "experience": 1, "knowledge": 1, "horizon": 1, "goal": 1,
                 "loss": 1, "reaction": 1}),
        Persona(c("wong-ka-yan", age=45, investable_assets=2_000_000, monthly_expenses=20_000, horizon_years=5,
                  annual_income=600_000, gba_resident=True, investment_experience_years=8,
                  net_household_financial_assets=3_500_000, household_financial_assets=4_000_000,
                  min_annual_income_3y=550_000), "Wong Ka-yan", "Shenzhen resident investing in Hong Kong funds",
                [Market.CN, Market.HK],
                {"age": 1, "income": 3, "share": 2, "experience": 3, "knowledge": 2, "horizon": 3, "goal": 2,
                 "loss": 2, "reaction": 2}),
        Persona(c("li-wen", age=52, investable_assets=8_000_000, monthly_expenses=40_000, horizon_years=3,
                  annual_income=1_200_000, qualified_private=True, investment_experience_years=15,
                  net_household_financial_assets=12_000_000, household_financial_assets=14_000_000,
                  min_annual_income_3y=1_100_000),
                "Li Wen", "Business owner, verified qualified investor: can buy private products", [Market.CN],
                {"age": 1, "income": 3, "share": 2, "experience": 3, "knowledge": 2, "horizon": 3, "goal": 2,
                 "loss": 2, "reaction": 2}),
    ]


# ---------------------------------------------------------------- serialization helpers

def violation_dict(v) -> dict:
    return {"rule_id": v.rule_id, "title": v.title, "citation": v.citation, "kind": v.kind.value,
            "message": v.message, "product_id": v.product_id}


def attempt_dict(a: Attempt) -> dict:
    return {"source": a.source, "ok": a.ok, "purchases": a.purchases, "message": a.message, "error": a.error,
            "sent_back": a.sent_back, "understood": a.understood,
            "violations": [violation_dict(v) for v in a.violations],
            "speech": [{"rule_id": s.rule_id, "citation": s.citation, "message": s.message, "excerpt": s.excerpt,
                        "product_id": s.product_id} for s in a.speech]}


def receipt_dict(r: AnchorReceipt) -> dict:
    return asdict(r)


# ---------------------------------------------------------------- rule builder (simulated rule changes)

RULE_TEMPLATES: dict[str, dict] = {
    "max_share_per_product": {
        "label": "Limit how much of a client's wealth can go into any single product",
        "params": {"pct": {"label": "Max % of wealth", "min": 1, "max": 100, "default": 15},
                   "min_risk": {"label": "Applies to products rated at least", "min": 1, "max": 5, "default": 2}},
    },
    "ban_risk_for_age": {
        "label": "Stop older clients from holding higher-risk products",
        "params": {"min_age": {"label": "Clients aged at least", "min": 18, "max": 100, "default": 65},
                   "min_risk": {"label": "Banned from products rated at least", "min": 2, "max": 5, "default": 4}},
    },
    "max_total_risky": {
        "label": "Limit the total share of wealth in higher-risk products",
        "params": {"pct": {"label": "Max % of wealth in total", "min": 0, "max": 100, "default": 25},
                   "min_risk": {"label": "Counting products rated at least", "min": 2, "max": 5, "default": 3}},
    },
    "min_reserve": {
        "label": "Require a bigger emergency cushion",
        "params": {"months": {"label": "Months of expenses kept in cash-like products", "min": 0, "max": 36,
                              "default": 12}},
    },
}


def build_rule(template: str, params: dict[str, int]) -> tuple[Rule, str]:
    spec = RULE_TEMPLATES.get(template)
    if spec is None:
        raise ValueError(f"unknown rule template {template!r}")
    p: dict[str, int] = {}
    for name, rng in spec["params"].items():
        v = int(params.get(name, rng["default"]))
        if not rng["min"] <= v <= rng["max"]:
            raise ValueError(f"{rng['label']} must be between {rng['min']} and {rng['max']}")
        p[name] = v

    if template == "max_share_per_product":
        text = f"No more than {p['pct']}% of wealth in any single product rated R{p['min_risk']} or above"

        def build(ctx, p=p, text=text):
            for prod in ctx.products():
                if prod.effective_risk >= p["min_risk"]:
                    yield Constraint("SIM-RULE", implies(amt(prod.id) > 0, 100 * amt(prod.id) <= p["pct"] * ctx.wealth),
                                     text + ".", prod.id)
    elif template == "ban_risk_for_age":
        text = f"Clients aged {p['min_age']}+ may not hold products rated R{p['min_risk']} or above"

        def build(ctx, p=p, text=text):
            if ctx.client.age >= p["min_age"]:
                for prod in ctx.products():
                    if prod.effective_risk >= p["min_risk"]:
                        yield Constraint("SIM-RULE", amt(prod.id).eq(0), text + ".", prod.id)
    elif template == "max_total_risky":
        text = f"No more than {p['pct']}% of wealth in total in products rated R{p['min_risk']} or above"

        def build(ctx, p=p, text=text):
            risky = [prod for prod in ctx.products() if prod.effective_risk >= p["min_risk"]]
            if risky:
                s = total(amt(prod.id) for prod in risky)
                yield Constraint("SIM-RULE", implies(s > 0, 100 * s <= p["pct"] * ctx.wealth), text + ".")
    else:
        text = f"Keep at least {p['months']} months of expenses in cash-like products"

        def build(ctx, p=p, text=text):
            non_cash = total(amt(prod.id) for prod in ctx.products() if not prod.is_cash_like)
            reserve = p["months"] * ctx.client.monthly_expenses
            yield Constraint("SIM-RULE", implies(non_cash > 0, ctx.client.investable_assets - non_cash >= reserve),
                             text + ".")
    return Rule("SIM-RULE", text, "SIMULATED rule change (not real law)", Kind.LAW, Stage.PROPOSAL, build), text


PRESETS = {"elderly-r4-ban": ("ban_risk_for_age", {"min_age": 65, "min_risk": 4}),
           "concentration-15": ("max_share_per_product", {"pct": 15, "min_risk": 2})}


# ---------------------------------------------------------------- review cases

CASE_ACTIONS: dict[str, dict[str, str]] = {
    "profile_capped": {
        "keep_cap": "Keep the lower level until the client is reassessed",
        "request_reassessment": "Ask the client to retake the risk questionnaire",
        "lift_cap": "Remove the cap: the client's questionnaire is accurate",
    },
    "profile_change": {
        "request_reassessment": "Ask the client to retake the risk questionnaire",
        "close": "Close: the client has already been contacted",
    },
    "rerating": {
        "offer_review": "Offer the client a portfolio review",
        "close": "Close: no further action needed",
    },
    "execution_blocked": {
        "withdraw": "Withdraw the recommendation and tell the client",
        "close": "Close without contacting the client",
    },
    "regulator_flag": {
        "withdraw": "Withdraw the recommendation and tell the client",
        "close": "Close: keep on hold pending the regulator",
    },
}


@dataclass
class StoredCert:
    cert: dict
    client_id: str
    stage: str
    receipt: AnchorReceipt | None = None
    synthetic: bool = False
    withdrawn: bool = False

    def to_doc(self) -> dict:
        return {"cert": self.cert, "client_id": self.client_id, "stage": self.stage,
                "receipt": asdict(self.receipt) if self.receipt else None, "synthetic": self.synthetic,
                "withdrawn": self.withdrawn}

    @classmethod
    def from_doc(cls, d: dict) -> "StoredCert":
        return cls(d["cert"], d["client_id"], d["stage"], AnchorReceipt(**d["receipt"]) if d["receipt"] else None,
                   d.get("synthetic", False), d.get("withdrawn", False))


# ---------------------------------------------------------------- service

class Service:
    def __init__(self, llm: LLM | None = None, chain_url: str | None = None, data: Path = DATA):
        self.data = data
        (data / "keys").mkdir(parents=True, exist_ok=True)
        self.store = Store(data / "state.db")
        self.catalog: dict[str, Product] = dict(load_catalog())
        for pid, risk in (self.store.get("meta", "ratings") or {}).items():
            if pid in self.catalog:
                self.catalog[pid] = self.catalog[pid].model_copy(update={"distributor_risk": risk})
        self.signer = Signer.load_or_create(data / "keys" / "signer.pem")
        self.salt = self._secret(data / "keys" / "salt", lambda: secrets.token_bytes(32))
        self.keys = {self.signer.key_id: self.signer.key.public_key()}
        self.ledger = Ledger(data / "ledger.jsonl")
        self.clock_offset = timedelta(seconds=self.store.get("meta", "clock_offset", 0))
        self.challenge = self.store.get("meta", "challenge") or {"attempts": 0, "blocked_drafts": 0, "breaks": 0}
        self.last_advice: dict[str, AdvisorResult] = {}

        self.llm = llm or OllamaLLM()
        self.sentinel = None
        if llm is None:  # only alongside a real local model
            from .sentinel import Sentinel

            sentinel = Sentinel()
            self.sentinel = sentinel if sentinel.available() else None
        self._connect_chain(chain_url or os.environ.get("PCA_CHAIN_URL", "http://127.0.0.1:8545"))
        self.gateway = Gateway(SandboxBroker(self.ledger), self.anchor, self.keys, [CN_NFRA, HK_WMC, HOUSE],
                               self.ledger)
        self._load()
        self._reanchor_on_fallback_chain()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        self.personas: dict[str, Persona] = {k: Persona.from_doc(v) for k, v in self.store.all("client")}
        for p in _example_personas():  # also adds example clients introduced after the data was created
            if p.client.id not in self.personas:
                assess(p.client, p.answers, self.now() - timedelta(days=30))
                self.personas[p.client.id] = p
                self._save_client(p.client.id)
        self.certs: dict[str, StoredCert] = {k: StoredCert.from_doc(v) for k, v in self.store.all("cert")}
        self.cases: dict[str, dict] = dict(self.store.all("case"))
        self.notes: dict[str, dict] = dict(self.store.all("note"))
        broker = self.gateway.broker
        broker.accounts = {k: BrokerAccount(v["cash"], v["positions"]) for k, v in self.store.all("acct")}
        for k, v in self.store.all("order"):
            settles = datetime.fromisoformat(v["settles_at"]) if v.get("settles_at") else None
            broker.orders[k] = Order(v["order_id"], v["client_ref"], v["product_id"], v["amount"], v["status"], settles)
        self.gateway.executed = set(self.store.get("meta", "executed") or [])
        self.chats: dict[str, list[dict]] = dict(self.store.all("chat"))

    def _save_client(self, cid: str) -> None:
        self.store.put("client", cid, self.personas[cid].to_doc())

    def _save_cert(self, cert_id: str) -> None:
        self.store.put("cert", cert_id, self.certs[cert_id].to_doc())

    def _save_broker(self) -> None:
        b = self.gateway.broker
        self.store.put_many("acct", [(k, asdict(v)) for k, v in b.accounts.items()])
        self.store.put_many("order", [(k, {**asdict(o), "settles_at": o.settles_at.isoformat() if o.settles_at else None})
                                      for k, o in b.orders.items()])
        self.store.put("meta", "executed", sorted(self.gateway.executed))

    def _save_meta(self) -> None:
        self.store.put("meta", "clock_offset", self.clock_offset.total_seconds())
        self.store.put("meta", "challenge", self.challenge)

    def reset_demo(self) -> dict:
        """Wipe demo state (clients, certificates, cases, messages, accounts, ratings, clock) and start fresh.

        The evidence ledger is append-only by design, so it isn't erased: a reset is recorded in it instead.
        On-chain records also remain, as they would on a real chain.
        """
        self.store.db.execute("DELETE FROM doc")
        self.catalog = dict(load_catalog())
        self.clock_offset = timedelta()
        self.challenge = {"attempts": 0, "blocked_drafts": 0, "breaks": 0}
        self.gateway.broker.accounts.clear()
        self.gateway.broker.orders.clear()
        self.gateway.executed.clear()
        self.last_advice.clear()
        self._load()
        self._save_meta()
        self.ledger.append("demo_reset", {})
        return {"ok": True, "clients": len(self.personas)}

    # ------------------------------------------------------------ infra

    @staticmethod
    def _secret(path: Path, make: Callable[[], bytes]) -> bytes:
        if not path.exists():
            path.write_bytes(make())
            path.chmod(0o600)
        return path.read_bytes()

    def _account(self, name: str):
        key = self._secret(self.data / "keys" / f"{name}.eth", lambda: secrets.token_bytes(32))
        return Account.from_key(key)

    def _connect_chain(self, url: str) -> None:
        self.bank_acct, self.regulator_acct = self._account("bank"), self._account("regulator")
        try:
            w3 = connect(url, [self.bank_acct, self.regulator_acct])
            w3.eth.block_number  # reachability check
            cfg_path = self.data / "chain.json"
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
            address = cfg.get(url)
            if address and not w3.eth.get_code(address):
                address = None  # chain was reset
            self.anchor = Anchor(w3, address)
            if not address:
                self.anchor.set_regulator(self.regulator_acct.address)
                cfg[url] = self.anchor.address
                cfg_path.write_text(json.dumps(cfg, indent=2))
            self.chain = {"kind": "FISCO BCOS (Web3 RPC)", "url": url, "chain_id": w3.eth.chain_id,
                          "contract": self.anchor.address}
        except Exception as e:  # offline demo fallback, clearly labelled
            from web3 import EthereumTesterProvider, Web3

            w3 = Web3(EthereumTesterProvider())
            w3.eth.default_account = w3.eth.accounts[0]
            self.regulator_addr_fallback = w3.eth.accounts[1]
            self.anchor = Anchor(w3)
            self.anchor.set_regulator(self.regulator_addr_fallback)
            self.chain = {"kind": "in-process EVM (fallback: chain unreachable)", "url": None,
                          "chain_id": w3.eth.chain_id, "contract": self.anchor.address, "error": str(e)[:200]}

    def _reanchor_on_fallback_chain(self) -> None:
        """Offline demo only: the in-process EVM starts empty on every restart, so stored anchors would vanish.

        Re-anchor the stored digests on the fresh simulated chain and record that in the evidence log.
        A real chain (FISCO BCOS) keeps its own state, so this never runs there.
        """
        if self.chain["url"] is not None:
            return
        stored = [(k, v) for k, v in self.certs.items() if v.receipt]
        if not stored:
            return
        receipts = self.anchor.anchor_batch([v.receipt.digest for _, v in stored])
        for (k, v), r in zip(stored, receipts):
            v.receipt = r
        self.store.put_many("cert", [(k, v.to_doc()) for k, v in stored])
        self.chain["note"] = f"simulated chain restarted: {len(stored)} stored anchors re-recorded"
        self.ledger.append("fallback_chain_reanchored", {"count": len(stored), "root": receipts[0].root})

    def _regulator_address(self) -> str:
        return getattr(self, "regulator_addr_fallback", None) or self.regulator_acct.address

    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None) + self.clock_offset

    def advance_clock(self, hours: float) -> datetime:
        self.clock_offset += timedelta(hours=hours)
        settled = self.gateway.broker.settle(self.now())
        self.ledger.append("clock_advanced", {"hours": hours, "settled": [o.order_id for o in settled]})
        for cid in self.personas:
            self._sync_holdings(cid)
        self._save_meta()
        self._save_broker()
        return self.now()

    def packs_for(self, client_id: str) -> list[RulePack]:
        return default_packs(self.personas[client_id].markets)

    def _ref(self, client_id: str) -> str:
        return pseudonym(client_id, self.salt)

    def display_name(self, client_id: str) -> str:
        if client_id in self.personas:
            return self.personas[client_id].name
        m = re.match(r"synthetic-\d+-(\d+)$", client_id)
        return f"Simulated client #{int(m.group(1)) + 1}" if m else client_id

    # ------------------------------------------------------------ notifications

    def notify(self, client_id: str, title: str, text: str, kind: str = "info", action: str | None = None) -> dict:
        nid = secrets.token_hex(6)
        note = {"id": nid, "client_id": client_id, "at": self.now().isoformat(), "kind": kind, "title": title,
                "text": text, "action": action, "read": False}
        self.notes[nid] = note
        self.store.put("note", nid, note)
        self.ledger.append("client_notified", {"client_ref": self._ref(client_id), "title": title})
        return note

    def notifications(self, client_id: str) -> list[dict]:
        return sorted((n for n in self.notes.values() if n["client_id"] == client_id), key=lambda n: n["at"])

    def mark_read(self, client_id: str) -> None:
        for n in self.notes.values():
            if n["client_id"] == client_id and not n["read"]:
                n["read"] = True
                self.store.put("note", n["id"], n)

    # ------------------------------------------------------------ review cases

    def open_case(self, type: str, client_id: str, title: str, summary: str, details: list[str],
                  refs: dict | None = None) -> dict:
        for case in self.cases.values():  # one open case per issue: repeat signals add to it
            if (case["status"] == "open" and case["type"] == type and case["client_id"] == client_id
                    and case["refs"] == (refs or {})):
                case["details"] = case["details"] + [d for d in details if d not in case["details"]]
                case["summary"] = summary
                case["at"] = self.now().isoformat()
                self.store.put("case", case["id"], case)
                return case
        cid = secrets.token_hex(5)
        case = {"id": cid, "type": type, "client_id": client_id, "name": self.display_name(client_id),
                "at": self.now().isoformat(), "status": "open", "title": title, "summary": summary,
                "details": details, "refs": refs or {}, "actions": CASE_ACTIONS[type], "resolution": None}
        self.cases[cid] = case
        self.store.put("case", cid, case)
        self.ledger.append("case_opened", {"case": cid, "type": type, "client_ref": self._ref(client_id)})
        return case

    def list_cases(self, status: str = "open") -> list[dict]:
        rows = [c for c in self.cases.values() if status == "all" or c["status"] == status]
        return sorted(rows, key=lambda c: c["at"], reverse=True)

    def resolve_case(self, case_id: str, action: str, reason: str) -> dict:
        case = self.cases[case_id]
        if case["status"] != "open":
            raise ValueError("this case is already resolved")
        if action not in case["actions"]:
            raise ValueError(f"action {action!r} isn't available for this case")
        if len(reason.strip()) < 8:
            raise ValueError("please write a short reason (at least 8 characters); it goes into the evidence log")
        cid = case["client_id"]
        p = self.personas.get(cid)
        effect = "No change to the client."
        if action == "lift_cap" and p:
            p.client.level_cap = None
            p.client.profile_flags = []
            self._save_client(cid)
            effect = "Cap removed. The questionnaire level applies again."
            self.notify(cid, "Your profile review is complete",
                        "We reviewed your profile and your current risk assessment stands.")
        elif action == "keep_cap" and p:
            effect = "Lower level kept until the client is reassessed."
        elif action == "request_reassessment" and p:
            p.client.reported_change = True
            self._save_client(cid)
            effect = "Sales paused until the client retakes the questionnaire."
            self.notify(cid, "Please update your risk assessment",
                        "Something you told us may affect which products suit you. Please retake the short risk "
                        "questionnaire before we recommend anything else.", kind="action", action="reassess")
        elif action == "offer_review" and p:
            product = self.catalog.get(case["refs"].get("product_id", ""))
            effect = "Portfolio review offered."
            self.notify(cid, "Would you like a portfolio review?",
                        f"{product.name if product else 'A product you hold'} is now rated higher risk than your "
                        "profile. We'd like to review your portfolio with you. Nothing changes unless you decide.")
        elif action == "withdraw":
            cert_id = case["refs"].get("cert_id")
            if cert_id in self.certs:
                self.certs[cert_id].withdrawn = True
                self._save_cert(cert_id)
            effect = "Recommendation withdrawn."
            if p:
                self.notify(cid, "A recommendation was withdrawn",
                            "After a compliance review we've withdrawn one of our recommendations to you. No order was "
                            "placed. We're sorry for the inconvenience.")
        case["status"] = "resolved"
        case["resolution"] = {"action": action, "label": case["actions"][action], "reason": reason.strip(),
                              "effect": effect, "at": self.now().isoformat()}
        self.store.put("case", case_id, case)
        self.ledger.append("case_resolved", {"case": case_id, "action": action, "reason": reason.strip()})
        return case

    # ------------------------------------------------------------ clients

    def _holdings_conflict(self, stored: StoredCert) -> list[str]:
        """Re-check certified advice against what the client owns *now*.

        Two plans certified before either was executed were each checked against the old holdings;
        together they could break a concentration or high-risk cap. Returns the violated rules, if any.
        """
        cur = self.personas[stored.client_id].client
        cert_client, _, proposal = _reconstruct(stored.cert)
        if cur.holdings == cert_client.holdings:
            return []
        now_client = cert_client.model_copy(update={"holdings": cur.holdings,
                                                    "investable_assets": cur.investable_assets})
        r = check(now_client, self.catalog, proposal, self.packs_for(stored.client_id), Stage.EXECUTION)
        return [f"{v.rule_id}: {v.message}" for v in r.violations]

    def _sync_holdings(self, client_id: str) -> None:
        """Once a client has a sandbox account, it is the source of truth for their money.

        Holdings include purchases still in cooling-off (the money is committed), so concentration and
        high-risk caps see everything the client owns. Cash includes nothing still being redeemed.
        """
        ref = self._ref(client_id)
        acct = self.gateway.broker.accounts.get(ref)
        if acct is None:
            return
        held = dict(acct.positions)
        for o in self.gateway.broker.orders.values():
            if o.client_ref == ref and o.status == "pending_cooling_off":
                held[o.product_id] = held.get(o.product_id, 0) + o.amount
        c = self.personas[client_id].client
        c.holdings = {k: v for k, v in held.items() if v}
        c.investable_assets = acct.cash
        self._save_client(client_id)

    def orders_of(self, client_id: str) -> list[dict]:
        ref = self._ref(client_id)
        return [{"order_id": o.order_id, "product_id": o.product_id, "amount": o.amount, "status": o.status,
                 "settles_at": o.settles_at.isoformat() if o.settles_at else None,
                 "cancellable": o.status == "pending_cooling_off" and bool(o.settles_at and self.now() < o.settles_at)}
                for o in self.gateway.broker.orders.values() if o.client_ref == ref]

    def redeem(self, client_id: str, product_id: str, amount: int) -> dict:
        if client_id not in self.personas:
            raise KeyError(client_id)
        if product_id not in self.catalog:
            raise ValueError("unknown product")
        o = self.gateway.redeem(self._ref(client_id), self.catalog[product_id], amount, self.now(),
                                f"sell-{secrets.token_hex(6)}")
        self._sync_holdings(client_id)
        self._save_broker()
        name = self.catalog[product_id].name
        when = ("The cash is in your account now." if o.status == "redeemed" else
                f"The cash arrives on {o.settles_at:%Y-%m-%d}, after the product's "
                f"{(o.settles_at - self.now()).days}-day redemption period.")
        self.notify(client_id, f"You sold ¥{amount:,} of {name}", when)
        return {"order": next(x for x in self.orders_of(client_id) if x["order_id"] == o.order_id),
                "account": self.client_view(client_id)["account"]}

    def cancel_order(self, client_id: str, order_id: str) -> dict:
        if client_id not in self.personas:
            raise KeyError(client_id)
        o = self.gateway.cancel(self._ref(client_id), order_id, self.now())
        self._sync_holdings(client_id)
        self._save_broker()
        self.notify(client_id, f"Purchase cancelled: {self.catalog[o.product_id].name}",
                    f"¥{o.amount:,} is back in your account. Cancelling within the cooling-off period is free.")
        return {"order": next(x for x in self.orders_of(client_id) if x["order_id"] == order_id),
                "account": self.client_view(client_id)["account"]}

    def client_view(self, client_id: str) -> dict:
        p = self.personas[client_id]
        c = p.client
        acct = self.gateway.broker.accounts.get(self._ref(client_id))
        notes = self.notifications(client_id)
        return {"id": c.id, "name": p.name, "blurb": p.blurb, "markets": [m.value for m in p.markets],
                "example": p.example,
                "level": c.current_assessment.level if c.current_assessment else None,
                "level_cap": c.level_cap, "reported_change": c.reported_change,
                "assessed_at": c.current_assessment.at.isoformat() if c.current_assessment else None,
                "assessment_valid": c.assessment_valid_at(self.now()), "facts": c.model_dump(mode="json"),
                "account": asdict(acct) if acct else None, "orders": self.orders_of(client_id),
                "profile_flags": c.profile_flags,
                "unread": sum(1 for n in notes if not n["read"])}

    def create_client(self, name: str, age: int, investable_assets: int, monthly_expenses: int, horizon_years: int,
                      answers: dict[str, int], annual_income: int = 0, gba_resident: bool = False,
                      investment_experience_years: int = 0, net_household_financial_assets: int = 0) -> dict:
        """A client entered in the UI, assessed with the real questionnaire."""
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "client"
        cid = f"{slug}-{secrets.token_hex(2)}"
        client = Client(id=cid, age=age, investable_assets=investable_assets, monthly_expenses=monthly_expenses,
                        horizon_years=horizon_years, annual_income=annual_income, gba_resident=gba_resident,
                        investment_experience_years=investment_experience_years,
                        net_household_financial_assets=net_household_financial_assets,
                        household_financial_assets=net_household_financial_assets,
                        min_annual_income_3y=annual_income)
        result = assess(client, answers, self.now())
        markets = [Market.CN, Market.HK] if gba_resident else [Market.CN]
        blurb = f"{age} · ¥{investable_assets:,} to invest · {horizon_years}-year horizon"
        self.personas[cid] = Persona(client, name.strip() or "Client", blurb, markets, answers, example=False)
        self._save_client(cid)
        self.ledger.append("client_created", {"client_ref": self._ref(cid), "level": result.level})
        return {**self.client_view(cid), "score": result.score, "knockout": result.knockout}

    def reassess(self, client_id: str, answers: dict[str, int]) -> dict:
        c = self.personas[client_id].client
        try:
            r = assess(c, answers, self.now())
        except AssessmentLimitError as e:
            self.ledger.append("assessment_refused", {"client_ref": self._ref(client_id), "reason": str(e)})
            raise
        self._save_client(client_id)
        self.ledger.append("assessment", {"client_ref": self._ref(client_id), "level": r.level, "score": r.score,
                                          "changed_from": r.changed_from})
        for case in self.cases.values():  # a fresh assessment resolves open profile cases
            if case["client_id"] == client_id and case["status"] == "open" and case["type"].startswith("profile"):
                case["status"] = "resolved"
                case["resolution"] = {"action": "reassessed", "label": "Client retook the questionnaire",
                                      "reason": f"New assessment: C{r.level}", "effect": "Profile updated.",
                                      "at": self.now().isoformat()}
                self.store.put("case", case["id"], case)
        return {"level": r.level, "score": r.score, "knockout": r.knockout, "changed_from": r.changed_from}

    # ------------------------------------------------------------ advice

    def chat(self, client_id: str) -> list[dict]:
        """The client's conversation as they saw it (survives reloads and restarts)."""
        if client_id not in self.personas:
            raise KeyError(client_id)
        return self.chats.get(client_id, [])

    def _history(self, client_id: str) -> list[dict]:
        """What the model may see of earlier turns: the client's words and *delivered* replies only."""
        return [{"client": t["request"], "advisor": t["result"]["final"]["message"],
                 "plan": t["result"]["final"]["purchases"],
                 # which products the turn was about, so "put 50k in it" can be resolved next time
                 "about": next((a["understood"]["products"] for a in t["result"].get("attempts", [])
                                if a.get("understood")), [])}
                for t in self.chats.get(client_id, []) if t["result"].get("final")]

    def _remember(self, client_id: str, request: str, out: dict) -> dict:
        turns = self.chats.setdefault(client_id, [])
        turns.append({"request": request, "result": out, "at": self.now().isoformat()})
        del turns[:-CHAT_KEEP]
        self.store.put("chat", client_id, turns)
        return out

    def _track(self, client_id: str, match: Callable[[dict], bool], **progress) -> None:
        """Record how far the client got with a recommendation (review, anchoring, order) in their chat."""
        for t in reversed(self.chats.get(client_id, [])):
            cert = t["result"].get("certificate")
            flow = t.setdefault("flow", {})
            if cert and match({"proposal": cert["cert_id"], **flow}):
                flow.update(progress)
                self.store.put("chat", client_id, self.chats[client_id])
                return

    def advise(self, client_id: str, request: str, on_attempt: Callable[[dict], None] | None = None,
               challenge: bool = False) -> dict:
        return self._remember(client_id, request, self._advise(client_id, request, on_attempt, challenge))

    def _advise(self, client_id: str, request: str, on_attempt: Callable[[dict], None] | None,
                challenge: bool) -> dict:
        p = self.personas[client_id]
        packs = self.packs_for(client_id)
        catalog = {k: v for k, v in self.catalog.items() if v.market in p.markets}
        now = self.now()
        t0 = time.perf_counter()

        assessed = p.client.current_assessment.level if p.client.current_assessment else None
        integ = assess_integrity(request, assessed, self.sentinel)
        integrity = {"signals": [s.__dict__ for s in integ.signals], "assessed_level": integ.assessed_level,
                     "effective_level": integ.effective_level, "capped": integ.capped,
                     "reported_change": integ.reported_change}
        if integ.flagged:
            p.client.profile_flags = sorted({*p.client.profile_flags, *(s.kind for s in integ.signals)})
            if integ.reported_change:
                p.client.reported_change = True  # Art. 29: reassess before any sale
            if integ.capped:
                p.client.level_cap = min(integ.effective_level, p.client.level_cap or 5)
            self._save_client(client_id)
            quotes = [f"“{s.excerpt}”" for s in integ.signals]
            if integ.reported_change:
                self.open_case("profile_change", client_id, "Client reported a change in circumstances",
                               "Sales are paused until the client is reassessed (NFRA Art. 29).", quotes)
            else:
                self.open_case("profile_capped", client_id, "What the client said contradicts their questionnaire",
                               f"Questionnaire says C{assessed}; their own words point to C{integ.effective_level}. "
                               f"Advice is capped at C{integ.effective_level} until someone reviews it.", quotes)
            self.ledger.append("profile_integrity", {"client_ref": self._ref(client_id), **integrity})

        if p.client.reported_change:
            msg = ("感谢您告诉我。由于您的情况可能发生了变化，按规定在推荐或销售任何产品之前，需要先更新您的风险评估"
                   "（《金融机构产品适当性管理办法》第二十九条）。我们先一起重新做一次问卷吧。"
                   if language(request) == "zh" else
                   "Thank you for telling me. Because your circumstances may have changed, the rules require us to "
                   "update your risk assessment before recommending or selling any product (NFRA Art. 29). "
                   "Let's retake the questionnaire together first.")
            return {"attempts": [], "final": {"source": "solver-fix", "ok": True, "purchases": {}, "message": msg,
                                              "error": None, "violations": [], "speech": []},
                    "first_pass_ok": True, "seconds": round(time.perf_counter() - t0, 1), "model": self.llm.name,
                    "blocked_requests": {}, "fix": None, "certificate": None, "integrity": integrity,
                    "reassessment_required": True}

        advisor = Advisor(self.llm, catalog, packs, sentinel=self.sentinel)
        res = advisor.recommend(p.client, request, now,
                                on_attempt=(lambda a: on_attempt(attempt_dict(a))) if on_attempt else None,
                                history=self._history(client_id))
        self.last_advice[client_id] = res
        out: dict = {"attempts": [attempt_dict(a) for a in res.attempts], "final": attempt_dict(res.final),
                     "first_pass_ok": res.first_pass_ok, "seconds": round(time.perf_counter() - t0, 1),
                     "model": self.llm.name, "blocked_requests": res.blocked_requests,
                     "fix": asdict(res.fix) if res.fix else None, "certificate": None, "integrity": integrity}
        requested = list(res.blocked_requests) + [pid for pid in (res.fix.removed if res.fix else {})]
        out["rationale"] = rationale(p.client, catalog, res.final.purchases)
        out["compare_default"] = default_candidates(p.client, catalog, res.final.purchases, requested)
        if res.final.purchases:
            proposal = Proposal(client_id=client_id, purchases=res.final.purchases, as_of=now)
            cert = issue(p.client, catalog, proposal, packs, Stage.PROPOSAL, self.signer, self.salt,
                         message=res.final.message)
            self.certs[cert["cert_id"]] = StoredCert(cert, client_id, "proposal")
            self._save_cert(cert["cert_id"])
            out["certificate"] = {"cert_id": cert["cert_id"], "digest": digest(cert),
                                  "required_procedure": required_procedure(p.client, catalog, res.final.purchases,
                                                                           now, packs),
                                  "rules": cert["result"]["rules"], "checked": cert["result"]["checked"],
                                  "scope": cert["scope"]}
        self.ledger.append("advice", {"client_ref": self._ref(client_id), "model": self.llm.name,
                                      "drafts": len(res.attempts), "first_pass_ok": res.first_pass_ok,
                                      "final_source": res.final.source,
                                      "cert_id": out["certificate"]["cert_id"] if out["certificate"] else None})
        if challenge:
            blocked = sum(1 for a in res.attempts if not a.ok)
            self.challenge["attempts"] += 1
            self.challenge["blocked_drafts"] += blocked
            self.challenge["breaks"] += 0 if res.final.ok else 1  # by construction always 0
            self._save_meta()
            out["challenge"] = dict(self.challenge)
        return out

    def compare_products(self, client_id: str, product_ids: list[str]) -> list[dict]:
        p = self.personas[client_id]
        catalog = {k: v for k, v in self.catalog.items() if v.market in p.markets}
        unknown = [pid for pid in product_ids if pid not in catalog]
        if unknown:
            raise ValueError(f"not available to this client: {unknown}")
        if not 1 <= len(product_ids) <= 4:
            raise ValueError("compare between 1 and 4 products")
        return compare(p.client, catalog, product_ids, self.packs_for(client_id), self.now())

    # ------------------------------------------------------------ confirmation and execution

    def confirm(self, client_id: str, cert_id: str, acknowledged: list[str], level_change_confirmed: bool = False,
                enhanced_warning_ack: bool = False) -> dict:
        stored = self.certs[cert_id]
        if stored.client_id != client_id or stored.stage != "proposal":
            raise ValueError("not a proposal certificate for this client")
        if stored.withdrawn:
            raise ValueError("this recommendation was withdrawn by the bank")
        p = self.personas[client_id]
        packs = self.packs_for(client_id)
        catalog = {k: v for k, v in self.catalog.items() if v.market in p.markets}
        purchases = stored.cert["purchases"]
        needs = required_procedure(p.client, catalog, purchases, self.now(), packs)
        procedure = Procedure(disclosures_shown={d for d in DISCLOSURE_ITEMS if d in acknowledged},
                              client_confirmed=True, level_change_confirmed=level_change_confirmed,
                              enhanced_warning="CN-18" in needs and enhanced_warning_ack,
                              consideration_hours=24 if "CN-18" in needs else 0,
                              follow_up_scheduled="CN-18" in needs)
        proposal = Proposal(client_id=client_id, purchases=purchases, as_of=self.now(), procedure=procedure)
        result = check(p.client, catalog, proposal, packs, Stage.EXECUTION)
        if not result.ok:
            return {"ok": False, "violations": [violation_dict(v) for v in result.violations]}
        cert = issue(p.client, catalog, proposal, packs, Stage.EXECUTION, self.signer, self.salt,
                     prev=digest(stored.cert))
        receipt = self.anchor.anchor_batch([digest(cert)])[0]
        self.certs[cert["cert_id"]] = StoredCert(cert, client_id, "execution", receipt)
        self._save_cert(cert["cert_id"])
        self.ledger.append("confirmed", {"client_ref": cert["client_ref"], "cert_id": cert["cert_id"],
                                         "digest": digest(cert), "tx_hash": receipt.tx_hash,
                                         "consideration_hours": procedure.consideration_hours})
        out = {"ok": True, "cert_id": cert["cert_id"], "digest": digest(cert), "receipt": receipt_dict(receipt),
               "executable_after": (self.now() + timedelta(hours=procedure.consideration_hours)).isoformat()}
        self._track(client_id, lambda f: f.get("proposal") == cert_id, proposal=cert_id,
                    executionCert=cert["cert_id"], receipt=out["receipt"], executableAfter=out["executable_after"])
        return out

    def execute(self, cert_id: str) -> dict:
        stored = self.certs[cert_id]
        if stored.receipt is None:
            raise ValueError("certificate is not anchored")
        if stored.withdrawn:
            return {"ok": False, "reasons": ["this recommendation was withdrawn by the bank"], "escalated": False,
                    "orders": [], "account": self.client_view(stored.client_id)["account"]}
        changed = self._holdings_conflict(stored) if cert_id not in self.gateway.executed else []
        if changed:
            self.open_case("execution_blocked", stored.client_id, "Order refused: the client's holdings changed",
                           "Since this advice was certified, the client bought other products. Together they "
                           "break the rules below, so new advice is needed.", changed, {"cert_id": cert_id})
            return {"ok": False, "reasons": changed, "escalated": True, "orders": [],
                    "account": self.client_view(stored.client_id)["account"]}
        rep = self.gateway.execute(stored.cert, stored.receipt, self.catalog, self.now())
        if rep.escalated and not any(c["refs"].get("cert_id") == cert_id and c["status"] == "open"
                                     for c in self.cases.values()):
            flagged = any("flagged by the regulator" in r for r in rep.reasons)
            self.open_case("regulator_flag" if flagged else "execution_blocked", stored.client_id,
                           "Order refused: flagged by the regulator" if flagged else "Order refused at execution",
                           "The execution gateway refused to place this order.", rep.reasons, {"cert_id": cert_id})
        self._sync_holdings(stored.client_id)
        self._save_broker()
        out = {"ok": rep.ok, "reasons": rep.reasons, "escalated": rep.escalated,
               "orders": [{**asdict(o), "settles_at": o.settles_at.isoformat() if o.settles_at else None}
                          for o in rep.orders],
               "account": self.client_view(stored.client_id)["account"]}
        self._track(stored.client_id, lambda f: f.get("executionCert") == cert_id,
                    exec={k: out[k] for k in ("ok", "reasons", "escalated", "orders")})
        return out

    # ------------------------------------------------------------ certificates

    def cert_status(self, cert_id: str, chain: bool = False) -> dict:
        s = self.certs[cert_id]
        flagged = bool(chain and s.receipt and any(self.anchor.flag_of(digest(s.cert))))
        return {"executed": cert_id in self.gateway.executed, "withdrawn": s.withdrawn, "flagged": flagged,
                "anchored": s.receipt is not None}

    def list_certs(self, include_synthetic: bool = False, limit: int = 50) -> list[dict]:
        rows = [(k, v) for k, v in self.certs.items() if include_synthetic or not v.synthetic]
        rows = rows[-limit:]
        return [{"cert_id": k, "client_id": v.client_id, "client_name": self.display_name(v.client_id),
                 "stage": v.stage, "purchases": v.cert["purchases"], "issued_at": v.cert["issued_at"],
                 "tx_hash": v.receipt.tx_hash if v.receipt else None, "synthetic": v.synthetic,
                 **self.cert_status(k, chain=v.stage == "execution" and not v.synthetic)} for k, v in rows]

    def inspect(self, cert_id: str) -> dict:
        """What the regulator's independent verifier sees for one certificate."""
        s = self.certs[cert_id]
        report = verify(s.cert, self.keys)
        client, products, proposal = _reconstruct(s.cert)
        packs = [PACKS[r["id"]] for r in s.cert["rule_packs"]]
        results = explain(client, products, proposal, packs, Stage(s.cert["stage"]))
        anchored = False
        if s.receipt:
            anchored = s.receipt.digest == digest(s.cert) and self.anchor.is_anchored(s.receipt)
        a = client.current_assessment
        return {
            "cert_id": cert_id, "client_name": self.display_name(s.client_id), "stage": s.stage,
            "issued_at": s.cert["issued_at"], "rule_packs": s.cert["rule_packs"],
            "purchases": s.cert["purchases"],
            "facts": {"age": client.age, "assessed_level": a.level if a else None, "level_cap": client.level_cap,
                      "investable_assets": client.investable_assets, "monthly_expenses": client.monthly_expenses,
                      "horizon_years": client.horizon_years},
            "signature_ok": report.signature_ok, "rules_match": report.rules_match, "recheck_ok": report.recheck_ok,
            "anchored": anchored, "receipt": asdict(s.receipt) if s.receipt else None,
            **self.cert_status(cert_id, chain=True),
            "constraints": [{"rule_id": r.rule_id, "title": r.title, "citation": r.citation, "kind": r.kind.value,
                             "message": r.message, "product_id": r.product_id, "passed": r.passed} for r in results],
            "client_ref": s.cert["client_ref"], "digest": digest(s.cert),
            "scope": s.cert.get("scope", SCOPE),
        }

    # ------------------------------------------------------------ regulator

    def seed_synthetic(self, n: int = 10_000, seed: int = 7) -> dict:
        """Demo data: generate ``n`` realistic compliant certificates and anchor them in one batch."""
        rng = random.Random(seed + len(self.certs))
        catalog = {k: v for k, v in self.catalog.items() if v.market == Market.CN}
        products = list(catalog.values())
        t0 = time.perf_counter()
        made = []
        base = sum(1 for c in self.certs.values() if c.synthetic)
        for i in range(n):
            level = rng.randint(1, 5)
            c = Client(id=f"synthetic-{seed}-{base + i}", age=rng.randint(22, 80),
                       investable_assets=rng.randrange(50_000, 3_000_000, 1_000),
                       monthly_expenses=rng.randrange(2_000, 20_000, 500), horizon_years=rng.randint(1, 8))
            c.assessments.append(Assessment(level=level, at=self.now() - timedelta(days=rng.randint(1, 300))))
            pool = [p for p in products if p.effective_risk <= level and not p.private and p.min_investment <= 10]
            picks = rng.sample(pool, k=min(len(pool), rng.randint(1, 3)))
            budget = (c.investable_assets - 6 * c.monthly_expenses) // 4
            purchases = {p.id: max(p.min_investment, (budget // len(picks)) // 100 * 100) for p in picks}
            proposal = Proposal(client_id=c.id, purchases=purchases, as_of=self.now(),
                                procedure=Procedure(disclosures_shown=set(DISCLOSURE_ITEMS), client_confirmed=True,
                                                    enhanced_warning=True, consideration_hours=24,
                                                    follow_up_scheduled=True, level_change_confirmed=True))
            packs = [CN_NFRA, HOUSE]
            if not check(c, catalog, proposal, packs, Stage.EXECUTION).ok:
                continue
            made.append((c.id, issue(c, catalog, proposal, packs, Stage.EXECUTION, self.signer, self.salt)))
        t_issue = time.perf_counter() - t0
        t1 = time.perf_counter()
        receipts = self.anchor.anchor_batch([digest(cert) for _, cert in made])
        t_anchor = time.perf_counter() - t1
        docs = []
        for (cid, cert), r in zip(made, receipts):
            sc = StoredCert(cert, cid, "execution", r, synthetic=True)
            self.certs[cert["cert_id"]] = sc
            docs.append((cert["cert_id"], sc.to_doc()))
        self.store.put_many("cert", docs)
        self.ledger.append("synthetic_batch", {"count": len(made), "tx_hash": receipts[0].tx_hash})
        return {"count": len(made), "issue_seconds": round(t_issue, 2), "anchor_seconds": round(t_anchor, 2),
                "tx_hash": receipts[0].tx_hash, "block": receipts[0].block_number}

    def verify_all(self, packs: list[RulePack] | None = None) -> dict:
        t0 = time.perf_counter()
        ok = sig_fail = recheck_fail = unanchored = 0
        failures = []
        roots: dict[str, bool] = {}  # each batch root is looked up on-chain once; proofs are checked locally
        for cid, s in self.certs.items():
            r = verify(s.cert, self.keys, packs)
            anchored = False
            if s.receipt is not None and s.receipt.digest == digest(s.cert):
                rc = s.receipt
                if rc.root not in roots:
                    try:
                        roots[rc.root] = self.anchor.root_on_chain(rc.root)
                    except Exception:
                        roots[rc.root] = False
                anchored = roots[rc.root] and verify_proof(rc.digest, [bytes.fromhex(p[2:]) for p in rc.proof],
                                                           bytes.fromhex(rc.root[2:]))
            if s.stage == "execution" and not anchored:
                unanchored += 1
                if len(failures) < 50:
                    failures.append({"cert_id": cid, "client_id": s.client_id,
                                     "problems": ["anchor not found on the current chain"], "violations": []})
            if r.ok:
                ok += 1
            else:
                sig_fail += not r.signature_ok
                recheck_fail += not r.recheck_ok
                if len(failures) < 50:
                    failures.append({"cert_id": cid, "client_id": s.client_id, "problems": r.problems,
                                     "violations": r.violations})
        return {"total": len(self.certs), "ok": ok, "signature_failures": sig_fail, "recheck_failures": recheck_fail,
                "unanchored": unanchored, "seconds": round(time.perf_counter() - t0, 2), "failures": failures}

    def tamper_demo(self, cert_id: str | None = None) -> dict:
        """Alter a copy of a stored certificate's amount and show that verification catches it."""
        sid = cert_id or next((k for k, v in reversed(self.certs.items()) if v.receipt), None)
        if sid is None:
            raise ValueError("no recorded certificates yet")
        s = self.certs[sid]
        bad = json.loads(json.dumps(s.cert))
        pid = next(iter(bad["purchases"]))
        bad["purchases"][pid] = bad["purchases"][pid] * 3
        r = verify(bad, self.keys)
        on_chain = False
        if s.receipt:
            fake = AnchorReceipt(**{**asdict(s.receipt), "digest": digest(bad)})
            on_chain = self.anchor.is_anchored(fake)
        return {"cert_id": sid, "client_name": self.display_name(s.client_id),
                "changed": {pid: [s.cert["purchases"][pid], bad["purchases"][pid]]},
                "signature_ok": r.signature_ok, "anchored_on_chain": on_chain, "problems": r.problems}

    def flag(self, cert_id: str, reason: str) -> dict:
        s = self.certs[cert_id]
        tx = self.anchor.flag(digest(s.cert), reason, self._regulator_address())
        self.ledger.append("regulator_flag", {"cert_id": cert_id, "reason": reason, "tx_hash": tx})
        if s.client_id in self.personas:
            if cert_id not in self.gateway.executed:
                self.notify(s.client_id, "A recommendation is on hold",
                            "One of your recommendations is being reviewed. No order will be placed until the review "
                            "is complete.")
            self.open_case("regulator_flag", s.client_id, "The regulator flagged a recommendation",
                           f"Flagged on-chain: {reason}. The order system will refuse it.",
                           [f"Transaction {tx}"], {"cert_id": cert_id})
        return {"tx_hash": tx, **self.cert_status(cert_id, chain=True)}

    def rule_templates(self) -> dict:
        return RULE_TEMPLATES

    def rule_change(self, template: str | None = None, params: dict | None = None, change: str | None = None) -> dict:
        if change:
            template, params = PRESETS[change]
        rule, text = build_rule(template or "", params or {})
        sim = RulePack("sim-rule", "sim", text, frozenset({Market.CN, Market.HK}), (rule,))
        affected = []
        t0 = time.perf_counter()
        for cid, s in self.certs.items():
            base = [PACKS[r["id"]] for r in s.cert["rule_packs"]]
            r = verify(s.cert, self.keys, base + [sim])
            if r.signature_ok and not r.recheck_ok:
                affected.append({"cert_id": cid, "client_id": s.client_id, "client_name": self.display_name(s.client_id),
                                 "real_client": s.client_id in self.personas,
                                 "products": [v.split("@")[1] for v in r.violations if "@" in v] or
                                             list(s.cert["purchases"])})
        self.ledger.append("rule_change_simulated", {"rule": text, "affected": len(affected)})
        affected.sort(key=lambda a: not a["real_client"])
        return {"rule": text, "citation": rule.citation, "checked": len(self.certs), "affected_count": len(affected),
                "affected_real_clients": sum(a["real_client"] for a in affected), "affected": affected[:60],
                "seconds": round(time.perf_counter() - t0, 2)}

    # ------------------------------------------------------------ monitoring

    def rerate(self, product_id: str, distributor_risk: int) -> dict:
        """NFRA Art. 22: on a re-rating, re-check holders and pending advice, and tell affected investors."""
        if not 1 <= distributor_risk <= 5:
            raise ValueError("rating must be between 1 and 5")
        old = self.catalog[product_id]
        self.catalog[product_id] = old.model_copy(update={"distributor_risk": distributor_risk})
        ratings = self.store.get("meta", "ratings") or {}
        ratings[product_id] = distributor_risk
        self.store.put("meta", "ratings", ratings)
        new = self.catalog[product_id]
        affected = []
        for cid, p in self.personas.items():
            acct = self.gateway.broker.accounts.get(self._ref(cid))
            holds = bool(acct and acct.positions.get(product_id))
            pending = [k for k, s in self.certs.items() if s.client_id == cid and product_id in s.cert["purchases"]
                       and s.stage == "execution" and k not in self.gateway.executed and not s.withdrawn]
            a = p.client.current_assessment
            level = min(a.level, p.client.level_cap) if a and p.client.level_cap else (a.level if a else 0)
            if (holds or pending) and new.effective_risk > level:
                what = "holds it" if holds else "has a pending order"
                affected.append({"client_id": cid, "name": p.name, "level": level, "holds": holds,
                                 "pending_certs": pending})
                self.notify(cid, f"{new.name} is now rated R{new.effective_risk}",
                            f"Its risk rating went up from R{old.effective_risk} to R{new.effective_risk}, which is "
                            f"above your risk level (C{level}). "
                            + ("You can keep holding it; new purchases are paused." if holds else
                               "Your pending order can't go ahead unless the rating comes back down."), kind="warning")
                self.open_case("rerating", cid, f"{new.name} re-rated above the client's level",
                               f"R{old.effective_risk} → R{new.effective_risk}; the client (C{level}) {what}. "
                               "The client has been told automatically (NFRA Art. 22).", [],
                               {"product_id": product_id})
        self.ledger.append("product_rerated", {"product_id": product_id, "from": old.effective_risk,
                                               "to": new.effective_risk, "affected": len(affected)})
        return {"product_id": product_id, "from": old.effective_risk, "to": new.effective_risk, "affected": affected}
