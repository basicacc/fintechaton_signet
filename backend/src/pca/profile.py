"""Deterministic risk profiling.

The questionnaire is scored by code, never by the LLM, so the model can't nudge
a client's level (NFRA Art. 13(1)). The question set follows the information
categories in NFRA Art. 25. The wording and scoring are our own house design,
modelled on typical bank risk questionnaires; they are not regulatory text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Assessment, Client


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    category: str  # NFRA Art. 25 category
    options: tuple[tuple[str, int], ...]  # (label, points)
    knockout_option: int | None = None  # choosing this option caps the level at C1
    caps: tuple[int, ...] | None = None  # per option: the highest level this answer allows


QUESTIONS: tuple[Question, ...] = (
    Question("age", "What is your age?", "basic (Art. 25(1))",
             (("Under 30", 4), ("30–49", 5), ("50–64", 3), ("65 or over", 1))),
    Question("income", "How stable is your main income?", "financial (Art. 25(2))",
             (("No regular income", 0), ("Unstable", 2), ("Stable salary or pension", 4), ("Stable and growing", 5))),
    Question("share", "What share of your financial assets would this investment be?", "financial (Art. 25(2))",
             (("More than 70%", 0), ("40–70%", 2), ("10–40%", 4), ("Less than 10%", 5))),
    Question("experience", "How long have you invested in funds, stocks or wealth products?", "experience (Art. 25(3))",
             (("Never", 0), ("Under 2 years", 2), ("2–5 years", 4), ("Over 5 years", 5))),
    Question("knowledge", "Which best describes your investment knowledge?", "experience (Art. 25(3))",
             (("Very limited", 0), ("I understand basic products", 2), ("I understand funds and bonds", 4),
              ("I understand derivatives and leverage", 5))),
    Question("horizon", "How long can this money stay invested?", "needs (Art. 25(4))",
             (("Under 1 year", 0), ("1–3 years", 2), ("3–5 years", 4), ("Over 5 years", 5))),
    Question("goal", "What is your main goal?", "needs (Art. 25(4))",
             (("Keep my principal safe", 0), ("Steady income", 2), ("Balanced growth", 4), ("Maximum growth", 5))),
    Question("loss", "What is the largest loss you could accept in a year?", "risk tolerance (Art. 25(5))",
             (("No loss at all", 0), ("Up to 5%", 2), ("Up to 20%", 4), ("More than 20%", 5)),
             knockout_option=0, caps=(1, 2, 4, 5)),
    Question("reaction", "If your investment fell 20% in a month, you would…", "risk tolerance (Art. 25(5))",
             (("Sell everything", 0), ("Sell some", 2), ("Hold", 4), ("Buy more", 5)),
             caps=(2, 3, 5, 5)),
)

MAX_SCORE = sum(max(p for _, p in q.options) for q in QUESTIONS)
THRESHOLDS = ((0.20, 1), (0.40, 2), (0.60, 3), (0.80, 4), (1.01, 5))  # share of max score -> level


@dataclass
class ProfileResult:
    level: int
    score: int
    knockout: bool  # chose "no loss at all": lowest category (cf. CSRC C1 definition)
    changed_from: int | None  # previous level if different (Art. 29: client must re-confirm)


class AssessmentLimitError(Exception):
    pass


def score(answers: dict[str, int]) -> tuple[int, int, bool]:
    """``answers`` maps question id -> chosen option index. Returns (level, score, knockout)."""
    missing = [q.id for q in QUESTIONS if q.id not in answers]
    if missing:
        raise ValueError(f"unanswered questions: {missing}")
    total, knockout, cap = 0, False, 5
    for q in QUESTIONS:
        idx = answers[q.id]
        if not 0 <= idx < len(q.options):
            raise ValueError(f"invalid option {idx} for {q.id}")
        total += q.options[idx][1]
        knockout |= q.knockout_option == idx
        if q.caps:
            cap = min(cap, q.caps[idx])
    level = next(lvl for frac, lvl in THRESHOLDS if total / MAX_SCORE < frac)
    # Stated loss tolerance overrides a high total: a client who accepts at most 5%
    # loss can't be classed as balanced or above, however they answered elsewhere.
    return (1 if knockout else min(level, cap)), total, knockout


def assess(client: Client, answers: dict[str, int], now: datetime) -> ProfileResult:
    """Score the questionnaire and record it, enforcing NFRA Art. 29 frequency limits."""
    day = sum(1 for a in client.assessments if now - a.at <= timedelta(days=1))
    year = sum(1 for a in client.assessments if now - a.at <= timedelta(days=365))
    if day >= 2 or year >= 8:
        raise AssessmentLimitError("Assessment limit reached: at most 2 per day and 8 per 12 months (NFRA Art. 29).")
    level, total, knockout = score(answers)
    prev = client.current_assessment.level if client.current_assessment else None
    client.assessments.append(Assessment(level=level, at=now))
    client.reported_change = False
    client.level_cap = None  # a fresh assessment resolves integrity flags
    client.profile_flags = []
    return ProfileResult(level, total, knockout, prev if prev is not None and prev != level else None)
