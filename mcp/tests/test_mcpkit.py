"""The shared server kit: clean tool errors, the reason in the log, journald priorities."""

import logging

import pytest
from fastmcp.exceptions import ToolError

from mcpkit.config import KitConfig
from mcpkit.errors import ToolFailure
from mcpkit.server import JournalFormatter, app, cap, guard


def server():
    config = KitConfig(
        github_client_id=None,
        github_client_secret=None,
        base_url=None,
        allowed_gh_id=None,
        host="127.0.0.1",
        port=0,
    )
    mcp = app("kit", "A test server.", config)

    async def read_chat(name: str) -> str:
        raise ToolFailure(f"no chat matches {name!r}\n  candidates: …")

    @mcp.tool()
    async def lookup(name: str) -> str:
        """Look a chat up."""
        return await guard(read_chat)(name)

    return mcp


async def test_tool_failures_become_clean_errors_and_log_their_reason(caplog):
    """Claude sees the message, not a traceback; FastMCP's own line said only the tool's name."""
    with pytest.raises(ToolError, match="no chat matches 'zz'"):
        await server().call_tool("lookup", {"name": "zz"})
    [record] = [r for r in caplog.records if r.name == "fastmcp.mcpkit"]
    assert record.levelname == "WARNING"
    assert record.getMessage() == "read_chat: no chat matches 'zz'"  # first line only


def test_journal_lines_carry_their_priority():
    """journald files an untagged line as info, whatever it says."""
    fmt = JournalFormatter("%(levelname)s: %(message)s")

    def line(level):
        return fmt.format(logging.LogRecord("x", level, "", 0, "boom", None, None))

    assert line(logging.ERROR) == "<3>ERROR: boom"
    assert line(logging.WARNING) == "<4>WARNING: boom"
    assert line(logging.INFO) == "<6>INFO: boom"


def test_a_long_answer_is_cut_at_a_line_and_says_so():
    text = "\n".join(f"line {i}" for i in range(100))
    out = cap(text, 50)
    assert out.endswith("… cut here: narrow the period or filters")
    assert all(line.startswith("line ") for line in out.splitlines()[:-1])
