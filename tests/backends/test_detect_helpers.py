from __future__ import annotations

import sys
from pathlib import Path

import pytest

import anki_cli.backends.detect as detect_mod


class _FakeResponse:
    def __init__(
        self,
        *,
        payload: object | None = None,
        raise_http: bool = False,
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._raise_http = raise_http
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self._raise_http:
            raise detect_mod.httpx.HTTPError("http boom")

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeClient:
    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self._response = response
        self._post_error = post_error
        self.captured_url: str | None = None
        self.captured_json: object | None = None

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, json: object) -> _FakeResponse:
        self.captured_url = url
        self.captured_json = json
        if self._post_error is not None:
            raise self._post_error
        assert self._response is not None
        return self._response


def _patch_path_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(detect_mod.Path, "home", lambda *args, **kwargs: home)


def test_ankiconnect_reachable_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        response=_FakeResponse(payload={"error": None, "result": 6}),
    )
    monkeypatch.setattr(detect_mod.httpx, "Client", lambda timeout: client)

    ok = detect_mod._ankiconnect_reachable("http://localhost:8765")

    assert ok is True
    assert client.captured_url == "http://localhost:8765"
    assert client.captured_json == {"action": "version", "version": 6}


def test_ankiconnect_reachable_false_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(post_error=detect_mod.httpx.HTTPError("boom"))
    monkeypatch.setattr(detect_mod.httpx, "Client", lambda timeout: client)

    assert detect_mod._ankiconnect_reachable("http://localhost:8765") is False


def test_ankiconnect_reachable_false_on_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        response=_FakeResponse(payload=None, json_error=ValueError("bad json")),
    )
    monkeypatch.setattr(detect_mod.httpx, "Client", lambda timeout: client)

    assert detect_mod._ankiconnect_reachable("http://localhost:8765") is False


def test_ankiconnect_reachable_false_on_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(response=_FakeResponse(payload={"error": "x", "result": None}))
    monkeypatch.setattr(detect_mod.httpx, "Client", lambda timeout: client)

    assert detect_mod._ankiconnect_reachable("http://localhost:8765") is False


def test_ankiconnect_reachable_skips_non_localhost_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without allow_non_localhost the URL is never even POSTed to.
    client = _FakeClient(
        response=_FakeResponse(payload={"error": None, "result": 6}),
    )
    monkeypatch.setattr(detect_mod.httpx, "Client", lambda timeout: client)

    assert detect_mod._ankiconnect_reachable("http://192.168.1.100:8765") is False
    assert client.captured_url is None

    assert (
        detect_mod._ankiconnect_reachable(
            "http://192.168.1.100:8765", allow_non_localhost=True
        )
        is True
    )
    assert client.captured_url == "http://192.168.1.100:8765"


def test_discover_collections_scans_roots_and_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()

    # Non-directory entry under root should be ignored.
    (root_a / "README.txt").write_text("x", encoding="utf-8")

    profile_a = root_a / "User 1"
    profile_b = root_b / "User 0"
    profile_a.mkdir()
    profile_b.mkdir()

    db_a = profile_a / "collection.anki2"
    db_b = profile_b / "collection.anki21b"
    db_a.touch()
    db_b.touch()

    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root_a, root_b])

    assert detect_mod._discover_collections(None) == [db_a, db_b]
    assert detect_mod._pick_collection([db_a, db_b], None) == db_a


def test_discover_collections_returns_empty_when_no_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root = tmp_path / "missing"
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [missing_root])

    assert detect_mod._discover_collections(None) == []
    assert detect_mod._pick_collection([], None) is None


def _make_anki_root(tmp_path: Path, profiles: list[str]) -> Path:
    root = tmp_path / "Anki2"
    for profile in profiles:
        profile_dir = root / profile
        profile_dir.mkdir(parents=True)
        (profile_dir / "collection.anki2").touch()
    return root


def test_pick_collection_prefers_configured_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_anki_root(tmp_path, ["User 1", "Custom"])
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])

    candidates = detect_mod._discover_collections(None)

    assert detect_mod._pick_collection(candidates, None) == (
        root / "Custom" / "collection.anki2"
    )
    assert detect_mod._pick_collection(candidates, "User 1") == (
        root / "User 1" / "collection.anki2"
    )
    assert detect_mod._require_direct_collection(None, "User 1") == (
        root / "User 1" / "collection.anki2"
    )


def test_pick_collection_unmatched_profile_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Informational pick never raises — a miss just means nothing to display.
    root = _make_anki_root(tmp_path, ["Personal", "User 1"])
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])

    candidates = detect_mod._discover_collections(None)

    assert detect_mod._pick_collection(candidates, "Work") is None


def test_require_direct_collection_unmatched_profile_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Near-miss profile names pin the exact `==` comparison: a mutated
    # `in`/`.startswith`/`.lower()` match would return a path here instead
    # of raising.
    root = _make_anki_root(
        tmp_path, ["Personal", "User 1", "User", "ser 1", "User 1 (copy)"]
    )
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])

    with pytest.raises(detect_mod.DetectionError) as exc_info:
        detect_mod._require_direct_collection(None, "user 1")

    assert exc_info.value.exit_code == 3
    assert "Anki profile 'user 1' not found" in str(exc_info.value)
    assert "Personal" in str(exc_info.value)
    assert "User 1" in str(exc_info.value)


def test_require_direct_collection_profile_strips_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_anki_root(tmp_path, ["Work"])
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])

    assert detect_mod._require_direct_collection(None, "  Work  ") == (
        root / "Work" / "collection.anki2"
    )


def test_require_direct_collection_col_override_wins_over_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_anki_root(tmp_path, ["User 1"])
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [root])

    override = tmp_path / "elsewhere" / "collection.anki2"
    override.parent.mkdir()
    override.touch()

    resolved = detect_mod._require_direct_collection(
        override, "nonexistent-profile"
    )

    assert resolved == override.resolve()


def test_anki_data_roots_darwin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    _patch_path_home(monkeypatch, home)
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)

    assert detect_mod._anki_data_roots() == [
        home / "Library" / "Application Support" / "Anki2"
    ]


def test_anki_data_roots_win32_with_appdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"

    _patch_path_home(monkeypatch, home)
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))

    assert detect_mod._anki_data_roots() == [appdata / "Anki2"]


def test_anki_data_roots_win32_without_appdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"

    _patch_path_home(monkeypatch, home)
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    assert detect_mod._anki_data_roots() == [home / "AppData" / "Roaming" / "Anki2"]


def test_anki_data_roots_linux_with_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"

    _patch_path_home(monkeypatch, home)
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    assert detect_mod._anki_data_roots() == [
        xdg / "Anki2",
        home / ".var" / "app" / "net.ankiweb.Anki" / "data" / "Anki2",
        home / "snap" / "anki" / "current" / ".local" / "share" / "Anki2",
    ]


def test_anki_data_roots_linux_without_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"

    _patch_path_home(monkeypatch, home)
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert detect_mod._anki_data_roots() == [
        home / ".local" / "share" / "Anki2",
        home / ".var" / "app" / "net.ankiweb.Anki" / "data" / "Anki2",
        home / "snap" / "anki" / "current" / ".local" / "share" / "Anki2",
    ]
