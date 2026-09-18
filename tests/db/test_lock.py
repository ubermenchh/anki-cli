"""``anki_cli.db.lock``: the two probes that decide whether a direct write is
safe. Shared by ``detect_backend`` and the store's write guard (#31)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import anki_cli.db.lock as lock_mod


def _patch_proc_root(monkeypatch: pytest.MonkeyPatch, proc_root: Path) -> None:
    real_path = Path

    def fake_path(raw):
        if str(raw) == "/proc":
            return proc_root
        return real_path(raw)

    monkeypatch.setattr(lock_mod, "Path", fake_path)


def test_anki_process_running_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock_mod, "anki_process_running_windows", lambda: True)
    monkeypatch.setattr(lock_mod, "anki_process_running_macos", lambda: False)
    monkeypatch.setattr(lock_mod, "anki_process_running_linux", lambda: False)

    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    assert lock_mod.anki_process_running() is True

    monkeypatch.setattr(sys, "platform", "darwin", raising=False)
    assert lock_mod.anki_process_running() is False

    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    assert lock_mod.anki_process_running() is False


def test_anki_process_running_macos_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert lock_mod.anki_process_running_macos() is True

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert lock_mod.anki_process_running_macos() is False

    def raise_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert lock_mod.anki_process_running_macos() is False

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pgrep", timeout=2)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert lock_mod.anki_process_running_macos() is False


def test_anki_process_running_windows_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="anki.exe   1234", returncode=0),
    )
    assert lock_mod.anki_process_running_windows() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="something else", returncode=0),
    )
    assert lock_mod.anki_process_running_windows() is False

    def raise_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert lock_mod.anki_process_running_windows() is False

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="tasklist", timeout=3)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert lock_mod.anki_process_running_windows() is False


def test_anki_process_running_linux_returns_false_when_proc_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_proc = tmp_path / "proc"
    _patch_proc_root(monkeypatch, missing_proc)

    assert lock_mod.anki_process_running_linux() is False


def test_anki_process_running_linux_detects_by_comm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "1234"
    pid_dir.mkdir(parents=True)
    (pid_dir / "comm").write_text("anki\n", encoding="utf-8")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(lock_mod.os, "getpid", lambda: 99999)

    assert lock_mod.anki_process_running_linux() is True


def test_anki_process_running_linux_detects_by_argv0_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "2000"
    pid_dir.mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(b"/usr/bin/anki\x00")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(lock_mod.os, "getpid", lambda: 99999)

    assert lock_mod.anki_process_running_linux() is True


def test_anki_process_running_linux_detects_flatpak_app_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "3000"
    pid_dir.mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(b"flatpak\x00run\x00net.ankiweb.anki\x00")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(lock_mod.os, "getpid", lambda: 99999)

    assert lock_mod.anki_process_running_linux() is True


def test_anki_process_running_linux_skips_current_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "4000"
    pid_dir.mkdir(parents=True)
    (pid_dir / "comm").write_text("anki\n", encoding="utf-8")

    _patch_proc_root(monkeypatch, proc_root)
    monkeypatch.setattr(lock_mod.os, "getpid", lambda: 4000)

    assert lock_mod.anki_process_running_linux() is False


def test_sqlite_write_locked_non_lock_operational_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that cannot run must not report "not locked" — it re-raises."""
    db_path = tmp_path / "collection.db"
    db_path.touch()

    def fake_connect(*args, **kwargs):
        raise sqlite3.OperationalError("permission denied")

    monkeypatch.setattr(lock_mod.sqlite3, "connect", fake_connect)

    with pytest.raises(sqlite3.OperationalError, match="permission denied"):
        lock_mod.sqlite_write_locked(db_path)


@pytest.mark.parametrize(
    "filename",
    [
        "col%100.db",
        "col#1.db",
        "col 100.db",
        pytest.param(
            "col?100.db",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="'?' is reserved in NTFS filenames",
            ),
        ),
    ],
    ids=["percent", "hash", "space", "question-mark"],
)
def test_sqlite_write_locked_probes_path_with_uri_special_chars(
    tmp_path: Path,
    filename: str,
) -> None:
    """``?``/``#``/``%``/space in the filename must be percent-encoded, else the
    URI probe silently fails open on exactly the collections it exists to
    protect. (``#`` truncates the URI at the fragment; ``?`` starts the query —
    both cut the path before SQLite sees it.)"""
    db_path = tmp_path / filename
    setup = sqlite3.connect(str(db_path))
    setup.execute("CREATE TABLE t (id INTEGER)")
    setup.commit()
    setup.close()

    locker = sqlite3.connect(str(db_path), isolation_level=None, timeout=1.0)
    locker.execute("BEGIN IMMEDIATE")
    try:
        assert lock_mod.sqlite_write_locked(db_path) is True
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    assert lock_mod.sqlite_write_locked(db_path) is False


def test_sqlite_write_locked_false_when_db_missing(tmp_path: Path) -> None:
    assert lock_mod.sqlite_write_locked(tmp_path / "missing.db") is False


def test_sqlite_write_locked_false_when_db_is_writable(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert lock_mod.sqlite_write_locked(db_path) is False


def test_sqlite_write_locked_true_when_other_connection_holds_immediate_lock(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "collection.db"
    setup = sqlite3.connect(str(db_path))
    setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    setup.commit()
    setup.close()

    locker = sqlite3.connect(str(db_path), isolation_level=None, timeout=1.0)
    locker.execute("BEGIN IMMEDIATE")
    try:
        assert lock_mod.sqlite_write_locked(db_path) is True
    finally:
        locker.execute("ROLLBACK")
        locker.close()
