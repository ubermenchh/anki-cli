import json

import anki_cli.core.undo as undo_mod
from anki_cli.core.undo import UndoItem, UndoStore, now_epoch_ms


def _item(collection: str, card_id: int, *, created_at_epoch_ms: int = 1) -> UndoItem:
    return UndoItem(
        collection=collection,
        card_id=card_id,
        snapshot={"card_id": card_id, "state": "demo"},
        created_at_epoch_ms=created_at_epoch_ms,
    )


def test_pop_returns_none_when_store_missing(tmp_path) -> None:
    path = tmp_path / "undo.json"
    store = UndoStore(path=path)

    assert store.pop(collection="col-A") is None
    assert not path.exists()


def test_push_then_pop_round_trip(tmp_path) -> None:
    path = tmp_path / "undo.json"
    store = UndoStore(path=path)

    store.push(_item("col-A", 123, created_at_epoch_ms=999))
    popped = store.pop(collection="col-A")

    assert popped is not None
    assert popped.collection == "col-A"
    assert popped.card_id == 123
    assert popped.snapshot == {"card_id": 123, "state": "demo"}
    assert popped.created_at_epoch_ms == 999
    assert store.pop(collection="col-A") is None


def test_pop_is_collection_scoped_and_lifo(tmp_path) -> None:
    store = UndoStore(path=tmp_path / "undo.json")

    store.push(_item("col-A", 1))
    store.push(_item("col-B", 2))
    store.push(_item("col-A", 3))

    first_a = store.pop(collection="col-A")
    second_a = store.pop(collection="col-A")
    b = store.pop(collection="col-B")
    none_left = store.pop(collection="col-A")

    assert first_a is not None and first_a.card_id == 3
    assert second_a is not None and second_a.card_id == 1
    assert b is not None and b.card_id == 2
    assert none_left is None


def test_push_respects_max_items(tmp_path) -> None:
    path = tmp_path / "undo.json"
    store = UndoStore(path=path)

    for cid in range(5):
        store.push(_item("col-A", cid), max_items=3)

    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["items"]

    assert len(items) == 3
    assert [entry["card_id"] for entry in items] == [2, 3, 4]


def test_corrupted_json_is_handled_and_recovered(tmp_path) -> None:
    path = tmp_path / "undo.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = UndoStore(path=path)

    assert store.pop(collection="col-A") is None

    store.push(_item("col-A", 42))
    popped = store.pop(collection="col-A")
    assert popped is not None and popped.card_id == 42


def test_invalid_snapshot_entry_returns_none_and_is_removed(tmp_path) -> None:
    path = tmp_path / "undo.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "collection": "col-A",
                        "card_id": 10,
                        "snapshot": "not-a-dict",
                        "created_at_epoch_ms": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = UndoStore(path=path)
    assert store.pop(collection="col-A") is None

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["items"] == []


def test_now_epoch_ms_uses_time_seconds(monkeypatch) -> None:
    monkeypatch.setattr(undo_mod.time, "time", lambda: 1234.5678)
    assert now_epoch_ms() == 1234567