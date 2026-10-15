from datetime import timedelta

import pytest

from pca.models import Client
from pca.profile import QUESTIONS, AssessmentLimitError, assess, score

from test_verifier import NOW


def answers(i: int) -> dict[str, int]:
    return {q.id: min(i, len(q.options) - 1) for q in QUESTIONS}


def test_most_conservative_answers_give_c1():
    assert score(answers(0))[0] == 1


def test_most_aggressive_answers_give_c5():
    ans = answers(3)
    ans["age"] = 1  # 30–49 scores highest
    assert score(ans)[0] == 5


def test_levels_are_monotone_in_score():
    levels = [score(answers(i))[0] for i in range(4)]
    assert levels == sorted(levels)


def test_no_loss_knockout_forces_c1():
    ans = answers(3)
    ans["loss"] = 0
    level, _, knockout = score(ans)
    assert knockout and level == 1


def test_missing_answers_rejected():
    with pytest.raises(ValueError):
        score({"age": 1})


def test_frequency_limit_per_day():
    c = Client(id="x", age=40, investable_assets=100_000)
    assess(c, answers(2), NOW)
    assess(c, answers(2), NOW + timedelta(hours=1))
    with pytest.raises(AssessmentLimitError):
        assess(c, answers(3), NOW + timedelta(hours=2))


def test_frequency_limit_per_year():
    c = Client(id="x", age=40, investable_assets=100_000)
    for d in range(8):
        assess(c, answers(2), NOW + timedelta(days=30 * d))
    with pytest.raises(AssessmentLimitError):
        assess(c, answers(2), NOW + timedelta(days=30 * 8 - 1))


def test_level_change_is_reported():
    c = Client(id="x", age=40, investable_assets=100_000)
    assess(c, answers(1), NOW)
    r = assess(c, answers(3), NOW + timedelta(days=60))
    assert r.changed_from is not None and r.changed_from != r.level


def test_loss_tolerance_caps_level():
    ans = answers(3)
    ans["age"] = 1
    ans["loss"] = 1  # up to 5%
    assert score(ans)[0] == 2
    ans["loss"] = 2  # up to 20%
    assert score(ans)[0] == 4


def test_panic_selling_caps_level():
    ans = answers(3)
    ans["age"] = 1
    ans["reaction"] = 0  # sell everything after a 20% fall
    assert score(ans)[0] == 2
