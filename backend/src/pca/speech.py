"""Speech compliance: what the advisor *says*, not just what it allocates.

Deterministic layer. It catches the clear-cut violations of NFRA Art. 13(2) and
13(4) with patterns and checks against catalogue data. A fine-tuned sentinel
model adds judgement on subtler cases; this layer never relies on it.

- SP-GUAR   promises of principal protection or guaranteed returns  (Art. 13(2))
- SP-DEP    presenting a non-deposit product as a deposit           (Art. 13(2))
- SP-PERF   performance figures that don't match catalogue data    (Art. 13(4))
- SP-RISK   omitting principal-loss risk when recommending R2+      (Art. 13(2), Art. 31(3))
- SP-UNSUIT mentioning a product the client can't buy as a recommendation (Art. 13(3))
- SP-COACH  suggesting the client change their risk assessment to qualify (Art. 13(1))
- SP-UNCERT recommending a product that isn't in the certified allocation (the advice
            would reach the client without having been verified)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Product, ProductType


@dataclass(frozen=True)
class SpeechIssue:
    rule_id: str
    citation: str
    message: str
    excerpt: str
    product_id: str | None = None


GUARANTEE_PATTERNS = [
    r"\bguarantee[ds]?\b(?![^.]*\bnot\b)",
    r"\bprincipal[- ]protected\b",
    r"\b(?:risk[- ]free|no risk|zero risk|can(?:'|no)t lose|won'?t lose|never lose)\b",
    r"\bsafe as (?:a )?(?:bank )?deposits?\b",
    r"\bassured returns?\b",
    r"\b(?:no|zero|without(?:\s+any)?)\s+(?:risk\s+of\s+)?(?:principal\s+)?loss(?:es)?(?:\s+risk)?\b",  # "zero principal loss risk"
    r"\bprincipal\s+(?:is\s+)?(?:\w+\s+)?(?:protected|safe|secure)\b",  # "principal mostly protected"
    r"\b(?:fully|completely|100%)\s+(?:safe|protected|secure)\b",
    r"保本",
    r"保收益",
    r"稳赚",
    r"零风险",
    r"无风险",
    r"不会亏",
    r"刚性兑付",
    r"保[证證].{0,8}(?:收益|回[报報]|年化|\d+(?:\.\d+)?\s*%|[赚賺]|不[亏虧])",
    r"承[诺諾].{0,6}(?:收益|回[报報]|年化|\d+(?:\.\d+)?\s*%)",
    r"[稳穩][赚賺]|包[赚賺]|只[赚賺]不[赔賠]|[稳穩]赚不[赔賠]|[绝絕][对對]安全|一定(?:会)?(?:[赚賺]|[涨漲]|盈利)|肯定(?:会)?(?:[赚賺]|[涨漲])",
    r"保本|零[风風][险險]|[无無][风風][险險]|不[会會][亏虧]",
]
# Phrases that negate a guarantee are fine: "returns are not guaranteed", "不保本".
NEGATIONS = [
    r"\b(?:do|does|did|can|could|will|would|cannot|can't|won't|doesn't|don't)\s*(?:not\s+)?guarantee\w*(?:\s+(?:any|your|the))?(?:\s+principal(?:[- ]protection)?|\s+returns?)?",
    r"\bnot\s+(?:be\s+)?guarantee\w*",
    r"\bwithout\s+(?:any\s+)?guarantee\w*",
    r"\b(?:not|never)\s+(?:be\s+)?(?:\w+\s+)?(?:protected|safe|secure|risk[- ]free)\b",
    r"\b(?:cannot|can't|won't|will not|do not|don't|never|must not|mustn't|should not|may not|not allowed to)\s+"
    r"(?:[\w-]+\s+){0,8}?(?:promise|promising|offer|provide|give|make|describe)\s+(?:you\s+)?(?:any\s+)?"
    r"(?:[\w-]+\s+){0,4}?guarantee\w*(?:\s+(?:returns?|outcomes?|principal(?:[- ]protection)?))?",
    r"\bwithout\s+promising\s+(?:any\s+)?guarantee\w*(?:\s+\w+)?",
    r"\bor\s+(?:promise\s+)?guarantee\w*\s+(?:returns?|principal)(?:/principal protection)?",
    r"\bwithout\s+guaranteed\s+\w+",
    r"\bno\s+risk[- ]free\s+\w+",
    r"\bnor\s+(?:can|will|do|does|would)\s+(?:i|we)\s+(?:promise|offer|provide|guarantee)\s+(?:any\s+)?(?:guarantee\w*\s+)?(?:\w+\s+){0,2}",
    r"\b(?:do|does)\s+not\s+recommend\s+treating\s+[\w\s-]{0,40}?\s+as\s+guarantee\w*(?:\s+\w+)?",
    r"\b(?:no|none of)\s+(?:[\w-]+\s+){0,6}?(?:offers?|has|have|provides?|gives?)\s+(?:any\s+)?guarantee\w*(?:\s+(?:returns?|principal(?:[- ]protection)?))?",
    r"\bany\s+product\s+that\s+promises\s+guarantee\w*(?:\s+\w+){0,2}",
    r"\b(?:no|nothing|none|neither)\b(?:\s+[\w-]+){0,4}?\s+guarantee\w*(?:\s+[\w-]+){0,3}",
    r"\bnot\s+(?:be\s+)?guaranteed\b",
    r"\bno\s+guarantee\b",
    r"\bnot\s+principal[- ]protected\b",
    r"\bisn'?t\s+guaranteed\b",
    r"不保本",
    r"非保本",
    # "不保证收益" / "无法承诺年化6%": the negated promise and its object only, never across punctuation
    r"(?:不|无法|無法|不能|不会|不會|并不|並不|不可|没有|沒有)(?:能)?(?:承[诺諾]|保[证證]|提供)(?:任何|您的|你的|会有|會有)?"
    r"(?:保本|本金(?:安全)?|收益|回[报報]|年化(?:收益)?|翻倍|\d+(?:\.\d+)?\s*%(?:的)?(?:收益|回[报報])?)?",
    r"不是保本",
    r"并非保本|並非保本",
]
DEPOSIT_WORDS = r"(?:\bdeposits?\b|存款)"
PCT = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")


def _sentences(text: str) -> list[str]:
    # ASCII punctuation must be followed by whitespace, so "24.5%" isn't split.
    parts = re.split(r"(?<=[.!?])\s+|(?<=[。！？])|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _mentions(sentence: str, p: Product) -> bool:
    s = sentence.lower()
    return any(n.lower() in s for n in (p.id, p.name, p.name_zh, *p.aliases) if n)


DEPOSIT_ONLY = re.compile(r"\b(?:deposits?|savings accounts?|time deposit|demand deposit)\b|存款", re.I)
NON_DEPOSIT = re.compile(r"\b(?:wealth|fund|funds|bond|equity|insurance|portfolio|investment)\b|理财|基金", re.I)


def check_guarantees(text: str, products: dict[str, Product] | None = None) -> list[SpeechIssue]:
    """Guarantee language is only a violation when it's about a non-deposit product.

    Deposits really are principal-protected (deposit insurance up to RMB 500,000),
    so "a time deposit has guaranteed principal" is accurate, not mis-selling.
    """
    out = []
    deposit_names = [p.name for p in (products or {}).values() if p.type == ProductType.DEPOSIT]
    for s in _sentences(text):
        about = s
        for name in deposit_names:
            about = re.sub(re.escape(name), " deposit ", about, flags=re.I)
        compares = re.search(r"\b(?:as safe as|as good as|like a|just like|same as|equivalent to|no different from)\b"
                             r"|跟.{0,4}一样|和.{0,6}一样", s, flags=re.I)
        if DEPOSIT_ONLY.search(about) and not NON_DEPOSIT.search(about) and not compares:
            continue  # an accurate statement about an actual deposit
        cleaned = s
        for n in NEGATIONS:
            cleaned = re.sub(n, " ", cleaned, flags=re.I)
        for pat in GUARANTEE_PATTERNS:
            if re.search(pat, cleaned, flags=re.I):
                out.append(SpeechIssue("SP-GUAR", "NFRA Art. 13(2)",
                                       "Promises principal protection or guaranteed returns.", s))
                break
    return out


def check_deposit_confusion(text: str, products: dict[str, Product]) -> list[SpeechIssue]:
    out = []
    deposit_names = sorted({n for p in products.values() if p.type == ProductType.DEPOSIT
                            for n in (p.name, p.name_zh, *p.aliases) if n}, key=len, reverse=True)
    for s in _sentences(text):
        # "deposit" inside a real deposit product's name (any language) isn't a claim about other products
        bare = s
        for name in deposit_names:
            bare = re.sub(re.escape(name), " ", bare, flags=re.I)
        if not re.search(DEPOSIT_WORDS, bare, flags=re.I):
            continue
        for p in products.values():
            if p.type != ProductType.DEPOSIT and _mentions(s, p):
                if re.search(r"\b(?:not|isn'?t)\b[^.]*\bdeposit|\b(?:except|other than|unlike|apart from)\b[^.]*\bdeposits?|"
                             r"不是(?:银行)?存款|非存款|并非存款|並非存款|不属于存款|不屬於存款|不同于存款|不等于存款|"
                             r"除(?:了)?存款(?:以)?外|存款以外|存款除外", s, flags=re.I):
                    continue
                out.append(SpeechIssue("SP-DEP", "NFRA Art. 13(2)",
                                       f"{p.name} is a {p.type.value} product, not a deposit.", s, p.id))
    return out


def check_performance(text: str, products: dict[str, Product]) -> list[SpeechIssue]:
    """Any percentage stated next to a product must match one of its catalogue figures."""
    out = []
    for s in _sentences(text):
        nums = [float(m) for m in PCT.findall(s)]
        if not nums:
            continue
        named = [p for p in products.values() if _mentions(s, p)]
        if len(named) != 1:  # ambiguous or no product: the sentinel model handles these
            continue
        p = named[0]
        known = list(p.returns_pct.values()) + ([p.max_drawdown_pct] if p.max_drawdown_pct is not None else [])
        known += [p.annual_fee_bps / 100]
        for n in nums:
            if not any(abs(n - k) < 0.051 or abs(-n - k) < 0.051 for k in known):
                out.append(SpeechIssue("SP-PERF", "NFRA Art. 13(4)",
                                       f"{n}% for {p.name} doesn't match its catalogue data {sorted(set(known))}.",
                                       s, p.id))
    return out


def check_risk_disclosure(text: str, recommended: dict[str, Product]) -> list[SpeechIssue]:
    if not any(p.effective_risk >= 2 for p in recommended.values()):
        return []
    if re.search(r"(?:lose|loss of|losses?)\b[^.]*\b(?:principal|money|capital|value)|"
                 r"(?:principal|capital)\b[^.]*\b(?:at risk|loss|lost)|本金.{0,8}(?:[亏虧][损損]|[损損]失|风险|風險|不保)|"
                 r"[损損]失本金|[亏虧][损損]本金|不保本|非保本|不保[证證].{0,4}本金|可能.{0,6}(?:[亏虧]|[损損]失)|"
                 r"(?:存在|有)[亏虧][损損](?:的)?(?:风险|風險|可能)",
                 text, flags=re.I):
        return []
    return [SpeechIssue("SP-RISK", "NFRA Art. 13(2), Art. 31(3)",
                        "Recommends products that can lose money without saying principal may be lost.", "")]


def check_unsuitable_mentions(text: str, catalog: dict[str, Product], level: int,
                              recommended: set[str]) -> list[SpeechIssue]:
    """Talking up a product above the client's level counts as recommending it (Art. 13(3)),
    unless the sentence explains that it is not suitable."""
    out = []
    for s in _sentences(text):
        for p in catalog.values():
            if p.effective_risk > level and p.id not in recommended and _mentions(s, p):
                if re.search(r"\b(?:not suitable|unsuitable|can'?t|cannot|not allowed|above your|exceed(?:s|ing|ed)?|"
                             r"(?:does|do) not (?:permit|allow)|not permitted|outside your|too risky|only permits?|"
                             r"carr(?:y|ies) (?:potential |significant )?loss(?:es)?|not as safe)\b|不适合|不適合|不能|不推荐|不推薦|不建议|不建議|无法推荐|無法推薦|高于您|高於您|超出您|超过您|超過您|未纳入|未納入|不纳入|不納入|不符合|不在.{0,6}范围|不宜|暂不|暫不",
                             s, flags=re.I):
                    continue
                out.append(SpeechIssue("SP-UNSUIT", "NFRA Art. 13(3)",
                                       f"Promotes {p.name} (R{p.effective_risk}) to a C{level} client.", s, p.id))
    return out


COACH = re.compile(r"\b(?:adjust|change|raise|increase|upgrade|update)\s+your\s+(?:risk\s+)?(?:tolerance|level|profile|"
                   r"assessment|rating)\s+(?:to|so)|(?:answer|retake)\s+(?:the\s+)?(?:questionnaire|assessment)\s+"
                   r"(?:differently|again)\s+(?:to|so)|重新做.{0,6}(?:测评|评估).{0,6}(?:提高|升到)|调高.{0,4}风险等级", re.I)


def check_coaching(text: str) -> list[SpeechIssue]:
    """Suggesting the client change their assessment to qualify (NFRA Art. 13(1))."""
    return [SpeechIssue("SP-COACH", "NFRA Art. 13(1)",
                        "Suggests changing the client's risk assessment to qualify for a product.", s)
            for s in _sentences(text) if COACH.search(s)]


RECOMMEND_WORDS = (r"\b(?:suggest|recommend|propose|advise|consider|put|place|placing|allocat\w*|invest(?:ing)?\s+in|"
                   r"buy|switch\s+to|instead|mix|includes?|including|go\s+with|opt\s+for)\b|建议|推荐|可以考虑|买入|组合|配置")


_EARLIER_PLAN = re.compile(r"\b(?:your|the|my) (?:current|last|previous|earlier|existing|original) (?:plan|allocation|mix)\b|"
                           r"\bI (?:picked|chose|selected|suggested|recommended|included)\b|\b(?:earlier|before|last time)\b|"
                           r"之前|上次|刚才|剛才|原来的|原來的", re.I)


def check_uncertified(text: str, catalog: dict[str, Product], level: int, recommended: set[str]) -> list[SpeechIssue]:
    """A suitable product recommended in the text but absent from the allocation was never verified."""
    out = []
    for s in _sentences(text):
        if not re.search(RECOMMEND_WORDS, s, flags=re.I):
            continue
        if re.search(r"\b(?:not suitable|unsuitable|cannot|can'?t|not allowed|(?:will|would|do|does|did) not "
                     r"(?:suggest|recommend|propose)|won'?t (?:suggest|recommend|propose)|don'?t (?:suggest|recommend))\b"
                     r"|不适合|不適合|不能|不推荐|不推薦|不建议|不建議|无法推荐|無法推薦|高于您|高於您|超出您|超过您|超過您|未纳入|未納入|不纳入|不納入|不符合|不在.{0,6}范围|不宜|暂不|暫不", s, flags=re.I):
            continue
        if _EARLIER_PLAN.search(s):  # explaining advice already given (and certified) on an earlier turn
            continue
        for p in catalog.values():
            if p.effective_risk <= level and p.id not in recommended and _mentions(s, p):
                out.append(SpeechIssue("SP-UNCERT", "Certification scope",
                                       f"Recommends {p.name}, which is not in the verified allocation.", s, p.id))
    return out


def check_risk_levels(text: str, catalog: dict[str, Product]) -> list[SpeechIssue]:
    """A product named with the wrong risk level ("STAR Market fund (R4)" when it's R5) misleads the client."""
    out = []
    for p in catalog.values():
        for name in sorted({n for n in (p.id, p.name, p.name_zh, *p.aliases) if n}, key=len, reverse=True):
            for m in re.finditer(re.escape(name) + r"[^。.!?！？;；]{0,16}?(?<![A-Za-z])R([1-5])(?![0-9])", text, re.I):
                if int(m.group(1)) != p.effective_risk:
                    out.append(SpeechIssue("SP-FACT", "NFRA Art. 13(2)",
                                           f"States {p.name} is R{m.group(1)}; the catalogue rates it "
                                           f"R{p.effective_risk}.", m.group(0), p.id))
            if any(i.product_id == p.id for i in out):
                break
    return out


_TOTAL = re.compile(r"(?:\bin total\b|\btotal(?:ling| of|s)?\b|合计|合計|总计|總計|共计|共計|总投入|總投入|一共|总额|總額)"
                    r"[^。.!?！？;；]{0,14}?(?:¥|RMB\s*|人民币\s*)?(\d[\d,]*(?:\.\d+)?)\s*(万|萬|k|K)?", re.I)


_TOTAL_AFTER = re.compile(r"(?:¥|RMB\s*)(\d[\d,]*(?:\.\d+)?)\s*(k|K)?\s+(?:in total|total)\b", re.I)


def _amount(num: str, unit: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * (10_000 if unit in ("万", "萬") else 1_000 if unit in ("k", "K") else 1)


def check_product_facts(text: str, catalog: dict[str, Product], purchases: dict[str, int] | None) -> list[SpeechIssue]:
    """Facts the client relies on must match the plan: the stated total, and which name belongs to which product."""
    out = []
    if purchases:
        total = sum(purchases.values())
        for m in [*_TOTAL.finditer(text), *_TOTAL_AFTER.finditer(text)]:
            v = _amount(m.group(1), m.group(2))
            if v >= 1_000 and abs(v - total) > max(0.01 * total, 1):
                out.append(SpeechIssue("SP-FACT", "NFRA Art. 13(2)",
                                       f"States a total of ¥{v:,.0f}; the plan totals ¥{total:,}.", m.group(0)))
                break
    names = [(n, p) for p in catalog.values() for n in (p.name, p.name_zh, *p.aliases) if n and len(n) >= 3]
    for a_name, a in names:
        for m in re.finditer(re.escape(a_name) + r"['’”\"]?\s*[（(]([^)）]{2,40})[)）]", text, re.I):
            inner = m.group(1).lower()
            if any(n.lower() in inner and b.id != a.id for n, b in names) and not any(
                    n.lower() in inner for n in (a.name, a.name_zh, *a.aliases) if n):
                out.append(SpeechIssue("SP-FACT", "NFRA Art. 13(2)",
                                       f"Labels {a.name} with another product's name.", m.group(0), a.id))
    return out


def check_message(text: str, catalog: dict[str, Product], recommended: set[str], level: int,
                  purchases: dict[str, int] | None = None) -> list[SpeechIssue]:
    rec = {pid: catalog[pid] for pid in recommended if pid in catalog}
    return (check_guarantees(text, catalog) + check_coaching(text) + check_deposit_confusion(text, catalog)
            + check_performance(text, catalog)
            + check_risk_disclosure(text, rec) + check_unsuitable_mentions(text, catalog, level, recommended)
            + check_uncertified(text, catalog, level, recommended) + check_risk_levels(text, catalog)
            + check_product_facts(text, catalog, purchases))
