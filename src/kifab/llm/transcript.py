"""Append-only provider transcript.

Every provider call, every sandbox access and every refusal lands here as one
JSON object per line. The file is the evidence: the blind-holdout test does not
take the generator's word that it never looked at an existing footprint, it
reads this and checks.

Append-only and flushed per event on purpose — a crash mid-run must not lose
the record of what happened before it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRANSCRIPT_NAME = "transcript.jsonl"


@dataclass
class Transcript:
    """The write side. `read()` is the audit side."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
        entry.update(fields)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            handle.flush()
        return entry

    def entries(self) -> list[dict[str, Any]]:
        return read(self.path)


def read(path: Path) -> list[dict[str, Any]]:
    """Read a transcript. A malformed line is kept, not skipped.

    A line the auditor cannot parse is *more* suspicious than one it can, so it
    is surfaced as `{"event": "unparseable", ...}` rather than dropped.
    """
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            out.append({"event": "unparseable", "line": number, "raw": line})
            continue
        if not isinstance(value, dict):
            out.append({"event": "unparseable", "line": number, "raw": line})
            continue
        out.append(value)
    return out
