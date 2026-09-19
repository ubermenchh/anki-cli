from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from anki_cli.backends.ankiconnect import (
    AnkiConnectAPIError,
    AnkiConnectBackend,
    AnkiConnectProtocolError,
    AnkiConnectUnavailableError,
)


def _backend_with_handler(
    handler: Any,
    *,
    url: str = "http://localhost:8765",
    verify_version: bool = False,
    api_version: int = 6,
    allow_non_localhost: bool = False,
) -> AnkiConnectBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return AnkiConnectBackend(
        url=url,
        verify_version=verify_version,
        api_version=api_version,
        allow_non_localhost=allow_non_localhost,
        client=client,
    )


def _json_handler(body: dict[str, Any], status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


def test_invoke_success_returns_result_and_sends_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"error": None, "result": {"ok": True}})

    backend = _backend_with_handler(handler)
    result = backend._invoke("version")

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://localhost:8765"
    assert captured["payload"] == {
        "action": "version",
        "version": 6,
        "params": {},
    }


def test_invoke_timeout_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    backend = _backend_with_handler(handler)

    with pytest.raises(AnkiConnectUnavailableError, match="Timeout contacting AnkiConnect"):
        backend._invoke("version")


def test_invoke_connect_error_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = _backend_with_handler(handler)

    with pytest.raises(AnkiConnectUnavailableError, match="Cannot connect to AnkiConnect"):
        backend._invoke("version")


def test_invoke_http_error_maps_to_unavailable() -> None:
    backend = _backend_with_handler(_json_handler({"error": None, "result": None}, status_code=503))

    with pytest.raises(AnkiConnectUnavailableError, match="HTTP error contacting AnkiConnect"):
        backend._invoke("version")


def test_invoke_non_json_response_raises_protocol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    backend = _backend_with_handler(handler)

    with pytest.raises(AnkiConnectProtocolError, match="non-JSON"):
        backend._invoke("version")


def test_invoke_non_object_response_raises_protocol() -> None:
    backend = _backend_with_handler(lambda request: httpx.Response(200, json=[1, 2, 3]))

    with pytest.raises(AnkiConnectProtocolError, match="must be an object"):
        backend._invoke("version")


@pytest.mark.parametrize(
    "body",
    [
        {"result": 6},
        {"error": None},
    ],
)
def test_invoke_missing_required_keys_raises_protocol(body: dict[str, Any]) -> None:
    backend = _backend_with_handler(_json_handler(body))

    with pytest.raises(AnkiConnectProtocolError, match="missing required keys"):
        backend._invoke("version")


def test_invoke_api_error_raises_api_error_with_action() -> None:
    backend = _backend_with_handler(_json_handler({"error": "boom", "result": None}))

    with pytest.raises(AnkiConnectAPIError) as exc_info:
        backend._invoke("deckNamesAndIds")

    assert exc_info.value.action == "deckNamesAndIds"
    assert "boom" in str(exc_info.value)


def test_check_version_requires_integer_result() -> None:
    backend = _backend_with_handler(
        _json_handler({"error": None, "result": "6"}),
        api_version=6,
    )

    with pytest.raises(AnkiConnectProtocolError, match="Expected integer"):
        backend.check_version()


def test_check_version_rejects_too_old() -> None:
    backend = _backend_with_handler(
        _json_handler({"error": None, "result": 5}),
        api_version=6,
    )

    with pytest.raises(AnkiConnectProtocolError, match="too old"):
        backend.check_version()


def test_validate_url_rejects_non_http_scheme() -> None:
    with pytest.raises(AnkiConnectProtocolError, match="must use http:// or https://"):
        _backend_with_handler(_json_handler({"error": None, "result": 6}), url="ftp://localhost:8765")


def test_validate_url_rejects_non_localhost_by_default() -> None:
    with pytest.raises(AnkiConnectProtocolError, match="Refusing non-localhost"):
        _backend_with_handler(
            _json_handler({"error": None, "result": 6}),
            url="http://example.com:8765",
        )


def test_validate_url_allows_non_localhost_when_flag_enabled() -> None:
    backend = _backend_with_handler(
        _json_handler({"error": None, "result": 6}),
        url="http://example.com:8765",
        allow_non_localhost=True,
    )
    assert backend.name == "ankiconnect"


def test_normalize_ids_deduplicates_and_preserves_order() -> None:
    backend = _backend_with_handler(_json_handler({"error": None, "result": 6}))
    assert backend._normalize_ids([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_normalize_ids_rejects_non_int() -> None:
    backend = _backend_with_handler(_json_handler({"error": None, "result": 6}))

    values = cast(list[int], [1, "x"])
    with pytest.raises(AnkiConnectProtocolError, match="IDs must be integers"):
        backend._normalize_ids(values)


def test_normalize_tags_trims_and_deduplicates() -> None:
    backend = _backend_with_handler(_json_handler({"error": None, "result": 6}))
    assert backend._normalize_tags(["  a  ", "b", "a", "", "   "]) == ["a", "b"]


def test_deck_query_prefix_none_and_escaping() -> None:
    backend = _backend_with_handler(_json_handler({"error": None, "result": 6}))

    assert backend._deck_query_prefix(None) == ""
    assert backend._deck_query_prefix('Japanese "Core"') == 'deck:"Japanese \\"Core\\"" '
    assert backend._deck_query_prefix(r"C:\Anki\Deck") == 'deck:"C:\\\\Anki\\\\Deck" '


def _multi_envelope(results: list[Any]) -> dict[str, Any]:
    """What AnkiConnect returns for ``multi`` when every sub-action carries a
    version: one ``{"result", "error"}`` object per action."""
    return {"error": None, "result": [{"result": r, "error": None} for r in results]}


def test_get_due_counts_uses_expected_queries_in_one_request() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        requests.append(payload)
        return httpx.Response(200, json=_multi_envelope([[1, 2], [3], [4, 5, 6]]))

    backend = _backend_with_handler(handler)

    counts = backend.get_due_counts(deck='Deck "A"')

    assert counts == {"new": 2, "learn": 1, "review": 3, "total": 6}
    # Three findCards in one round trip (#32). Anki's is:due excludes new
    # cards, so the new count must not be gated on it (the old "is:due is:new"
    # query always returned 0).
    assert len(requests) == 1
    assert requests[0]["action"] == "multi"
    assert [a["action"] for a in requests[0]["params"]["actions"]] == ["findCards"] * 3
    assert [a["version"] for a in requests[0]["params"]["actions"]] == [6, 6, 6]
    assert [a["params"]["query"] for a in requests[0]["params"]["actions"]] == [
        'deck:"Deck \\"A\\"" is:new',
        'deck:"Deck \\"A\\"" is:due is:learn',
        'deck:"Deck \\"A\\"" is:due is:review',
    ]


# ---- transport hygiene (#32) --------------------------------------------------------


def test_requests_reuse_the_connection_and_do_not_ask_to_close() -> None:
    headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(dict(request.headers))
        return httpx.Response(200, json={"error": None, "result": 6})

    backend = _backend_with_handler(handler)
    backend._invoke("version")
    backend._invoke("version")

    assert all(h.get("connection", "").lower() != "close" for h in headers)


def test_default_client_connects_fast_and_reads_patiently() -> None:
    backend = AnkiConnectBackend(verify_version=False, timeout_seconds=1.5, read_timeout_seconds=45)
    try:
        timeout = backend._client.timeout
        assert timeout.connect == 1.5
        assert timeout.read == 45
        assert timeout.write == 45
        assert timeout.pool == 1.5
    finally:
        backend.close()


def test_remote_protocol_error_is_retried_once_on_a_fresh_request() -> None:
    """A stale keep-alive socket fails before Anki sees the request; the retry
    is what replaces the old blanket ``Connection: close``."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(200, json={"error": None, "result": 6})

    backend = _backend_with_handler(handler)

    assert backend._invoke("version") == 6
    assert attempts == 2


def test_remote_protocol_error_twice_is_unavailable() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    backend = _backend_with_handler(handler)

    with pytest.raises(AnkiConnectUnavailableError, match="disconnected unexpectedly"):
        backend._invoke("version")
    assert attempts == 2  # exactly one retry, never a loop


def test_other_transport_errors_are_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    backend = _backend_with_handler(handler)

    with pytest.raises(AnkiConnectUnavailableError):
        backend._invoke("findCards", query="deck:*")
    assert attempts == 1


# ---- multi -----------------------------------------------------------------------------


def test_multi_labels_every_action_with_the_version_and_unwraps_in_order() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=_multi_envelope([["a"], {"k": 1}]))

    backend = _backend_with_handler(handler)

    out = backend._multi([("deckNames", {}), ("getDeckConfig", {"deck": "D"})])

    assert out == [["a"], {"k": 1}]
    assert requests[0]["params"]["actions"] == [
        {"action": "deckNames", "version": 6, "params": {}},
        {"action": "getDeckConfig", "version": 6, "params": {"deck": "D"}},
    ]


def test_multi_with_no_actions_makes_no_request() -> None:
    backend = _backend_with_handler(lambda request: pytest.fail("no request expected"))
    assert backend._multi([]) == []


def test_multi_sub_action_error_names_that_action() -> None:
    body = {
        "error": None,
        "result": [
            {"result": ["Default"], "error": None},
            {"result": None, "error": "deck was not found: Nope"},
        ],
    }
    backend = _backend_with_handler(_json_handler(body))

    with pytest.raises(AnkiConnectAPIError) as excinfo:
        backend._multi([("deckNames", {}), ("getDeckConfig", {"deck": "Nope"})])
    assert excinfo.value.action == "getDeckConfig"
    assert "deck was not found" in str(excinfo.value)


@pytest.mark.parametrize(
    "result",
    [
        [{"result": 1, "error": None}],  # one item for two actions
        "nope",  # not a list
        [["bare"], ["bare"]],  # unlabelled sub-results (server ignored version)
    ],
    ids=["short", "not-a-list", "unwrapped-items"],
)
def test_multi_rejects_malformed_responses(result: Any) -> None:
    backend = _backend_with_handler(_json_handler({"error": None, "result": result}))

    with pytest.raises(AnkiConnectProtocolError):
        backend._multi([("deckNames", {}), ("deckNames", {})])
