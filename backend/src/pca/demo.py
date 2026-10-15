"""Deterministic stand-in for the LLM, for offline demos and API tests.

It behaves like an over-eager advisor: the first draft follows the client's wishes
(including unsuitable products or guarantee talk), and after verifier feedback it
produces a compliant allocation. It is labelled "demo-scripted" in the UI.
"""

from __future__ import annotations

import json
import re


def _extract(prompt: str, label: str):
    m = re.search(label + r"[^\n]*\n(.+?)\n\n", prompt, flags=re.S)
    return json.loads(m.group(1)) if m else None


class DemoLLM:
    name = "demo-scripted"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, schema=None, temperature=0.3) -> str:
        self.calls += 1
        first_user = next(m["content"] for m in messages if m["role"] == "user")
        facts = _extract(first_user, "Client facts") or {}
        catalog = _extract(first_user, "Catalogue") or []
        level = int(str(facts.get("risk_tolerance", "C1"))[1:] or 1)
        assets = int(facts.get("investable_assets_rmb", 100_000))

        if schema and "purchases" not in json.dumps(schema):  # explanation-only request
            return json.dumps({"message": self._explain(first_user)})

        request = first_user.split("Client request:\n", 1)[-1].split("\n\nReply", 1)[0].lower()
        from .advisor import requested_amount

        asked = requested_amount(request)
        asked = asked if asked and asked <= assets else None
        if len(messages) <= 2:  # first draft: over-eager; after any feedback: compliant
            return json.dumps(self._eager(request, catalog, assets, asked))
        last = messages[-1]["content"]
        rules = re.findall(r"^- ([A-Z]+-[A-Z0-9-]+) ", last, flags=re.M)
        if rules and all(r.startswith("SP-") for r in rules):  # only the wording was wrong: keep the plan
            prev = json.loads(messages[-2]["content"])
            names = [next((p["name"] for p in catalog if p["id"] == x["product_id"]), x["product_id"])
                     for x in prev["purchases"]]
            prev["message"] = (f"I suggest {' and '.join(names)}. This is not a deposit: returns are not "
                               "guaranteed and you could lose part of your principal. Take your time to decide.")
            return json.dumps(prev)
        out = self._compliant(catalog, level, assets, facts, asked)
        picked = {p["product_id"] for p in out["purchases"]}
        for p in catalog:  # say why a product the client named was left out
            named = p["name"].lower() in request or p["id"].lower() in request
            if named and p["id"] not in picked and int(p["risk"][1:]) > level:
                out["message"] += (f" I can't recommend {p['name']}: it is rated {p['risk']}, above your "
                                   f"risk tolerance of C{level}.")
        return json.dumps(out)

    @staticmethod
    def _explain(prompt: str) -> str:
        """Explain a solver plan the way a careful advisor would, from the facts in the prompt."""
        try:
            table = json.loads(re.search(r"fixed as: (\[.*?\]) with amounts", prompt, re.S).group(1))
            amounts = json.loads(re.search(r"with amounts (\{.*?\})", prompt, re.S).group(1))
            blocked = json.loads(re.search(r"exact reason: (\[.*?\])\n", prompt, re.S).group(1))
        except (AttributeError, ValueError):
            table, amounts, blocked = [], {}, []
        names = {p["id"]: p["name"] for p in table}
        parts = [f"¥{a:,} in {names.get(pid, pid)}" for pid, a in amounts.items()]
        msg = ("I've put together a plan that stays within your risk level: " + ", ".join(parts) + "."
               if parts else "I can't suggest a purchase that fits your risk level right now.")
        if blocked:
            msg += " " + " ".join(f"{b['product']} ({b['risk']}) isn't included: {b['reason'].rstrip('.')}."
                                  for b in blocked)
        return msg + (" Wealth products and funds are not deposits: returns are not guaranteed and you could lose "
                      "part of your principal. Take your time, and nothing is bought until you confirm.")

    @staticmethod
    def _eager(request: str, catalog: list[dict], assets: int, asked: int | None = None) -> dict:
        named = [p for p in catalog if p["name"].lower() in request or p["id"].lower() in request]
        wants_risk = any(w in request for w in ("tech", "star", "growth", "aggressive", "fast", "double"))
        if named:
            pick = max(named, key=lambda p: p["risk"])
        elif wants_risk:
            pick = max(catalog, key=lambda p: (p["risk"], p["returns_pct"].get("2025", 0)))
        else:
            pick = max(catalog, key=lambda p: p["returns_pct"].get("2025", 0))
        amount = (asked or min(assets // 2, 500_000)) // 1000 * 1000
        guarantee = any(w in request for w in ("guarantee", "safe", "can't lose", "cannot lose", "保本"))
        msg = (f"Great choice! {pick['name']} is a strong performer"
               + (" and your returns are basically guaranteed." if guarantee else "."))
        return {"purchases": [{"product_id": pick["id"], "amount": amount}], "message": msg}

    @staticmethod
    def _compliant(catalog: list[dict], level: int, assets: int, facts: dict, asked: int | None = None) -> dict:
        reserve = 6 * int(facts.get("monthly_expenses_rmb", 0))
        ok = [p for p in catalog if int(p["risk"][1:]) <= min(level, 3) and p["min_investment"] <= 1000
              and p["liquidity_days"] <= 365 * int(facts.get("horizon_years", 1))]
        ok.sort(key=lambda p: (-int(p["risk"][1:]), p["annual_fee_pct"]))
        picks = ok[:3] or [p for p in catalog if p["risk"] == "R1"][:1]
        budget = max(0, assets - reserve)
        if asked:
            budget = min(budget, asked)
        each = min(budget // max(len(picks), 1), int(assets * 0.3)) // 1000 * 1000
        names = [p["name"] for p in picks]
        names = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)
        return {"purchases": [{"product_id": p["id"], "amount": each} for p in picks],
                "message": f"I suggest {names}, which fit your risk tolerance. Wealth products and funds are "
                           "not deposits: returns are not guaranteed and you could lose part of your principal."}
