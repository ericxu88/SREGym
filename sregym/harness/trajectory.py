"""JSONL trajectory format.

One record per line:
  {"type": "meta", ...}                              episode header (seed, fault, model, prompts, world dir)
  {"type": "step", "step": N, "observation": ..., "assistant_text": ..., "tool_call": name,
   "tool_args": {...}, "tool_result": ..., "tool_error": bool, "state_hash": "sha256:...",
   "usage": {...}|null, "ts": iso}                   one per tool call
  {"type": "end", "stop_reason": ..., "verification": {...}, "reward": float, ...}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


@dataclass
class Step:
    step: int
    observation: str
    assistant_text: str | None
    tool_call: str | None
    tool_args: dict[str, Any]
    tool_result: str
    tool_error: bool
    state_hash: str
    usage: dict[str, int] | None = None
    assistant_thinking: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = "step"
        return d


class TrajectoryWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")

    def _write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def write_meta(self, **meta: Any) -> None:
        self._write({"type": "meta", **meta})

    def write_step(self, step: Step) -> None:
        self._write(step.to_record())

    def write_end(self, **end: Any) -> None:
        self._write({"type": "end", **end})

    def close(self) -> None:
        self._fh.close()


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_trajectory(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    meta, steps, end = None, [], None
    for rec in iter_records(Path(path)):
        if rec.get("type") == "meta":
            meta = rec
        elif rec.get("type") == "step":
            steps.append(rec)
        elif rec.get("type") == "end":
            end = rec
    return meta, steps, end
