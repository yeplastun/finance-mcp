import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from finance.truelayer import BankingError, NotSupported, TrueLayerClient


def client_for(handler) -> TrueLayerClient:
    return TrueLayerClient("cid", "secret", httpx.Client(transport=httpx.MockTransport(handler)))


def test_auth_link_limits_scopes_to_provider():
    link = client_for(lambda r: None).auth_link(
        {"provider_id": "ob-amex", "scopes": ["info", "cards", "balance", "transactions", "offline_access"]},
        "https://127.0.0.1:8765/callback",
        "st",
    )
    query = parse_qs(urlsplit(link).query)
    assert query["scope"] == ["info cards transactions offline_access"]
    assert query["provider_id"] == ["ob-amex"] and query["state"] == ["st"]


def test_token_sends_secret_in_body_and_requires_refresh_token():
    def handler(request: httpx.Request):
        assert parse_qs(request.content.decode())["client_secret"] == ["secret"]
        return httpx.Response(200, json={"access_token": "a"})

    with pytest.raises(BankingError, match="refresh token"):
        client_for(handler).token(grant_type="refresh_token", refresh_token="r")


def test_results_retries_get_on_5xx(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(502, json={}) if len(calls) < 3 else httpx.Response(200, json={"results": [{"x": 1}]})

    assert client_for(handler).results("tok", "/accounts") == [{"x": 1}]
    assert len(calls) == 3


def test_errors_are_safe_and_typed():
    leak = "SECRET_ACCOUNT_ID"

    def handler(request: httpx.Request):
        status = {"/data/v1/a": 501, "/data/v1/b": 401, "/data/v1/c": 400}[request.url.path]
        return httpx.Response(status, json={"error": "invalid_request" if status == 400 else "x", "details": leak})

    client = client_for(handler)
    with pytest.raises(NotSupported):
        client.results("tok", "/a")
    with pytest.raises(BankingError, match="reconnect this bank"):
        client.results("tok", "/b")
    with pytest.raises(BankingError) as error:
        client.results("tok", "/c")
    assert str(error.value) == "HTTP 400: invalid_request."
