# Finance sync

Collects UK bank transactions through TrueLayer's Data API v1 (Open Banking AIS, read-only:
no payment scope) into the `ledger` schema of a PostgreSQL database, then labels them (merchant,
flow, category, trip) from your own `state/ledger.toml`. Tested with Barclays (`ob-barclays`),
American Express UK (`ob-amex`, cards only) and Monzo (`ob-monzo`, pots appear as savings accounts).
TrueLayer because, as of 2026, its free Live developer tier is the one route an individual can use
for UK banks (Enable Banking covers only the EEA).

## Rules

- Never print or commit tokens, the client secret, callback URLs, account identifiers or raw
  financial data. Error messages (`BankingError`) never carry response bodies.
- Keep original provider payloads, fetch all pages, and mark incomplete runs (no `complete.json`).
- Money is integer pence, never floats. Money out is negative, on cards too.
- No bank-site scraping, no provider fallback. A missing pending list proves nothing.
- Personal rules (`state/ledger.toml`) name people and never enter git; keep the code generic.

## Setup

1. **TrueLayer:** make a free account at console.truelayer.com, create an app, switch it to
   **Live**, and add `https://127.0.0.1:8765/callback` as a redirect URI.
2. **PostgreSQL 16+:** `createdb finance`. One role can own and write it; split them (an owner
   for `migrate`, a writer for everything else) if you want the sync unable to change the schema.
3. **State:** `mkdir -m 700 state` and write `state/env`:
   ```
   TRUELAYER_CLIENT_ID=...
   TRUELAYER_CLIENT_SECRET=...
   LEDGER_DSN=dbname=finance
   LEDGER_ADMIN_DSN=dbname=finance
   ```
   and copy `ledger.example.toml` to `state/ledger.toml`.
4. `uv sync`, then `uv run python -m finance migrate`, `uv run python -m finance check` (lists
   UK provider IDs) and `uv run python -m finance connect <provider_id>` for each bank.
5. Schedule `sync` (`deploy/` has a systemd user timer: 08:00, 16:00 and 23:00).

## State (`state/`, gitignored, 0700)

| File | Holds |
|---|---|
| `env` | `TRUELAYER_CLIENT_ID`, `TRUELAYER_CLIENT_SECRET`, `LEDGER_DSN`, `LEDGER_ADMIN_DSN`; optionally `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` for the Sheet |
| `sheet.json` | the Google Sheet's ID |
| `connections.json` | refresh token per bank (they rotate on every use: only one machine may sync) |
| `ledger.toml` | categories, merchant names, trips, overrides (start from `ledger.example.toml`) |
| `status.json` | per bank: last success, last error, consent expiry; ledger: last result |
| `data/<run>/` | raw runs: `me.json`, then `account-N/{account,transactions,pending,complete}.json` |

## Commands (in this directory)

```
uv run python -m finance sync      # what the timer runs: last 90 days of every bank, then the ledger
uv run python -m finance ledger    # ingest new runs, relabel everything from state/ledger.toml, then the Sheet
uv run python -m finance sheet     # rewrite the Google Sheet copy now
uv run python -m finance health    # problems only: stale banks, expiring consent, ledger failures
uv run python -m finance migrate   # schema changes, as LEDGER_ADMIN_DSN
uv run python -m finance check     # list TrueLayer's UK providers
uv run python -m finance connect ob-monzo
```

**Rate limits:** banks allow about 4 unattended pulls per account a day; watch for `429`s.

**Connecting and renewing consent** (every 90 days; `health` warns 14 days ahead): run
`connect <provider_id>`, open the link it writes to `state/authorisation.json` and authorise. It ends on
`https://127.0.0.1:8765/callback?...`, a page that fails to load; paste that address into the
hidden prompt. That also fetches all available history (up to 2 years); banks release more than
90 days only within minutes of a fresh login.

## Ledger

Query `ledger.entry`: one row per real transaction (booked, plus pending while the latest run still
lists it and it hasn't booked), labelled with merchant, flow, category, trip and the rule that decided it.
`ingest` only adds rows (`run`, `account`, `transaction`); `enrich` rebuilds `label` and `trip`
from `state/ledger.toml`, so editing the config and running `ledger` relabels everything. Each run
also copies the config into `ledger.config`, so the database backup covers it (restore:
`psql -At -d finance -c 'select text from ledger.config' > state/ledger.toml`). Barclays
"ON 10 OCT" dates, FX amounts and fees are parsed; same-day reversals and own-account transfers
are paired. A trip takes spending from departure to the day after return, plus travel booked up to 150 days
ahead. `domestic = true` on a `[[trip]]` in the UK takes neither, so it can't claim the next trip
abroad's bookings; name its own with overrides or from chat.

**Category groups.** `[groups]` in `ledger.toml` maps a group to its categories (Home = Rent, Council tax,
Utilities, …); each run copies it into `ledger.category_group` (migration 007), and the finance MCP totals by
group with `finance_breakdown(by="group")`. A category in no group stands alone; one in two groups is an error.

**Notes.** `notes = """…"""` in `ledger.toml` holds the user's own notes on people and money arrangements
(who is who, how money with a partner works). Each run copies them into `ledger.config.notes`
(migration 006), the one column of the config the finance MCP may read; its `finance_notes` tool
serves them to every chat, so they're written once, here.

**Labels from chat.** The finance MCP's `finance_label` (`../mcp`) writes `entry_edit`,
`merchant_rule` and `merchant_alias` (migration 003). `enrich` never touches them; the `entry` view
lays them over `label`. Who wins: the entry's chat edit, a toml `[[override]]`, the longest chat rule
(word-boundary merchant prefix), then the config. Chat renames apply first. They're append-only, so
`select * from ledger.entry_edit order by n` is the history. Because `enrich` doesn't see them,
transfer pairing, reversals and trip dates still follow the toml labels; a chat flow change out of
`spend` does drop the entry from its trip, and so does a chat category in `not_trip` (migration 011), unless
chat names the trip.

**Pending that books, and splits** (migration 005). A booked row gets a new ID, so `enrich` links it to
the pending row it was (`booked_from`: same account and amount, within days; kept once made). The view
then shows only the booked row, under the pending row's ref and `added`, with whatever chat set on the
pending row: chat state keys on an entry's first row (`edit_id`, `edit_status`). A pending row marked
`reversed` (a hold that won't book) loses that mark if it books after all. `entry_share` holds what
people owe of a payment (`finance_label(shares=…)`); the view's `shared` is their sum, and spending
counts `pence + shared`.

**Parts** (migration 008). `entry_part` moves part of an entry to another category with that category's
flow (`finance_label(parts={"Gifts": 15})`: £30 of opera, half a gift, half an advance). `ledger.entry`
stays one row per entry; `ledger.entry_line` has one line per entry less its parts plus one per part,
and the finance MCP's totals and listings read it, so every total counts each part where it belongs.

**Whose** (migration 009). Every spending line is `personal` or `joint` (shared with a partner). The first that says wins: the
entry's label from chat, the longest chat rule (`finance_label(whose=...)` on refs or a merchant), a trip
with `joint = true`, off trips the category's weekend default from `[whose.weekends]` (eating out on a
Saturday or Sunday is joint, migration 010), the category's default from `[whose]` in ledger.toml, else personal. A part takes its
own category's default unless its entry has a label from chat. `ledger.entry` has `whose` and `whose_set`
(what chat chose), `entry_line` a `whose` per line.

**IOUs** (migration 012). Money owed with no payment of the user's behind it ("I owe Kim £15 for the court"),
from chat (`finance_iou`): `iou` is append-only, the newest row per IOU wins, and null `pence` closes it.
`pence > 0`: the person owes the user. The finance MCP's debts add open IOUs to shares less loans.

**Google Sheet copy** (`sheet.py`, optional). After every ledger update the service rewrites one Google
Sheet ("Finance ledger", tab Entries: every entry with its ref, labels, account, description and when a
sync added it). One-way: edits in the Sheet are overwritten. Bank text is stored as text, never a formula
(a description is written by the other party). It needs a refresh token with the `drive.file` scope from
a Google OAuth desktop client of your own, in the three `GOOGLE_*` lines of `state/env`; without them the
step is off. A failed upload shows in `health`.

Known gaps: Barclays has no `provider_transaction_id`, so the ledger keys on TrueLayer's
`transaction_id`; `vanished_booked` in `health` flags a booked row a later window stopped listing
(a re-issued ID would double count). Barclays pending has always been empty; check again on a day
with card spend. Raw runs grow about 1.5 MB a day and aren't backed up; back up the database.
