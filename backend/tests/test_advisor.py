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
    assert sum(res.final.purchases.values()) == 200_000  # the 200k the client wrote, not the draft's 150k


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
    res, _ = run([proposal({"CRYPTO-MOON": 150_000}), proposal({"CN-WM-FI90": 50_000, "CN-WM-CASH": 100_000})])
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


def test_asking_for_something_different_still_wants_a_plan():
    from pca.advisor import wants_alternative
    h = [{"client": "x", "advisor": "y", "plan": {"CN-WM-CASH": 100_000}}]
    for ask in ("i didnt like these maybe better choice", "what else would you suggest", "any other options?",
                "不喜欢这个，换一个吧"):
        assert wants_alternative(ask, h), ask
    assert not wants_alternative("Why did you pick those?", h)
    assert not wants_alternative("what else would you suggest", [])  # nothing to be different from yet


def test_words_only_reply_to_a_request_for_alternatives_is_sent_back():
    # the reply that prompted this: products named in the text, nothing in the plan
    words = {"purchases": [], "message": "Understood. Something different could suit you. Returns are not "
                                         "guaranteed and principal can be lost."}
    llm = ScriptedLLM([words, proposal({"CN-BOND-GOV": 80_000, "CN-WM-FI90": 60_000})])
    h = [{"client": "Invest 200k", "advisor": "Daily Cash Wealth Product", "plan": {"CN-WM-CASH": 200_000}}]
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "i didnt like these maybe better choice", NOW, history=h)
    assert res.attempts[0].sent_back and res.final.purchases and res.final.ok


def test_explaining_the_plan_already_given_is_not_an_uncertified_recommendation():
    explain = {"purchases": [], "message": "China Government Bond Index Fund (R2) suits your C2 level and "
                                           "your horizon. Principal can be lost."}
    llm = ScriptedLLM([explain])
    h = [{"client": "Invest 100k", "advisor": "…", "plan": {"CN-BOND-GOV": 100_000}}]
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "Why did you pick those?", NOW, history=h)
    assert res.first_pass_ok and res.final.purchases == {}


def test_amount_named_earlier_carries_over_to_follow_ups():
    from pca.advisor import conversation_amount
    h = [{"client": "I have 200k to invest for 3 years", "advisor": "…", "plan": {"CN-WM-CASH": 200_000}}]
    assert conversation_amount("what else would you suggest", h) == 200_000
    assert conversation_amount("make it safer", h) == 200_000
    assert conversation_amount("actually invest 300k", h) == 300_000  # a new amount wins
    assert conversation_amount("Why did you pick those?", h) is None  # a question, not a new plan
    h2 = [{"client": "help me invest", "advisor": "…", "plan": {"CN-WM-CASH": 90_000, "CN-WM-FI90": 60_000}}]
    assert conversation_amount("any other options?", h2) == 150_000  # no amount ever named: the last plan's total


def test_follow_up_plan_is_held_to_the_amount_from_earlier():
    big = proposal({"CN-WM-CASH": 400_000})  # compliant, but twice what the client said
    llm = ScriptedLLM([big, proposal({"CN-WM-CASH": 100_000, "CN-BOND-GOV": 60_000})])
    h = [{"client": "I have 200k to invest", "advisor": "…", "plan": {"CN-DEP-01": 200_000}}]
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "what else would you suggest", NOW, history=h)
    assert "asked for ¥200,000" in res.attempts[0].sent_back
    assert sum(res.final.purchases.values()) <= 200_000
    assert "Amount to invest: ¥200,000" in llm.calls[0][-1]["content"]


def test_a_different_plan_must_actually_be_different():
    same = proposal({"CN-DEP-01": 50_000, "CN-WM-CASH": 80_000, "CN-BOND-GOV": 70_000})
    other = proposal({"CN-WM-FI90": 60_000, "CN-WM-CASH": 80_000, "CN-BOND-GOV": 60_000})
    llm = ScriptedLLM([same, other])
    h = [{"client": "I have 200k", "advisor": "…", "plan": {"CN-DEP-01": 50_000, "CN-WM-CASH": 80_000,
                                                           "CN-BOND-GOV": 70_000}}]
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "i didnt like these maybe better choice", NOW, history=h)
    assert "just turned down" in res.attempts[0].sent_back
    assert res.final.purchases != h[0]["plan"] and res.final.ok


def test_history_names_the_current_plan_with_risk_levels():
    from pca.advisor import history_block
    h = [{"client": "a", "advisor": "b", "plan": {"CN-WM-FI90": 100_000}},
         {"client": "make it safer", "advisor": "c", "plan": {"CN-DEP-12M": 100_000}}]
    block = history_block(h, CAT)
    assert "current plan" in block and f"{CAT['CN-DEP-12M'].name} (R{CAT['CN-DEP-12M'].effective_risk})" in block


# ------------------------------------------------ the model states what it understood; the code checks against it

def understood(kind="new", amount=None, products=(), only=False) -> dict:
    return {"kind": kind, "amount": amount, "products": list(products), "only": only}


def with_u(reply: dict, **u) -> dict:
    return {"understood": understood(**u), **reply}


def test_only_this_product_means_nothing_else_is_added():
    # the screenshot: "lets only do steady income 90 day" got Daily Cash added that nobody asked for
    u = dict(kind="change", amount=100_000, products=["CN-WM-FI90"], only=True)
    llm = ScriptedLLM([with_u(proposal({"CN-WM-CASH": 60_000, "CN-WM-FI90": 40_000}), **u),
                       with_u(proposal({"CN-WM-FI90": 100_000}), **u)])
    h = [{"client": "I have 100k", "advisor": "…", "plan": {"CN-DEP-12M": 100_000}}]
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "lets only do steady income 90 day", NOW, history=h)
    assert "only Steady Income 90-Day" in res.attempts[0].sent_back
    assert res.final.purchases == {"CN-WM-FI90": 100_000}


def test_only_a_capped_product_gets_the_most_allowed_not_padding():
    from pca.verifier import max_amount
    from pca.models import Proposal
    c = client(level=2)
    cap = max_amount(c, CAT, Proposal(client_id=c.id, purchases={}, as_of=NOW), CN, "CN-WM-FI90")
    want = cap + 100_000  # more than the rules allow in one R2 product
    u = dict(kind="change", amount=want, products=["CN-WM-FI90"], only=True)
    too_much = with_u(proposal({"CN-WM-FI90": want}), **u)
    res = Advisor(ScriptedLLM([too_much] * 3 + [{"message": ""}]), CAT, CN).recommend(
        c, "only steady income 90 day please", NOW)
    assert set(res.final.purchases) == {"CN-WM-FI90"} and res.final.purchases["CN-WM-FI90"] <= cap
    assert res.final.ok


def test_products_come_from_the_models_reading_not_text_matching():
    # "that steady 90 one" is no name or alias in the catalogue; the model picks the id from the enum
    u = dict(kind="new", amount=50_000, products=["CN-WM-FI90"], only=False)
    llm = ScriptedLLM([with_u(proposal({"CN-WM-CASH": 50_000}), **u),
                       with_u(proposal({"CN-WM-FI90": 30_000, "CN-WM-CASH": 20_000}), **u)])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "want some of that steady 90 one, 50k", NOW)
    assert "leaves it out" in res.attempts[0].sent_back and res.final.purchases.get("CN-WM-FI90")


def test_a_question_about_a_product_needs_no_plan():
    u = dict(kind="question", amount=None, products=["CN-WM-FI90"], only=False)
    reply = with_u({"purchases": [], "message": "Steady Income 90-Day is rated R2: low risk, but principal can "
                                                "be lost."}, **u)
    res, _ = run([reply], request="is steady income 90 day safe?")
    assert res.first_pass_ok and res.final.purchases == {}


def test_malformed_understanding_falls_back_to_patterns():
    from pca.advisor import resolve_intent
    c = client(level=2)
    h = [{"client": "I have 200k", "advisor": "…", "plan": {"CN-DEP-12M": 200_000}}]
    bad = {"kind": "whatever", "amount": "lots", "products": ["NOT-A-PRODUCT"], "only": True}
    from pca.advisor import _understood
    assert _understood(bad, CAT) is None
    i = resolve_intent("lets only do steady income 90 day", h, None, CAT, c)
    assert i.source == "patterns" and i.products == ["CN-WM-FI90"] and i.only and i.amount == 200_000


def test_name_matching_ignores_hyphens_and_spaces():
    from pca.advisor import intent_from_request
    for text in ("steady income 90 day", "Steady-Income-90-Day", "steadyincome90day please"):
        assert "CN-WM-FI90" in intent_from_request(text, CAT, client()), text


def test_an_amount_the_client_wrote_beats_the_models_guess():
    from pca.advisor import resolve_intent
    h = [{"client": "I have 200k to invest for 3 years", "advisor": "…", "plan": {"CN-WM-CASH": 200_000}}]
    guess = understood(kind="change", amount=500_000, products=["CN-WM-FI90"], only=True)  # its whole balance
    assert resolve_intent("lets only do steady income 90 day", h, guess, CAT, client()).amount == 200_000
    assert resolve_intent("make it 100k", h, guess, CAT, client()).amount == 100_000  # a new amount wins
    assert resolve_intent("put everything in", [], understood(amount=300_000), CAT, client()).amount == 300_000


def test_only_x_is_kept_even_if_the_model_swaps_products_after_a_rule_cut():
    from pca.verifier import max_amount
    from pca.models import Proposal
    c = client(level=2)
    cap = max_amount(c, CAT, Proposal(client_id=c.id, purchases={}, as_of=NOW), CN, "CN-WM-FI90")
    u = dict(kind="change", amount=cap + 100_000, products=["CN-WM-FI90"], only=True)
    swapped = with_u(proposal({"CN-DEP-12M": cap + 100_000}), **u)
    llm = ScriptedLLM([with_u(proposal({"CN-WM-FI90": cap + 100_000}), **u), swapped, swapped, {"message": ""}])
    res = Advisor(llm, CAT, CN).recommend(c, "put it only in steady income 90-day", NOW)
    assert "keep only" in llm.calls[1][-1]["content"]  # the rule feedback says: lower it, don't switch
    assert set(res.final.purchases) == {"CN-WM-FI90"} and res.final.ok


def test_a_product_the_client_dropped_stays_out():
    u = dict(kind="change", amount=200_000, products=[], only=False)
    keep = {"understood": {**understood(**u), "exclude": ["CN-BOND-GOV"]}}
    llm = ScriptedLLM([{**keep, **proposal({"CN-BOND-GOV": 60_000, "CN-WM-FI90": 60_000, "CN-WM-CASH": 80_000})},
                       {**keep, **proposal({"CN-WM-FI90": 60_000, "CN-WM-CASH": 140_000})}])
    h = [{"client": "invest 200k", "advisor": "…", "plan": {"CN-BOND-GOV": 60_000, "CN-WM-FI90": 60_000,
                                                            "CN-WM-CASH": 80_000}}]
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "drop the government bond fund", NOW, history=h)
    assert "don't want China Government Bond Index Fund" in res.attempts[0].sent_back
    assert "CN-BOND-GOV" not in res.final.purchases and res.final.ok


def test_a_question_never_turns_into_a_sale():
    u = dict(kind="question", amount=None, products=["CN-BOND-GOV"], only=False)
    sells = with_u(proposal({"CN-BOND-GOV": 100_000}), **u)
    answer = with_u({"purchases": [], "message": "China Government Bond Index Fund is rated R2: low risk, but "
                                                 "principal can be lost."}, **u)
    res, _ = run([sells, answer], request="is the government bond fund safe?")
    assert "asked a question" in res.attempts[0].sent_back and res.final.purchases == {}
    # and if every draft fails, the solver doesn't invent a purchase either
    bad = with_u({"purchases": [], "message": "It is guaranteed and risk-free."}, **u)
    res, _ = run([bad, bad, bad, {"message": "China Government Bond Index Fund is rated R2. Principal can be lost."}],
                 request="is the government bond fund safe?")
    assert res.final.purchases == {} and res.final.ok


def test_a_plan_far_below_the_written_amount_is_sent_back_once():
    # the 9B reads 八十万 (800,000) as 80,000; the number is parsed exactly and the plan held to it
    small = proposal({"CN-WM-CASH": 20_000, "CN-WM-FI90": 30_000, "CN-BOND-GOV": 30_000})
    right = proposal({"CN-WM-CASH": 100_000, "CN-WM-FI90": 100_000, "CN-BOND-GOV": 100_000})
    llm = ScriptedLLM([small, right])
    res = Advisor(llm, CAT, CN).recommend(client(level=2), "invest 300k please", NOW)
    assert "but the client wrote ¥300,000" in res.attempts[0].sent_back
    assert sum(res.final.purchases.values()) == 300_000
    assert "Amount the client wrote: ¥300,000" in llm.calls[0][-1]["content"]


def test_history_says_what_a_question_turn_was_about():
    from pca.advisor import history_block
    h = [{"client": "is the government bond fund safe?", "advisor": "It is rated R2…", "plan": {},
          "about": ["CN-BOND-GOV"]}]
    assert "This turn was about: China Government Bond Index Fund" in history_block(h, CAT)


def test_a_failed_answer_to_a_question_falls_back_to_facts_not_false_refusals():
    from pca.advisor import question_message
    u = dict(kind="question", amount=None, products=["CN-BOND-GOV"], only=False)
    bad = with_u({"purchases": [], "message": "It is guaranteed and risk-free."}, **u)
    res, llm = run([bad, bad, bad, {"message": "Totally risk-free!"}], request="is the government bond fund safe?")
    msg = res.final.message
    assert res.final.ok and res.final.purchases == {}
    assert "doesn't meet the rules" not in msg and "fits your risk level (C2)" in msg and "R2" in msg
    assert "answer it, keep purchases empty" in llm.calls[1][-1]["content"]
    zh = question_message(["CN-BOND-GOV"], CAT, 2, "zh", {"CN-BOND-GOV": True})
    assert "符合您的风险等级（C2）" in zh and "本金可能亏损" in zh
    for m in (msg, zh):
        assert not check_message(m, CAT, {"CN-BOND-GOV"}, 2)


def test_listing_the_products_a_question_asks_about_is_not_a_recommendation():
    r1 = ["CN-DEP-01", "CN-DEP-12M", "CN-WM-CASH"]
    u = dict(kind="question", amount=None, products=r1, only=False)
    reply = with_u({"purchases": [], "message": "The R1 options I could recommend are Demand Deposit Plus, 12-Month "
                                                "Time Deposit and Daily Cash Wealth Product. Wealth products are not "
                                                "deposits; principal can be lost."}, **u)
    res, _ = run([reply], request="can you list all r1 options possible")
    assert res.first_pass_ok and res.final.purchases == {}
