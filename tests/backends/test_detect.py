from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import anki_cli.backends.detect as detect_mod
from anki_cli.backends.detect import DetectionError, detect_backend


def _patch_detect_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reachable: bool,
    direct_path: Path | None,
    standalone_path: Path,
    running: bool = False,
    locked: bool = False,
) -> None:
    monkeypatch.setattr(detect_mod, "_ankiconnect_reachable", lambda url: reachable)
    monkeypatch.setattr(detect_mod, "_resolve_direct_collection", lambda col_override: direct_path)
    monkeypatch.setattr(
        detect_mod,
        "_resolve_standalone_collection",
        lambda col_override: standalone_path,
    )
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: running)
    monkeypatch.setattr(detect_mod, "_sqlite_write_locked", lambda path: locked)


def test_detect_backend_rejects_unknown_forced_backend() -> None:
    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="nope")

    assert exc_info.value.exit_code == 2
    assert "Unsupported backend" in str(exc_info.value)


def test_forced_ankiconnect_unreachable_raises_exit7(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=tmp_path / "collection.anki2",
        standalone_path=tmp_path / "standalone.db",
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="ankiconnect")

    assert exc_info.value.exit_code == 7
    assert "not reachable" in str(exc_info.value)


def test_forced_ankiconnect_returns_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    direct_path = tmp_path / "collection.anki2"
    standalone_path = tmp_path / "standalone.db"

    _patch_detect_helpers(
        monkeypatch,
        reachable=True,
        direct_path=direct_path,
        standalone_path=standalone_path,
    )

    result = detect_backend(forced_backend="  AnKiCoNnEcT  ")

    assert result.backend == "ankiconnect"
    assert result.collection_path == direct_path
    assert result.reason == "forced"


def test_forced_direct_requires_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=None,
        standalone_path=tmp_path / "standalone.db",
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="direct")

    assert exc_info.value.exit_code == 3
    assert "no Anki collection DB was found" in str(exc_info.value)


def test_forced_direct_refuses_when_anki_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=tmp_path / "collection.anki2",
        standalone_path=tmp_path / "standalone.db",
        running=True,
        locked=False,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="direct")

    assert exc_info.value.exit_code == 7
    assert "Anki Desktop appears to be running" in str(exc_info.value)


def test_forced_direct_refuses_when_db_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=tmp_path / "collection.anki2",
        standalone_path=tmp_path / "standalone.db",
        running=False,
        locked=True,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="direct")

    assert exc_info.value.exit_code == 7
    assert "Anki Desktop appears to be running" in str(exc_info.value)


def test_forced_direct_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    direct_path = tmp_path / "collection.anki2"

    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=direct_path,
        standalone_path=tmp_path / "standalone.db",
        running=False,
        locked=False,
    )

    result = detect_backend(forced_backend="direct")

    assert result.backend == "direct"
    assert result.collection_path == direct_path
    assert result.reason == "forced"


def test_forced_standalone_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    standalone_path = tmp_path / "standalone.db"

    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=None,
        standalone_path=standalone_path,
    )

    result = detect_backend(forced_backend="standalone")

    assert result.backend == "standalone"
    assert result.collection_path == standalone_path
    assert result.reason == "forced"


def test_auto_prefers_ankiconnect_when_reachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    direct_path = tmp_path / "collection.anki2"

    _patch_detect_helpers(
        monkeypatch,
        reachable=True,
        direct_path=direct_path,
        standalone_path=tmp_path / "standalone.db",
    )

    result = detect_backend(forced_backend="auto")

    assert result.backend == "ankiconnect"
    assert result.collection_path == direct_path
    assert result.reason == "ankiconnect reachable"


def test_auto_uses_direct_when_ankiconnect_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    direct_path = tmp_path / "collection.anki2"

    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=direct_path,
        standalone_path=tmp_path / "standalone.db",
        running=False,
        locked=False,
    )

    result = detect_backend(forced_backend="auto")

    assert result.backend == "direct"
    assert result.collection_path == direct_path
    assert result.reason == "ankiconnect unavailable, direct collection found"


def test_auto_direct_path_but_running_raises_exit7(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=tmp_path / "collection.anki2",
        standalone_path=tmp_path / "standalone.db",
        running=True,
        locked=False,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="auto")

    assert exc_info.value.exit_code == 7
    assert "Anki is running but AnkiConnect is unavailable" in str(exc_info.value)


def test_auto_falls_back_to_standalone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    standalone_path = tmp_path / "standalone.db"

    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=None,
        standalone_path=standalone_path,
        running=False,
        locked=False,
    )

    result = detect_backend(forced_backend="auto")

    assert result.backend == "standalone"
    assert result.collection_path == standalone_path
    assert result.reason == "no ankiconnect and no direct collection found"


def test_resolve_direct_collection_override_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.anki2"
    db_path.touch()

    resolved = detect_mod._resolve_direct_collection(db_path)

    assert resolved == db_path.resolve()


def test_resolve_direct_collection_override_missing_returns_none(tmp_path: Path) -> None:
    resolved = detect_mod._resolve_direct_collection(tmp_path / "missing.anki2")

    assert resolved is None


def test_sqlite_write_locked_false_when_db_missing(tmp_path: Path) -> None:
    assert detect_mod._sqlite_write_locked(tmp_path / "missing.db") is False


def test_sqlite_write_locked_false_when_db_is_writable(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert detect_mod._sqlite_write_locked(db_path) is False


def test_sqlite_write_locked_true_when_other_connection_holds_immediate_lock(
    tmp_path: Path
) -> None:
    db_path = tmp_path / "collection.db"
    setup = sqlite3.connect(str(db_path))
    setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    setup.commit()
    setup.close()

    locker = sqlite3.connect(str(db_path), isolation_level=None, timeout=1.0)
    locker.execute("BEGIN IMMEDIATE")
    try:
        assert detect_mod._sqlite_write_locked(db_path) is True
    finally:
        locker.execute("ROLLBACK")
        locker.close()