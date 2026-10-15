"""Small durable document store (SQLite) for the service's state.

Everything the demo needs to survive a restart (clients, certificates, cases,
notifications, sandbox accounts, product ratings, clock) is stored as JSON
documents keyed by (namespace, key). The chain and the evidence ledger already
persist on their own.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS doc (ns TEXT, key TEXT, value TEXT, PRIMARY KEY (ns, key))")

    def put(self, ns: str, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO doc VALUES (?, ?, ?)",
                        (ns, key, json.dumps(value, ensure_ascii=False, default=str)))

    def put_many(self, ns: str, items: list[tuple[str, Any]]) -> None:
        self.db.execute("BEGIN")
        self.db.executemany("INSERT OR REPLACE INTO doc VALUES (?, ?, ?)",
                            [(ns, k, json.dumps(v, ensure_ascii=False, default=str)) for k, v in items])
        self.db.execute("COMMIT")

    def get(self, ns: str, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM doc WHERE ns=? AND key=?", (ns, key)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, ns: str) -> Iterator[tuple[str, Any]]:
        for k, v in self.db.execute("SELECT key, value FROM doc WHERE ns=? ORDER BY rowid", (ns,)):
            yield k, json.loads(v)

    def delete(self, ns: str, key: str) -> None:
        self.db.execute("DELETE FROM doc WHERE ns=? AND key=?", (ns, key))
