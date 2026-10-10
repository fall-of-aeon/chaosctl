"""Append-only JSONL audit trail of everything agents see/decide and the harness does."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, entry: dict) -> None:
        self.write(entry)

    def write(self, entry: dict) -> None:
        rec = {"ts": round(time.time(), 3), **entry}
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


class NullJournal(Journal):
    def __init__(self):
        pass

    def write(self, entry: dict) -> None:
        pass

    def read(self) -> list[dict]:
        return []
