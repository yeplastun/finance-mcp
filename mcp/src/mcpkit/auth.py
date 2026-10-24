"""Caller authentication.

A backend credential (a database role, a Drive token) authenticates *this
server* to its data. It says nothing about who is calling the server. Without a
second layer, anyone who finds the URL reads everything behind it.

claude.ai connectors cannot present a static API key or custom header — they
connect anonymously or complete an OAuth flow — so OAuth is the only mechanism
that is both locked down and usable from the web client.

GitHubProvider handles the flow but has **no user allowlist**: out of the box,
any GitHub account that completes it gets a valid token. AllowlistMiddleware is
what closes that, and it is not optional.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST

from .config import KitConfig


class AllowlistMiddleware(Middleware):
    """Admit exactly one GitHub account, by its numeric user ID.

    The ID, not the login: a login can be renamed and then registered by
    someone else, the ID never changes. GitHubProvider puts it in `sub`.

    Fails closed: a missing token, missing claims or missing ID is a denial,
    not a pass. Registered at server level so it covers every tool, including
    any added later. It checks every request, not only tool calls: another
    account that completes the OAuth flow can't connect or list the tools
    either, so it never learns what this server holds.
    """

    def __init__(self, allowed_id: str) -> None:
        self.allowed_id = allowed_id

    def _user_id(self) -> str | None:
        try:
            token = get_access_token()
        except Exception:
            return None
        claims: dict[str, Any] = getattr(token, "claims", None) or {}
        user_id = claims.get("sub")
        return user_id if isinstance(user_id, str) and user_id else None

    def _denial(self) -> str | None:
        user_id = self._user_id()
        if user_id is None:
            return "access denied: no authenticated GitHub identity"
        if user_id != self.allowed_id:
            return f"access denied for GitHub user ID {user_id!r}"
        return None

    @override
    async def on_request(self, context: MiddlewareContext, call_next: Callable) -> Any:
        if denial := self._denial():
            raise MCPError(INVALID_REQUEST, denial)
        return await call_next(context)

    @override
    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable) -> Any:
        if denial := self._denial():
            raise ToolError(denial)
        return await call_next(context)


def github_provider(config: KitConfig) -> GitHubProvider:
    client_id = config.github_client_id
    client_secret = config.github_client_secret
    base_url = config.base_url
    if not (client_id and client_secret and base_url and config.allowed_gh_id):
        raise ValueError(
            "GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, BASE_URL and ALLOWED_GH_ID "
            "must all be set to serve over HTTP"
        )
    return GitHubProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
    )
