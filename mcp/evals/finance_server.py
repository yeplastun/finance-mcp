"""The finance MCP over stdio on the synthetic ledger (tests/finance_fixture.py), for evaluations.

Manual only: an evaluation calls the paid API. From mcp/, with the mcp-builder skill's
scripts/evaluation.py and an ANTHROPIC_API_KEY:

    uv run --with anthropic python <skill>/scripts/evaluation.py evals/finance.xml \\
        -m claude-sonnet-5-5 -t stdio -c uv -a run python evals/finance_server.py

Needs PostgreSQL 16 binaries, like the tests.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from finance_fixture import NOW, ledger_db  # noqa: E402

from finance_mcp.server import Config, build  # noqa: E402


def main() -> None:
    with ledger_db() as dsn:
        if dsn is None:
            sys.exit("PostgreSQL binaries not installed")
        config = Config(
            dsn=dsn,
            github_client_id=None,
            github_client_secret=None,
            base_url=None,
            allowed_gh_id=None,
            host="127.0.0.1",
            port=0,
        )
        build(config, now=lambda: NOW).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
