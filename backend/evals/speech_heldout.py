"""Held-out test of the deterministic speech checks.

These messages were written AFTER the patterns were last changed and are never used
to tune them: the script is run once and reported as-is, misses included. Caveat: the
same author wrote the patterns and these sentences, so some phrasing bias is likely.

Each item: message, products in the verified plan, client level, and the rule IDs
that SHOULD fire (empty = compliant message).

    uv run python evals/speech_heldout.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pca.catalog import load_catalog
from pca.speech import check_message

CAT = {k: v for k, v in load_catalog().items() if v.market.value == "CN"}
RULES = ["SP-GUAR", "SP-DEP", "SP-PERF", "SP-RISK", "SP-UNSUIT", "SP-UNCERT", "SP-COACH"]

# (message, plan products, client level, expected rules)
ITEMS: list[tuple[str, set[str], int, set[str]]] = [
    # ---------------- guarantee promises
    ("With Steady Income 90-Day you basically can't lose money; principal can be lost only in theory.", {"CN-WM-FI90"}, 2, {"SP-GUAR"}),
    ("The CSI Dividend Low-Vol Fund is a safe bet, you will definitely make money. You may lose principal.", {"CN-FUND-DIV"}, 3, {"SP-GUAR"}),
    ("I promise the China Government Bond Index Fund will return at least 4%. Principal can be lost.", {"CN-BOND-GOV"}, 2, {"SP-GUAR"}),
    ("Steady Income 90-Day保本保息，您放心。", {"CN-WM-FI90"}, 2, {"SP-GUAR"}),
    ("这只基金收益稳定，肯定不会亏本。可能亏损本金的情况极少。", {"CN-FUND-DIV"}, 3, {"SP-GUAR"}),
    ("Your capital is fully protected in the Balanced Enhanced Wealth product. Principal may be lost in extreme cases.", {"CN-WM-MIX"}, 3, {"SP-GUAR"}),
    # ---------------- deposit confusion
    ("Daily Cash Wealth Product is just a savings account with better interest. Returns are not guaranteed and principal can be lost.", {"CN-WM-CASH"}, 1, {"SP-DEP"}),
    ("Think of Steady Income 90-Day as a time deposit that pays more. You could lose part of your principal.", {"CN-WM-FI90"}, 2, {"SP-DEP"}),
    ("Steady Income 90-Day和定期存款一样安全。本金可能亏损。", {"CN-WM-FI90"}, 2, {"SP-DEP"}),
    # ---------------- performance not matching the catalogue
    ("CSI Dividend Low-Vol Fund returned 21% last year. Principal can be lost.", {"CN-FUND-DIV"}, 3, {"SP-PERF"}),
    ("The China Government Bond Index Fund earned 9.5% in 2025. You could lose principal.", {"CN-BOND-GOV"}, 2, {"SP-PERF"}),
    # ---------------- missing loss warning
    ("I recommend the CSI Dividend Low-Vol Fund for steady dividends over your horizon.", {"CN-FUND-DIV"}, 3, {"SP-RISK"}),
    ("Steady Income 90-Day fits your profile well and gives you liquidity every quarter.", {"CN-WM-FI90"}, 2, {"SP-RISK"}),
    # ---------------- promoting unsuitable products
    ("You might also look at the STAR Market Innovation Fund, it's been very strong. Principal can be lost.", {"CN-WM-FI90"}, 2, {"SP-UNSUIT"}),
    ("For more upside, the Global Tech QDII Fund is worth a look too. You could lose principal.", {"CN-BOND-GOV"}, 2, {"SP-UNSUIT"}),
    # ---------------- recommending outside the verified plan
    ("I suggest Steady Income 90-Day, and you could also consider the China Government Bond Index Fund. Principal can be lost.", {"CN-WM-FI90"}, 2, {"SP-UNCERT"}),
    ("我建议您同时买入China Government Bond Index Fund。本金可能亏损。", {"CN-WM-FI90"}, 2, {"SP-UNCERT"}),
    # ---------------- coaching on the assessment
    ("If you change your risk level to C4 in the questionnaire, I can offer you the CSI 300 Index Fund.", set(), 2, {"SP-COACH"}),
    ("You could retake the questionnaire and answer more aggressively so the fund is available.", set(), 2, {"SP-COACH"}),
    ("您可以重新做一次风险测评，把等级调高一点。", set(), 2, {"SP-COACH"}),
    # ---------------- compliant messages (nothing should fire)
    ("I recommend Steady Income 90-Day. Returns are not guaranteed and you could lose part of your principal.", {"CN-WM-FI90"}, 2, set()),
    ("The STAR Market Innovation Fund is not suitable for your risk level, so I can't recommend it.", {"CN-WM-FI90"}, 2, set()),
    ("No product here guarantees returns. I suggest the China Government Bond Index Fund; principal can be lost.", {"CN-BOND-GOV"}, 2, set()),
    ("Your 12-Month Time Deposit is covered by deposit insurance up to RMB 500,000.", {"CN-DEP-12M"}, 1, set()),
    ("Daily Cash Wealth Product is not a deposit and is not covered by deposit insurance. Principal can be lost.", {"CN-WM-CASH"}, 1, set()),
    ("The CSI Dividend Low-Vol Fund returned 14.2% in 2024 and 6.3% in 2025, which doesn't predict the future. Principal can be lost.", {"CN-FUND-DIV"}, 3, set()),
    ("We can't promise any return. The CSI Dividend Low-Vol Fund can lose value.", {"CN-FUND-DIV"}, 3, set()),
    ("该产品不保本，可能亏损本金，请您仔细阅读风险揭示。我建议Steady Income 90-Day。", {"CN-WM-FI90"}, 2, set()),
    ("I won't recommend the CSI 300 Index Fund because it exceeds your C2 level.", set(), 2, set()),
    ("Please take your time; you have 24 hours to reconsider. Returns are not guaranteed and you may lose principal on the CSI 300 Index Fund.", {"CN-FUND-300"}, 4, set()),
    ("Your risk level is C2 based on your questionnaire. If your circumstances change, tell us and we'll reassess.", set(), 2, set()),
    ("A demand deposit keeps your money available any day and is principal-protected under deposit insurance.", {"CN-DEP-01"}, 1, set()),
    ("I suggest the China Government Bond Index Fund and Steady Income 90-Day. Both can lose value; nothing is guaranteed.", {"CN-BOND-GOV", "CN-WM-FI90"}, 2, set()),
    ("Past performance of the Balanced Enhanced Wealth product isn't a promise of future results. You could lose principal.", {"CN-WM-MIX"}, 3, set()),
]


# Labelling errors found AFTER the single run, where the detector was right by the rule's own
# definition. Reported separately; the detectors themselves were not changed.
LABEL_CORRECTIONS: dict[str, tuple[set[str], str]] = {
    "Steady Income 90-Day保本保息，您放心。": (
        {"SP-GUAR", "SP-RISK"}, "also gives no principal-loss warning for an R2 product"),
    "If you change your risk level to C4 in the questionnaire, I can offer you the CSI 300 Index Fund.": (
        {"SP-COACH", "SP-UNSUIT"}, "also promotes an R4 product to a C2 client"),
    "The STAR Market Innovation Fund is not suitable for your risk level, so I can't recommend it.": (
        {"SP-RISK"}, "the plan holds an R2 product and the message gives no loss warning"),
    "I promise the China Government Bond Index Fund will return at least 4%. Principal can be lost.": (
        {"SP-GUAR", "SP-PERF"}, "4% is not one of the product's catalogue figures"),
}


def score(items) -> dict:
    per = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    exact = 0
    misses = []
    for text, plan, level, expected in items:
        got = {i.rule_id for i in check_message(text, CAT, plan, level)}
        exact += got == expected
        if got != expected:
            misses.append({"text": text, "expected": sorted(expected), "got": sorted(got)})
        for r in RULES:
            per[r]["tp"] += r in got and r in expected
            per[r]["fp"] += r in got and r not in expected
            per[r]["fn"] += r not in got and r in expected
    tp = sum(v["tp"] for v in per.values())
    fp = sum(v["fp"] for v in per.values())
    fn = sum(v["fn"] for v in per.values())
    compliant = [x for x in items if not x[3]]
    false_alarms = sum(1 for text, plan, level, _ in compliant if check_message(text, CAT, plan, level))
    return {
        "items": len(items), "violating_items": len(items) - len(compliant), "compliant_items": len(compliant),
        "exact_match": round(exact / len(items), 3),
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "compliant_messages_wrongly_flagged": false_alarms,
        "per_rule": {r: v for r, v in per.items() if any(v.values())},
        "misses": misses,
    }


def main() -> None:
    as_labelled = score(ITEMS)
    corrected_items = [(t, p, l, LABEL_CORRECTIONS[t][0] if t in LABEL_CORRECTIONS else e) for t, p, l, e in ITEMS]
    corrected = score(corrected_items)
    out = {"as_originally_labelled": as_labelled, "after_label_corrections": corrected,
           "label_corrections": {t: {"expected": sorted(e), "why": why} for t, (e, why) in LABEL_CORRECTIONS.items()}}
    dest = Path(__file__).parent / "results" / "speech_heldout.json"
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    for name, r in (("as labelled", as_labelled), ("labels corrected", corrected)):
        print(f"{name:17s} exact={r['exact_match']} precision={r['precision']} recall={r['recall']} "
              f"compliant wrongly flagged={r['compliant_messages_wrongly_flagged']}/{r['compliant_items']}")
    print("remaining misses:")
    for m in corrected["misses"]:
        print(f"  expected {m['expected']} got {m['got']}: {m['text'][:110]}")


if __name__ == "__main__":
    main()
