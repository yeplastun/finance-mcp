"""The allowlist.

GitHubProvider admits any GitHub account that completes the OAuth flow. This
middleware is the only thing standing between a public URL and every message in
the account, so its failure modes matter more than its success one.
"""

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from mcpkit import auth
from mcpkit.auth import AllowlistMiddleware, github_provider
from mcpkit.config import KitConfig

SENTINEL = object()
OWNER_ID = "1234567"


async def call_next(context):
    return SENTINEL


def token(claims):
    return SimpleNamespace(claims=claims)


@pytest.fixture
def middleware():
    return AllowlistMiddleware(OWNER_ID)


def as_user(monkeypatch, value):
    """Patch the token the middleware will see. `value` may be an exception."""

    def fake():
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(auth, "get_access_token", fake)


async def test_allows_the_owner(monkeypatch, middleware):
    as_user(monkeypatch, token({"sub": OWNER_ID, "login": "owner"}))
    assert await middleware.on_call_tool(None, call_next) is SENTINEL


async def test_rejects_another_github_user(monkeypatch, middleware):
    as_user(monkeypatch, token({"sub": "1", "login": "someone-else"}))
    with pytest.raises(ToolError, match="access denied for GitHub user ID"):
        await middleware.on_call_tool(None, call_next)


async def test_the_owners_login_on_another_account_is_rejected(monkeypatch, middleware):
    """A renamed login can be registered by someone else; only the ID counts."""
    as_user(monkeypatch, token({"sub": "1", "login": "owner"}))
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, call_next)


# ── fail closed ─────────────────────────────────────────────────────────────


async def test_rejects_a_missing_id_claim(monkeypatch, middleware):
    as_user(monkeypatch, token({"login": "owner"}))
    with pytest.raises(ToolError, match="no authenticated GitHub identity"):
        await middleware.on_call_tool(None, call_next)


async def test_rejects_empty_claims(monkeypatch, middleware):
    as_user(monkeypatch, token({}))
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, call_next)


async def test_rejects_when_claims_are_absent_entirely(monkeypatch, middleware):
    as_user(monkeypatch, SimpleNamespace())
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, call_next)


async def test_rejects_when_there_is_no_token(monkeypatch, middleware):
    as_user(monkeypatch, None)
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, call_next)


async def test_rejects_when_token_lookup_raises(monkeypatch, middleware):
    """An unauthenticated request raises rather than returning None."""
    as_user(monkeypatch, RuntimeError("no auth context"))
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, call_next)


async def test_rejects_a_non_string_id(monkeypatch, middleware):
    as_user(monkeypatch, token({"sub": {"$ne": None}}))
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, call_next)


async def test_denied_calls_never_reach_the_tool(monkeypatch, middleware):
    reached = False

    async def spy(context):
        nonlocal reached
        reached = True

    as_user(monkeypatch, token({"sub": "1", "login": "intruder"}))
    with pytest.raises(ToolError):
        await middleware.on_call_tool(None, spy)
    assert reached is False


# ── provider wiring ─────────────────────────────────────────────────────────


def config(**kw):
    base = dict(
        github_client_id=None,
        github_client_secret=None,
        base_url=None,
        allowed_gh_id=None,
        host="127.0.0.1",
        port=8787,
    )
    base.update(kw)
    return KitConfig(**base)


def test_auth_configured_requires_every_field():
    assert config().auth_configured is False
    assert config(github_client_id="a").auth_configured is False
    assert (
        config(
            github_client_id="a",
            github_client_secret="b",
            base_url="https://finance.example.com",
            allowed_gh_id="1234567",
        ).auth_configured
        is True
    )


def test_provider_refuses_partial_configuration():
    with pytest.raises(ValueError, match="ALLOWED_GH_ID"):
        github_provider(config(github_client_id="a", github_client_secret="b"))


async def test_another_user_cannot_connect_or_list_the_tools(monkeypatch, middleware):
    """Not even the server's description and tool names reach another account."""
    from mcp.shared.exceptions import MCPError

    as_user(monkeypatch, token({"sub": "1", "login": "someone-else"}))
    with pytest.raises(MCPError, match="access denied for GitHub user ID"):
        await middleware.on_request(None, call_next)
    as_user(monkeypatch, None)
    with pytest.raises(MCPError, match="no authenticated GitHub identity"):
        await middleware.on_request(None, call_next)


async def test_the_owner_connects_and_lists(monkeypatch, middleware):
    as_user(monkeypatch, token({"sub": OWNER_ID}))
    assert await middleware.on_request(None, call_next) is SENTINEL
