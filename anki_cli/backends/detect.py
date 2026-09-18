from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, get_args
from urllib.parse import urlparse

import httpx

from anki_cli.db.lock import anki_process_running as _anki_process_running
from anki_cli.db.lock import sqlite_write_locked as _sqlite_write_locked
from anki_cli.models.config import (
    DEFAULT_ANKICONNECT_URL,
    BackendPreference,
)

BackendName = Literal["ankiconnect", "direct"]
DEFAULT_ANKICONNECT_TIMEOUT_S: Final[float] = 0.35
_LOCAL_HOSTS: Final[set[str]] = {"localhost", "127.0.0.1", "::1"}


class DetectionError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class DetectionResult:
    backend: BackendName
    collection_path: Path | None
    reason: str
    profile: str | None = None
    """Profile directory that owns ``collection_path`` when one was
    discovered (never set for an explicit ``--col``/``collection.path``)."""


def detect_backend(
    *,
    forced_backend: str = "auto",
    col_override: Path | None = None,
    ankiconnect_url: str = DEFAULT_ANKICONNECT_URL,
    anki_profile: str | None = None,
    allow_non_localhost: bool = False,
) -> DetectionResult:
    forced = forced_backend.strip().lower()

    if forced not in get_args(BackendPreference):
        raise DetectionError(
            f"Unsupported backend '{forced_backend}'. Expected auto|ankiconnect|direct.",
            exit_code=2,
        )

    profile = anki_profile.strip() if anki_profile else ""

    if forced == "ankiconnect":
        if not _ankiconnect_reachable(ankiconnect_url, allow_non_localhost):
            raise DetectionError(
                f"AnkiConnect backend forced, but it is not reachable at {ankiconnect_url}.",
                exit_code=7,
            )
        # The collection lookup is informational here — the backend answers
        # without it, so a profile miss or a stale --col must not fail.
        info = _pick_collection(_discover_collections(col_override), profile)
        return DetectionResult(
            "ankiconnect",
            info,
            "forced",
            profile=info.parent.name if info is not None and col_override is None else None,
        )

    if forced == "direct":
        path = _require_direct_collection(
            col_override,
            profile,
            empty_message="Direct backend forced, but no Anki collection DB was found.",
        )
        if _anki_process_running() or _probe_write_lock(path):
            raise DetectionError(
                "Anki Desktop appears to be running while AnkiConnect is unavailable. "
                "Close Anki Desktop or use --backend ankiconnect.",
                exit_code=7,
            )
        return DetectionResult(
            "direct",
            path,
            "forced",
            profile=path.parent.name if col_override is None else None,
        )

    if _ankiconnect_reachable(ankiconnect_url, allow_non_localhost):
        info = _pick_collection(_discover_collections(col_override), profile)
        return DetectionResult(
            "ankiconnect",
            info,
            "ankiconnect reachable",
            profile=info.parent.name if info is not None and col_override is None else None,
        )

    direct_path = _require_direct_collection(
        col_override,
        profile,
        empty_message="No AnkiConnect and no collection found.",
    )
    if _anki_process_running() or _probe_write_lock(direct_path):
        raise DetectionError(
            "Anki is running but AnkiConnect is unavailable. "
            "Install AnkiConnect or close Anki Desktop.",
            exit_code=7,
        )
    return DetectionResult(
        "direct",
        direct_path,
        "ankiconnect unavailable, direct collection found",
        profile=direct_path.parent.name if col_override is None else None,
    )


def _ankiconnect_reachable(url: str, allow_non_localhost: bool = False) -> bool:
    host = urlparse(url).hostname or ""
    if not allow_non_localhost and host not in _LOCAL_HOSTS:
        # Don't leak a probe to a host the config hasn't allowed; data ops
        # would refuse it anyway via AnkiConnectBackend._validate_url.
        return False

    payload = {"action": "version", "version": 6}
    try:
        with httpx.Client(timeout=DEFAULT_ANKICONNECT_TIMEOUT_S) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(data, dict) and data.get("error") is None and "result" in data


def _discover_collections(col_override: Path | None) -> list[Path]:
    """Candidate collection DBs: the explicit override, else every
    ``<root>/<profile>/collection.anki21b|.anki2`` under the Anki data roots.
    Pure discovery — no decisions, no raises."""
    if col_override is not None:
        resolved = col_override.expanduser().resolve()
        return [resolved] if resolved.exists() else []

    candidates: list[Path] = []
    for root in _anki_data_roots():
        if not root.exists():
            continue
        for profile_dir in sorted(root.iterdir()):
            if not profile_dir.is_dir():
                continue
            for filename in ("collection.anki21b", "collection.anki2"):
                db_path = profile_dir / filename
                if db_path.exists():
                    candidates.append(db_path)
    return candidates


def _pick_collection(candidates: list[Path], anki_profile: str | None) -> Path | None:
    """Best-effort pick for display only: the configured profile's
    collection, else the first candidate; ``None`` when nothing matches.
    Never raises."""
    if anki_profile:
        return next((p for p in candidates if p.parent.name == anki_profile), None)
    return candidates[0] if candidates else None


def _require_direct_collection(
    col_override: Path | None,
    anki_profile: str | None,
    *,
    empty_message: str = "No Anki collection DB was found.",
) -> Path:
    """Resolve the collection the direct backend will open, or raise exit 3
    naming what actually went wrong."""
    if col_override is not None:
        resolved = col_override.expanduser().resolve()
        if not resolved.exists():
            raise DetectionError(
                f"Collection override {resolved} does not exist.",
                exit_code=3,
            )
        return resolved

    profile = anki_profile.strip() if anki_profile else ""
    candidates = _discover_collections(None)
    picked = _pick_collection(candidates, profile or None)
    if picked is not None:
        return picked

    if profile and candidates:
        available = ", ".join(sorted({p.parent.name for p in candidates}))
        raise DetectionError(
            f"Anki profile '{profile}' not found. Available: {available}.",
            exit_code=3,
        )
    raise DetectionError(empty_message, exit_code=3)


def _anki_data_roots() -> list[Path]:
    import sys

    home = Path.home()
    roots: list[Path] = []

    if sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "Anki2")

    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "Anki2")
        else:
            roots.append(home / "AppData" / "Roaming" / "Anki2")

    else:
        # Linux: native / AppImage
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            roots.append(Path(xdg_data) / "Anki2")
        else:
            roots.append(home / ".local" / "share" / "Anki2")

        # Linux: Flatpak
        roots.append(home / ".var" / "app" / "net.ankiweb.Anki" / "data" / "Anki2")

        # Linux: Snap
        roots.append(home / "snap" / "anki" / "current" / ".local" / "share" / "Anki2")

    return roots


def _probe_write_lock(db_path: Path) -> bool:
    """``_sqlite_write_locked`` for ``detect_backend``, failures translated.

    ``detect_backend`` runs for every subcommand and its only documented
    failure type is ``DetectionError``; a probe error (unopenable path,
    non-SQLite file) must surface as BACKEND_UNAVAILABLE, not a raw
    ``sqlite3`` traceback out of the Click group.
    """
    try:
        return _sqlite_write_locked(db_path)
    except sqlite3.Error as exc:
        raise DetectionError(
            f"Cannot probe the collection lock state at {db_path}: {exc}",
            exit_code=7,
        ) from exc
