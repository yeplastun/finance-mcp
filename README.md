# finance-mcp

A personal finance setup in two parts:

- **`finance/`** pulls UK bank and card transactions through TrueLayer (Open Banking, read-only)
  into PostgreSQL a few times a day and labels them from a TOML config you write: merchant, flow
  (spend, income, transfer, loan, …), category, trip, joint or personal. See `finance/README.md`.
- **`mcp/`** is an MCP server over that ledger, so Claude (or any MCP client) can answer "what did
  I spend on eating out last month?" and fix labels from chat ("#1234 was a transfer", "everything
  from PRET is Lunch"), record splits and IOUs, and show who owes what. Bank data itself is never
  changed: chat edits live in append-only tables laid over it, and each can be undone.

Python 3.14 with [uv](https://docs.astral.sh/uv/); each part is its own uv project.

## Quick start

1. Set up and run the sync: `finance/README.md` (TrueLayer app, database, `migrate`, `connect`, `sync`).
2. Optional but recommended: give the MCP a database role that can read the ledger and write only
   the chat tables: `sudo -u postgres psql -d finance -f mcp/deploy/finance.sql`.
3. Run the MCP locally over stdio, e.g. in Claude Code:
   ```
   cd mcp && uv sync
   claude mcp add finance -e FINANCE_DSN="dbname=finance" -- uv run --directory "$PWD" python -m finance_mcp
   ```
   Or serve it over HTTP behind GitHub OAuth (one allowed account) for claude.ai: see
   `mcp/.env.example` and `mcp/deploy/finance-mcp.service`.

Tools: `finance_overview`, `finance_breakdown`, `finance_transactions`, `finance_accounts`,
`finance_label`, `finance_iou`, `finance_debts`, `finance_changes`, `finance_notes`.

## Tests

Offline; the database tests need PostgreSQL 16+ binaries (`brew install postgresql@16` or Ubuntu's
`postgresql`) and spin up a throwaway cluster:

```
cd finance && uv run pytest
cd mcp && uv run pytest
```

`mcp/tests/finance_fixture.py` builds a synthetic ledger; `mcp/evals/` holds questions for an
LLM evaluation against it (paid API, run by hand).
