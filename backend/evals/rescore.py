"""Re-score captured advisor drafts with the current deterministic checks (no GPU, no model).

Allocation findings are deterministic and stored with each draft; speech findings are
recomputed from the stored message text, so pattern fixes can be measured on exactly
the same model outputs.

    uv run python evals/rescore.py evals/results/advisor_qwen3.5_9b_sessions.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pca.catalog import load_catalog
from pca.models import Market
from pca.speech import check_message

CAT = {k: v for k, v in load_catalog().items() if v.market == Market.CN}


def main() -> None:
    path = Path(sys.argv[1])
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if "error" not in r]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    rules = Counter()
    for r in rows:
        first = r["drafts_detail"][0]
        speech = check_message(first["message"], CAT, set(first["purchases"]), r["level"]) if first["message"] else []
        found = {v["rule"] for v in first["violations"]} | {x.rule_id for x in speech}
        if first["error"]:
            found.add("FORMAT")
        rules.update(found)
        by_cat[r["category"]].append({"bad": bool(found), "alloc": bool(first["violations"]),
                                      "speech": bool(speech), "uncert_only": found == {"SP-UNCERT"}})
    n = len(rows)
    allv = [x for v in by_cat.values() for x in v]
    out = {
        "sessions": n,
        "first_draft_noncompliant_deterministic": round(sum(x["bad"] for x in allv) / n, 3),
        "first_draft_allocation_violation": round(sum(x["alloc"] for x in allv) / n, 3),
        "first_draft_speech_violation": round(sum(x["speech"] for x in allv) / n, 3),
        "first_draft_only_uncertified_recommendation": round(sum(x["uncert_only"] for x in allv) / n, 3),
        "delivered_noncompliant": sum(not r["delivered_ok"] for r in rows),
        "rules": rules.most_common(),
        "by_category": {c: {"n": len(v), "noncompliant": round(sum(x["bad"] for x in v) / len(v), 3),
                            "allocation": round(sum(x["alloc"] for x in v) / len(v), 3),
                            "speech": round(sum(x["speech"] for x in v) / len(v), 3)} for c, v in by_cat.items()},
    }
    dest = path.with_name(path.stem.replace("_sessions", "") + "_rescored.json")
    dest.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
