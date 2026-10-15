"""Client for the fine-tuned compliance sentinel (Qwen3.5-2B LoRA, served by Ollama).

The prompt format must match training/make_data.py exactly. The sentinel only adds
signals on top of the deterministic layers; if it's unavailable or errors, the
deterministic checks still run, so a sentinel failure never loosens a check.
"""

from __future__ import annotations

import json
import os

import httpx

from .integrity import Signal
from .speech import SpeechIssue

SYSTEM = ("You are a compliance sentinel for a bank's wealth advisory service. Classify the text and reply "
          "with JSON only.")
TASK_HINT = {
    "client_signals": "Labels: loss_averse, dependent_funds, short_horizon, life_change. Use [] if none apply.",
    "advisor_claims": "Labels: guarantee, deposit_confusion, exaggeration, pressure. Use [] if none apply.",
}
SIGNAL_CAPS = {"loss_averse": 2, "dependent_funds": 2, "short_horizon": 3, "life_change": None}
CLAIM_RULES = {
    "guarantee": ("SP-GUAR", "NFRA Art. 13(2)", "Promises principal protection or guaranteed returns."),
    "deposit_confusion": ("SP-DEP", "NFRA Art. 13(2)", "Presents a non-deposit product as a deposit."),
    "exaggeration": ("SP-EXAG", "NFRA Art. 13(2)", "Exaggerates returns or certainty of gains."),
    "pressure": ("SP-PRESS", "NFRA Art. 13(4)", "Pressures or induces the client to buy."),
}
SCHEMA = {"type": "object", "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
          "required": ["labels"]}


class Sentinel:
    def __init__(self, model: str | None = None, host: str | None = None, timeout: float = 30.0):
        self.model = model or os.environ.get("PCA_SENTINEL", "pca-sentinel")
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self._http = httpx.Client(timeout=timeout)
        # Inputs are short, so a 1,024-token window is enough. By default the sentinel runs on the CPU with
        # 2 threads: on an 8 GB laptop GPU, the 9B advisor plus the desktop leave too little VRAM, and Ollama
        # would otherwise evict one model per request. PCA_SENTINEL_DEVICE=gpu if there is room.
        self.options = {"temperature": 0, "num_ctx": int(os.environ.get("PCA_SENTINEL_CTX", "1024"))}
        if os.environ.get("PCA_SENTINEL_DEVICE", "cpu") == "cpu":
            self.options |= {"num_gpu": 0, "num_thread": int(os.environ.get("PCA_SENTINEL_THREADS", "2"))}

    def available(self) -> bool:
        try:
            tags = self._http.get(f"{self.host}/api/tags").json().get("models", [])
            return any(m.get("name", "").split(":")[0] == self.model.split(":")[0] for m in tags)
        except httpx.HTTPError:
            return False

    def labels(self, task: str, text: str) -> list[str]:
        body = {"model": self.model, "stream": False, "think": False,
                "options": self.options,
                "format": SCHEMA, "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"TASK: {task}\n{TASK_HINT[task]}\nTEXT: {text[:2500]}"}]}
        try:
            r = self._http.post(f"{self.host}/api/chat", json=body)
            r.raise_for_status()
            labels = json.loads(r.json()["message"]["content"]).get("labels", [])
        except (httpx.HTTPError, ValueError, KeyError):
            return []
        return [l for l in labels if isinstance(l, str)]

    def signals(self, text: str) -> list[Signal]:
        return [Signal(l, text[:200], SIGNAL_CAPS[l], l == "life_change", "sentinel")
                for l in self.labels("client_signals", text) if l in SIGNAL_CAPS]

    def claims(self, text: str) -> list[SpeechIssue]:
        """Off by default: on long advisor messages the sentinel over-flags disclaimers (see the README's evaluation section).

        Set PCA_SENTINEL_CLAIMS=1 to enable it. Client-signal detection is unaffected.
        """
        if os.environ.get("PCA_SENTINEL_CLAIMS", "0") != "1":
            return []
        out = []
        for l in self.labels("advisor_claims", text):
            if l in CLAIM_RULES:
                rid, cite, msg = CLAIM_RULES[l]
                out.append(SpeechIssue(rid, cite, msg + " (sentinel model)", text[:200]))
        return out
