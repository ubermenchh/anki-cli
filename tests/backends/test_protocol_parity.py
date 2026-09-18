"""#30 item 2: the scheduler-introspection methods are on the Protocol, both
backends implement them, and no consumer probes ``backend.name == "direct"`` or
``backend._store`` any more."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from anki_cli.backends.ankiconnect import AnkiConnectBackend
from anki_cli.backends.direct import DirectBackend
from anki_cli.backends.protocol import AnkiBackend, BackendUnsupportedError

INTROSPECTION = (
    "get_next_due_card",
    "preview_ratings",
    "snapshot_card_state",
    "restore_card_state",
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("cls", [DirectBackend, AnkiConnectBackend])
def test_backends_implement_every_protocol_method(cls: type) -> None:
    protocol_methods = {
        name
        for name, member in inspect.getmembers(AnkiBackend)
        if inspect.isfunction(member) and not name.startswith("_")
    }
    missing = [m for m in protocol_methods if not callable(getattr(cls, m, None))]
    assert not missing, f"{cls.__name__} lacks {missing}"
    assert isinstance(cls.supports_scheduler_introspection, bool)


def test_capability_flags() -> None:
    assert DirectBackend.supports_scheduler_introspection is True
    assert AnkiConnectBackend.supports_scheduler_introspection is False


@pytest.mark.parametrize("method", INTROSPECTION)
def test_ankiconnect_raises_backend_unsupported(method: str) -> None:
    backend = AnkiConnectBackend.__new__(AnkiConnectBackend)  # no HTTP probe
    args = {"get_next_due_card": (), "preview_ratings": (1,),
            "snapshot_card_state": (1,), "restore_card_state": ({},)}[method]

    with pytest.raises(BackendUnsupportedError) as excinfo:
        getattr(backend, method)(*args)

    err = excinfo.value
    assert isinstance(err, NotImplementedError)  # -> BACKEND_UNAVAILABLE at the CLI
    assert err.operation == method
    assert err.backend == "ankiconnect"
    assert str(err) == (
        f"{method} is not supported by the ankiconnect backend. Use --backend direct."
    )


def test_no_consumer_probes_the_backend_by_name_or_store() -> None:
    """The point of the Protocol additions: callers branch on the capability."""
    probe = re.compile(
        r'(_store\b'
        r'|getattr\([^,]+,\s*"name"[^)]*\)\s*[!=]=\s*"direct"'
        r'|\.name\s*[!=]=\s*"direct")'
    )
    offenders: list[str] = []
    for path in [*ROOT.glob("anki_cli/cli/**/*.py"), *ROOT.glob("anki_cli/tui/**/*.py"),
                 *ROOT.glob("anki_cli/core/**/*.py")]:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if probe.search(line) and "UndoStore" not in line and "undo_store" not in line.lower():
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
