from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _default_undo_path() -> Path:
    return Path("~/.local/share/anki-cli/undo.json").expanduser()


@dataclass(frozen=True, slots=True)
class UndoItem:
    collection: str
    card_id: int
    snapshot: dict[str, Any]
    created_at_epoch_ms: int


class UndoStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_undo_path()

    def push(self, item: UndoItem, *, max_items: int = 50) -> None:
        data = self._load()
        items = data.get("items", [])
        if not isinstance(items, list):
            items = []

        items.append(
            {
                "collection": item.collection,
                "card_id": int(item.card_id),
                "snapshot": item.snapshot,
                "created_at_epoch_ms": int(item.created_at_epoch_ms),
            }
        )

        data["items"] = items[-max(1, int(max_items)) :]
        self._save(data)

    def pop(self, *, collection: str) -> UndoItem | None:
        data = self._load()
        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            return None

        for idx in range(len(items) - 1, -1, -1):
            raw = items[idx]
            if not isinstance(raw, dict):
                continue
            if str(raw.get("collection", "")) != collection:
                continue

            items.pop(idx)
            data["items"] = items
            self._save(data)

            snap = raw.get("snapshot")
            if not isinstance(snap, dict):
                return None

            return UndoItem(
                collection=collection,
                card_id=int(raw.get("card_id") or 0),
                snapshot=dict(snap),
                created_at_epoch_ms=int(raw.get("created_at_epoch_ms") or 0),
            )

        return None

    def _load(self) -> dict[str, Any]:
        try:
            if not self._path.exists():
                return {"version": 1, "items": []}
            text = self._path.read_text(encoding="utf-8")
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"version": 1, "items": []}
        except Exception:
            return {"version": 1, "items": []}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data.setdefault("version", 1)
        data.setdefault("items", [])
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_epoch_ms() -> int:
    return int(time.time() * 1000)
