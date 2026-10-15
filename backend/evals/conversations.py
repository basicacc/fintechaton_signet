"""Does the advisor follow a conversation? Multi-turn chats, English and Chinese, with checkable expectations.

    uv run python evals/conversations.py --url http://127.0.0.1:8078 --out evals/results/conversations.json

Single messages are covered by messy_language.py; this is about follow-ups: "only X", "what else?", "why those?",
"make it safer", "make it 100k", "drop the bonds", "didn't like it". Each conversation starts from a reset demo
state. Run against a separate data directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rehearse as R  # noqa: E402  (API client and thermal pacing)

FI90, CASH, BOND, DEP12, CSI300, MMF = "CN-WM-FI90", "CN-WM-CASH", "CN-BOND-GOV", "CN-DEP-12M", "CN-FUND-300", "HK-FUND-MMF"

# id, client, [(message, expectations), ...]
#   plan: a plan was given · question: no new plan needed (none, or the same one) · max/min: total bounds
#   only: every product is in this set · includes / exclude · differs: not the previous turn's plan · lang
CONVERSATIONS = [
    ("en-only", "chen-mei", [
        ("I have 200k to invest for 3 years. What do you suggest?", {"plan": True, "max": 200_000, "lang": "en"}),
        ("lets only do steady income 90 day", {"only": [FI90], "max": 200_000, "min": 150_000, "lang": "en"}),
    ]),
    ("en-only-capped", "chen-mei", [
        ("put 300k only in steady income 90-day", {"only": [FI90], "max": 300_000, "lang": "en"}),
    ]),
    ("en-what-else", "chen-mei", [
        ("help me invest 150k, low risk please", {"plan": True, "max": 150_000}),
        ("what else would you suggest", {"plan": True, "differs": True, "max": 150_000}),
        ("nah didnt like it, something better?", {"plan": True, "differs": True, "max": 150_000, "lang": "en"}),
    ]),
    ("en-why", "chen-mei", [
        ("invest 100k for me", {"plan": True, "max": 100_000}),
        ("why did you pick those?", {"question": True, "lang": "en"}),
        ("ok make it safer", {"plan": True, "max": 100_000}),
    ]),
    ("en-change-amount", "wong-ka-yan", [
        ("i want to invest 300k, some growth is fine", {"plan": True, "max": 300_000}),
        ("actually make it 100k", {"plan": True, "max": 100_000, "min": 50_000}),
    ]),
    ("en-drop", "wong-ka-yan", [
        ("invest 400k for me, balanced", {"plan": True, "max": 400_000}),
        ("drop the government bond fund, i dont like bonds", {"plan": True, "exclude": [BOND], "max": 400_000}),
    ]),
    ("en-add", "wong-ka-yan", [
        ("invest 300k for me", {"plan": True, "max": 300_000}),
        ("can you add some csi300 index fund to it", {"plan": True, "includes": [CSI300], "max": 300_000}),
    ]),
    ("en-question-then-buy", "chen-mei", [
        ("is the government bond fund safe?", {"question": True, "lang": "en"}),
        ("ok put 50k in it", {"only": [BOND], "max": 50_000, "min": 40_000}),
    ]),
    ("en-list-options", "chen-mei", [
        ("can you list all r1 options possible", {"question": True, "lang": "en"}),
        ("ok put 100k in the daily cash one", {"only": [CASH], "max": 100_000, "min": 80_000, "lang": "en"}),
    ]),
    ("zh-list-options", "chen-mei", [
        ("有哪些R1的产品可以买？", {"question": True, "lang": "zh"}),
        ("那就买10万天天现金", {"only": [CASH], "max": 100_000, "min": 80_000, "lang": "zh"}),
    ]),
    ("zh-question-then-buy", "chen-mei", [
        ("国债基金安全吗？", {"question": True, "lang": "zh"}),
        ("那就放5万进去", {"only": [BOND], "max": 50_000, "min": 40_000, "lang": "zh"}),
    ]),
    ("zh-only", "chen-mei", [
        ("我有20万想投资三年，有什么建议", {"plan": True, "max": 200_000, "lang": "zh"}),
        ("那就只买稳健收益90天吧", {"only": [FI90], "max": 200_000, "min": 150_000, "lang": "zh"}),
    ]),
    ("zh-only-messy", "chen-mei", [
        ("就要那个天天现金的 十万 别的不要", {"only": [CASH], "max": 100_000, "min": 50_000, "lang": "zh"}),
    ]),
    ("zh-what-else", "chen-mei", [
        ("帮我投15万，低风险的", {"plan": True, "max": 150_000, "lang": "zh"}),
        ("还有别的推荐吗", {"plan": True, "differs": True, "max": 150_000, "lang": "zh"}),
        ("不太喜欢，换一个", {"plan": True, "differs": True, "max": 150_000, "lang": "zh"}),
    ]),
    ("zh-why", "chen-mei", [
        ("帮我投10万", {"plan": True, "max": 100_000, "lang": "zh"}),
        ("为什么选这些？", {"question": True, "lang": "zh"}),
        ("再保守一点", {"plan": True, "max": 100_000, "lang": "zh"}),
    ]),
    ("zh-change-amount", "wong-ka-yan", [
        ("我想投30万，可以承受一些波动", {"plan": True, "max": 300_000, "lang": "zh"}),
        ("算了还是投10万吧", {"plan": True, "max": 100_000, "min": 50_000, "lang": "zh"}),
    ]),
    ("zh-drop", "wong-ka-yan", [
        ("帮我配置40万，稳健一点", {"plan": True, "max": 400_000, "lang": "zh"}),
        ("国债基金不要了，我不喜欢债券", {"plan": True, "exclude": [BOND], "max": 400_000, "lang": "zh"}),
    ]),
    ("zh-traditional-only", "wong-ka-yan", [
        ("我想投資20萬，穩健一點", {"plan": True, "max": 200_000, "lang": "zh"}),
        ("只要港元貨幣基金就好", {"only": [MMF], "max": 200_000, "lang": "zh"}),
    ]),
    ("zh-safer-12m", "chen-mei", [
        ("给我推荐一下，二十万左右", {"plan": True, "max": 200_000, "lang": "zh"}),
        ("全部放一年定存可以吗", {"only": [DEP12], "max": 200_000, "min": 150_000, "lang": "zh"}),
    ]),
]


def lang_of(text: str) -> str:
    return "zh" if len(re.findall(r"[一-鿿]", text)) >= 6 else "en"


def judge(final: dict, exp: dict, previous: dict[str, int]) -> dict[str, bool]:
    plan = final["purchases"]
    total = sum(plan.values())
    out = {"compliant": final["ok"]}
    if exp.get("plan") or exp.get("only") or exp.get("includes"):
        out["gave a plan"] = bool(plan)
    if exp.get("question"):
        out["answered without a new plan"] = not plan or plan == previous
    if "max" in exp:
        out["within the amount"] = total <= exp["max"] * 1.02
    if "min" in exp:
        out["used the amount"] = total >= exp["min"]
    if "only" in exp:
        out["only what was asked"] = bool(plan) and set(plan) <= set(exp["only"])
    for pid in exp.get("includes", []):
        out[f"has {pid}"] = pid in plan
    for pid in exp.get("exclude", []):
        out[f"no {pid}"] = pid not in plan
    if exp.get("differs"):
        out["different from before"] = bool(plan) and plan != previous
    if "lang" in exp:
        out["reply language"] = lang_of(final["message"]) == exp["lang"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8078")
    ap.add_argument("--out", default="evals/results/conversations.json")
    ap.add_argument("--only", default="", help="comma-separated conversation ids")
    a = ap.parse_args()
    api = R.Api(a.url)
    pick = set(filter(None, a.only.split(",")))
    rows = []
    for cid, client, turns in CONVERSATIONS:
        if pick and cid not in pick:
            continue
        api.post("/api/admin/reset", {})
        previous: dict[str, int] = {}
        for n, (msg, exp) in enumerate(turns, 1):
            R.cool_down(70)
            r = api.advise(client, msg)
            checks = judge(r["final"], exp, previous)
            und = next((x.get("understood") for x in r["attempts"] if x.get("understood")), None)
            rows.append({"id": cid, "turn": n, "client": client, "message": msg, "checks": checks,
                         "understood": und, "purchases": r["final"]["purchases"], "reply": r["final"]["message"],
                         "drafts": [[x["source"], x["ok"], x.get("sent_back"),
                                     [v["rule_id"] for v in x["violations"]] + [s["rule_id"] for s in x["speech"]]]
                                    for x in r["attempts"]], "seconds": r["seconds"]})
            bad = [k for k, v in checks.items() if not v]
            print(f"{cid:20s} {n}  {'PASS' if not bad else 'FAIL ' + ', '.join(bad)}  {r['final']['purchases']}"
                  f"  understood={und}", flush=True)
            if r["final"]["purchases"]:
                previous = r["final"]["purchases"]
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    n = sum(len(x["checks"]) for x in rows)
    ok = sum(sum(x["checks"].values()) for x in rows)
    convs = {x["id"] for x in rows}
    full = sum(all(all(y["checks"].values()) for y in rows if y["id"] == c) for c in convs)
    print(f"\n{sum(all(x['checks'].values()) for x in rows)}/{len(rows)} turns fully right; "
          f"{full}/{len(convs)} conversations fully right; {ok}/{n} checks passed")


if __name__ == "__main__":
    main()
