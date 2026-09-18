"""Is it safe to write to the collection right now?

Two independent probes, both shared by ``detect_backend`` (which picks a
backend up front) and the direct store's write guard (which re-checks
immediately before every write, since Anki may have started in between):

* :func:`anki_process_running` - is Anki Desktop running on this machine?
* :func:`sqlite_write_locked` - does the collection file hold a write lock?

This module deliberately imports nothing from ``anki_cli.backends`` so the
``db`` package never depends upward on it (#31).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def anki_process_running() -> bool:
    if sys.platform == "win32":
        return anki_process_running_windows()
    if sys.platform == "darwin":
        return anki_process_running_macos()
    return anki_process_running_linux()


def anki_process_running_linux() -> bool:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return False

    current_pid = str(os.getpid())
    desktop_names = {"anki", "anki-bin", "anki.exe"}
    flatpak_app_id = "net.ankiweb.anki"

    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or entry.name == current_pid:
            continue

        comm = entry / "comm"
        cmdline = entry / "cmdline"

        try:
            if comm.exists():
                name = comm.read_text(encoding="utf-8", errors="ignore").strip().lower()
                if name in desktop_names:
                    return True

            if cmdline.exists():
                raw = cmdline.read_bytes().split(b"\x00")
                argv = [
                    part.decode("utf-8", errors="ignore").strip().lower() for part in raw if part
                ]
                if not argv:
                    continue

                argv0_name = Path(argv[0]).name.lower()
                if argv0_name in desktop_names:
                    return True

                if argv0_name == "flatpak" and any(tok == flatpak_app_id for tok in argv[1:]):
                    return True

        except OSError:
            continue

    return False


def anki_process_running_macos() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-xi", "anki"],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def anki_process_running_windows() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq anki.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        output = result.stdout.lower()
        return "anki.exe" in output
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def sqlite_write_locked(db_path: Path) -> bool:
    if not db_path.exists():
        return False

    conn: sqlite3.Connection | None = None
    try:
        # ``as_uri`` percent-encodes the path: a raw ``?``/``#``/``%`` in
        # ``db_path`` would corrupt the URI and make the probe fail with
        # "unable to open", which must not be read as "not locked".
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.05)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return True
        # Fail closed: an unexpected probe error means the lock state is
        # unknown, not that the collection is safe to write.
        raise
    finally:
        if conn is not None:
            conn.close()
