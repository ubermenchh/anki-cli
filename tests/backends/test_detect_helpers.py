from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _patch_proc_root(monkeypatch: pytest.MonkeyPatch, proc_root: Path) -> None:
    real_path = Path

    def fake_path(raw):
        if str(raw) == "/proc":
            return proc_root
        return real_path(raw)

    monkeypatch.setattr(detect_mod, "Path", fake_path)


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


def test_resolve_direct_collection_scans_roots_and_profiles(
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

    assert detect_mod._resolve_direct_collection(None) == db_a


def test_resolve_direct_collection_returns_none_when_no_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root = tmp_path / "missing"
    monkeypatch.setattr(detect_mod, "_anki_data_roots", lambda: [missing_root])

    assert detect_mod._resolve_direct_collection(None) is None


def test_resolve_standalone_collection_override(tmp_path: Path) -> None:
    override = tmp_path / "x" / "collection.db"
    expected = override.resolve()

    assert detect_mod._resolve_standalone_collection(override) == expected


def test_resolve_standalone_collection_prefers_nearest_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)

    db = project / ".anki-cli" / "collection.db"
    db.parent.mkdir(parents=True)
    db.touch()

    monkeypatch.chdir(nested)

    assert detect_mod._resolve_standalone_collection(None) == db.resolve()


def test_resolve_standalone_collection_falls_back_to_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()

    _patch_path_home(monkeypatch, home)
    monkeypatch.chdir(work)

    expected = (home / ".local" / "share" / "anki-cli" / "collection.db").resolve()
    assert detect_mod._resolve_standalone_collection(None) == expected


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


def test_anki_process_running_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_mod, "_anki_process_running_windows", lambda: True)
    monkeypatch.setattr(detect_mod, "_anki_process_running_macos", lambda: False)
    monkeypatch.setattr(detect_mod, "_anki_process_running_linux", lambda: False)

    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    assert detect_mod._anki_process_running() is True

    monkeypatch.setattr(sys, "platform", "darwin", raising=False)
    assert detect_mod._anki_process_running() is False

    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    assert detect_mod._anki_process_running() is False


def test_anki_process_running_macos_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert detect_mod._anki_process_running_macos() is True

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert detect_mod._anki_process_running_macos() is False

    def raise_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert detect_mod._anki_process_running_macos() is False

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pgrep", timeout=2)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert detect_mod._anki_process_running_macos() is False


def test_anki_process_running_windows_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="anki.exe   1234", returncode=0),
    )
    assert detect_mod._anki_process_running_windows() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="something else", returncode=0),
    )
    assert detect_mod._anki_process_running_windows() is False

    def raise_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert detect_mod._anki_process_running_windows() is False

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="tasklist", timeout=3)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert detect_mod._anki_process_running_windows() is False


def test_anki_process_running_linux_returns_false_when_proc_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_proc = tmp_path / "proc"
    _patch_proc_root(monkeypatch, missing_proc)

    assert detect_mod._anki_process_running_linux() is False


def test_anki_process_running_linux_detects_by_comm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "1234"
    pid_dir.mkdir(parents=True)
    (pid_dir / "comm").write_text("anki\n", encoding="utf-8")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(detect_mod.os, "getpid", lambda: 99999)

    assert detect_mod._anki_process_running_linux() is True


def test_anki_process_running_linux_detects_by_argv0_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "2000"
    pid_dir.mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(b"/usr/bin/anki\x00")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(detect_mod.os, "getpid", lambda: 99999)

    assert detect_mod._anki_process_running_linux() is True


def test_anki_process_running_linux_detects_flatpak_app_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "3000"
    pid_dir.mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(b"flatpak\x00run\x00net.ankiweb.anki\x00")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(detect_mod.os, "getpid", lambda: 99999)

    assert detect_mod._anki_process_running_linux() is True


def test_anki_process_running_linux_skips_current_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "4000"
    pid_dir.mkdir(parents=True)
    (pid_dir / "comm").write_text("anki\n", encoding="utf-8")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(detect_mod.os, "getpid", lambda: 4000)

    assert detect_mod._anki_process_running_linux() is False


def test_sqlite_write_locked_non_lock_operational_error_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "collection.db"
    db_path.touch()

    def fake_connect(*args, **kwargs):
        raise sqlite3.OperationalError("permission denied")

    monkeypatch.setattr(detect_mod.sqlite3, "connect", fake_connect)

    assert detect_mod._sqlite_write_locked(db_path) is False