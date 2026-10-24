"""A synthetic ledger (no personal data) for the finance tests and evaluations.

Today is 2026-10-12. History starts on 1 January 2026, so October compares with six whole
earlier months. The Monzo savings account last synced two days ago, so it shows as stale.
Rent and groceries are joint spending by default, eating out is at weekends, and everything on the Porto
trip is.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
from pg import scratch_db

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT.parent / "finance" / "finance" / "migrations"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
FIRST = date(2026, 1, 1)
TRIP = ("2026-09 Porto", date(2026, 9, 18), date(2026, 9, 21))
NOTES = "Alex is a friend: loans go both ways."
JOINT = ("Rent", "Groceries")  # categories joint by default, as ledger.toml [whose]
WEEKENDS = ("Eating out & coffee",)  # joint at weekends off trips, as [whose.weekends]
ACCOUNTS = {  # id: provider, name, kind, last run
    "bank": ("ob-barclays", "Premier Current Account", "accounts", "r1"),
    "amex": ("ob-amex", "Gold Card", "cards", "r1"),
    "monzo": ("ob-monzo", "Personal", "accounts", "r1"),
    "savings": ("ob-monzo", "Savings", "accounts", "r0"),
}
RUNS = {
    "r0": datetime(2026, 10, 10, 7, 0, tzinfo=UTC),
    "r1": datetime(2026, 10, 12, 7, 0, tzinfo=UTC),
}


def rows() -> list[dict]:
    """Every entry: account, day, pence (negative = out), merchant, flow, category, and extras."""
    out: list[dict] = []

    def add(account, day, pence, merchant, flow, category, **extra):
        out.append(
            {
                "account": account,
                "day": day,
                "pence": pence,
                "merchant": merchant,
                "flow": flow,
                "category": category,
            }
            | extra
        )

    day = FIRST
    while day <= TODAY:
        n = day.toordinal()
        if day.day == 1:
            add("bank", day, -150000, "CITY LETTINGS", "housing", "Rent")
        if day.day == 25:
            add("bank", day, 420000, "ACME LTD", "income", "Salary")
        if day.day == 5 and day.month > 1:
            add("bank", day, -50000, "AMERICAN EXPRESS", "transfer", "Card payment")
            add("amex", day, 50000, "PAYMENT RECEIVED", "transfer", "Card payment")
        if day.day == 15:
            add("amex", day, -1599, "NETFLIX", "spend", "Subscriptions & software")
        if day.day == 26:
            add("monzo", day, -20000, "SAVINGS POT", "transfer", "Pot move")
            add("savings", day, 20000, "SAVINGS POT", "transfer", "Pot move")
        if n % 3 == 0:
            add(
                "amex",
                day,
                -(3000 + n * 37 % 4000),
                "TESCO" if n % 2 else "WAITROSE",
                "spend",
                "Groceries",
            )
        if n % 2 == 0:
            add(
                "monzo",
                day,
                -(600 + n * 53 % 2400),
                "PRET A MANGER" if n % 4 else "DISHOOM",
                "spend",
                "Eating out & coffee",
            )
        if day.weekday() < 5:
            add("monzo", day, -280, "TFL TRAVEL", "spend", "Transport")
        day += timedelta(days=1)

    add("amex", date(2026, 6, 20), -25000, "IKEA", "spend", "Home & furniture")
    add("monzo", date(2026, 7, 3), -800, "SQ *MARKET STALL", "spend", "Uncategorised")
    add(
        "amex",
        date(2026, 8, 1),
        -24000,
        "TAP AIR PORTUGAL",
        "spend",
        "Travel",
        trip=TRIP[0],
        tags=["booked-ahead"],
    )
    add(
        "monzo", date(2026, 8, 10), -20000, "ALEX", "loan", "Friend loan", note="lent for a deposit"
    )
    add("monzo", date(2026, 9, 2), -3000, "DISHOOM", "reversed", "Eating out & coffee")
    add("monzo", date(2026, 9, 2), 3000, "DISHOOM", "reversed", "Eating out & coffee")
    add("monzo", date(2026, 9, 10), 20000, "ALEX", "loan", "Friend loan")
    add("amex", date(2026, 9, 18), -42000, "HOTEL PORTO", "spend", "Travel")
    add("monzo", date(2026, 10, 3), -1250, "SQ *MARKET STALL", "spend", "Uncategorised")
    add("amex", date(2026, 10, 8), -89900, "APPLE STORE", "spend", "Shopping")
    add("amex", TODAY, -4520, "TESCO", "spend", "Groceries", status="pending")
    for r in out:  # spending from departure to the day after return counts towards the trip
        if r["flow"] == "spend" and TRIP[1] <= r["day"] <= TRIP[2] + timedelta(days=1):
            r.setdefault("trip", TRIP[0])
    return out


def load(conn: psycopg.Connection) -> None:
    """The finance service's schema, the MCP's role and grants, then the synthetic rows."""
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        conn.execute(path.read_bytes())
    conn.execute((ROOT / "deploy" / "finance.sql").read_bytes())
    with conn.cursor() as cur:
        cur.executemany(
            "insert into ledger.run (id, fetched_at) values (%s, %s)", list(RUNS.items())
        )
        cur.executemany(
            "insert into ledger.account (id, provider, name, kind, currency, raw, last_run)"
            " values (%s, %s, %s, %s, 'GBP', '{}', %s)",
            [(id, *spec) for id, spec in ACCOUNTS.items()],
        )
        cur.execute("insert into ledger.trip values (%s, %s, %s, 'holiday', true)", TRIP)
        cur.executemany(
            "insert into ledger.category_whose values (%s, 'joint', null)", [(c,) for c in JOINT]
        )
        cur.executemany(
            "insert into ledger.category_whose values (%s, null, 'joint')", [(c,) for c in WEEKENDS]
        )
        cur.execute(
            "insert into ledger.category_group values ('Groceries', 'Food'), ('Eating out & coffee', 'Food')"
        )
        cur.execute(
            "insert into ledger.config (text, notes) values (%s, %s)",
            ('secret = "the rest of the config"', NOTES),
        )
        for i, r in enumerate(rows()):
            key = (r["account"], f"t{i}", r.get("status", "booked"))
            run = ACCOUNTS[r["account"]][3]
            cur.execute(
                "insert into ledger.transaction (account_id, id, status, at, day, pence, currency, description,"
                " merchant_raw, raw, first_run, last_run) values (%s, %s, %s, %s, %s, %s, 'GBP', %s, %s, '{}', %s, %s)",
                (
                    *key,
                    datetime.combine(r["day"], datetime.min.time(), UTC),
                    r["day"],
                    r["pence"],
                    r["merchant"],
                    r["merchant"],
                    run,
                    run,
                ),
            )
            cur.execute(
                "insert into ledger.label (account_id, id, status, merchant, flow, category, rule, trip, tags, note)"
                " values (%s, %s, %s, %s, %s, %s, 'fixture', %s, %s, %s)",
                (
                    *key,
                    r["merchant"],
                    r["flow"],
                    r["category"],
                    r.get("trip"),
                    r.get("tags", []),
                    r.get("note"),
                ),
            )


def ledger_db() -> AbstractContextManager[str | None]:
    """A throwaway PostgreSQL holding the synthetic ledger; yields the MCP role's DSN (None: no binaries)."""
    return scratch_db(load, "finmcp")
