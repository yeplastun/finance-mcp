"""FastMCP assembly shared by every server: OAuth, the error guard, limits, logging, transport.

Usage guidance lives in the server rather than in a client-side skill: a skill
does not travel with an MCP server, so it would need one copy per surface and
the copies would drift. Tool descriptions and each server's `instructions`
reach every client that connects, from one source.

Both are always in the caller's context, so both are kept short.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Awaitable, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.logging import get_logger
from mcp_types import ToolAnnotations

from .auth import AllowlistMiddleware, github_provider
from .config import KitConfig
from .errors import BackendError, ToolFailure

log = get_logger("mcpkit")

#: Every tool here only reads. Clients may skip confirmation for these.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

#: About 1.5k tokens: the most one answer may cost before it is cut.
MAX_CHARS = 6000


def app(name: str, instructions: str, config: KitConfig) -> FastMCP:
    """A server that, when OAuth is configured, admits only the allowlisted GitHub user."""
    auth = None
    middleware = []
    if config.auth_configured and config.allowed_gh_id:
        auth = github_provider(config)
        middleware.append(AllowlistMiddleware(config.allowed_gh_id))
    return FastMCP(name=name, instructions=instructions, auth=auth, middleware=middleware)


# Generic, so each tool's own return type survives the wrapper instead of being
# flattened to object.
def guard[T](fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Turn failures meant for the caller into clean tool errors."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except (ToolFailure, BackendError) as exc:
            # FastMCP's own line for this names the tool but not the reason,
            # so it is demoted and the reason logged instead. First line
            # only: a candidate list stays out of the journal.
            log.warning("%s: %s", fn.__name__, str(exc).partition("\n")[0])
            raise ToolError(str(exc), log_level=logging.DEBUG) from None

    return wrapper


def cap(text: str, limit: int = MAX_CHARS) -> str:
    """Cut an answer at a line boundary, saying so, so its cost can't run away."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > 0 else limit] + "\n… cut here: narrow the period or filters"


class JournalFormatter(logging.Formatter):
    """Prefix each record with its syslog priority.

    journald reads a leading `<N>` as the line's priority (SyslogLevelPrefix=,
    on by default). Without it every line was stored as info, so
    `journalctl -p warning` came back empty even when a tool call had failed.
    """

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno >= logging.ERROR:
            priority = 3
        elif record.levelno >= logging.WARNING:
            priority = 4
        else:
            priority = 6
        return f"<{priority}>{super().format(record)}"


def log_to_journal() -> dict[str, object]:
    """Route FastMCP's and uvicorn's records through one priority-tagged handler.

    Returns the uvicorn settings that stop it installing handlers of its own.
    The root logger stays at WARNING, so libraries' chatter stays out; uvicorn's
    loggers are set to INFO on their own.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JournalFormatter("%(levelname)s: %(message)s"))
    logging.basicConfig(handlers=[handler], force=True)
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.handlers.clear()
    fastmcp_logger.propagate = True
    return {"log_config": None, "log_level": "info"}


def serve(mcp: FastMCP, config: KitConfig) -> int:
    if config.auth_configured:
        # systemd sets JOURNAL_STREAM when stderr is the journal.
        uvicorn_config = log_to_journal() if os.environ.get("JOURNAL_STREAM") else None
        mcp.run(transport="http", host=config.host, port=config.port, uvicorn_config=uvicorn_config)
    else:
        # No OAuth configured: stdio only, which is reachable from Claude Code
        # on this machine and from nowhere else.
        mcp.run(transport="stdio")
    return 0
