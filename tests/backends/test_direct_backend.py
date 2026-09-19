"""``DirectBackend`` is the store plus a backend identity (#31).

There is no delegation layer left to test; what matters is that every
``AnkiBackend`` method on the store accepts the *protocol's* calling
convention, because ``cli``/``tui`` call through the protocol with positional
arguments.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from anki_cli.backends.direct import DirectBackend
from anki_cli.backends.protocol import AnkiBackend
from anki_cli.db.store import AnkiDirectStore
from tests.conftest import new_collection


def test_init_missing_collection_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="Direct collection not found"):
        DirectBackend(missing)


def test_init_sets_identity_and_resolved_collection_path(tmp_path: Path) -> None:
    col = new_collection(tmp_path / "sub" / ".." / "collection.anki2")

    backend = DirectBackend(col.db_path)

    assert backend.name == "direct"
    assert backend.supports_scheduler_introspection is True
    assert backend.collection_path == col.db_path.resolve()
    assert backend.db_path == backend.collection_path
    assert isinstance(backend, AnkiDirectStore)
    # One real call through the backend: the store is the backend, no forwarding.
    assert [d["name"] for d in backend.get_decks()] == ["Default"]


def _protocol_methods() -> list[str]:
    return sorted(
        name
        for name, member in inspect.getmembers(AnkiBackend)
        if inspect.isfunction(member) and not name.startswith("_")
    )


def test_protocol_exposes_the_expected_surface() -> None:
    """Guards the parametrize below against silently collecting nothing."""
    methods = _protocol_methods()
    assert len(methods) > 40
    assert {"answer_card", "find_notes", "find_cards", "add_note"} <= set(methods)


@pytest.mark.parametrize("method", _protocol_methods())
def test_store_signature_accepts_the_protocol_calling_convention(method: str) -> None:
    """Every parameter the protocol lets callers pass positionally must be
    positional on the store too, in the same order, defaults must match, and
    the store may not demand anything the protocol does not name."""
    proto_fn = getattr(AnkiBackend, method)
    # Resolve on the store, not on DirectBackend: a method missing from the
    # store would otherwise fall through the MRO to the protocol's own stub and
    # compare equal to itself.
    impl_fn = getattr(AnkiDirectStore, method)
    assert impl_fn is not proto_fn, f"{method}: store does not implement it"

    proto = inspect.signature(proto_fn)
    impl = inspect.signature(impl_fn)

    proto_params = list(proto.parameters.values())[1:]  # drop self
    impl_param_list = list(impl.parameters.values())[1:]
    impl_params = {p.name: p for p in impl_param_list}

    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    proto_order = [p.name for p in proto_params if p.kind is positional]
    impl_order = [p.name for p in impl_param_list if p.kind is positional]
    assert impl_order[: len(proto_order)] == proto_order, (
        f"{method}: positional order differs ({impl_order} vs {proto_order})"
    )

    for p in proto_params:
        got = impl_params.get(p.name)
        assert got is not None, f"{method}: store lacks parameter {p.name!r}"
        if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            assert got.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, (
                f"{method}: {p.name!r} is positional on the protocol but keyword-only on the store"
            )
        assert got.default == p.default, f"{method}: default for {p.name!r} differs"

    extra_required = [
        name
        for name, p in impl_params.items()
        if name not in proto.parameters and p.default is inspect.Parameter.empty
    ]
    assert not extra_required, f"{method}: store requires {extra_required} the protocol lacks"
