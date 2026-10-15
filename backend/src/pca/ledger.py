"""Append-only, hash-chained event log: the execution evidence trail.

Each event commits to the previous one, so deleting or editing any past event
breaks every hash after it (NFRA Art. 17: key steps must be traceable).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .certificate import canonical, sha256

GENESIS = "0" * 64


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    type: str
    data: dict
    prev: str
    hash: str


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.events: list[Event] = []
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.events.append(Event(**json.loads(line)))

    @property
    def head(self) -> str:
        return self.events[-1].hash if self.events else GENESIS

    def append(self, type: str, data: dict[str, Any]) -> Event:
        seq = len(self.events)
        ts = datetime.now(timezone.utc).isoformat()
        body = {"seq": seq, "ts": ts, "type": type, "data": data, "prev": self.head}
        ev = Event(**body, hash=sha256(canonical(body)))
        self.events.append(ev)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev.__dict__, ensure_ascii=False, default=str) + "\n")
        return ev

    def verify(self) -> tuple[bool, int | None]:
        """Returns (ok, index of the first broken event)."""
        prev = GENESIS
        for i, ev in enumerate(self.events):
            body = {"seq": ev.seq, "ts": ev.ts, "type": ev.type, "data": ev.data, "prev": ev.prev}
            if ev.seq != i or ev.prev != prev or sha256(canonical(body)) != ev.hash:
                return False, i
            prev = ev.hash
        return True, None

    def of_type(self, *types: str) -> Iterator[Event]:
        return (e for e in self.events if e.type in types)
