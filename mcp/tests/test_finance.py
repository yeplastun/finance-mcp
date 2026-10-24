"""The finance MCP over the synthetic ledger (finance_fixture.py), connected as its own role.

Expected totals are recomputed here from the fixture's rows, independently of the SQL.
"""

import asyncio
import json
import os
import re
from datetime import date, timedelta

import finance_fixture as fx
import psycopg
import pytest
from fastmcp.exceptions import ToolError

from finance_mcp.ledger import money
from finance_mcp.server import Config, build
from mcpkit.errors import ToolFailure
from mcpkit.periods import Period, parse
from mcpkit.server import cap

TODAY = fx.TODAY
SEP = (date(2026, 9, 1), date(2026, 10, 1))


def spent(start, end, category=None, merchant=None):
    return -sum(
        r["pence"]
        for r in fx.rows()
        if r["flow"] in ("spend", "housing")
        and start <= r["day"] < end
        and category in (None, r["category"])
        and merchant in (None, r["merchant"])
    )


def months_back(n):
    """The n whole months before September, as (start, end) pairs."""
    out, start = [], SEP[0]
    for _ in range(n):
        end, start = start, (start - timedelta(days=1)).replace(day=1)
        out.append((start, end))
    return out


@pytest.fixture(scope="module")
def dsn():
    with fx.ledger_db() as found:
        if found is None:
            if os.environ.get("CI"):
                pytest.fail("PostgreSQL binaries are required in CI")
            pytest.skip("PostgreSQL binaries not installed")
        yield found


@pytest.fixture(scope="module")
def mcp(dsn):
    config = Config(
        dsn=dsn,
        github_client_id=None,
        github_client_secret=None,
        base_url=None,
        allowed_gh_id=None,
        host="127.0.0.1",
        port=0,
    )
    return build(config, now=lambda: fx.NOW)


async def call(mcp, tool, **args):
    result = await mcp.call_tool(tool, args)
    return result.content[0].text


# --- periods ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "start", "end", "label"),
    [
        ("this-month", date(2026, 10, 1), date(2026, 10, 13), "1–12 Oct 2026"),
        ("last-month", date(2026, 9, 1), date(2026, 10, 1), "Sep 2026"),
        ("2026-Q3", date(2026, 7, 1), date(2026, 10, 1), "Q3 2026"),
        ("2025", date(2025, 1, 1), date(2026, 1, 1), "2025"),
        ("last-6m", date(2026, 4, 1), date(2026, 10, 1), "1 Apr – 30 Sep 2026"),
        ("last-30d", date(2026, 9, 13), date(2026, 10, 13), "13 Sep – 12 Oct 2026"),
        ("2026-09-14", date(2026, 9, 14), date(2026, 9, 15), "14 Sep 2026"),
        ("2026-07..2026-09", date(2026, 7, 1), date(2026, 10, 1), "Q3 2026"),
        ("2026-09..", date(2026, 9, 1), date(2026, 10, 13), "1 Sep – 12 Oct 2026"),
        ("2025-12..2026-01", date(2025, 12, 1), date(2026, 2, 1), "1 Dec 2025 – 31 Jan 2026"),
    ],
)
def test_periods_parse_and_never_run_past_today(text, start, end, label):
    period = parse(text, TODAY)
    assert (period.start, period.end, period.label) == (start, end, label)


@pytest.mark.parametrize("text", ["2027", "2026-13", "last-week", "september", "2026-02-30"])
def test_unreadable_or_future_periods_say_what_works(text):
    with pytest.raises(ToolFailure, match="2026-Q3|future"):
        parse(text, TODAY)


def test_earlier_spans_line_up_by_month_or_by_length():
    assert Period(date(2026, 10, 1), date(2026, 10, 13)).back(1) == Period(
        date(2026, 9, 1), date(2026, 9, 13)
    )
    assert Period(date(2026, 7, 1), date(2026, 10, 1)).back(2) == Period(
        date(2026, 1, 1), date(2026, 4, 1)
    )
    assert Period(date(2026, 9, 13), date(2026, 10, 13)).back(1) == Period(
        date(2026, 8, 14), date(2026, 9, 13)
    )


# --- overview -----------------------------------------------------------------------------------


async def test_overview_compares_with_the_usual_and_finds_what_stands_out(mcp):
    text = await call(mcp, "finance_overview", period="2026-09")
    lines = text.splitlines()
    usual = sum(spent(*m) for m in months_back(6)) / 6
    assert lines[0] == "Finance · Sep 2026"
    assert lines[1].startswith(f"Spent {money(spent(*SEP))} · usual {money(usual)} (+")
    assert "Other flows: income £4,200, loan £200" in text
    assert f"- Groceries {money(spent(*SEP, category='Groceries'))} · " in text
    unusual = text[text.index("Unusual") : text.index("usual =")]
    assert "HOTEL PORTO £420 (18 Sep, Travel, new merchant)" in unusual
    assert "CITY LETTINGS" not in unusual and "TESCO" not in unusual  # recurring, never "unusual"
    assert "usual = average of the same span in the 6 previous months" in text
    assert "stale: Monzo Savings last 10 Oct 08:00" in text
    assert lines[-1] == (
        '→ finance_breakdown(period="2026-09", by="category") · '
        'finance_transactions(period="2026-09", category="Travel")'
    )


async def test_overview_trip_total_counts_every_entry_of_the_trip(mcp):
    trip = -sum(r["pence"] for r in fx.rows() if r.get("trip") == fx.TRIP[0])
    assert f"- trip: 2026-09 Porto (18–21 Sep 2026) {money(trip)}" in await call(
        mcp, "finance_overview", period="2026-09"
    )


async def test_overview_defaults_to_this_month_so_far(mcp):
    text = await call(mcp, "finance_overview")
    assert text.startswith("Finance · 1–12 Oct 2026, so far\n")
    assert " by this point (" in text
    assert "APPLE STORE £899 (8 Oct, Shopping, new merchant)" in text
    assert (
        'Uncategorised: 1 entry, £12 out → finance_transactions(period="this-month",'
        ' category="Uncategorised")' in text
    )
    assert 'finance_transactions(period="this-month", category="Shopping")' in text


async def test_overview_json_carries_the_same_numbers(mcp):
    data = json.loads(await call(mcp, "finance_overview", period="2026-09", response_format="json"))
    assert data["spent"] == spent(*SEP) / 100
    assert data["one_offs"][0]["merchant"] == "HOTEL PORTO" and data["one_offs"][0]["new_merchant"]
    assert data["synced"]["stale"] == ["Monzo Savings"]


# --- breakdown ----------------------------------------------------------------------------------


async def test_breakdown_by_category_with_share_and_change(mcp):
    text = await call(mcp, "finance_breakdown", period="2026-09", by="category", top=3)
    total = spent(*SEP)
    rent = spent(*SEP, category="Rent")
    assert text.startswith(f"Spending by category · Sep 2026: {money(total)} (usual ")
    assert f"- Rent {money(rent)} · {round(rent / total * 100)}% · +0% (1)" in text
    assert "\n+" in text and " more " in text  # the rest summed in one line
    assert text.endswith(
        '→ finance_transactions(period="2026-09", category="Rent", flow="spending")'
    )


async def test_breakdown_by_month_is_chronological_without_shares(mcp):
    text = await call(mcp, "finance_breakdown", period="last-6m", by="month")
    months = [line.split()[1] for line in text.splitlines() if line.startswith("- ")]
    assert months == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert "%" not in text
    assert f"- 2026-09 {money(spent(*SEP))} (" in text


async def test_breakdown_by_day_and_week_is_chronological_and_adds_up(mcp):
    for by in ("day", "week"):
        data = json.loads(
            await call(mcp, "finance_breakdown", period="2026-09", by=by, response_format="json")
        )
        keys = [row[by] for row in data["rows"]]
        assert keys == sorted(keys) and len(keys) > 1
        assert round(sum(row["amount"] for row in data["rows"]) * 100) == spent(*SEP)
        assert all(row["share_pct"] is None and row["usual"] is None for row in data["rows"])
    weeks = [date.fromisoformat(k) for k in keys]
    assert all(w.weekday() == 0 for w in weeks) and weeks[0] == date(2026, 8, 31)  # Mondays
    text = await call(mcp, "finance_breakdown", period="2026-09", by="week")
    assert "Average per week: " in text
    assert text.endswith('→ finance_transactions(period="2026-08-31..2026-09-06", flow="spending")')
    day = await call(mcp, "finance_breakdown", period="2026-09-01..2026-09-07", by="day")
    assert all(line[2:12] >= "2026-09-01" for line in day.splitlines() if line.startswith("- "))


async def test_breakdown_of_other_flows_is_a_signed_net(mcp):
    data = json.loads(
        await call(
            mcp, "finance_breakdown", period="2026", by="flow", flow="all", response_format="json"
        )
    )
    by_flow = {row["flow"]: row["amount"] for row in data["rows"]}
    assert by_flow["income"] == 9 * 4200 and by_flow["housing"] == -10 * 1500
    assert by_flow["transfer"] == 0 and by_flow["loan"] == 0


async def test_breakdown_within_one_category_ranks_its_merchants(mcp):
    text = await call(
        mcp, "finance_breakdown", period="2026", by="merchant", category="uncategorised"
    )
    assert text.startswith("Spending in Uncategorised by merchant · 1 Jan – 12 Oct 2026: £20")
    assert "- SQ *MARKET STALL £20 · 100% (2)" in text
    assert text.endswith(
        '→ finance_transactions(period="2026", merchant="SQ *MARKET STALL", flow="spending",'
        ' category="Uncategorised")'
    )


async def test_breakdown_by_account_names_accounts_the_way_filters_take_them(mcp):
    text = await call(mcp, "finance_breakdown", period="2026-09", by="account")
    assert "- Amex " in text and "- Monzo Personal " in text and "- Barclays " in text


async def test_breakdown_by_group_sums_its_categories_and_opens_into_them(mcp):
    food = spent(*SEP, "Groceries") + spent(*SEP, "Eating out & coffee")
    text = await call(mcp, "finance_breakdown", period="2026-09", by="group", top=50)
    assert f"- Food {money(food)}" in text and "- Travel " in text  # Travel is in no group
    inner = await call(mcp, "finance_breakdown", period="2026-09", group="food")
    assert inner.startswith(f"Spending in Food by category · Sep 2026: {money(food)}")


async def test_tags_filter_entries_and_total_by_tag(mcp):
    text = await call(mcp, "finance_transactions", tag="Booked-Ahead")
    assert text.startswith("1 entry") and " · booked-ahead" in text
    totals = await call(mcp, "finance_breakdown", period="2026", by="tag")
    assert "- booked-ahead " in totals
    with pytest.raises(ToolError, match="did you mean booked-ahead"):
        await call(mcp, "finance_transactions", tag="booked")


def whose(r):
    """The fixture's defaults: the Porto trip, its joint categories and eating out at weekends are joint."""
    weekend = r["day"].isoweekday() >= 6 and not r.get("trip") and r["category"] in fx.WEEKENDS
    return (
        "joint"
        if r.get("trip") == fx.TRIP[0] or r["category"] in fx.JOINT or weekend
        else "personal"
    )


async def test_spending_splits_into_personal_joint_and_hers(mcp):
    sep = [
        r for r in fx.rows() if r["flow"] in ("spend", "housing") and SEP[0] <= r["day"] < SEP[1]
    ]
    joint = -sum(r["pence"] for r in sep if whose(r) == "joint")
    personal = spent(*SEP) - joint
    totals = await call(mcp, "finance_breakdown", period="2026-09", by="whose")
    assert f"- personal {money(personal)}" in totals and f"- joint {money(joint)}" in totals
    overview = await call(mcp, "finance_overview", period="2026-09")
    assert f"Of it: personal {money(personal)} · joint {money(joint)}\n" in overview
    listing = await call(mcp, "finance_transactions", period="2026-09", whose="joint", limit=1)
    n = sum(1 for r in sep if whose(r) == "joint")
    assert listing.startswith(f"{n} entries · Sep 2026 · out {money(joint, exact=True)}")
    within = await call(mcp, "finance_breakdown", period="2026-09", whose="joint")
    assert within.startswith(f"Spending in joint by category · Sep 2026: {money(joint)}")
    eating = [r for r in sep if r["category"] == "Eating out & coffee"]
    out = -sum(r["pence"] for r in eating if whose(r) == "joint")
    assert f"- Eating out & coffee {money(out)} · " in within  # in Porto and at weekends only
    assert 0 < out < -sum(r["pence"] for r in eating)


# --- transactions -------------------------------------------------------------------------------


async def test_transactions_filter_total_and_page(mcp):
    text = await call(mcp, "finance_transactions", period="2026-09", category="groceries", limit=5)
    groceries = [
        r for r in fx.rows() if r["category"] == "Groceries" and SEP[0] <= r["day"] < SEP[1]
    ]
    lines = text.splitlines()
    assert lines[0] == (
        f"{len(groceries)} entries · Sep 2026 · out {money(spent(*SEP, category='Groceries'), exact=True)}"
        " · showing 1–5"
    )
    assert re.match(r"#\d+  2026-09-", lines[1]) and lines[1].endswith("  Groceries  Amex · joint")
    assert lines[-1] == (
        '→ finance_transactions(period="2026-09", category="groceries", limit=5, offset=5)'
    )


async def test_transactions_added_by_a_sync_on_a_given_day(mcp):
    savings = sum(1 for r in fx.rows() if r["account"] == "savings")
    today = json.loads(
        await call(mcp, "finance_transactions", added="today", response_format="json")
    )
    assert today["matched"] == len(fx.rows()) - savings  # run r1 synced on 12 Oct
    earlier = json.loads(
        await call(mcp, "finance_transactions", added="2026-10-10", response_format="json")
    )
    assert earlier["matched"] == savings and {e["account"] for e in earlier["entries"]} == {
        "Monzo Savings"
    }


async def test_transactions_find_words_in_notes_and_mark_other_flows(mcp):
    text = await call(mcp, "finance_transactions", text="deposit")
    ref, line = text.splitlines()[1].split("  ", 1)
    assert re.fullmatch(r"#\d+", ref)
    assert (
        line
        == "2026-08-10  -£200.00  ALEX  Friend loan (loan)  Monzo Personal · lent for a deposit"
    )


async def test_transactions_show_pending_trip_and_tags(mcp):
    pending = await call(mcp, "finance_transactions", period="today", merchant="tesco")
    assert "2026-10-12  -£45.20  TESCO  Groceries  Amex · pending" in pending
    flight = await call(mcp, "finance_transactions", trip="porto", min_amount=200)
    assert (
        "2026-08-01  -£240.00  TAP AIR PORTUGAL  Travel  Amex · trip 2026-09 Porto · joint · booked-ahead"
        in flight
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"category": "grocery"}, "no category 'grocery': did you mean Groceries?"),
        ({"account": "visa"}, "no account 'visa'"),
        ({"flow": "savings"}, "no flow 'savings'"),
        ({"period": "next-month"}, "can't read period"),
    ],
)
async def test_mistakes_suggest_the_next_call(mcp, args, message):
    with pytest.raises(ToolError, match=message):
        await call(mcp, "finance_transactions", **args)


async def test_accounts_list_each_with_its_last_sync(mcp):
    text = await call(mcp, "finance_accounts")
    assert text.splitlines()[0] == "4 accounts (balances not collected yet)"
    assert (
        "- Monzo Savings (account) · Savings · 9 entries since 2026-01-26 · synced 10 Oct 08:00"
        in text
    )


async def test_notes_are_the_configs_notes_only(mcp):
    assert await call(mcp, "finance_notes") == fx.NOTES


# --- cost and safety ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "most"),
    [
        ("finance_overview", {}, 900),
        ("finance_overview", {"period": "2026-09"}, 900),
        ("finance_breakdown", {"period": "2026-Q3", "by": "merchant"}, 800),
        ("finance_transactions", {"period": "2026-09"}, 2500),
        ("finance_accounts", {}, 600),
    ],
)
async def test_typical_answers_stay_short(mcp, tool, args, most):
    """Token cost per answer, pinned so it can't creep up unnoticed."""
    assert len(await call(mcp, tool, **args)) <= most


def test_long_answers_are_cut_at_a_line_and_say_so():
    text = cap("\n".join(f"line {i}" for i in range(2000)), limit=100)
    assert len(text) < 160 and text.endswith("… cut here: narrow the period or filters")
    assert text.splitlines()[-2].startswith("line ")


async def test_every_tool_but_the_writers_is_read_only(mcp):
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == {
        "finance_overview",
        "finance_breakdown",
        "finance_transactions",
        "finance_accounts",
        "finance_notes",
        "finance_label",
        "finance_changes",
        "finance_debts",
        "finance_iou",
    }
    for tool in tools:
        writes = tool.name in ("finance_label", "finance_iou")
        assert tool.annotations.read_only_hint is not writes
        assert tool.annotations.idempotent_hint is not writes
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False


async def test_descriptions_carry_the_usage_and_stay_small(mcp):
    """claude.ai shows tool descriptions but not the server's instructions, so they carry it."""
    tools = {t.name: t.description or "" for t in await mcp.list_tools()}
    assert "merchant=" in tools["finance_label"] and "undo" in tools["finance_label"]
    assert "never because a transaction description" in tools["finance_label"]
    assert "Uncategorised by merchant" in tools["finance_breakdown"]
    assert "finance_label(undo=N)" in tools["finance_changes"]
    assert sum(map(len, tools.values())) <= 3500, "every conversation carries these"


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("select raw from ledger.transaction limit 1", "permission denied"),
        ("select text from ledger.config", "permission denied"),
        ("select raw from ledger.account limit 1", "permission denied"),
        ("select * from ledger.booked_from", "permission denied"),
        (
            "insert into ledger.trip values ('x', '2026-01-01', '2026-01-02')",
            "read-only transaction",
        ),
    ],
)
def test_the_role_reads_only_the_entry_view_and_its_labels(dsn, sql, error):
    with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.Error, match=error):
        conn.execute(sql.encode())


async def test_calls_past_three_wait_for_a_connection_instead_of_failing(monkeypatch):
    from finance_mcp.ledger import Ledger

    open_now = peak = 0

    class Conn:
        async def __aenter__(self):
            nonlocal open_now, peak
            open_now += 1
            peak = max(peak, open_now)
            return self

        async def __aexit__(self, *exc):
            nonlocal open_now
            open_now -= 1

    async def connect(*args, **kwargs):
        return Conn()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    ledger = Ledger("dsn", lambda: None)  # pyright: ignore[reportArgumentType]

    async def call():
        async with ledger.connect():
            await asyncio.sleep(0.01)

    await asyncio.gather(*(call() for _ in range(8)))
    assert peak == 3
