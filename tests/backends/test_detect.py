from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import anki_cli.backends.detect as detect_mod
from anki_cli.backends.detect import DetectionError, detect_backend


def _patch_detect_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reachable: bool,
    direct_path: Path | None,
    running: bool = False,
    locked: bool = False,
) -> dict[str, Any]:
    """Patch detect internals; returns a dict capturing resolver kwargs.

    Discovery is faked to ``[direct_path]`` while the pick/require decisions
    stay real, so the ``anki_profile`` plumbing is exercised end to end.
    """
    captured: dict[str, Any] = {}

    def _discover_spy(col_override):
        captured["col_override"] = col_override
        return [direct_path] if direct_path is not None else []

    real_pick = detect_mod._pick_collection

    def _pick_spy(candidates, anki_profile=None):
        captured["anki_profile"] = anki_profile
        return real_pick(candidates, anki_profile)

    monkeypatch.setattr(
        detect_mod,
        "_ankiconnect_version",
        lambda url, allow_non_localhost=False: 6 if reachable else None,
    )
    monkeypatch.setattr(detect_mod, "_discover_collections", _discover_spy)
    monkeypatch.setattr(detect_mod, "_pick_collection", _pick_spy)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: running)
    monkeypatch.setattr(detect_mod, "_sqlite_write_locked", lambda path: locked)
    return captured


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

    _patch_detect_helpers(
        monkeypatch,
        reachable=True,
        direct_path=direct_path,
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
        running=True,
        locked=False,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="direct")

    assert exc_info.value.exit_code == 7
    assert "Anki Desktop appears to be running" in str(exc_info.value)


@pytest.mark.parametrize("forced", ["direct", "auto"])
def test_lock_probe_runs_before_the_process_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forced: str
) -> None:
    """The SQLite lock probe is microseconds and authoritative; pgrep/tasklist
    is a subprocess. A locked collection must refuse without ever spawning it
    (#32)."""
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=tmp_path / "collection.anki2",
        running=False,
        locked=True,
    )

    def never_scan() -> bool:
        raise AssertionError("process scan must not run when the lock probe already refused")

    monkeypatch.setattr(detect_mod, "_anki_process_running", never_scan)

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend=forced)

    assert exc_info.value.exit_code == 7


def test_detection_carries_the_probed_ankiconnect_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_detect_helpers(monkeypatch, reachable=True, direct_path=tmp_path / "c.anki2")
    monkeypatch.setattr(detect_mod, "_ankiconnect_version", lambda *a, **k: 7)

    assert detect_backend(forced_backend="auto").ankiconnect_version == 7
    assert detect_backend(forced_backend="ankiconnect").ankiconnect_version == 7


def test_forced_direct_refuses_when_db_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=tmp_path / "collection.anki2",
        running=False,
        locked=True,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="direct")

    assert exc_info.value.exit_code == 7
    assert "Anki Desktop appears to be running" in str(exc_info.value)


def test_forced_direct_unopenable_collection_raises_detection_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unopenable collection must fail as ``DetectionError``, not sqlite3.

    ``_sqlite_write_locked`` re-raises non-lock ``sqlite3.Error`` (fail closed);
    ``_probe_write_lock`` translates that into ``DetectionError`` because
    ``detect_backend``'s only documented failure type is ``DetectionError``.
    A directory passes ``exists()`` but ``sqlite3.connect`` cannot open it —
    and the ``_patch_detect_helpers`` seam can only ever return a bool, so this
    exercises the real probe.
    """
    bad = tmp_path / "collection.anki2"
    bad.mkdir()
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: False)

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="direct", col_override=bad)

    assert exc_info.value.exit_code == 7


def test_auto_unopenable_collection_raises_detection_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Same as the forced path, reached via ``forced_backend="auto"`` (a config
    ``collection_path`` pointing at an unopenable file)."""
    bad = tmp_path / "collection.anki2"
    bad.mkdir()
    monkeypatch.setattr(detect_mod, "_ankiconnect_version", lambda *a, **k: None)
    monkeypatch.setattr(detect_mod, "_anki_process_running", lambda: False)

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="auto", col_override=bad)

    assert exc_info.value.exit_code == 7


def test_forced_direct_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    direct_path = tmp_path / "collection.anki2"

    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=direct_path,
        running=False,
        locked=False,
    )

    result = detect_backend(forced_backend="direct")

    assert result.backend == "direct"
    assert result.collection_path == direct_path
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
        running=True,
        locked=False,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="auto")

    assert exc_info.value.exit_code == 7
    assert "Anki is running but AnkiConnect is unavailable" in str(exc_info.value)


@pytest.mark.parametrize(
    ("forced", "reachable"),
    [
        ("auto", True),
        ("auto", False),
        ("direct", False),
        ("ankiconnect", True),
    ],
)
def test_detect_backend_forwards_anki_profile_to_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forced: str,
    reachable: bool,
) -> None:
    # Pins every collection-resolution call site in detect_backend:
    # dropping `anki_profile=` anywhere along these paths must fail.
    # The candidate's parent dir is named "Work" so the real matcher hits.
    captured = _patch_detect_helpers(
        monkeypatch,
        reachable=reachable,
        direct_path=tmp_path / "Work" / "collection.anki2",
    )

    result = detect_backend(forced_backend=forced, anki_profile="Work")

    assert captured["anki_profile"] == "Work"
    assert result.collection_path is not None
    assert result.profile == "Work"


def test_forced_ankiconnect_with_unmatched_profile_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The AnkiConnect-branch collection lookup is informational: a profile
    # miss must not kill a backend that just answered `version`.
    root = tmp_path / "Anki2"
    profile_dir = root / "Personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "collection.anki2").touch()

    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])
    monkeypatch.setattr(detect_mod, "_ankiconnect_version", lambda *a, **k: 6)

    result = detect_backend(forced_backend="ankiconnect", anki_profile="Work")

    assert result.backend == "ankiconnect"
    assert result.collection_path is None
    assert result.profile is None


def test_auto_ankiconnect_reachable_with_unmatched_profile_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "Anki2"
    profile_dir = root / "Personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "collection.anki2").touch()

    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])
    monkeypatch.setattr(detect_mod, "_ankiconnect_version", lambda *a, **k: 6)

    result = detect_backend(forced_backend="auto", anki_profile="Work")

    assert result.backend == "ankiconnect"
    assert result.collection_path is None


@pytest.mark.parametrize("forced", ["auto", "direct"])
def test_missing_col_override_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forced: str,
) -> None:
    # An explicit --col to a nonexistent file must name the path, not fall
    # through to a generic "no collection found".
    monkeypatch.setattr(detect_mod, "_ankiconnect_version", lambda *a, **k: None)
    missing = tmp_path / "missing.anki2"

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend=forced, col_override=missing)

    assert exc_info.value.exit_code == 3
    assert "Collection override" in str(exc_info.value)
    assert str(missing.resolve()) in str(exc_info.value)


def test_missing_col_override_is_informational_on_ankiconnect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(detect_mod, "_ankiconnect_version", lambda *a, **k: 6)

    result = detect_backend(forced_backend="ankiconnect", col_override=tmp_path / "missing.anki2")

    assert result.backend == "ankiconnect"
    assert result.collection_path is None
    assert result.profile is None


def test_col_override_result_has_no_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Pins the `col_override is not None` arm: an explicit --col is not a
    # discovered profile.
    db = tmp_path / "collection.anki2"
    db.touch()
    _patch_detect_helpers(
        monkeypatch, reachable=False, direct_path=None, running=False, locked=False
    )

    result = detect_backend(forced_backend="auto", col_override=db)

    assert result.backend == "direct"
    assert result.collection_path == db.resolve()
    assert result.profile is None


def test_auto_raises_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_detect_helpers(
        monkeypatch,
        reachable=False,
        direct_path=None,
        running=False,
        locked=False,
    )

    with pytest.raises(DetectionError) as exc_info:
        detect_backend(forced_backend="auto")

    assert exc_info.value.exit_code == 3
    assert "No AnkiConnect and no collection found" in str(exc_info.value)


def test_require_direct_collection_override_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.anki2"
    db_path.touch()

    resolved = detect_mod._require_direct_collection(db_path, None)

    assert resolved == db_path.resolve()


def test_require_direct_collection_override_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.anki2"

    with pytest.raises(DetectionError) as exc_info:
        detect_mod._require_direct_collection(missing, None)

    assert exc_info.value.exit_code == 3
    assert "does not exist" in str(exc_info.value)
