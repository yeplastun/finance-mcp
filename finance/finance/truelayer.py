import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://auth.truelayer.com"
API_URL = "https://api.truelayer.com/data/v1"
SCOPES = ("info", "accounts", "cards", "transactions", "offline_access")


class BankingError(Exception):
    """Safe to display: never includes a response body, token or request URL."""


class NotSupported(BankingError):
    """The provider answered 501: this endpoint is not offered for this bank."""


class TrueLayerClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http

    def providers(self) -> list[dict]:
        body = self._send("GET", f"{AUTH_URL}/api/providers")
        if not isinstance(body, list) or any(not isinstance(p, dict) for p in body):
            raise BankingError("Unexpected provider list shape.")
        return body

    def auth_link(self, provider: dict, redirect_uri: str, state: str) -> str:
        scopes = [scope for scope in SCOPES if scope in provider.get("scopes", [])]
        return f"{AUTH_URL}/?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "provider_id": provider["provider_id"],
                "state": state,
            }
        )

    def token(self, **grant: str) -> dict:
        body = self._send(
            "POST",
            f"{AUTH_URL}/connect/token",
            data={"client_id": self.client_id, "client_secret": self.client_secret, **grant},
        )
        if not isinstance(body, dict) or not body.get("access_token") or not body.get("refresh_token"):
            raise BankingError("Token response lacked an access or refresh token; reconnect this bank.")
        return body

    def results(self, access_token: str, path: str, **params: str) -> list[dict]:
        body = self._send("GET", f"{API_URL}{path}", params=params, headers={"Authorization": f"Bearer {access_token}"})
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BankingError("Unexpected results shape.")
        return rows

    def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError:
                if method == "GET" and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise BankingError("Network request failed; retry later.") from None
            if method == "GET" and response.status_code in {408, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2**attempt)
                continue
            try:
                body = response.json()
            except ValueError:
                raise BankingError(f"HTTP {response.status_code}: non-JSON response.") from None
            if response.is_success:
                return body
            code = body.get("error", "unknown_error") if isinstance(body, dict) else "unknown_error"
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z_]{1,80}", code):
                code = "unknown_error"
            hint = ""
            if response.status_code == 401 or code == "invalid_grant":
                hint = " Consent expired or was revoked; reconnect this bank."
            elif response.status_code == 429:
                hint = " Rate limited; stop and retry on a later scheduled run."
            error = NotSupported if response.status_code == 501 else BankingError
            raise error(f"HTTP {response.status_code}: {code}.{hint}")
        raise AssertionError("Request retry loop exhausted")
