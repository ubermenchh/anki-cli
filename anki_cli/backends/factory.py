from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from anki_cli.backends.ankiconnect import AnkiConnectBackend, AnkiConnectError
from anki_cli.backends.detect import DetectionError, detect_backend
from anki_cli.backends.direct import DirectBackend
from anki_cli.backends.protocol import AnkiBackend
from anki_cli.db.errors import UnsupportedCollectionError
from anki_cli.models.config import DEFAULT_ANKICONNECT_URL, AppConfig
from anki_cli.models.output import ExitCode

DETECTION_PENDING = "pending"
"""``obj["backend_reason"]`` value meaning "detect on first use" (#32).

The CLI group callback used to probe AnkiConnect, scan for an Anki process and
open the collection on *every* invocation, then the factory posted ``version``
again. Now the callback only records what was requested and the first
``create_backend_from_context`` call detects, writing the result back into
``obj`` so later sessions in the same process (the REPL opens one per action)
reuse it.
"""


class BackendFactoryError(RuntimeError):
    """Base backend factory error.

    ``exit_code`` preserves a detection failure's meaning: 3 when nothing was
    found, 7 when the backend exists but is unavailable.
    """

    def __init__(self, message: str, *, exit_code: int = ExitCode.BACKEND_UNAVAILABLE) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)


def _resolve_pending_detection(obj: dict[str, Any]) -> None:
    """Run detection once and record the outcome in ``obj``.

    Success writes ``backend`` / ``collection_path`` / ``backend_reason`` /
    ``ankiconnect_version``; failure writes ``backend = "none"`` with the
    reason and exit code, so a second call reports the same failure without
    re-probing.
    """
    if obj.get("backend_reason") != DETECTION_PENDING:
        return
    app_config = obj.get("app_config")
    cfg = app_config if isinstance(app_config, AppConfig) else AppConfig()
    col_override = obj.get("collection_override")
    try:
        detection = detect_backend(
            forced_backend=str(obj.get("requested_backend") or "auto"),
            col_override=col_override if isinstance(col_override, Path) else None,
            ankiconnect_url=cfg.backend.ankiconnect_url,
            anki_profile=cfg.collection.anki_profile,
            allow_non_localhost=cfg.backend.allow_non_localhost,
        )
    except DetectionError as exc:
        obj.update(
            {"backend": "none", "backend_reason": str(exc), "backend_exit_code": exc.exit_code}
        )
        raise BackendFactoryError(str(exc), exit_code=exc.exit_code) from exc
    obj.update(
        {
            "backend": detection.backend,
            "collection_path": detection.collection_path,
            "backend_reason": detection.reason,
            "ankiconnect_version": detection.ankiconnect_version,
        }
    )


def create_backend_from_context(obj: dict[str, Any]) -> AnkiBackend:
    _resolve_pending_detection(obj)
    backend_name = str(obj.get("backend", "")).strip().lower()
    collection_path = _coerce_path(obj.get("collection_path"))
    app_config = obj.get("app_config")

    ankiconnect_url = DEFAULT_ANKICONNECT_URL
    allow_non_localhost = False
    if isinstance(app_config, AppConfig):
        ankiconnect_url = app_config.backend.ankiconnect_url
        allow_non_localhost = app_config.backend.allow_non_localhost

    if backend_name == "ankiconnect":
        # Detection already received ``version``; only re-ask when it did not
        # run (a caller built ``obj`` by hand) or returned something too old
        # or malformed, so the backend can raise the descriptive error.
        probed = obj.get("ankiconnect_version")
        verify_version = not (isinstance(probed, int) and probed >= AnkiConnectBackend.API_VERSION)
        try:
            return AnkiConnectBackend(
                url=ankiconnect_url,
                collection_path=collection_path,
                verify_version=verify_version,
                allow_non_localhost=allow_non_localhost,
            )
        except AnkiConnectError as exc:
            raise BackendFactoryError(str(exc)) from exc

    if backend_name == "direct":
        if collection_path is None:
            raise BackendFactoryError("Direct backend requires a collection path.")
        try:
            return DirectBackend(collection_path)
        except (FileNotFoundError, UnsupportedCollectionError) as exc:
            raise BackendFactoryError(str(exc)) from exc

    if backend_name in {"", "none"}:
        # Reached when detection failed and the caller degraded to a
        # backend-less context (e.g. the REPL on a host with no Anki) —
        # surface the recorded detection failure, not a cryptic name error.
        reason = str(obj.get("backend_reason") or "").strip().rstrip(".")
        detail = f": {reason}" if reason and reason != "not required" else ""
        exit_code = obj.get("backend_exit_code")
        raise BackendFactoryError(
            f"No Anki backend available{detail}.",
            exit_code=exit_code if isinstance(exit_code, int) else ExitCode.BACKEND_UNAVAILABLE,
        )

    raise BackendFactoryError(f"Unknown backend '{backend_name}'.")


@contextmanager
def backend_session_from_context(obj: dict[str, Any]) -> Generator[AnkiBackend, None, None]:
    backend = create_backend_from_context(obj)
    try:
        yield backend
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _coerce_path(value: object) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return None
