"""Profile integrity: does what the client says match their questionnaire?

A common real-world mis-selling route is a risk profile that doesn't reflect the
client: answers coached upward, or circumstances that changed since. Two legal
hooks apply:

- NFRA Art. 29: if the client reports a change that may affect their risk
  tolerance, they must be reassessed before any sale. We treat statements like
  "I just lost my job" as such a report: the assessment stops being valid.
- NFRA Art. 13(1): influencing assessment results is prohibited. When the client's
  own words contradict a high assessed level, we apply a conservative cap for this
  session and route the case to a human, never the other way round.

Signals come from a classifier (the fine-tuned sentinel model when available) with
a deterministic pattern layer that always runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Signal:
    kind: str  # "loss_averse" | "short_horizon" | "life_change" | "dependent_funds"
    excerpt: str
    max_level: int | None = None  # highest level consistent with this statement
    reported_change: bool = False  # NFRA Art. 29: triggers reassessment
    source: str = "pattern"


@dataclass
class IntegrityResult:
    signals: list[Signal] = field(default_factory=list)
    assessed_level: int | None = None
    effective_level: int | None = None
    reported_change: bool = False

    @property
    def capped(self) -> bool:
        return (self.assessed_level is not None and self.effective_level is not None
                and self.effective_level < self.assessed_level)

    @property
    def flagged(self) -> bool:
        return self.capped or self.reported_change


PATTERNS: list[tuple[str, str, int | None, bool]] = [
    # (kind, regex, max_level, reported_change)
    ("loss_averse", r"can'?t (?:afford to )?lose|cannot (?:afford to )?lose|don'?t want to lose any|no loss|"
                    r"zero risk|must not lose|不能亏|不能损失|一分都不能|输不起|保本|亏不起|亏不得|赔不起|不能赔|"
                    r"一点都不能亏|亏一点都不行|一点(?:都|也)?(?:不想|不能)亏|不想亏钱", 2, False),
    ("dependent_funds", r"retirement (?:money|savings|fund)|pension|life savings|all (?:the )?money i have|"
                        r"my only savings|养老钱|养老金|全部积蓄|救命钱|全部家当|唯一的积蓄|所有的钱|给父母看病|看病的钱", 2, False),
    ("short_horizon", r"need (?:it|the money|this money) (?:back )?(?:in|within|next) (?:\d+ )?(?:weeks?|months?|"
                      r"year)|down ?payment next|tuition (?:next|this)|下个月要用|明年要用|半年后要用|明年就要用|下个月就要用|"
                      r"马上(?:就)?要用|很快(?:就)?要用|学费|首付|(?:\d+|[一二两三四五六七八九十半])个?月(?:内|后|以后)?(?:就)?要用",
                      3, False),
    ("life_change", r"lost my job|laid off|got fired|unemployed|medical bills|diagnosed|divorc|bankrupt|"
                    r"失业|被裁|生病|住院|离婚", None, True),
]


# "也不是说完全不能亏" / "it's not that I can't lose anything": the client is denying the statement.
NEGATED = re.compile(r"(?:不是说|并不是|並不是|也不是|不是|倒不是|并非|並非)[^，,。.!?！？]{0,6}$|"
                     r"\b(?:not that|it'?s not like|doesn'?t mean|not saying)\b[^,.!?]{0,20}$", re.I)


def pattern_signals(text: str) -> list[Signal]:
    out = []
    sentences = re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text)
    for kind, pat, cap, change in PATTERNS:
        for s in sentences:
            m = next((m for m in re.finditer(pat, s, flags=re.I) if not NEGATED.search(s[:m.start()])), None)
            if m:
                out.append(Signal(kind, s.strip(), cap, change))
                break
    return out


class SignalClassifier(Protocol):
    def signals(self, text: str) -> list[Signal]: ...


def assess_integrity(text: str, assessed_level: int | None,
                     classifier: SignalClassifier | None = None) -> IntegrityResult:
    sigs = pattern_signals(text)
    if classifier is not None:
        seen = {s.kind for s in sigs}
        sigs += [s for s in classifier.signals(text) if s.kind not in seen]
    caps = [s.max_level for s in sigs if s.max_level is not None]
    effective = min([assessed_level, *caps]) if assessed_level is not None and caps else assessed_level
    return IntegrityResult(sigs, assessed_level, effective, any(s.reported_change for s in sigs))
