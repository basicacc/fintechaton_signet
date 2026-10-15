"""Model-agnostic LLM interface.

The default backend is a local model served by Ollama (no data leaves the machine).
Any backend that implements ``LLM.chat`` can be swapped in, including scripted
fakes for tests and offline demos.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict[str, str]], schema: dict | None = None,
             temperature: float = 0.3) -> str: ...


class OllamaLLM:
    def __init__(self, model: str | None = None, host: str | None = None, timeout: float = 180.0):
        self.model = model or os.environ.get("PCA_MODEL", "qwen3.5:9b")
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.name = f"ollama:{self.model}"
        self._http = httpx.Client(timeout=timeout)

    def chat(self, messages, schema=None, temperature=0.3) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature},
        }
        if schema is not None:
            body["format"] = schema
        r = self._http.post(f"{self.host}/api/chat", json=body)
        r.raise_for_status()
        return r.json()["message"]["content"]


class ScriptedLLM:
    """Returns canned replies in order. Used by tests and the offline demo mode."""

    def __init__(self, replies: list[str | dict]):
        self.replies = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in replies]
        self.calls: list[list[dict[str, str]]] = []
        self.name = "scripted"

    def chat(self, messages, schema=None, temperature=0.3) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError("ScriptedLLM ran out of replies")
        return self.replies.pop(0)


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or extra text."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("\n") + 1:] if "\n" in t else t
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(t[start:end + 1])
