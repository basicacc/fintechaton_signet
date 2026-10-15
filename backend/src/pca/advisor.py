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
from .verifier import LOT, Fix, Violation, check, max_amount, minimal_fix

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

KINDS = ("new", "change", "alternative", "question", "other")


def proposal_schema(catalog: dict[str, Product]) -> dict:
    """The proposal, preceded by what the model understood the client to want. ``understood`` comes first, so
    the model commits to its reading of the request before it writes the plan; product ids are an enum, so a
    client's spelling ("steady income 90 day", 稳健90天) never has to be matched as text."""
    return {
        "type": "object",
        "properties": {
            "understood": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "amount": {"type": ["integer", "null"]},
                    "products": {"type": "array", "items": {"type": "string", "enum": sorted(catalog)}},
                    "only": {"type": "boolean"},
                    "exclude": {"type": "array", "items": {"type": "string", "enum": sorted(catalog)}},
                },
                "required": ["kind", "amount", "products", "only", "exclude"],
            },
            **PROPOSAL_SCHEMA["properties"],
        },
        "required": ["understood", *PROPOSAL_SCHEMA["required"]],
    }


UNDERSTAND = """First fill "understood", reading the new message in the context of the conversation: \
kind: "new" (wants a plan), "change" (wants the current plan adjusted: safer, smaller, only some products, \
and so on), "alternative" (wants something different from the current plan), "question" (asks something; \
no new plan needed) or "other". amount: the RMB they want to invest, from this message or, if they don't \
say, from earlier in the conversation; null if they never said. products: the catalogue ids of products \
the client asks for by any name or spelling (empty if none). only: true if they want just those products. \
exclude: the ids of products the client says they don't want (drop, remove, no more, 不要). "It", \
"that one", 这个 or 那个 mean the product the conversation was just about. \
Then make the plan match what you understood."""

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
Sound like a friendly, plain-spoken human advisor, not a legal notice: no "I cannot recommend", no \
"Please confirm if you wish to proceed", no repeating the conversation back. Only mention a limit when \
the client actually asked for something that isn't allowed, and then say briefly why and what they can \
do instead. If the client asks a question (why, what is, how), answer it; you may leave purchases empty. \
When the client wants to invest, put the plan in purchases now; you may end with one short question \
(access to the money, steadier value, a bit more growth?), but never ask instead of proposing. If the \
client doesn't like a plan or asks for something else, propose a genuinely different suitable plan. \
Every product you suggest in the message must be in purchases. Don't recite the bank's internal limits \
(like the 30% rule) to the client; just follow them. State the loss warning once, in one \
short sentence. Keep the message under 120 words. Reply with JSON only."""


@dataclass
class Attempt:
    source: str  # "llm" | "llm-retry" | "solver-fix"
    purchases: dict[str, int]
    message: str
    violations: list[Violation]
    speech: list[SpeechIssue]
    error: str | None = None
    sent_back: str | None = None  # passed the rules but wasn't useful advice, so the model was asked again
    understood: dict | None = None  # the model's own reading of the request (kind, amount, products, only)

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
        about = [catalog[p].name for p in turn.get("about") or [] if p in catalog and p not in plan]
        lines.append(f"Advisor: {turn['advisor']}" + (f" [Verified plan: {plan_txt}]" if plan_txt else "")
                     + (f" [This turn was about: {', '.join(about)}]" if about else ""))
    last = next((t.get("plan") for t in reversed(list(history)) if t.get("plan")), None)
    latest = ("The client's current plan (the latest one they were shown): "
              + "; ".join(f"{catalog[p].name if p in catalog else p} (R{catalog[p].effective_risk if p in catalog else '?'}) "
                          f"¥{a:,}" for p, a in last.items()) + ".\n") if last else ""
    recent = list(history)[-1] if history else {}
    asked_about = [catalog[p].name for p in recent.get("about") or [] if p in catalog]
    just = (f"The client was just asking about: {', '.join(asked_about)}. If the new message asks to invest without "
            "naming a product (\"put 50k in it\", \"那就放5万进去\"), they mean putting that amount into this product "
            "(products: this one, only: true).\n") if asked_about else ""
    return ("Conversation so far (exactly what the client saw):\n" + "\n".join(lines) + "\n\n" + latest + just +
            "Read the new request in the context of this conversation: 'make it safer', 'why those?' or "
            "'something else' refer to the current plan.\n\n")


def _proposal_prompt(client: Client, catalog: dict[str, Product], request: str,
                     history: Sequence[dict] = ()) -> list[dict[str, str]]:
    user = (f"Client facts (confirmed by the client):\n{json.dumps(facts_card(client), ensure_ascii=False)}\n\n"
            f"Catalogue (fictional demo products):\n{json.dumps(catalogue_table(catalog), ensure_ascii=False)}\n\n"
            f"{history_block(history, catalog)}"
            f"Client request:\n{request}\n\n"
            + (f"Amount the client wrote: ¥{amt:,} (read exactly from their words; use this number).\n"
               if (amt := requested_amount(request)) is not None else
               f"Amount to invest: ¥{amt:,} (from earlier in the conversation).\n"
               if (amt := conversation_amount(request, history)) is not None else "") +
            f"Write the message in {reply_language(request)}.\n\n{UNDERSTAND}\n"
            'Reply as JSON: {"understood": {"kind": ..., "amount": ..., "products": [...], "only": ..., "exclude": [...]}, '
            '"purchases": [{"product_id": ..., "amount": ...}], "message": ...}')
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


_FOLLOW_UP = re.compile(r"\b(?:safer|less risky|lower risk|riskier|more risk|smaller|bigger|instead|adjust|switch|"
                        r"swap|replace|remove|increase|reduce|rebalance|make it|that plan|this plan|the plan)\b|"
                        r"换|调整|少一点|多一点|更安全|降低|增加|减少", re.I)


# The client wants a different plan, without naming an amount or a product ("what else?", "didn't like these")
_ALTERNATIVE = re.compile(r"\b(?:else|other|another|alternatives?|different|better|options?|instead)\b|"
                          r"\b(?:did ?n[o']?t|do ?n[o']?t|not) (?:like|want|love)\b|"
                          r"其他|其它|别的|別的|换一个|換一個|不喜欢|不喜歡|更好|还有什么|還有什麼", re.I)


def wants_alternative(request: str, history: Sequence[dict]) -> bool:
    """After a delivered plan, a request for something different still wants a plan, not just words."""
    return any(t.get("plan") for t in history) and bool(_ALTERNATIVE.search(request))


def conversation_amount(request: str, history: Sequence[dict]) -> int | None:
    """The amount to invest: named in this message, else (for a follow-up or a request for alternatives) the
    one the client named earlier in the conversation, else the total of the plan they were last given."""
    if (asked := requested_amount(request)) is not None:
        return asked
    if not (follow_up_base(request, history) or wants_alternative(request, history)):
        return None
    for t in reversed(list(history)):
        if (earlier := requested_amount(t.get("client", ""))) is not None:
            return earlier
    last = next((t["plan"] for t in reversed(list(history)) if t.get("plan")), None)
    return sum(last.values()) if last else None


def follow_up_base(request: str, history: Sequence[dict]) -> dict[str, int]:
    """If the request modifies the last delivered plan, that plan is the solver's starting point."""
    last = next((t.get("plan") for t in reversed(list(history)) if t.get("plan")), None)
    return dict(last) if last and _FOLLOW_UP.search(request) else {}


_ONLY = re.compile(r"\b(?:only|just|nothing but|solely)\b|只|僅|仅|就要|只要|全部放", re.I)


@dataclass
class Intent:
    """What the client wants in this message. Read by the model (``understood``); the phrase patterns above are
    only a fallback for when that is missing (the offline demo model, a malformed draft)."""
    kind: str                       # one of KINDS
    amount: int | None              # RMB to invest, if known
    products: list[str]             # catalogue ids the client asked for
    only: bool                      # they want just those products
    source: str                     # "model" or "patterns"
    exclude: list[str] = field(default_factory=list)  # products the client said they don't want
    written: bool = False           # the amount comes from the client's own words, read exactly

    @property
    def wants_plan(self) -> bool:
        return self.kind in ("new", "change", "alternative") or (self.amount is not None and self.kind != "question")


def resolve_intent(request: str, history: Sequence[dict], understood: dict | None,
                   catalog: dict[str, Product], client: Client) -> Intent:
    explicit = requested_amount(request)  # an amount written in this very message is unambiguous
    named = list(intent_from_request(request, catalog, client))
    if understood is not None:
        exclude = understood.get("exclude", [])
        products = [p for p in dict.fromkeys([*understood["products"], *named]) if p not in exclude]
        # An amount the client actually wrote, in this message or earlier, is read exactly and wins: the model
        # tends to fill the gap with the client's whole balance. Its own number only counts when the client
        # never wrote one ("put everything in").
        written = explicit if explicit is not None else (_earlier_amount(history) if understood["kind"] != "question"
                                                         else None)
        amount = written if written is not None else understood["amount"]
        kind = understood["kind"]
        if kind == "other" and (products or explicit is not None):
            kind = "new"
        intent = Intent(kind, amount, products, understood["only"] and bool(products), "model", exclude,
                        written is not None and amount == written)
    else:
        if wants_alternative(request, history):
            kind = "alternative"
        elif follow_up_base(request, history) or (named and history):
            kind = "change"
        elif named or explicit is not None:
            kind = "new"
        else:
            kind = "other"
        amount = explicit if explicit is not None else (
            conversation_amount(request, history) or (_earlier_amount(history) if named else None))
        intent = Intent(kind, amount, named, bool(named) and bool(_ONLY.search(request)), "patterns",
                        written=amount is not None)
    if intent.amount is not None and intent.amount > client.investable_assets:
        intent.amount = None  # can't be met anyway; the rules decide how much is possible
    return intent


def _earlier_amount(history: Sequence[dict]) -> int | None:
    for t in reversed(list(history)):
        if (a := requested_amount(t.get("client", ""))) is not None:
            return a
    return None


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
              packs: Sequence[RulePack], sentinel=None, earlier: frozenset[str] = frozenset()) -> Attempt:
    """``sentinel`` (optional) adds model-based speech findings for LLM-written text. ``earlier``: the plan
    the client was already given (and certified) on a previous turn; a reply that proposes nothing new may
    discuss those products, e.g. to answer "why did you pick those?"."""
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
    understood = _understood(data.get("understood"), catalog)
    unknown = sorted(set(purchases) - set(catalog))
    if unknown:
        return Attempt(source, purchases, message, [], [], error=f"unknown product ids {unknown}",
                       understood=understood)
    level = _level(client)
    result = check(client, catalog, Proposal(client_id=client.id, purchases=purchases, as_of=as_of), packs)
    # A reply with nothing to buy may discuss the client's current plan, and, when the client asked a question,
    # the products the question is about ("list all R1 options"): that's information, not a recommendation.
    asked_about = set(understood["products"]) if understood and understood["kind"] == "question" else set()
    discussed = set(purchases) if purchases else set(earlier) | asked_about
    speech = check_message(message, catalog, discussed, level, purchases)
    if sentinel is not None and message:
        seen = {x.rule_id for x in speech}
        speech += [x for x in sentinel.claims(message) if x.rule_id not in seen]
    return Attempt(source, purchases, message, result.violations, speech, understood=understood)


def _understood(raw, catalog: dict[str, Product]) -> dict | None:
    """The model's reading of the request, kept only if it is well-formed; unknown ids are dropped."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        return None
    amount = raw.get("amount")
    amount = int(amount) if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0 else None
    exclude = [p for p in raw.get("exclude") or [] if isinstance(p, str) and p in catalog]
    products = [p for p in raw.get("products") or [] if isinstance(p, str) and p in catalog and p not in exclude]
    return {"kind": raw["kind"], "amount": amount, "products": list(dict.fromkeys(products)),
            "only": bool(raw.get("only")) and bool(products), "exclude": list(dict.fromkeys(exclude))}


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


def _norm(text: str) -> str:
    """Lower case, letters, digits and CJK only: "Steady Income 90-Day" and "steady income 90 day" match."""
    return re.sub(r"[^0-9a-z一-鿿]", "", text.lower())


def intent_from_request(request: str, catalog: dict[str, Product], client: Client) -> dict[str, int]:
    """What the client literally asked for: named products and amount.

    Used as the starting point for the minimal legal fix when the model refuses
    without proposing anything, so the client gets the closest compliant
    alternative rather than a bare refusal.
    """
    text = _norm(request)
    named = [p for p in catalog.values()
             if any(_norm(n) in text for n in (p.id, p.name, p.name_zh, *p.aliases) if n)]
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
                 blocked: dict[str, str] | None, limited: dict[str, tuple[int, str]] | None = None) -> str:
    fit = f"符合您的风险等级（C{level}，{LEVEL_WORDS_ZH[level]}）" if level else ""
    name = lambda p: p.name_zh or p.name
    left_out = []
    for pid in blocked or {}:
        p = catalog.get(pid)
        if p is None:
            continue
        left_out.append(f"{name(p)}为R{p.effective_risk}，高于您的风险等级，因此未纳入" if level and p.effective_risk > level
                        else f"{name(p)}不符合您情况下的相关规定，因此未纳入")
    for pid, (amt, _why) in (limited or {}).items():
        if pid in catalog:
            left_out.append(f"按规定，您在{name(catalog[pid])}最多可投¥{amt:,}，其余金额暂不投资")
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


def question_message(products: list[str], catalog: dict[str, Product], level: int | None, lang: str,
                     fits: dict[str, bool]) -> str:
    """Deterministic answer to a question about products: the catalogue facts, nothing sold."""
    zh = lang == "zh"
    out = []
    for pid in products:
        p = catalog.get(pid)
        if p is None:
            continue
        r = p.effective_risk
        if zh:
            fit = (f"符合您的风险等级（C{level}）" if fits.get(pid) else f"不在您目前可购买的范围内") if level else ""
            out.append(f"{p.name_zh or p.name}为R{r}（{RISK_WORDS_ZH[r]}），{fit}，年费率{p.annual_fee_bps / 100:.2f}%，"
                       f"{_access_zh(p)}")
        else:
            fit = (f"it fits your risk level (C{level})" if fits.get(pid) else "it isn't available to you at the "
                   "moment") if level else ""
            out.append(f"{p.name} is rated R{r} ({RISK_WORDS[r]}); {fit}; annual fee {p.annual_fee_bps / 100:.2f}%, "
                       f"{_access(p)}")
    deposits = all(catalog[p].type.value == "deposit" for p in products if p in catalog)
    if zh:
        risk = "" if deposits and out else "理财产品和基金不是存款：收益不作保证，本金可能亏损。"
        return ("。".join(out) + "。" if out else "") + risk + "如需配置方案，告诉我您想投资的金额即可。"
    risk = "" if deposits and out else " Wealth products and funds are not deposits: returns are not guaranteed and " \
                                       "you could lose part of your principal."
    return (". ".join(out) + "." if out else "") + risk + " If you'd like a plan, just tell me how much to invest."


def template_message(purchases: dict[str, int], catalog: dict[str, Product], level: int | None = None,
                     blocked: dict[str, str] | None = None, lang: str = "en",
                     limited: dict[str, tuple[int, str]] | None = None) -> str:
    """Deterministic advice text used whenever the model's wording can't be shown. Always passes the checks.
    ``limited``: products the client asked for whose amount the rules cut, (most allowed, reason)."""
    if lang == "zh":
        return _template_zh(purchases, catalog, level, blocked, limited)
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
    for pid, (amt, _why) in (limited or {}).items():
        if pid in catalog:
            left_out.append(f"For you, the rules allow at most ¥{amt:,} in {catalog[pid].name}, so the rest of the "
                            "amount stays uninvested")
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
        schema = proposal_schema(self.catalog)
        messages = _proposal_prompt(client, self.catalog, request, history)
        attempts: list[Attempt] = []
        last_plan = next((dict(t["plan"]) for t in reversed(list(history)) if t.get("plan")), {})
        intent: Intent | None = None
        nudged = asked_language = asked_named = asked_under = False

        for i in range(self.max_retries + 1):
            raw = self.llm.chat(messages, schema=schema)
            att = _evaluate("llm" if i == 0 else "llm-retry", raw, client, self.catalog, as_of, self.packs,
                            self.sentinel, frozenset(last_plan))
            attempts.append(att)
            if intent is None:  # the first draft's reading, before any feedback could bend it
                intent = resolve_intent(request, history, att.understood, self.catalog, client)
            last = i == self.max_retries
            reason = None if att.ok else "rules"
            if reason is None:
                reason = self._usefulness(att, intent, request, last_plan, as_of, client,
                                          asked_named, asked_language, nudged, last, asked_under)
            if reason is None:
                notify(att)
                return AdvisorResult(att, attempts, blocked_requests=self._blocked(client, attempts, as_of))
            if reason == "rules":
                feedback, stop = _feedback(att), False
                if intent.kind == "question":
                    feedback = feedback.replace("Reply with JSON only.", "The client only asked a question: answer "
                                                "it, keep purchases empty and don't suggest other products. "
                                                "Reply with JSON only.")
                if intent.only:  # a rule cut the amount: keep the product, don't swap it for another
                    want = ", ".join(self.catalog[p].name for p in intent.products)
                    feedback = feedback.replace("Reply with JSON only.", f"The client wants only {want}: keep only "
                                                f"that, and lower the amount to what the rules allow instead of "
                                                "switching products. Reply with JSON only.")
            else:
                att.sent_back, feedback = reason[1], reason[2]
                stop = last or reason[0] == "empty" and nudged
                asked_named |= reason[0] == "named"
                asked_language |= reason[0] == "language"
                asked_under |= reason[0] == "under"
                nudged |= reason[0] == "empty"
            notify(att)
            if stop:
                break
            messages = messages + [{"role": "assistant", "content": raw}, {"role": "user", "content": feedback}]

        assert intent is not None
        purchases, limited, fix = self._closest_plan(client, request, attempts, intent, last_plan, as_of)
        if intent.kind == "question" and not purchases:
            final = self._answer(client, request, intent.products, as_of, last_plan)
            final.understood = attempts[0].understood if attempts else None
            attempts.append(final)
            notify(final)
            return AdvisorResult(final, attempts, None, {})
        blocked = {v.product_id: v.message for a in attempts for v in a.violations
                   if v.product_id and v.product_id not in purchases}
        for pid in intent.products:
            if pid not in purchases and pid not in blocked:
                why = check(client, self.catalog, Proposal(client_id=client.id, purchases={pid: 1}, as_of=as_of),
                            self.packs).violations
                blocked[pid] = why[0].message if why else "not part of the compliant allocation"
        message = self._explain(client, purchases, request, blocked, limited)
        final = _evaluate("solver-fix", json.dumps({"purchases": [{"product_id": p, "amount": a}
                                                                  for p, a in purchases.items()],
                                                    "message": message}),
                          client, self.catalog, as_of, self.packs, self.sentinel, frozenset(last_plan))
        if not final.ok:  # never show unverified text; fall back to the template
            final = _evaluate("solver-fix", json.dumps({"purchases": [{"product_id": p, "amount": a}
                                                                      for p, a in purchases.items()],
                                                        "message": template_message(purchases, self.catalog, _level(client),
                                                                                   blocked, language(request), limited)}),
                              client, self.catalog, as_of, self.packs)
        assert final.ok, "solver fix and template must always verify"
        final.understood = attempts[0].understood if attempts else None
        attempts.append(final)
        notify(final)
        return AdvisorResult(final, attempts, fix, self._blocked(client, attempts, as_of))

    def _answer(self, client: Client, request: str, products: list[str], as_of: datetime,
                last_plan: dict[str, int]) -> Attempt:
        """Every draft answering a question failed a check: answer it from the catalogue facts instead. Nothing
        is sold, and nothing is described as refused unless it really is out of reach for this client."""
        level = _level(client)
        facts = [{**catalogue_table({p: self.catalog[p]})[0], "fits_client": self._allowed(client, p, as_of)}
                 for p in products if p in self.catalog]
        prompt = [{"role": "system", "content": SYSTEM},
                  {"role": "user", "content":
                      f"The client asked: {request}\nClient risk tolerance: C{level}.\n"
                      f"Facts about the products they asked about: {json.dumps(facts, ensure_ascii=False)}\n"
                      "Answer the question plainly and briefly, using only these facts. Don't recommend or suggest "
                      "any product, and don't propose a plan. For anything that isn't a deposit, say that "
                      f"principal can be lost. Write the message in {reply_language(request)}. "
                      'Reply as JSON: {"message": ...}'}]
        about = frozenset([*products, *last_plan])
        try:
            msg = str(parse_json(self.llm.chat(prompt, schema=MESSAGE_SCHEMA)).get("message", ""))
            att = _evaluate("solver-fix", json.dumps({"purchases": [], "message": msg}), client, self.catalog,
                            as_of, self.packs, self.sentinel, about)
            if att.ok and msg:
                return att
        except Exception:
            pass
        text = question_message(products, self.catalog, level, language(request),
                                {p: self._allowed(client, p, as_of) for p in products})
        att = _evaluate("solver-fix", json.dumps({"purchases": [], "message": text}), client, self.catalog, as_of,
                        self.packs, None, about)
        assert att.ok, "the factual answer must always verify"
        return att

    def _usefulness(self, att: Attempt, intent: Intent, request: str, last_plan: dict[str, int], as_of: datetime,
                    client: Client, asked_named: bool, asked_language: bool, nudged: bool,
                    last: bool, asked_under: bool = False) -> tuple[str, str, str] | None:
        """A draft that passed every rule can still be the wrong answer. Returns (kind, why it was sent back,
        feedback for the model), or None if it does what the client asked. Every test here is against the
        client's intent, not against phrasings."""
        total = sum(att.purchases.values())
        if intent.amount is not None and total > intent.amount * 1.02:  # rounding slack only
            return ("over", f"It invests ¥{total:,}, but the client asked for ¥{intent.amount:,}.",
                    f"The client asked to invest ¥{intent.amount:,}, but your plan totals ¥{total:,}. Keep the "
                    f"total at or below ¥{intent.amount:,}. Reply with JSON only.")
        if (intent.written and intent.amount and att.purchases and total < intent.amount * 0.5 and not asked_under
                and intent.kind != "question"):
            return ("under", f"It invests ¥{total:,}, but the client wrote ¥{intent.amount:,}.",
                    f"The client wants to invest ¥{intent.amount:,} (that is the exact number), but your plan totals "
                    f"only ¥{total:,}. Plan for ¥{intent.amount:,}; if a rule limits it, invest the most allowed "
                    "and say why. Reply with JSON only.")
        if intent.kind == "question" and att.purchases and att.purchases != last_plan:
            return ("question", "The client asked a question; the draft turned it into a new plan.",
                    "The client only asked a question. Answer it, and leave purchases empty unless they ask to "
                    "invest. Reply with JSON only.")
        unwanted = [p for p in intent.exclude if att.purchases.get(p)]
        if unwanted:
            names = ", ".join(self.catalog[p].name for p in unwanted)
            return ("exclude", f"The client said they don't want {names}, but the plan still has it.",
                    f"The client said they don't want {names}. Remove it and rebalance the same amount across "
                    "other suitable products. Reply with JSON only.")
        if intent.only and att.purchases and set(att.purchases) - set(intent.products):
            want = ", ".join(self.catalog[p].name for p in intent.products)
            extra = ", ".join(self.catalog[p].name for p in att.purchases if p not in intent.products)
            return ("only", f"The client asked for only {want}, but the plan adds {extra}.",
                    f"The client wants only {want}. Don't add other products. If a rule limits how much can go "
                    "into it, put in the most that is allowed and say plainly that the rest stays uninvested and "
                    "why. Reply with JSON only.")
        missing = [p for p in intent.products if not att.purchases.get(p) and self._allowed(client, p, as_of)]
        if missing and intent.kind != "question" and not asked_named:
            names = ", ".join(self.catalog[p].name for p in missing)
            return ("named", f"The client asked for {names}, which is allowed for them, but the plan leaves it out.",
                    f"The client asked for {names}. It is allowed for this client, so include it unless a rule "
                    "prevents the amount. Reply with JSON only.")
        if intent.kind == "alternative" and att.purchases and att.purchases == last_plan:
            return ("same", "The client asked for something different, but this is the plan they just turned down.",
                    "This is the same plan the client just said they didn't like. Propose a genuinely different "
                    "suitable plan (other products or a clearly different balance), for the same amount. "
                    "Reply with JSON only.")
        if language(request) == "zh" and language(att.message) != "zh" and not asked_language:
            return ("language", "The client wrote in Chinese, but the reply is in English.",
                    f"The client wrote in Chinese. Keep the same plan and rewrite the message in "
                    f"{reply_language(request)}. Reply with JSON only.")
        if not att.purchases and intent.wants_plan:
            # A compliant but empty reply to someone who wants to invest isn't useful advice. Give the model
            # the computed room once; if it still refuses, the solver offers the closest legal plan.
            return ("empty", "The client asked to invest, and a compliant plan exists for them.",
                    self._capacity_note(client, request))
        return None

    def _allowed(self, client: Client, pid: str, as_of: datetime) -> bool:
        """Can this client hold any of the product at all (risk level, eligibility, ...)?"""
        p = self.catalog[pid]
        return max_amount(client, self.catalog, Proposal(client_id=client.id, purchases={}, as_of=as_of),
                          self.packs, pid) >= max(p.min_investment, 1)

    def _closest_plan(self, client: Client, request: str, attempts: list[Attempt], intent: Intent,
                      last_plan: dict[str, int], as_of: datetime
                      ) -> tuple[dict[str, int], dict[str, tuple[int, str]], Fix | None]:
        """No draft was both compliant and useful: the solver builds the closest compliant plan. Returns the
        plan and, for products the client asked for that the rules cap, (the most allowed, the rule's reason)."""
        if intent.kind == "question":  # a question never becomes a sale; at most their current plan, re-checked
            base = dict(last_plan)
            fix = minimal_fix(client, self.catalog, Proposal(client_id=client.id, purchases=base, as_of=as_of),
                              self.packs) if base else None
            return (fix.purchases if fix else {}), {}, fix
        if intent.only:
            plan, limited = self._only_these(client, intent, as_of)
            if plan:
                return plan, limited, None
        named = {p: (intent.amount or client.investable_assets // 5) // max(len(intent.products), 1)
                 for p in intent.products}
        named_ok = bool(named) and check(client, self.catalog, Proposal(client_id=client.id, purchases=named,
                                                                        as_of=as_of), self.packs).ok
        base = ((named if named_ok else {})  # the client's own compliant request comes first
                or next((a.purchases for a in attempts if a.error is None and a.purchases), {})
                or named
                or (last_plan if intent.kind in ("change", "question") else {})  # adjust / explain their plan
                or (self._default_plan(client, request, as_of) if intent.wants_plan else {}))
        base = {p: a for p, a in base.items() if p not in intent.exclude}
        total = sum(base.values())
        if intent.amount is not None and total and (total > intent.amount or intent.written):
            base = {p: a * intent.amount // total for p, a in base.items()}  # to what they asked for
        if not base:
            return {}, {}, None
        fix = minimal_fix(client, self.catalog, Proposal(client_id=client.id, purchases=base, as_of=as_of), self.packs)
        plan = fix.purchases if fix else {}
        if any(p in plan for p in intent.exclude):  # the solver may add products to keep the total: never these
            kept = {p: a for p, a in plan.items() if p not in intent.exclude}
            if check(client, self.catalog, Proposal(client_id=client.id, purchases=kept, as_of=as_of), self.packs).ok:
                plan = kept
        return plan, {}, fix

    def _only_these(self, client: Client, intent: Intent, as_of: datetime
                    ) -> tuple[dict[str, int], dict[str, tuple[int, str]]]:
        """"Only X": never pad the plan with products the client didn't ask for. Each product gets its share,
        cut to the most the rules allow; whatever doesn't fit stays uninvested."""
        total = intent.amount or client.investable_assets // 5
        share = total // len(intent.products)
        plan: dict[str, int] = {}
        limited: dict[str, tuple[int, str]] = {}
        for pid in intent.products:
            cap = max_amount(client, self.catalog, Proposal(client_id=client.id, purchases=plan, as_of=as_of),
                             self.packs, pid)
            amt = min(share, cap) // LOT * LOT
            if amt < share:
                why = check(client, self.catalog, Proposal(client_id=client.id, purchases={**plan, pid: share},
                                                           as_of=as_of), self.packs).violations
                limited[pid] = (amt, next((v.message for v in why if v.product_id in (pid, None)),
                                          why[0].message if why else "a rule limits the amount"))
            if amt >= max(self.catalog[pid].min_investment, 1):
                plan[pid] = amt
        if plan and not check(client, self.catalog, Proposal(client_id=client.id, purchases=plan, as_of=as_of),
                              self.packs).ok:
            return {}, {}
        return plan, limited

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
                 blocked: dict[str, str] | None = None, limited: dict[str, tuple[int, str]] | None = None) -> str:
        level = _level(client)
        why_not = [{"product": self.catalog[p].name, "risk": f"R{self.catalog[p].effective_risk}", "reason": r}
                   for p, r in (blocked or {}).items() if p in self.catalog]
        capped = [{"product": self.catalog[p].name, "most_allowed": amt, "reason": r}
                  for p, (amt, r) in (limited or {}).items() if p in self.catalog]
        prompt = [{"role": "system", "content": SYSTEM},
                  {"role": "user", "content":
                      f"The client asked: {request}\nThe compliant allocation is fixed as: "
                      f"{json.dumps(catalogue_table({p: self.catalog[p] for p in purchases}), ensure_ascii=False)} "
                      f"with amounts {json.dumps(purchases)}. Client risk tolerance is C{level}.\n"
                      f"Products the client asked for that are in our catalogue but were blocked, with the exact "
                      f"reason: {json.dumps(why_not, ensure_ascii=False)}\n"
                      f"Products the client asked for whose amount the rules cut, with the most allowed and the "
                      f"reason: {json.dumps(capped, ensure_ascii=False)} (say plainly that the rest stays "
                      "uninvested and why).\n"
                      "Explain the allocation to the client and, if relevant, why their original request could not "
                      "be met, using only the reasons given. Do not mention other products. "
                      f"Write the message in {reply_language(request)}. "
                      'Reply as JSON: {"message": ...}'}]
        try:
            return str(parse_json(self.llm.chat(prompt, schema=MESSAGE_SCHEMA)).get("message", ""))
        except Exception:
            return template_message(purchases, self.catalog, level, blocked, language(request), limited)

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
