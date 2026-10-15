from pca.advisor import Advisor, template_message
from pca.llm import ScriptedLLM
from pca.speech import check_message

from test_verifier import CAT, CN, NOW, client

def safe_msg(purchases: dict[str, int]) -> str:
    names = " and ".join(CAT[p].name if p in CAT else p for p in purchases)
    return f"I suggest {names}. Returns are not guaranteed and you could lose part of your principal."


def proposal(purchases: dict[str, int], message: str | None = None) -> dict:
    return {"purchases": [{"product_id": p, "amount": a} for p, a in purchases.items()],
            "message": safe_msg(purchases) if message is None else message}


def run(replies, level=2, request="Invest 200k for me"):
    llm = ScriptedLLM(replies)
    res = Advisor(llm, CAT, CN).recommend(client(level=level), request, NOW)
    return res, llm


def test_compliant_first_try():
    res, _ = run([proposal({"CN-WM-FI90": 100_000, "CN-BOND-GOV": 100_000})])
    assert res.first_pass_ok and res.final.source == "llm" and len(res.attempts) == 1


def test_unsuitable_product_is_blocked_then_corrected():
    res, llm = run([
        proposal({"CN-FUND-TECH": 100_000, "CN-WM-FI90": 100_000}),
        proposal({"CN-BOND-GOV": 100_000, "CN-WM-FI90": 100_000}),
    ])
    assert not res.first_pass_ok
    assert res.final.ok and res.final.source == "llm-retry"
    assert "CN-12-1" in llm.calls[1][-1]["content"]  # violation was fed back to the model
    assert res.blocked_requests["CN-FUND-TECH"] == 0


def test_stubborn_model_falls_back_to_solver():
    bad = proposal({"CN-FUND-TECH": 150_000})
    res, _ = run([bad, bad, bad, {"message": "Here is a compliant plan. Returns are not guaranteed and "
                                             "you could lose part of your principal."}])
    assert res.final.source == "solver-fix" and res.final.ok
    assert "CN-FUND-TECH" not in res.final.purchases
    assert sum(res.final.purchases.values()) == 150_000


def test_guarantee_language_is_blocked():
    res, _ = run([
        proposal({"CN-WM-FI90": 100_000}, "Steady Income 90-Day is guaranteed, you can't lose."),
        proposal({"CN-WM-FI90": 100_000}),
    ])
    assert "SP-GUAR" in {s.rule_id for s in res.attempts[0].speech}
    assert res.final.ok


def test_garbage_output_never_reaches_client():
    res, _ = run(["not json", "still not json", "{broken", "also broken"])
    assert res.final.ok and res.final.source == "solver-fix"


def test_hallucinated_product_ids_rejected():
    res, _ = run([proposal({"CRYPTO-MOON": 50_000}), proposal({"CN-WM-FI90": 50_000})])
    assert res.attempts[0].error and res.final.ok


def test_unsafe_explanation_replaced_by_template():
    bad = proposal({"CN-FUND-TECH": 150_000})
    res, _ = run([bad, bad, bad, {"message": "Totally risk-free, guaranteed 10%!"}])
    assert res.final.ok
    assert "not guaranteed" in res.final.message


def test_template_always_passes_speech_checks():
    from itertools import combinations
    ids = list(CAT)
    plans = [[]] + [[p] for p in ids] + [list(c) for c in combinations(ids, 2)] + [list(c) for c in combinations(ids, 3)]
    for level in range(1, 6):
        for pids in plans:
            ok = [p for p in pids if CAT[p].effective_risk <= level]
            purchases = {p: 10_000 for p in ok}
            blocked = {p: "x" for p in ids if p not in ok and p in pids}
            for msg in (template_message(purchases, CAT), template_message(purchases, CAT, level, blocked),
                        template_message(purchases, CAT, level, blocked, "zh"), template_message(purchases, CAT, lang="zh")):
                assert check_message(msg, CAT, set(purchases), level=level) == [], msg


def test_refusal_without_allocation_still_gets_closest_compliant_alternative():
    refuse = {"purchases": [], "message": "I cannot recommend that fund; it is not suitable for you."}
    res, _ = run([refuse, refuse, refuse, {"message": "Here is a suitable alternative. Returns are not "
                                                      "guaranteed and you could lose part of your principal."}],
                 level=3, request="Put 150k into the STAR Market Innovation Fund now")
    assert res.final.source == "solver-fix" and res.final.ok
    assert sum(res.final.purchases.values()) == 150_000
    assert all(CAT[p].effective_risk <= 3 for p in res.final.purchases)


def test_requested_amount_parsing():
    from pca.advisor import requested_amount
    assert requested_amount("put 150k in") == 150_000
    assert requested_amount("invest 30万") == 300_000
    assert requested_amount("RMB 1.2 million please") == 1_200_000
    assert requested_amount("for 3 years") is None


class FakeSentinel:
    """Flags 'only goes up' style claims, like the fine-tuned model does."""

    def claims(self, text):
        from pca.speech import SpeechIssue
        return [SpeechIssue("SP-EXAG", "NFRA Art. 13(2)", "Exaggerates returns (sentinel model)", text)] \
            if "only goes up" in text else []


def test_sentinel_findings_block_drafts_regex_misses():
    llm = ScriptedLLM([proposal({"CN-WM-FI90": 50_000}, "Steady Income 90-Day only goes up. You could lose principal, "
                                                        "returns are not guaranteed."),
                       proposal({"CN-WM-FI90": 50_000})])
    res = Advisor(llm, CAT, CN, sentinel=FakeSentinel()).recommend(client(level=2), "invest 50k", NOW)
    assert "SP-EXAG" in {s.rule_id for s in res.attempts[0].speech}
    assert res.final.ok and res.final.source == "llm-retry"



def test_empty_refusal_of_a_request_to_invest_gets_one_nudge_with_the_computed_room():
    refuse = {"purchases": [], "message": "I can't help with that. Returns are not guaranteed."}
    llm = ScriptedLLM([refuse, proposal({"CN-BOND-GOV": 100_000, "CN-WM-CASH": 50_000})])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "Invest 600k for me in your best funds.", NOW)
    assert res.final.ok and res.final.source == "llm-retry" and res.final.purchases
    nudge = llm.calls[1][-1]["content"]
    assert res.attempts[0].sent_back  # compliant refusal, but the client wants to invest
    assert "does want to invest" in nudge and "C2" in nudge


def test_persistent_refusal_gets_a_default_compliant_plan():
    refuse = {"purchases": [], "message": "No. Returns are not guaranteed."}
    res, _ = run([refuse, refuse, refuse, {"message": "Here is a plan within your level. Returns are not "
                                                      "guaranteed and you could lose part of your principal."}],
                 level=2, request="Invest 300k for me in your best funds.")
    assert res.final.source == "solver-fix" and res.final.ok and res.final.purchases
    assert all(CAT[p].effective_risk <= 2 for p in res.final.purchases)


def test_questions_without_an_investment_request_may_be_answered_without_a_plan():
    answer = {"purchases": [], "message": "R2 means low risk. Returns are not guaranteed."}
    res, _ = run([answer], level=2, request="What does R2 mean?")
    assert res.final.source == "llm" and not res.final.purchases


def test_history_is_in_the_prompt_and_follow_ups_start_from_the_last_plan():
    history = [{"client": "Invest 200k for me", "advisor": "I suggest the government bond fund.",
                "plan": {"CN-BOND-GOV": 200_000}}]
    llm = ScriptedLLM([proposal({"CN-BOND-GOV": 150_000, "CN-WM-CASH": 50_000})])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "Make it safer please", NOW, history=history)
    prompt = llm.calls[0][-1]["content"]
    assert "Conversation so far" in prompt and "Invest 200k for me" in prompt and "Verified plan" in prompt
    assert prompt.index("Conversation so far") < prompt.index("Client request:")
    assert res.final.ok and res.final.purchases


def test_follow_up_detection_needs_a_plan_and_a_modifier():
    from pca.advisor import follow_up_base
    h = [{"client": "x", "advisor": "y", "plan": {"CN-BOND-GOV": 100_000}}]
    assert follow_up_base("make it safer", h) == {"CN-BOND-GOV": 100_000}
    assert follow_up_base("What does R2 mean?", h) == {}
    assert follow_up_base("make it safer", []) == {}


def test_plan_larger_than_the_requested_amount_is_sent_back():
    llm = ScriptedLLM([proposal({"CN-WM-FI90": 150_000, "CN-BOND-GOV": 150_000, "CN-WM-CASH": 100_000}),
                       proposal({"CN-WM-FI90": 100_000, "CN-BOND-GOV": 100_000})])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "I have 200k to invest. What do you suggest?", NOW)
    assert res.final.source == "llm-retry" and sum(res.final.purchases.values()) == 200_000
    assert "asked to invest ¥200,000" in llm.calls[1][-1]["content"]
    assert res.attempts[0].ok and "asked for ¥200,000" in res.attempts[0].sent_back
    assert res.final.sent_back is None


def test_solver_fallback_respects_the_requested_amount():
    big = proposal({"CN-WM-FI90": 150_000, "CN-BOND-GOV": 150_000, "CN-WM-CASH": 100_000})
    res, _ = run([big, big, big, {"message": "Here is a plan within your level. Returns are not "
                                             "guaranteed and you could lose part of your principal."}],
                 level=2, request="Invest 200k for me")
    assert res.final.source == "solver-fix" and res.final.ok
    assert sum(res.final.purchases.values()) <= 200_000


def test_chinese_amounts():
    from pca.advisor import requested_amount
    cases = {"10w全上科创": 100_000, "我想投资20wan": 200_000, "我想投资三百万": 3_000_000, "一百五十万左右": 1_500_000, "拿出个二十来万试试": 200_000,
             "1.5百万": 1_500_000, "五十万块钱": 500_000, "一千二百万": 12_000_000, "投30万": 300_000,
             "我今年三十五岁，想投二十万": 200_000, "三年内要用": None}
    for text, want in cases.items():
        assert requested_amount(text) == want, text


def test_a_chinese_client_gets_a_chinese_reply():
    english = proposal({"CN-WM-FI90": 100_000, "CN-BOND-GOV": 100_000})
    chinese = proposal({"CN-WM-FI90": 100_000, "CN-BOND-GOV": 100_000},
                       "建议配置稳健收益90天和中国国债指数基金。理财产品和基金不是存款，本金可能亏损。")
    llm = ScriptedLLM([english, chinese])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "我想投资二十万，稳健一点", NOW)
    assert "Write the message in Chinese, Simplified characters" in llm.calls[0][-1]["content"]
    assert res.attempts[0].sent_back and res.final.message.startswith("建议")


def test_reply_language_detects_traditional_characters():
    from pca.advisor import reply_language
    assert reply_language("我想將五十萬放喺穩陣啲嘅地方") == "Chinese, Traditional characters"
    assert reply_language("我想把五十万放在稳一点的地方") == "Chinese, Simplified characters"
    assert reply_language("invest 50k please") == "English"


def test_a_compliant_named_request_is_honoured():
    asked = proposal({"CN-WM-FI90": 100_000})
    other = proposal({"CN-BOND-GOV": 50_000, "CN-WM-CASH": 50_000})
    llm = ScriptedLLM([other, asked])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "Put 100k into Steady Income 90-Day please", NOW)
    assert res.attempts[0].sent_back and "Steady Income 90-Day" in res.attempts[0].sent_back
    assert res.final.purchases == {"CN-WM-FI90": 100_000}


def test_solver_starts_from_the_clients_own_compliant_request():
    other = proposal({"CN-BOND-GOV": 400_000})  # breaks the concentration cap every time
    res, _ = run([other, other, other, {"message": "Here is a plan within your level. Returns are not "
                                                   "guaranteed and you could lose part of your principal."}],
                 level=2, request="Put 100k into Steady Income 90-Day please")
    assert res.final.source == "solver-fix" and res.final.purchases == {"CN-WM-FI90": 100_000}
