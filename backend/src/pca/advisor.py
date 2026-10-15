"""The advisor loop: the LLM proposes, the verifier decides.

1. The LLM drafts a structured proposal (purchases + explanation).
2. The verifier checks the allocation; the speech layer checks the explanation.
3. Failures go back to the LLM as feedback, up to ``max_retries`` times.
4. If it still fails, the solver's minimal legal fix replaces the allocation and the
   explanation is regenerated (or replaced by a deterministic template).
5. Only a passing proposal is certified and shown to the client.

Every attempt is recorded so the UI can show what was blocked and why, and so the
evaluation can measure how often the raw model would have mis-sold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Sequence

from .llm import LLM, parse_json
from .models import Client, Product, Proposal
from .rules import RulePack, Stage
from .speech import SpeechIssue, check_message
from .verifier import Fix, Violation, check, max_amount, minimal_fix

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "purchases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}, "amount": {"type": "integer"}},
                "required": ["product_id", "amount"],
            },
        },
        "message": {"type": "string"},
    },
    "required": ["purchases", "message"],
}

MESSAGE_SCHEMA = {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}

SYSTEM = """You are a licensed wealth advisor at a mainland China bank. You recommend products \
from the catalogue only, as whole-RMB amounts. Follow suitability rules: never recommend a \
product whose risk level is above the client's risk tolerance, never promise guaranteed \
returns or principal protection, never describe a wealth product or fund as a deposit, \
quote performance only from the catalogue data, and always say that principal can be lost \
when recommending anything above R1. If the client names an amount, the plan's total must not \
exceed it. Write amounts as ¥ (for example ¥200,000); R1-R5 are risk levels, not currency. \
When the client wants to invest, build a sensible plan like a good human advisor: usually 2-4 \
suitable products that use the client's risk level (not only the safest product), keep no more \
than 30% of their wealth in any one product above R1, and match lock-up periods to their horizon. \
Amounts in whole thousands. Write the message in the client's language (English or Chinese; \
Traditional characters if the client uses them). Refer to products by name: the English name in \
English replies, name_zh in Chinese replies, never by ID and never mixing two products' names, and state each product's risk level exactly as in the catalogue. Clients often write \
informally, with typos, pinyin, slang or long run-on sentences: work out what they mean. \
under 120 words. Reply with JSON only."""


@dataclass
class Attempt:
    source: str  # "llm" | "llm-retry" | "solver-fix"
    purchases: dict[str, int]
    message: str
    violations: list[Violation]
    speech: list[SpeechIssue]
    error: str | None = None
    sent_back: str | None = None  # passed the rules but wasn't useful advice, so the model was asked again

    @property
    def ok(self) -> bool:
        return not self.violations and not self.speech and self.error is None


@dataclass
class AdvisorResult:
    final: Attempt
    attempts: list[Attempt] = field(default_factory=list)
    fix: Fix | None = None
    blocked_requests: dict[str, int] = field(default_factory=dict)  # product -> max allowed amount

    @property
    def first_pass_ok(self) -> bool:
        return bool(self.attempts) and self.attempts[0].ok


# ---------------------------------------------------------------- prompting

def facts_card(client: Client) -> dict:
    a = client.current_assessment
    level = min(a.level, client.level_cap) if a and client.level_cap else (a.level if a else None)
    return {
        "age": client.age,
        "risk_tolerance": f"C{level}" if level else "not assessed",
        "investable_assets_rmb": client.investable_assets,
        "monthly_expenses_rmb": client.monthly_expenses,
        "horizon_years": client.horizon_years,
        "existing_holdings": client.holdings,
    }


def catalogue_table(catalog: dict[str, Product]) -> list[dict]:
    return [{
        "id": p.id, "name": p.name, "name_zh": p.name_zh, "type": p.type.value, "market": p.market.value,
        "risk": f"R{p.effective_risk}", "min_investment": p.min_investment,
        "liquidity_days": p.liquidity_days, "annual_fee_pct": p.annual_fee_bps / 100,
        "returns_pct": p.returns_pct, "max_drawdown_pct": p.max_drawdown_pct,
    } for p in catalog.values()]


_TRAD = set("們這個為與說過還時會從後來點樣麼將萬錢買賣險風產開關門長東車馬鳥魚錯對處問題無經濟養歲幾嗎喺唔啲嘅")
_SIMP = set("们这个为与说过还时会从后来点样么将万钱买卖险风产开关门长东车马鸟鱼错对处问题无经济养岁几吗")


def language(text: str) -> str:
    """'zh' if the client writes Chinese (Simplified or Traditional), else 'en'."""
    return "zh" if len(re.findall(r"[\u4e00-\u9fff]", text)) >= 2 else "en"


def reply_language(request: str) -> str:
    """Spelled out in the prompt: the model doesn't always follow 'reply in the client's language'."""
    if language(request) != "zh":
        return "English"
    trad = sum(ch in _TRAD for ch in request)
    return "Chinese, Traditional characters" if trad > sum(ch in _SIMP for ch in request) else "Chinese, Simplified characters"


HISTORY_TURNS = 6


def history_block(history: Sequence[dict], catalog: dict[str, Product]) -> str:
    """The conversation as the client saw it: their messages and the *delivered* (verified) replies.

    Blocked drafts are never included, so the model can't build on advice that failed the checks.
    """
    if not history:
        return ""
    lines = []
    for turn in list(history)[-HISTORY_TURNS:]:
        lines.append(f"Client: {turn['client']}")
        plan = turn.get("plan") or {}
        plan_txt = "; ".join(f"{catalog[p].name if p in catalog else p} ¥{a:,}" for p, a in plan.items())
        lines.append(f"Advisor: {turn['advisor']}" + (f" [Verified plan: {plan_txt}]" if plan_txt else ""))
    return ("Conversation so far (exactly what the client saw):\n" + "\n".join(lines) + "\n\n"
            "Read the new request in the context of this conversation: a follow-up like 'make it safer' refers "
            "to the last verified plan.\n\n")


def _proposal_prompt(client: Client, catalog: dict[str, Product], request: str,
                     history: Sequence[dict] = ()) -> list[dict[str, str]]:
    user = (f"Client facts (confirmed by the client):\n{json.dumps(facts_card(client), ensure_ascii=False)}\n\n"
            f"Catalogue (fictional demo products):\n{json.dumps(catalogue_table(catalog), ensure_ascii=False)}\n\n"
            f"{history_block(history, catalog)}"
            f"Client request:\n{request}\n\n"
            f"Write the message in {reply_language(request)}.\n"
            'Reply as JSON: {"purchases": [{"product_id": ..., "amount": ...}], "message": ...}')
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


_FOLLOW_UP = re.compile(r"\b(?:safer|less risky|lower risk|riskier|more risk|smaller|bigger|instead|adjust|switch|"
                        r"swap|replace|remove|increase|reduce|rebalance|make it|that plan|this plan|the plan)\b|"
                        r"换|调整|少一点|多一点|更安全|降低|增加|减少", re.I)


def follow_up_base(request: str, history: Sequence[dict]) -> dict[str, int]:
    """If the request modifies the last delivered plan, that plan is the solver's starting point."""
    last = next((t.get("plan") for t in reversed(list(history)) if t.get("plan")), None)
    return dict(last) if last and _FOLLOW_UP.search(request) else {}


def _feedback(att: Attempt) -> str:
    lines = [f"- {v.rule_id} ({v.citation}): {v.message}" + (f" [{v.product_id}]" if v.product_id else "")
             for v in att.violations]
    lines += [f"- {s.rule_id} ({s.citation}): {s.message}" + (f' "{s.excerpt}"' if s.excerpt else "")
              for s in att.speech]
    if att.error:
        lines.append(f"- FORMAT: {att.error}")
    return ("Your proposal was blocked by the compliance verifier:\n" + "\n".join(lines) +
            "\nRevise it so that it passes. Reply with JSON only.")


# ---------------------------------------------------------------- evaluation of one attempt

def _level(client: Client) -> int:
    a = client.current_assessment
    level = a.level if a else 0
    return min(level, client.level_cap) if client.level_cap else level


def _evaluate(source: str, raw: str, client: Client, catalog: dict[str, Product], as_of: datetime,
              packs: Sequence[RulePack], sentinel=None) -> Attempt:
    """``sentinel`` (optional) adds model-based speech findings for LLM-written text."""
    try:
        data = parse_json(raw)
        purchases: dict[str, int] = {}
        for item in data.get("purchases", []):
            pid, amount = str(item["product_id"]), int(item["amount"])
            if amount > 0:
                purchases[pid] = purchases.get(pid, 0) + amount
        message = str(data.get("message", ""))
    except (ValueError, KeyError, TypeError) as e:
        return Attempt(source, {}, "", [], [], error=f"unparseable output: {e}")
    unknown = sorted(set(purchases) - set(catalog))
    if unknown:
        return Attempt(source, purchases, message, [], [], error=f"unknown product ids {unknown}")
    level = _level(client)
    result = check(client, catalog, Proposal(client_id=client.id, purchases=purchases, as_of=as_of), packs)
    speech = check_message(message, catalog, set(purchases), level, purchases)
    if sentinel is not None and message:
        seen = {x.rule_id for x in speech}
        speech += [x for x in sentinel.claims(message) if x.rule_id not in seen]
    return Attempt(source, purchases, message, result.violations, speech)


# ---------------------------------------------------------------- client intent

_AMOUNT = re.compile(r"(?:¥|rmb\s*|cny\s*)?(\d+(?:[.,]\d+)?)\s*(k|wan|w|万|m|million|thousand)?(?![a-z])", re.I)
_UNITS = {"k": 1_000, "thousand": 1_000, "w": 10_000, "wan": 10_000, "万": 10_000, "m": 1_000_000, "million": 1_000_000}


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1_000}
_CN_BIG = {"万": 10_000, "亿": 100_000_000}
_CN_AMOUNT = re.compile(r"[0-9.零〇一二两三四五六七八九十百千万亿来多余]*[十百千万亿][0-9.零〇一二两三四五六七八九十百千万亿来多余]*")


def cn_number(text: str) -> float | None:
    """Chinese amounts: 三百万, 一百五十万, 二十来万, 1.5百万, 一千二百万, 两亿."""
    text = re.sub("[来多余]", "", text)
    total = section = 0.0
    number: float | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isdigit() or ch == ".":
            j = i
            while j < len(text) and (text[j].isdigit() or text[j] == "."):
                j += 1
            try:
                number = float(text[i:j])
            except ValueError:
                return None
            i = j
            continue
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (1 if number is None else number) * _CN_UNITS[ch]
            number = None
        elif ch in _CN_BIG:
            section += number or 0
            total += (section or 1) * _CN_BIG[ch]
            section, number = 0.0, None
        i += 1
    value = total + section + (number or 0)
    return value or None


_PINYIN = {"ling": "零", "yi": "一", "er": "二", "liang": "两", "san": "三", "si": "四", "wu": "五", "liu": "六",
           "qi": "七", "ba": "八", "jiu": "九", "shi": "十", "bai": "百", "qian": "千", "wan": "万"}
_PINYIN_RUN = re.compile(r"\b(?:(?:ling|yi|er|liang|san|si|wu|liu|qi|ba|jiu|shi|bai|qian)\s*)+wan\b", re.I)


def _pinyin_numbers(text: str) -> str:
    """'er shi wan' -> '二十万', so amounts typed in pinyin are understood too."""
    return _PINYIN_RUN.sub(lambda m: "".join(_PINYIN[w.lower()] for w in m.group(0).split()), text)


def requested_amount(request: str) -> int | None:
    best = None
    request = _pinyin_numbers(request)
    for token in _CN_AMOUNT.findall(request):
        value = cn_number(token)
        if value and value >= 1_000 and (best is None or value > best):
            best = int(value)
    for num, unit in _AMOUNT.findall(request):
        value = float(num.replace(",", "")) * _UNITS.get(unit.lower(), 1) if unit else float(num.replace(",", ""))
        if value >= 1_000 and (best is None or value > best):
            best = int(value)
    return best


def intent_from_request(request: str, catalog: dict[str, Product], client: Client) -> dict[str, int]:
    """What the client literally asked for: named products and amount.

    Used as the starting point for the minimal legal fix when the model refuses
    without proposing anything, so the client gets the closest compliant
    alternative rather than a bare refusal.
    """
    text = request.lower()
    named = [p for p in catalog.values()
             if any(n.lower() in text for n in (p.id, p.name, p.name_zh, *p.aliases) if n)]
    amount = requested_amount(request) or client.investable_assets // 5
    amount = min(amount, client.investable_assets)
    if not named:
        return {}
    each = amount // len(named)
    return {p.id: each for p in named}


# ---------------------------------------------------------------- deterministic fallback text

RISK_WORDS = {1: "very low risk", 2: "low risk", 3: "medium risk", 4: "medium-high risk", 5: "high risk"}
LEVEL_WORDS = {1: "conservative", 2: "cautious", 3: "balanced", 4: "growth", 5: "aggressive"}


def _access(p: Product) -> str:
    days = 0 if p.type.value == "deposit" else p.liquidity_days
    if p.type.value == "deposit" and p.liquidity_days > 1:
        return "early withdrawal earns only the demand rate"
    return "money back the same day" if days <= 1 else f"money back in about {days} days"


RISK_WORDS_ZH = {1: "极低风险", 2: "低风险", 3: "中风险", 4: "中高风险", 5: "高风险"}
LEVEL_WORDS_ZH = {1: "保守型", 2: "谨慎型", 3: "稳健型", 4: "积极型", 5: "激进型"}


def _access_zh(p: Product) -> str:
    if p.type.value == "deposit" and p.liquidity_days > 1:
        return "提前支取按活期利率计息"
    days = 0 if p.type.value == "deposit" else p.liquidity_days
    return "当天可取回" if days <= 1 else f"约{days}天可取回"


def _template_zh(purchases: dict[str, int], catalog: dict[str, Product], level: int | None,
                 blocked: dict[str, str] | None) -> str:
    fit = f"符合您的风险等级（C{level}，{LEVEL_WORDS_ZH[level]}）" if level else ""
    name = lambda p: p.name_zh or p.name
    left_out = []
    for pid in blocked or {}:
        p = catalog.get(pid)
        if p is None:
            continue
        left_out.append(f"{name(p)}为R{p.effective_risk}，高于您的风险等级，因此未纳入" if level and p.effective_risk > level
                        else f"{name(p)}不符合您情况下的相关规定，因此未纳入")
    note = ("。".join(left_out) + "。") if left_out else ""
    if not purchases:
        return f"目前暂时无法为您推荐{fit}的产品。{note}我们可以先一起梳理您的目标和风险评估，再为您挑选合适的选择。"
    lines = [f"{name(catalog[pid])} ¥{amt:,}（{RISK_WORDS_ZH[catalog[pid].effective_risk]}，R{catalog[pid].effective_risk}，"
             f"年费率{catalog[pid].annual_fee_bps / 100:.2f}%，{_access_zh(catalog[pid])}）" for pid, amt in purchases.items()]
    deposits_only = all(catalog[p].type.value == "deposit" for p in purchases)
    risk = ("存款到期续存时利率可能调整。" if deposits_only else
            "理财产品和基金不是存款：收益不作保证，本金可能亏损。")
    return (f"为您配置一个{fit}的方案，合计¥{sum(purchases.values()):,}。" + "。".join(lines) + "。" + note + risk
            + "您确认之前不会进行任何购买，最终由您自己决定。")


def template_message(purchases: dict[str, int], catalog: dict[str, Product], level: int | None = None,
                     blocked: dict[str, str] | None = None, lang: str = "en") -> str:
    """Deterministic advice text used whenever the model's wording can't be shown. Always passes the checks."""
    if lang == "zh":
        return _template_zh(purchases, catalog, level, blocked)
    fit = f" for your risk level (C{level}, {LEVEL_WORDS[level]})" if level else ""
    left_out = []
    for pid in blocked or {}:
        p = catalog.get(pid)
        if p is None:
            continue
        if level and p.effective_risk > level:
            left_out.append(f"{p.name} is rated R{p.effective_risk}, above your level, so I can't include it")
        else:
            left_out.append(f"{p.name} doesn't meet the rules for your situation, so I can't include it")
    note = (" " + ". ".join(left_out) + ".") if left_out else ""
    if not purchases:
        return (f"I can't recommend a product{fit} right now.{note} Let's review your goals and risk "
                "assessment together, and I'll suggest options that fit.")
    lines = [f"¥{amt:,} in {catalog[pid].name}: {RISK_WORDS[catalog[pid].effective_risk]} "
             f"(R{catalog[pid].effective_risk}), annual fee {catalog[pid].annual_fee_bps / 100:.2f}%, "
             f"{_access(catalog[pid])}" for pid, amt in purchases.items()]
    total = sum(purchases.values())
    deposits_only = all(catalog[p].type.value == "deposit" for p in purchases)
    risk = (" Deposit rates can change when the deposit renews." if deposits_only else
            " Wealth products and funds are not deposits: returns are not guaranteed and you could lose part "
            "of your principal.")
    return (f"Here is a plan{fit}, ¥{total:,} in total. " + "; ".join(lines) + "." + note + risk
            + " Nothing is bought until you review and confirm it. The final decision is yours.")


# ---------------------------------------------------------------- main loop

class Advisor:
    def __init__(self, llm: LLM, catalog: dict[str, Product], packs: Sequence[RulePack], max_retries: int = 2,
                 sentinel=None):
        self.llm, self.catalog, self.packs, self.max_retries = llm, catalog, list(packs), max_retries
        self.sentinel = sentinel

    def recommend(self, client: Client, request: str, as_of: datetime,
                  on_attempt: Callable[[Attempt], None] | None = None,
                  history: Sequence[dict] = ()) -> AdvisorResult:
        """``on_attempt`` is called after each draft is judged, for live streaming to the UI.

        ``history`` holds earlier turns as delivered to the client: {"client", "advisor", "plan"}.
        """
        notify = on_attempt or (lambda _a: None)
        messages = _proposal_prompt(client, self.catalog, request, history)
        attempts: list[Attempt] = []
        intent = intent_from_request(request, self.catalog, client) or follow_up_base(request, history)
        asked = requested_amount(request)
        wants_to_invest = bool(intent) or asked is not None
        if asked is not None and asked > client.investable_assets:
            asked = None  # can't be met anyway; the rules decide how much is possible
        nudged = False
        asked_language = False

        def over(a: Attempt) -> bool:
            return asked is not None and sum(a.purchases.values()) > asked * 1.02  # rounding slack only

        # What the client literally asked for (named products, amount), if that alone is compliant for them.
        named = intent_from_request(request, self.catalog, client)
        named_ok = bool(named) and check(client, self.catalog, Proposal(client_id=client.id, purchases=named,
                                                                        as_of=as_of), self.packs).ok
        asked_named = False

        def ignores_request(a: Attempt) -> bool:
            return named_ok and not asked_named and not all(a.purchases.get(p) for p in named)
        for i in range(self.max_retries + 1):
            raw = self.llm.chat(messages, schema=PROPOSAL_SCHEMA)
            att = _evaluate("llm" if i == 0 else "llm-retry", raw, client, self.catalog, as_of, self.packs,
                            self.sentinel)
            attempts.append(att)
            wrong_language = (language(request) == "zh" and language(att.message) != "zh" and not asked_language
                              and i < self.max_retries)
            skipped = ignores_request(att) and i < self.max_retries
            if (att.ok and (att.purchases or not wants_to_invest) and not over(att) and not wrong_language
                    and not skipped):
                notify(att)
                return AdvisorResult(att, attempts, blocked_requests=self._blocked(client, attempts, as_of))
            if att.ok and over(att):
                # Compliant, but invests more than the client asked for: that's not what they wanted.
                att.sent_back = (f"It invests ¥{sum(att.purchases.values()):,}, but the client asked for "
                                 f"¥{asked:,}.")
                feedback = (f"The client asked to invest ¥{asked:,}, but your plan totals "
                            f"¥{sum(att.purchases.values()):,}. Keep the total at or below ¥{asked:,}. "
                            "Reply with JSON only.")
                stop = i == self.max_retries
            elif att.ok and skipped:
                names = ", ".join(f"¥{amt:,} in {self.catalog[p].name}" for p, amt in named.items())
                att.sent_back = f"The client asked for {names}, which is compliant for them, but the plan leaves it out."
                asked_named = True
                feedback = (f"The client explicitly asked for {names}. That is compliant for this client, so include "
                            "it as asked unless a rule prevents it. Reply with JSON only.")
                stop = False
            elif att.ok and wrong_language:
                att.sent_back = "The client wrote in Chinese, but the reply is in English."
                asked_language = True
                feedback = (f"The client wrote in Chinese. Keep the same plan and rewrite the message in "
                            f"{reply_language(request)}. Reply with JSON only.")
                stop = False
            elif att.ok:
                # A compliant but empty refusal of a request to invest isn't useful advice. Give the model
                # the computed room once; if it still refuses, the solver offers the closest legal plan.
                att.sent_back = "The client asked to invest, and a compliant plan exists for them."
                stop = nudged or i == self.max_retries
                nudged = True
                feedback = self._capacity_note(client, request)
            else:
                feedback, stop = _feedback(att), False
            notify(att)
            if stop:
                break
            messages = messages + [{"role": "assistant", "content": raw}, {"role": "user", "content": feedback}]

        # The model couldn't produce a useful compliant proposal: the solver takes over, starting from the
        # model's last allocation, else what the client named, else a sensible default for their amount.
        base = ((named if named_ok else {})  # the client's own compliant request comes first
                or next((a.purchases for a in attempts if a.error is None and a.purchases), {})
                or intent or self._default_plan(client, request, as_of))
        if asked is not None and sum(base.values()) > asked:  # scale down to what the client asked for
            total = sum(base.values())
            base = {p: a * asked // total for p, a in base.items()}
        fix = minimal_fix(client, self.catalog, Proposal(client_id=client.id, purchases=base, as_of=as_of), self.packs)
        purchases = fix.purchases if fix else {}
        blocked = {v.product_id: v.message for a in attempts for v in a.violations if v.product_id}
        for pid in intent_from_request(request, self.catalog, client):
            if pid not in purchases and pid not in blocked:
                why = check(client, self.catalog, Proposal(client_id=client.id, purchases={pid: 1}, as_of=as_of),
                            self.packs).violations
                blocked[pid] = why[0].message if why else "not part of the compliant allocation"
        message = self._explain(client, purchases, request, blocked)
        final = _evaluate("solver-fix", json.dumps({"purchases": [{"product_id": p, "amount": a}
                                                                  for p, a in purchases.items()],
                                                    "message": message}),
                          client, self.catalog, as_of, self.packs, self.sentinel)
        if not final.ok:  # never show unverified text; fall back to the template
            final = _evaluate("solver-fix", json.dumps({"purchases": [{"product_id": p, "amount": a}
                                                                      for p, a in purchases.items()],
                                                        "message": template_message(purchases, self.catalog, _level(client),
                                                                                   blocked, language(request))}),
                              client, self.catalog, as_of, self.packs)
        assert final.ok, "solver fix and template must always verify"
        attempts.append(final)
        notify(final)
        return AdvisorResult(final, attempts, fix, self._blocked(client, attempts, as_of))

    def _suitable(self, client: Client) -> list[Product]:
        level = _level(client)
        return [p for p in self.catalog.values() if p.effective_risk <= level and not p.private and not p.complex
                and p.liquidity_days <= max(client.horizon_years, 1) * 365]

    def _capacity_note(self, client: Client, request: str) -> str:
        level = _level(client)
        reserve = 6 * client.monthly_expenses
        room = max(client.investable_assets - reserve, 0)
        names = ", ".join(f"{p.name} (R{p.effective_risk})" for p in self._suitable(client))
        return (f"You refused, but the client does want to invest. Their risk level is C{level}, so products up to "
                f"R{level} are allowed. They have ¥{client.investable_assets:,} investable; keeping ¥{reserve:,} as "
                f"an emergency cushion leaves about ¥{room:,} for non-cash products (cash-like products don't count "
                f"against the cushion). Suitable products: {names}. Propose a compliant allocation for what they "
                "can afford, explain any part of the request you can't meet, never promise returns, and say that "
                "principal can be lost. Reply with JSON only.")

    def _default_plan(self, client: Client, request: str, as_of: datetime) -> dict[str, int]:
        """A sensible starting allocation when the client named no product: the closest-risk suitable
        products plus a cash-like one, for the amount they can afford. The solver then makes it compliant."""
        amount = min(requested_amount(request) or client.investable_assets // 2, client.investable_assets)
        suitable = [p for p in self._suitable(client) if p.min_investment <= amount // 3]
        if not suitable or amount <= 0:
            return {}
        growth = sorted((p for p in suitable if not p.is_cash_like),
                        key=lambda p: (-p.effective_risk, p.annual_fee_bps))[:2]
        cash = sorted((p for p in suitable if p.is_cash_like), key=lambda p: p.annual_fee_bps)[:1]
        picks = growth + cash or suitable[:1]
        each = amount // len(picks)
        return {p.id: each for p in picks}

    def _explain(self, client: Client, purchases: dict[str, int], request: str,
                 blocked: dict[str, str] | None = None) -> str:
        level = _level(client)
        why_not = [{"product": self.catalog[p].name, "risk": f"R{self.catalog[p].effective_risk}", "reason": r}
                   for p, r in (blocked or {}).items() if p in self.catalog]
        prompt = [{"role": "system", "content": SYSTEM},
                  {"role": "user", "content":
                      f"The client asked: {request}\nThe compliant allocation is fixed as: "
                      f"{json.dumps(catalogue_table({p: self.catalog[p] for p in purchases}), ensure_ascii=False)} "
                      f"with amounts {json.dumps(purchases)}. Client risk tolerance is C{level}.\n"
                      f"Products the client asked for that are in our catalogue but were blocked, with the exact "
                      f"reason: {json.dumps(why_not, ensure_ascii=False)}\n"
                      "Explain the allocation to the client and, if relevant, why their original request could not "
                      "be met, using only the reasons given. Do not mention other products. "
                      f"Write the message in {reply_language(request)}. "
                      'Reply as JSON: {"message": ...}'}]
        try:
            return str(parse_json(self.llm.chat(prompt, schema=MESSAGE_SCHEMA)).get("message", ""))
        except Exception:
            return template_message(purchases, self.catalog, level, blocked, language(request))

    def _blocked(self, client: Client, attempts: list[Attempt], as_of: datetime) -> dict[str, int]:
        """For products the model tried and the verifier blocked: how much would be allowed?"""
        out: dict[str, int] = {}
        final = attempts[-1].purchases
        for att in attempts:
            for v in att.violations:
                if v.product_id and v.product_id not in out and v.product_id in self.catalog:
                    p = Proposal(client_id=client.id, purchases={k: a for k, a in final.items() if k != v.product_id},
                                 as_of=as_of)
                    out[v.product_id] = max_amount(client, self.catalog, p, self.packs, v.product_id)
        return out
