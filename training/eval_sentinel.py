"""Evaluate signal detectors on the held-out sentinel test set.

Run from backend/ so the pattern layer is importable:
    cd backend && uv run python ../training/eval_sentinel.py --models qwen3.5:9b qwen3.5:2b pca-sentinel
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import httpx

from pca.integrity import pattern_signals
from pca.speech import check_guarantees

HERE = Path(__file__).parent
LABELS = {
    "client_signals": ["loss_averse", "dependent_funds", "short_horizon", "life_change"],
    "advisor_claims": ["guarantee", "deposit_confusion", "exaggeration", "pressure"],
}


def regex_labels(task: str, text: str) -> list[str]:
    if task == "client_signals":
        return sorted({s.kind for s in pattern_signals(text)})
    out = set()
    if check_guarantees(text):
        out.add("guarantee")
    # deposit confusion in speech.py needs a named catalogue product; the bare regex can't see it here
    return sorted(out)


def gpu_temp() -> int:
    r = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True).stdout.strip()
    return int(r or 0)


def model_labels(http: httpx.Client, model: str, row: dict) -> list[str]:
    while gpu_temp() >= 80:
        time.sleep(10)
    msgs = row["messages"][:2]
    body = {"model": model, "messages": msgs, "stream": False, "think": False, "options": {"temperature": 0},
            "format": {"type": "object", "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
                       "required": ["labels"]}}
    r = http.post("http://localhost:11434/api/chat", json=body, timeout=180)
    r.raise_for_status()
    try:
        labels = json.loads(r.json()["message"]["content"]).get("labels", [])
    except ValueError:
        return ["<unparseable>"]
    return sorted({l for l in labels if isinstance(l, str)})


def score(rows: list[dict], preds: list[list[str]]) -> dict:
    per = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    exact = 0
    for row, pred in zip(rows, preds):
        gold, p = set(row["labels"]), set(pred) & set(LABELS[row["task"]])
        exact += gold == set(pred)
        for l in LABELS[row["task"]]:
            per[l]["tp"] += l in gold and l in p
            per[l]["fp"] += l not in gold and l in p
            per[l]["fn"] += l in gold and l not in p
    out = {}
    for l, c in per.items():
        prec = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
        rec = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
        out[l] = {"precision": round(prec, 3), "recall": round(rec, 3),
                  "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0}
    tp = sum(c["tp"] for c in per.values())
    fp = sum(c["fp"] for c in per.values())
    fn = sum(c["fn"] for c in per.values())
    mp, mr = tp / (tp + fp) if tp + fp else 0, tp / (tp + fn) if tp + fn else 0
    return {"exact_match": round(exact / len(rows), 3), "micro_precision": round(mp, 3), "micro_recall": round(mr, 3),
            "micro_f1": round(2 * mp * mr / (mp + mr), 3) if mp + mr else 0.0, "per_label": out}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data" / "test.jsonl"))
    ap.add_argument("--models", nargs="*", default=[])
    ap.add_argument("--out", default=str(HERE / "results" / "sentinel_eval.json"))
    a = ap.parse_args()
    rows = [json.loads(l) for l in Path(a.data).read_text(encoding="utf-8").splitlines() if l.strip()]
    results = {"n": len(rows), "by_task": {t: sum(1 for r in rows if r["task"] == t) for t in LABELS}, "systems": {}}

    t = time.perf_counter()
    preds = [regex_labels(r["task"], r["text"]) for r in rows]
    results["systems"]["regex"] = {**score(rows, preds), "ms_per_item": round((time.perf_counter() - t) * 1000 / len(rows), 2)}

    with httpx.Client() as http:
        for m in a.models:
            model_labels(http, m, rows[0])  # warm-up / load
            t = time.perf_counter()
            preds = [model_labels(http, m, r) for r in rows]
            results["systems"][m] = {**score(rows, preds),
                                     "ms_per_item": round((time.perf_counter() - t) * 1000 / len(rows), 1),
                                     "errors": [{"text": r["text"], "gold": r["labels"], "pred": p}
                                                for r, p in zip(rows, preds) if set(p) != set(r["labels"])][:25]}
            print(m, {k: v for k, v in results["systems"][m].items() if k not in ("per_label", "errors")}, flush=True)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for name, s in results["systems"].items():
        print(f"{name:20s} exact={s['exact_match']:.3f} microF1={s['micro_f1']:.3f} "
              f"P={s['micro_precision']:.3f} R={s['micro_recall']:.3f} {s['ms_per_item']}ms/item")


if __name__ == "__main__":
    main()
