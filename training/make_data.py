"""Build the sentinel datasets.

Train: training seeds + paraphrases from the local 9B model (and simple
combinations of seeds into multi-label snippets).
Test: test seeds only (+ combinations of test seeds), never paraphrased into train.

    uv run python make_data.py --paraphrases 8 --out data/
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import time
from pathlib import Path

import httpx

from seeds import A_TEST, A_TRAIN, B_TEST, B_TRAIN

SYSTEM = ("You are a compliance sentinel for a bank's wealth advisory service. Classify the text and reply "
          "with JSON only.")
TASK_HINT = {
    "client_signals": "Labels: loss_averse, dependent_funds, short_horizon, life_change. Use [] if none apply.",
    "advisor_claims": "Labels: guarantee, deposit_confusion, exaggeration, pressure. Use [] if none apply.",
}

PARA_PROMPT = """Rewrite the sentence below in {n} different ways, as real people would say it to a bank \
advisor (task "{task}"). Keep exactly the same meaning and the same implication; do not add or remove claims. \
Vary style: casual, formal, long with extra context, short, with small typos, and about a third in Simplified \
Chinese. Reply as JSON: {{"paraphrases": [..]}}

Sentence: {text}"""


def gpu_temp() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def paraphrase(http: httpx.Client, model: str, task: str, text: str, n: int) -> list[str]:
    while gpu_temp() >= 80:  # thermal pacing
        time.sleep(10)
    body = {"model": model, "stream": False, "think": False, "options": {"temperature": 0.9},
            "format": {"type": "object", "properties": {"paraphrases": {"type": "array", "items": {"type": "string"}}},
                       "required": ["paraphrases"]},
            "messages": [{"role": "user", "content": PARA_PROMPT.format(n=n, task=task, text=text)}]}
    r = http.post("http://localhost:11434/api/chat", json=body, timeout=180)
    r.raise_for_status()
    try:
        items = json.loads(r.json()["message"]["content"])["paraphrases"]
    except (KeyError, ValueError):
        return []
    return [s.strip() for s in items if isinstance(s, str) and 3 < len(s.strip()) < 400][:n]


def example(task: str, text: str, labels: list[str]) -> dict:
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"TASK: {task}\n{TASK_HINT[task]}\nTEXT: {text}"},
        {"role": "assistant", "content": json.dumps({"labels": sorted(set(labels))}, ensure_ascii=False)},
    ], "task": task, "text": text, "labels": sorted(set(labels))}


def combos(task: str, pool: dict[str, list[str]], n: int, rng: random.Random) -> list[dict]:
    """Multi-label snippets: two or three sentences from different labels, or with neutral ones."""
    labels = [k for k in pool if k != "none"]
    out = []
    for _ in range(n):
        picked = rng.sample(labels, k=rng.choice([1, 2, 2, 3]))
        parts = [rng.choice(pool[l]) for l in picked] + rng.sample(pool["none"], k=rng.choice([0, 1]))
        rng.shuffle(parts)
        out.append(example(task, " ".join(parts), picked))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paraphrases", type=int, default=8)
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    train, test = [], []
    with httpx.Client() as http:
        for task, pool in (("client_signals", A_TRAIN), ("advisor_claims", B_TRAIN)):
            for label, seeds in pool.items():
                for s in seeds:
                    labels = [] if label == "none" else [label]
                    train.append(example(task, s, labels))
                    for p in paraphrase(http, a.model, task, s, a.paraphrases):
                        train.append(example(task, p, labels))
                print(f"{task}/{label}: {sum(1 for e in train if e['task'] == task and e['labels'] == ([] if label == 'none' else [label]))}", flush=True)
            train += combos(task, pool, 250, rng)

    for task, pool in (("client_signals", A_TEST), ("advisor_claims", B_TEST)):
        for label, seeds in pool.items():
            test += [example(task, s, [] if label == "none" else [label]) for s in seeds]
        test += combos(task, pool, 40, rng)

    rng.shuffle(train)
    texts_test = {e["text"] for e in test}
    train = [e for e in train if e["text"] not in texts_test]  # no leakage
    for name, rows in (("train", train), ("test", test)):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"train={len(train)} test={len(test)} -> {out.resolve()}")


if __name__ == "__main__":
    main()
