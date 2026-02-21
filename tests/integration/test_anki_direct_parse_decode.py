from __future__ import annotations

import sqlite3
from pathlib import Path

from anki_cli.db.anki_direct import AnkiDirectReadStore


def _make_store(tmp_path: Path) -> AnkiDirectReadStore:
    db_path = tmp_path / "collection.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    return AnkiDirectReadStore(db_path)


def test_parse_card_data_empty_string_returns_empty_dict(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store._parse_card_data("") == {}
    assert store._parse_card_data("   ") == {}


def test_parse_card_data_valid_json_scalar_and_structures(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store._parse_card_data('{"a":1,"b":[2]}') == {"a": 1, "b": [2]}
    assert store._parse_card_data("[1,2,3]") == [1, 2, 3]
    assert store._parse_card_data('"hello"') == "hello"
    assert store._parse_card_data("123") == 123
    assert store._parse_card_data("12.5") == 12.5
    assert store._parse_card_data("true") is True
    assert store._parse_card_data("null") is None


def test_parse_card_data_invalid_json_returns_raw_input(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    raw = "{not valid json"
    assert store._parse_card_data(raw) == raw


def test_decode_left_non_negative_and_negative(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store._decode_left(2003) == {
        "raw": 2003,
        "today_remaining": 2,
        "until_graduation": 3,
    }
    assert store._decode_left(0) == {
        "raw": 0,
        "today_remaining": 0,
        "until_graduation": 0,
    }
    assert store._decode_left(-1) == {"raw": -1}


def test_decode_due_new_learning_review_and_fallback(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store._decode_due(card_type=0, queue=0, due_raw=42, col_crt_sec=None) == {
        "kind": "new_position",
        "raw": 42,
        "position": 42,
    }

    assert store._decode_due(card_type=1, queue=1, due_raw=1_700_000_000, col_crt_sec=None) == {
        "kind": "learn_epoch_secs",
        "raw": 1_700_000_000,
        "epoch_secs": 1_700_000_000,
    }

    assert store._decode_due(card_type=3, queue=3, due_raw=1_700_000_100, col_crt_sec=None) == {
        "kind": "learn_epoch_secs",
        "raw": 1_700_000_100,
        "epoch_secs": 1_700_000_100,
    }

    # col crt day index: 864000 -> day 10
    out = store._decode_due(card_type=2, queue=2, due_raw=5, col_crt_sec=864000)
    assert out["kind"] == "review_day_index"
    assert out["raw"] == 5
    assert out["day_index"] == 5
    assert out["epoch_secs"] == (10 + 5) * 86400

    assert store._decode_due(card_type=99, queue=7, due_raw=123, col_crt_sec=None) == {
        "kind": "raw",
        "raw": 123,
        "queue": 7,
        "type": 99,
    }


def test_decode_revlog_interval_seconds_and_days(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store._decode_revlog_interval(-60) == {
        "raw": -60,
        "unit": "seconds",
        "seconds": 60,
        "days": None,
    }
    assert store._decode_revlog_interval(30) == {
        "raw": 30,
        "unit": "days",
        "days": 30,
        "seconds": None,
    }


def test_decode_revlog_factor_models(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store._decode_revlog_factor(500) == {
        "raw": 500,
        "model": "fsrs_difficulty",
        "difficulty": 5.0,
        "ease_multiplier": None,
    }

    assert store._decode_revlog_factor(2500) == {
        "raw": 2500,
        "model": "sm2_ease_permille",
        "difficulty": None,
        "ease_multiplier": 2.5,
    }

    assert store._decode_revlog_factor(0) == {
        "raw": 0,
        "model": "unknown",
        "difficulty": None,
        "ease_multiplier": None,
    }


def test_revlog_row_to_item_maps_all_fields(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY,
            cid INTEGER,
            usn INTEGER,
            ease INTEGER,
            ivl INTEGER,
            lastIvl INTEGER,
            factor INTEGER,
            time INTEGER,
            type INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1700000000123, 42, -1, 3, -60, 10, 500, 1234, 4),
    )
    row = conn.execute("SELECT * FROM revlog").fetchone()
    conn.close()
    assert row is not None

    item = store._revlog_row_to_item(row)

    assert item["id"] == 1700000000123
    assert item["card_id"] == 42
    assert item["usn"] == -1
    assert item["ease"] == 3
    assert item["review_type"] == 4
    assert item["review_type_name"] == "manual"
    assert item["duration_ms"] == 1234
    assert item["reviewed_at_epoch_ms"] == 1700000000123
    assert item["reviewed_at_epoch_secs"] == 1700000000
    assert item["interval"] == {
        "raw": -60,
        "unit": "seconds",
        "seconds": 60,
        "days": None,
    }
    assert item["last_interval"] == {
        "raw": 10,
        "unit": "days",
        "days": 10,
        "seconds": None,
    }
    assert item["factor"] == 500
    assert item["factor_info"] == {
        "raw": 500,
        "model": "fsrs_difficulty",
        "difficulty": 5.0,
        "ease_multiplier": None,
    }