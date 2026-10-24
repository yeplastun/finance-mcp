"""finance_label and finance_changes over the synthetic ledger, connected as finmcp.

Every test gets its own copy of the loaded database (a template copy takes milliseconds), so
the labels one test writes never reach another.
"""

import itertools
import json
import os
import re
from datetime import date

import finance_fixture as fx
import psycopg
import pytest
from fastmcp.exceptions import ToolError

from finance_mcp.server import Config, build

copies = itertools.count()


@pytest.fixture(scope="module")
def template():
    with fx.ledger_db() as found:
        if found is None:
            if os.environ.get("CI"):
                pytest.fail("PostgreSQL binaries are required in CI")
            pytest.skip("PostgreSQL binaries not installed")
        yield found


@pytest.fixture
def dsn(template):
    """finmcp's DSN on a fresh copy of the ledger."""
    name = f"copy{next(copies)}"
    with psycopg.connect(template.replace("user=finmcp", "user=postgres"), autocommit=True) as conn:
        conn.execute(f"create database {name} template postgres".encode())
    return template.replace("dbname=postgres", f"dbname={name}")


def owner(dsn):
    """The superuser on the same copy, for setting up what the finance service would write."""
    return psycopg.connect(dsn.replace("user=finmcp", "user=postgres"), autocommit=True)


@pytest.fixture
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


def ref(dsn, merchant, day, status="booked"):
    with psycopg.connect(dsn) as conn:
        [(num,)] = conn.execute(
            "select num from ledger.entry where merchant = %s and day = %s and status = %s",
            (merchant, day, status),
        ).fetchall()
    return num


def entry(dsn, num):
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "select merchant, flow, category, person, trip, tags, note, rule from ledger.entry"
            " where num = %s",
            (num,),
        ).fetchone()
    assert row is not None
    return dict(
        zip(
            ["merchant", "flow", "category", "person", "trip", "tags", "note", "rule"],
            row,
            strict=True,
        )
    )


def change_of(text):
    return int(re.search(r"undo=(\d+)\)", text).group(1))


IKEA = date(2026, 6, 20)
STALL = date(2026, 10, 3)


# --- entries ------------------------------------------------------------------------------------


async def test_a_label_shows_before_and_after_and_moves_the_totals_at_once(mcp, dsn):
    stall = ref(dsn, "SQ *MARKET STALL", STALL)
    before = json.loads(await call(mcp, "finance_overview", response_format="json"))
    text = await call(mcp, "finance_label", refs=[stall], category="groceries")
    lines = text.splitlines()
    assert re.fullmatch(r"Change \d+: 1 entry relabelled", lines[0])
    assert lines[1] == (
        f"#{stall}  2026-10-03  -£12.50  SQ *MARKET STALL: category Uncategorised → Groceries,"
        " whose personal → joint"  # groceries are joint by default
    )
    assert lines[-1] == f"→ undo: finance_label(undo={change_of(text)})"
    after = json.loads(await call(mcp, "finance_overview", response_format="json"))
    groceries = {c["category"]: c["spent"] for c in after["categories"]}["Groceries"]
    was = {c["category"]: c["spent"] for c in before["categories"]}["Groceries"]
    assert groceries == pytest.approx(was + 12.50)
    assert after["uncategorised"]["entries"] == 0
    assert entry(dsn, stall)["rule"] == "chat"


async def test_fields_merge_across_calls_and_clear_hands_back_to_the_config(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    await call(mcp, "finance_label", refs=[ikea], category="Groceries")
    await call(mcp, "finance_label", refs=[ikea], note="sofa", tags=["New Flat", "new flat"])
    labels = entry(dsn, ikea)
    assert (labels["category"], labels["note"], labels["tags"]) == (
        "Groceries",
        "sofa",
        ["new-flat"],
    )
    await call(mcp, "finance_label", refs=[ikea], note="")
    assert entry(dsn, ikea)["note"] is None and entry(dsn, ikea)["category"] == "Groceries"
    await call(mcp, "finance_label", refs=[ikea], clear=True)
    labels = entry(dsn, ikea)
    assert (labels["category"], labels["rule"], labels["tags"]) == (
        "Home & furniture",
        "fixture",
        [],
    )


async def test_the_same_label_twice_writes_nothing(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    await call(mcp, "finance_label", refs=[ikea, ikea], category="Groceries")
    again = await call(mcp, "finance_label", refs=[ikea], category="groceries")
    assert again.startswith("Nothing changed")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("select count(*) from ledger.entry_edit").fetchone() == (1,)


async def test_undo_restores_the_state_before_and_refuses_when_overtaken(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    first = change_of(await call(mcp, "finance_label", refs=[ikea], category="Groceries"))
    second = change_of(await call(mcp, "finance_label", refs=[ikea], note="sofa"))
    with pytest.raises(ToolError, match=f"change {second} changed the same labels since"):
        await call(mcp, "finance_label", undo=first)  # newer labels are never discarded silently
    undone = await call(mcp, "finance_label", undo=second)
    assert re.match(rf"Change \d+: undid change {second} \(1 entry back as they were\)", undone)
    labels = entry(dsn, ikea)
    assert (labels["category"], labels["note"]) == ("Groceries", None)
    await call(mcp, "finance_label", undo=first)
    assert entry(dsn, ikea)["rule"] == "fixture"
    with pytest.raises(ToolError, match="no change 999"):
        await call(mcp, "finance_label", undo=999)


async def test_moving_a_trip_day_spend_out_of_spending_takes_it_off_the_trip(mcp, dsn):
    hotel = ref(dsn, "HOTEL PORTO", date(2026, 9, 18))
    assert entry(dsn, hotel)["trip"] == fx.TRIP[0]
    await call(mcp, "finance_label", refs=[hotel], flow="people", person="Alex", new_name=True)
    assert entry(dsn, hotel)["trip"] is None
    await call(mcp, "finance_label", refs=[hotel], trip="2026-09 porto")
    assert entry(dsn, hotel)["trip"] == fx.TRIP[0]
    ikea = ref(dsn, "IKEA", IKEA)
    await call(mcp, "finance_label", refs=[ikea], trip="")
    assert entry(dsn, ikea)["trip"] is None


async def test_relabelling_one_leg_of_a_transfer_names_the_other(mcp, dsn):
    out, into = (
        ref(dsn, "AMERICAN EXPRESS", date(2026, 9, 5)),
        ref(dsn, "PAYMENT RECEIVED", date(2026, 9, 5)),
    )
    with owner(dsn) as conn:
        conn.execute(
            "update ledger.label l set pair_account_id = p.account_id, pair_id = p.id"
            " from ledger.transaction t, ledger.transaction p"
            " where (l.account_id, l.id) = (t.account_id, t.id) and t.num = %s and p.num = %s",
            (out, into),
        )
    text = await call(mcp, "finance_label", refs=[out], flow="spend", category="Shopping")
    assert (
        f"Note: #{out} was one leg of an own-account transfer; the other leg #{into} is still a transfer"
        in text
    )


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"category": "Groceries"}, "name entries with refs"),
        ({"refs": [1], "merchant": "TESCO", "category": "Groceries"}, "one of the two"),
        ({"refs": [1]}, "say what to set"),
        ({"refs": [1], "clear": True, "category": "Groceries"}, "clear=true takes no fields"),
        ({"refs": [1], "rename": "X"}, "rename is for a merchant"),
        ({"merchant": "TESCO", "note": "x"}, "note can only be set on entries"),
        ({"merchant": "TE", "category": "Groceries"}, "at least 3 characters"),
        ({"undo": 1, "refs": [1]}, "undo takes nothing else"),
        ({"refs": [999999], "category": "Groceries"}, "no entry #999999"),
        ({"refs": [1], "category": "Grocery"}, "did you mean Groceries\\?; or pass new_name=true"),
        ({"refs": [1], "person": "Alx"}, "^no such person\n"),
        ({"refs": [1], "trip": "Paris"}, "no trip 'Paris'"),
        ({"refs": [1], "tags": ["a/b"]}, "tag 'a/b'"),
        ({"merchant": "TESCO", "shares": {"Alex": 5}}, "shares can only be set on entries"),
        ({"merchant": "TESCO", "flow": "reversed"}, "reversed is for entries"),
        ({"refs": [1], "shares": {"Alex": 0.001}}, "at most 2 decimals"),
        ({"refs": [1], "clear": True, "shares": {}}, "clear=true takes no fields"),
        ({"merchant": "TESCO", "parts": {"Groceries": 5}}, "parts can only be set on entries"),
        ({"refs": [1], "parts": {"Groceries": 0.001}}, "part 0.001: pounds above 0"),
        ({"refs": [1], "parts": {"Grocery": 5}}, "did you mean Groceries"),
    ],
)
async def test_mistakes_say_what_works(mcp, dsn, args, message):
    with owner(dsn) as conn:  # a person to suggest
        conn.execute("insert into ledger.merchant_rule (prefix, person) values ('ALEX', 'Alex')")
    with pytest.raises(ToolError, match=message):
        await call(mcp, "finance_label", **args)


def book(dsn, pending, merchant=None):
    """What the finance service does when a pending entry books: a new booked row from a later run,
    labelled by enrich and linked to the pending row. The bank still lists the pending row (Monzo
    does for weeks)."""
    with owner(dsn) as conn:
        [(account, pending_id, day, pence, was)] = conn.execute(
            "select account_id, id, day, pence, merchant_raw from ledger.transaction where num = %s",
            (pending,),
        ).fetchall()
        merchant = merchant or was
        conn.execute("insert into ledger.run values ('r2', %s)", (R2,))
        conn.execute(
            "insert into ledger.transaction (account_id, id, status, at, day, pence, currency,"
            " description, merchant_raw, raw, first_run, last_run)"
            " values (%s, 'booked', 'booked', now(), %s, %s, 'GBP', %s, %s, '{}', 'r2', 'r2')",
            (account, day, pence, merchant, merchant),
        )
        conn.execute(
            "insert into ledger.label (account_id, id, status, merchant, flow, category, rule)"
            " values (%s, 'booked', 'booked', %s, 'spend', 'Groceries', 'fixture')",
            (account, merchant),
        )
        conn.execute(
            "insert into ledger.booked_from values (%s, 'booked', %s)", (account, pending_id)
        )


R2 = fx.NOW.replace(hour=20)


def listed(dsn, num):
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "select status, flow, category, shared, added from ledger.entry where num = %s", (num,)
        ).fetchall()


async def test_a_pending_entry_keeps_its_labels_and_split_when_it_books(mcp, dsn):
    pending = ref(dsn, "TESCO", fx.TODAY, status="pending")
    text = await call(
        mcp, "finance_label", refs=[pending], category="Shopping", shares={"Sam": 20}, new_name=True
    )
    assert (
        f"#{pending}  2026-10-12  -£45.20  TESCO: category Groceries → Shopping,"
        " whose joint → personal; split:" in text
    )
    assert "split: Sam £20.00, yours £25.20" in text
    assert f"Note: #{pending} pending; this goes with it when it books" in text
    book(dsn, pending)
    # One entry, under the same ref, as new as the pending row was: no second review, no double count.
    assert listed(dsn, pending) == [("booked", "spend", "Shopping", 2000, fx.RUNS["r1"])]
    lines = await call(mcp, "finance_transactions", period="today", merchant="tesco")
    assert f"#{pending}  2026-10-12  -£45.20  TESCO  Shopping  Amex · your share -£25.20" in lines
    assert "pending" not in lines


async def test_a_pending_entry_that_books_under_another_merchant_says_so_and_stays_new(mcp, dsn):
    pending = ref(dsn, "TESCO", fx.TODAY, status="pending")
    await call(mcp, "finance_label", refs=[pending], category="Shopping")
    book(dsn, pending, merchant="TESCO EXPRESS 123")  # linked on the amount alone: could be wrong
    assert listed(dsn, pending) == [("booked", "spend", "Shopping", 0, R2)]
    lines = await call(mcp, "finance_transactions", period="today", merchant="tesco express")
    assert (
        f"#{pending}  2026-10-12  -£45.20  TESCO EXPRESS 123  Shopping  Amex · was pending as TESCO"
        in lines
    )


async def test_a_hold_marked_reversed_counts_nowhere_and_comes_back_if_it_books(mcp, dsn):
    pending = ref(dsn, "TESCO", fx.TODAY, status="pending")
    today = json.loads(await call(mcp, "finance_breakdown", period="today", response_format="json"))
    await call(mcp, "finance_label", refs=[pending], flow="reversed")
    after = json.loads(await call(mcp, "finance_breakdown", period="today", response_format="json"))
    assert after["total"] == pytest.approx(today["total"] - 45.20)
    book(
        dsn, pending
    )  # it was a real payment after all: counted, and new again for the nightly review
    assert listed(dsn, pending) == [("booked", "spend", "Groceries", 0, R2)]


async def test_a_split_counts_only_your_share_and_tracks_who_owes(mcp, dsn):
    await call(mcp, "finance_label", merchant="ALEX", person="Alex", new_name=True)
    ikea = ref(dsn, "IKEA", IKEA)
    text = await call(
        mcp, "finance_label", refs=[ikea], shares={"Sam": 100, "alex": 50}, new_name=True
    )
    assert (
        f"#{ikea}  2026-06-20  -£250.00  IKEA: split: Sam £100.00, Alex £50.00, yours £100.00"
        in text
    )
    june = json.loads(
        await call(mcp, "finance_breakdown", period="2026-06", response_format="json")
    )
    assert {r["category"]: r["amount"] for r in june["rows"]}["Home & furniture"] == 100
    assert (await call(mcp, "finance_debts")).splitlines()[:3] == [
        "Owed, by person",
        "- Sam owes you £100.00",
        "- Alex owes you £50.00",
    ]
    alex = (await call(mcp, "finance_debts", person="alex")).splitlines()
    assert alex[:4] == [  # lent £200 and got it back; the IKEA share is still open
        "Alex owes you £50.00",
        f"#{ikea}  2026-06-20  +£50.00  IKEA (share)  → £50.00",
        f"#{ref(dsn, 'ALEX', date(2026, 8, 10))}  2026-08-10  +£200.00  ALEX  → £250.00",
        f"#{ref(dsn, 'ALEX', date(2026, 9, 10))}  2026-09-10  -£200.00  ALEX  → £50.00",
    ]
    again = await call(mcp, "finance_label", refs=[ikea], shares={"sam": 100, "Alex": 50})
    assert again.startswith("Nothing changed")


async def test_undoing_a_split_brings_it_back_and_the_change_list_shows_it(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    await call(mcp, "finance_label", refs=[ikea], shares={"Sam": 100}, new_name=True)
    removed = await call(mcp, "finance_label", refs=[ikea], shares={})
    assert f"#{ikea}  2026-06-20  -£250.00  IKEA: split removed" in removed
    assert (await call(mcp, "finance_debts")).startswith("Nobody owes anything")
    undone = await call(mcp, "finance_label", undo=change_of(removed))
    assert re.match(r"Change \d+: undid change \d+ \(1 share back as they were\)", undone)
    assert "- Sam owes you £100.00" in await call(mcp, "finance_debts")
    assert f"#{ikea}: Sam owes £100.00" in await call(mcp, "finance_changes")


async def test_only_a_payment_out_can_be_split_and_your_share_stays_above_zero(mcp, dsn):
    salary = ref(dsn, "ACME LTD", date(2026, 9, 25))
    with pytest.raises(ToolError, match=f"#{salary} isn't a payment out"):
        await call(mcp, "finance_label", refs=[salary], shares={"Sam": 5}, new_name=True)
    ikea = ref(dsn, "IKEA", IKEA)
    with pytest.raises(ToolError, match="your share must stay above £0"):
        await call(mcp, "finance_label", refs=[ikea], shares={"Sam": 250}, new_name=True)
    with pytest.raises(ToolError, match="isn't a payment out"):  # checked after this call's flow
        await call(
            mcp, "finance_label", refs=[ikea], flow="people", shares={"Sam": 5}, new_name=True
        )
    with psycopg.connect(dsn) as conn:  # a refused call writes nothing at all
        assert conn.execute("select count(*) from ledger.entry_edit").fetchone() == (0,)


async def test_parts_count_part_of_an_entry_under_another_category_with_its_flow(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)

    async def june(**args):
        return json.loads(
            await call(mcp, "finance_breakdown", period="2026-06", response_format="json", **args)
        )

    before, everything = await june(), (await june(flow="all"))["total"]
    text = await call(
        mcp, "finance_label", refs=[ikea], parts={"groceries": 100, "Friend loan": 50}
    )
    assert (
        f"#{ikea}  2026-06-20  -£250.00  IKEA: parts: Groceries £100.00, Friend loan £50.00,"
        " rest £100.00 Home & furniture" in text
    )
    was = {r["category"]: r["amount"] for r in before["rows"]}
    now = {r["category"]: r["amount"] for r in (await june())["rows"]}
    assert now["Home & furniture"] == 100
    assert now["Groceries"] == pytest.approx(was["Groceries"] + 100)
    assert (await june(flow="loan"))["total"] == -50  # the part takes its category's flow
    assert (await june(flow="all"))["total"] == pytest.approx(everything)  # the bank moved the same
    lines = await call(mcp, "finance_transactions", period="2026-06-20", merchant="IKEA")
    for line in (
        "-£100.00  IKEA  Home & furniture  Amex · part of -£250.00",
        "-£100.00  IKEA  Groceries  Amex · part of -£250.00",
        "-£50.00  IKEA  Friend loan (loan)  Amex · part of -£250.00",
    ):
        assert f"#{ikea}  2026-06-20  {line}" in lines
    again = await call(
        mcp, "finance_label", refs=[ikea], parts={"Friend loan": 50, "Groceries": 100}
    )
    assert again.startswith("Nothing changed")


async def test_undoing_parts_brings_them_back_and_the_change_list_shows_them(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    await call(mcp, "finance_label", refs=[ikea], parts={"Groceries": 100})
    removed = await call(mcp, "finance_label", refs=[ikea], parts={})
    assert f"#{ikea}  2026-06-20  -£250.00  IKEA: parts removed" in removed
    listing = await call(mcp, "finance_transactions", period="2026-06-20", merchant="IKEA")
    assert "part of" not in listing
    undone = await call(mcp, "finance_label", undo=change_of(removed))
    assert re.match(r"Change \d+: undid change \d+ \(1 part back as they were\)", undone)
    listing = await call(mcp, "finance_transactions", period="2026-06-20", merchant="IKEA")
    assert "Groceries  Amex · part of -£250.00" in listing
    assert f"#{ikea}: part Groceries £100.00 (spend)" in await call(mcp, "finance_changes")


async def test_parts_leave_some_of_the_entry_and_none_go_on_a_reversed_one(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    with pytest.raises(ToolError, match="some of it must stay"):
        await call(mcp, "finance_label", refs=[ikea], parts={"Groceries": 250})
    with pytest.raises(ToolError, match="and shares of £100.00 leave nothing of £250.00"):
        await call(
            mcp,
            "finance_label",
            refs=[ikea],
            shares={"Sam": 100},
            parts={"Groceries": 150},
            new_name=True,
        )
    pending = ref(dsn, "TESCO", fx.TODAY, status="pending")
    with pytest.raises(ToolError, match="is reversed and counts nowhere"):  # after this call's flow
        await call(mcp, "finance_label", refs=[pending], flow="reversed", parts={"Groceries": 5})
    with psycopg.connect(dsn) as conn:  # a refused call writes nothing at all
        for table in ("entry_part", "entry_share", "entry_edit"):
            assert conn.execute(f"select count(*) from ledger.{table}".encode()).fetchone() == (0,)
    await call(mcp, "finance_label", refs=[ikea], parts={"Groceries": 150})
    with pytest.raises(ToolError, match="leave nothing of £100.00 as yours"):
        await call(mcp, "finance_label", refs=[ikea], shares={"Sam": 100}, new_name=True)


async def test_a_pending_entry_keeps_its_parts_when_it_books(mcp, dsn):
    pending = ref(dsn, "TESCO", fx.TODAY, status="pending")
    await call(mcp, "finance_label", refs=[pending], parts={"Home & furniture": 20})
    book(dsn, pending)
    lines = await call(mcp, "finance_transactions", period="today", merchant="tesco")
    assert f"#{pending}  2026-10-12  -£25.20  TESCO  Groceries  Amex · part of -£45.20" in lines
    assert (
        f"#{pending}  2026-10-12  -£20.00  TESCO  Home & furniture  Amex · part of -£45.20" in lines
    )
    assert "pending" not in lines


async def test_whose_set_on_an_entry_or_a_merchant_beats_the_trip_and_the_category(mcp, dsn):
    apple, pending = (
        ref(dsn, "APPLE STORE", date(2026, 10, 8)),
        ref(dsn, "TESCO", fx.TODAY, "pending"),
    )

    async def october():
        data = json.loads(
            await call(
                mcp, "finance_breakdown", period="2026-10", by="whose", response_format="json"
            )
        )
        return {r["whose"]: r["amount"] for r in data["rows"]}

    before = await october()
    text = await call(mcp, "finance_label", refs=[apple], whose="joint")
    assert f"#{apple}  2026-10-08  -£899.00  APPLE STORE: whose personal → joint" in text
    now = await october()
    assert now["joint"] == pytest.approx(before["joint"] + 899)
    assert now["personal"] == pytest.approx(before["personal"] - 899)
    assert f"#{apple}: whose joint" in await call(mcp, "finance_changes")
    await call(mcp, "finance_label", refs=[pending], whose="joint")  # as its category: no change
    rule = await call(mcp, "finance_label", merchant="TESCO", whose="personal")
    assert "Rule: merchants starting TESCO → whose personal." in rule
    joint = await call(mcp, "finance_transactions", merchant="TESCO", whose="joint")
    assert joint.startswith("1 entry") and f"#{pending}  " in joint  # its own label beats the rule
    await call(mcp, "finance_label", undo=change_of(rule))
    assert (await october())["joint"] == pytest.approx(now["joint"])
    await call(mcp, "finance_label", undo=change_of(text))
    assert await october() == before


async def test_a_part_takes_its_categorys_whose_unless_its_entry_has_one(mcp, dsn):
    ikea, alex = ref(dsn, "IKEA", IKEA), ref(dsn, "ALEX", date(2026, 8, 10))
    await call(mcp, "finance_label", refs=[ikea], parts={"Groceries": 100})
    lines = await call(mcp, "finance_transactions", period="2026-06-20", merchant="IKEA")
    assert "Groceries  Amex · part of -£250.00 · joint" in lines
    assert "Home & furniture  Amex · part of -£250.00\n" in lines  # personal goes unsaid
    await call(mcp, "finance_label", refs=[ikea], whose="joint")
    lines = await call(mcp, "finance_transactions", period="2026-06-20", merchant="IKEA")
    assert lines.count("part of -£250.00 · joint") == 2
    # Not spending itself, but its spending part follows it: half a loan, half his own shopping.
    text = await call(mcp, "finance_label", refs=[alex], parts={"Groceries": 50}, whose="personal")
    assert (
        f"#{alex}  2026-08-10  -£200.00  ALEX: whose – → personal; parts: Groceries £50.00" in text
    )
    lines = await call(mcp, "finance_transactions", period="2026-08-10", merchant="ALEX")
    assert "Friend loan (loan)  Monzo Personal · part of -£200.00 · lent for a deposit" in lines
    assert "Groceries  Monzo Personal · part of -£200.00" in lines and "joint" not in lines


async def test_new_names_need_saying_so(mcp, dsn):
    ikea = ref(dsn, "IKEA", IKEA)
    await call(mcp, "finance_label", refs=[ikea], category="Flat setup", new_name=True)
    await call(mcp, "finance_label", refs=[ikea], person="Sam", new_name=True)
    stall = ref(dsn, "SQ *MARKET STALL", STALL)
    await call(mcp, "finance_label", refs=[stall], category="flat SETUP", person="sam")
    assert (entry(dsn, stall)["category"], entry(dsn, stall)["person"]) == ("Flat setup", "Sam")


# --- merchants ----------------------------------------------------------------------------------


async def test_a_rule_covers_a_merchant_on_a_word_boundary_and_new_entries_too(mcp, dsn):
    with owner(dsn) as conn:  # a merchant PRET must not catch
        conn.execute(
            "insert into ledger.transaction (account_id, id, status, at, day, pence, currency, description,"
            " raw, first_run, last_run) values ('monzo', 'pretty', 'booked', now(), '2026-10-01', -999,"
            " 'GBP', 'PRETTY THINGS', '{}', 'r1', 'r1')"
        )
        conn.execute(
            "insert into ledger.label (account_id, id, status, merchant, flow, category, rule)"
            " values ('monzo', 'pretty', 'booked', 'PRETTY THINGS', 'spend', 'Shopping', 'default')"
        )
    text = await call(mcp, "finance_label", merchant="pret", category="Lunch", new_name=True)
    prets = sum(1 for r in fx.rows() if r["merchant"] == "PRET A MANGER")
    assert f"Rule: merchants starting PRET → category Lunch. {prets} entries now" in text
    assert f"Before: Eating out & coffee (spend) ×{prets}" in text
    with psycopg.connect(dsn) as conn:
        categories = dict(
            conn.execute(
                "select merchant, string_agg(distinct category, ',') from ledger.entry"
                " where merchant like 'PRET%%' group by 1"
            ).fetchall()
        )
    assert categories == {"PRET A MANGER": "Lunch", "PRETTY THINGS": "Shopping"}
    with owner(dsn) as conn:  # the next sync brings a new one
        conn.execute(
            "insert into ledger.transaction (account_id, id, status, at, day, pence, currency, description,"
            " raw, first_run, last_run) values ('monzo', 'new', 'booked', now(), '2026-10-12', -450,"
            " 'GBP', 'PRET A MANGER', '{}', 'r1', 'r1')"
        )
        conn.execute(
            "insert into ledger.label (account_id, id, status, merchant, flow, category, rule)"
            " values ('monzo', 'new', 'booked', 'PRET A MANGER', 'spend', 'Eating out & coffee', 'rule 7')"
        )
    new = ref(dsn, "PRET A MANGER", date(2026, 10, 12))
    assert (entry(dsn, new)["category"], entry(dsn, new)["rule"]) == ("Lunch", "chat rule")


async def test_entry_edits_and_toml_overrides_outrank_a_rule(mcp, dsn):
    sep = [r for r in fx.rows() if r["merchant"] == "TESCO" and r["day"].month == 9]
    first, second = (ref(dsn, "TESCO", r["day"]) for r in sep[:2])
    with owner(dsn) as conn:
        conn.execute(
            "update ledger.label l set rule = 'override 3' from ledger.transaction t"
            " where (l.account_id, l.id, l.status) = (t.account_id, t.id, t.status) and t.num = %s",
            (second,),
        )
    await call(mcp, "finance_label", refs=[first], category="Shopping")
    text = await call(
        mcp,
        "finance_label",
        merchant="TESCO",
        flow="people",
        category="Lunch",
        person="Sam",
        new_name=True,
    )
    assert "Kept: 1 entry with set in chat on its own keep their own labels" in text
    assert "Kept: 1 entry with a toml override keep their own labels" in text
    one, two = entry(dsn, first), entry(dsn, second)
    assert (one["flow"], one["category"], one["person"]) == ("people", "Shopping", "Sam")
    assert (two["flow"], two["category"], two["person"], two["rule"]) == (
        "spend",
        "Groceries",
        "Sam",
        "override 3",
    )


async def test_a_rule_never_undoes_a_computed_reversal(mcp, dsn):
    await call(mcp, "finance_label", merchant="DISHOOM", flow="people", person="Sam", new_name=True)
    with psycopg.connect(dsn) as conn:
        flows = conn.execute(
            "select day = '2026-09-02', flow from ledger.entry where merchant = 'DISHOOM' group by 1, 2"
        ).fetchall()
    assert sorted(flows) == [(False, "people"), (True, "reversed")]


async def test_renames_merge_variants_and_a_rule_set_with_one_matches_the_new_name(mcp, dsn):
    with owner(dsn) as conn:
        conn.execute(  # a second spelling of the same person, as bank transfer references make
            "update ledger.label l set merchant = 'ALEX * AR03KV*SENT FROM'"
            " from ledger.transaction t where (l.account_id, l.id) = (t.account_id, t.id)"
            " and l.merchant = 'ALEX' and t.day = '2026-09-10'"
        )
    text = await call(
        mcp,
        "finance_label",
        merchant="alex",
        rename="Alex Smith",
        flow="loan",
        person="Alex",
        new_name=True,
    )
    assert "Renamed merchants starting ALEX → ALEX SMITH: 2 entries" in text
    assert "Merged: ALEX (1), ALEX * AR03KV*SENT FROM (1)" in text
    assert "Rule: merchants starting ALEX SMITH → flow loan, person Alex." in text
    breakdown = await call(mcp, "finance_breakdown", period="2026", by="person", flow="loan")
    assert "- Alex £0 (2)" in breakdown
    listed = await call(mcp, "finance_transactions", person="alex")
    assert "ALEX SMITH  Friend loan (loan)  Monzo Personal · person Alex" in listed


async def test_a_rename_warns_about_rules_it_leaves_matching_nothing(mcp, dsn):
    await call(mcp, "finance_label", merchant="TESCO", category="Lunch", new_name=True)
    text = await call(mcp, "finance_label", merchant="TESCO", rename="SUPERMARKET")
    assert "Note: rule TESCO now matches nothing; set it again on the new name" in text


async def test_clearing_a_merchant_removes_its_rule_and_rename(mcp, dsn):
    await call(mcp, "finance_label", merchant="ikea", rename="IKEA UK", category="Groceries")
    text = await call(mcp, "finance_label", merchant="ikea", clear=True)
    assert "Rule for IKEA UK removed: 1 entry back to the config's labels" in text
    assert "Rename of IKEA removed" in text
    labels = entry(dsn, ref(dsn, "IKEA", IKEA))
    assert (labels["merchant"], labels["category"]) == ("IKEA", "Home & furniture")


# --- the change list ----------------------------------------------------------------------------


async def test_changes_list_what_chat_set_with_numbers_to_undo(mcp, dsn):
    assert await call(mcp, "finance_changes") == "No labels set from chat yet."
    ikea, stall = ref(dsn, "IKEA", IKEA), ref(dsn, "SQ *MARKET STALL", STALL)
    await call(mcp, "finance_label", refs=[ikea, stall], category="Groceries", note="checked")
    rule = change_of(await call(mcp, "finance_label", merchant="PRET", category="Groceries"))
    lines = (await call(mcp, "finance_changes")).splitlines()
    assert lines[0] == "Labels set from chat, newest first"
    assert re.fullmatch(
        rf"- change {rule} · \d+ \w+ \d\d:\d\d · rule PRET: category Groceries", lines[1]
    )
    assert lines[2].endswith(f"#{ikea}, #{stall}: category Groceries, note checked")
    assert lines[-1] == "→ undo one: finance_label(undo=<change>)"


async def test_an_iou_counts_in_the_balance_until_closed_and_undo_brings_it_back(mcp, dsn):
    with pytest.raises(ToolError, match="no such person"):
        await call(mcp, "finance_iou", person="Sam", amount=-15, note="court")
    with pytest.raises(ToolError, match="needs person, amount"):
        await call(mcp, "finance_iou", person="Sam", amount=-15, new_name=True)
    court = await call(mcp, "finance_iou", person="Sam", amount=-15, note="court", new_name=True)
    assert "IOU 1: you owe Sam £15.00 (court)" in court
    dinner = await call(
        mcp, "finance_iou", person="sam", amount=40, note="dinner", day="2026-10-01"
    )
    assert "IOU 2: Sam owes you £40.00 (dinner)" in dinner
    assert (await call(mcp, "finance_debts")).splitlines()[:2] == [
        "Owed, by person",
        "- Sam owes you £25.00",
    ]
    sam = (await call(mcp, "finance_debts", person="Sam")).splitlines()
    assert sam[:3] == [
        "Sam owes you £25.00",
        "IOU 2  2026-10-01  +£40.00  dinner  → £40.00",
        f"IOU 1  {fx.NOW.date()}  -£15.00  court  → £25.00",
    ]
    closed = await call(mcp, "finance_iou", close=2)
    assert "IOU 2 closed: Sam owes you £40.00 no more" in closed
    assert "- You owe Sam £15.00" in await call(mcp, "finance_debts")
    with pytest.raises(ToolError, match="no open IOU 2"):
        await call(mcp, "finance_iou", close=2)
    await call(mcp, "finance_label", undo=change_of(closed))
    assert "- Sam owes you £25.00" in await call(mcp, "finance_debts")
    await call(mcp, "finance_label", undo=change_of(court))  # undoing its start closes it
    assert "- Sam owes you £40.00" in await call(mcp, "finance_debts")
    lines = (await call(mcp, "finance_changes")).splitlines()
    assert lines[1].endswith("IOU 1: closed") and lines[-2].endswith("IOU 1: you owe Sam £15.00")


# --- the role -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        # the read paths can't write: the role's transactions are read-only unless opened otherwise
        ("insert into ledger.merchant_rule (prefix, category) values ('TESCO', 'x')", "read-only"),
        ("begin read write; update ledger.entry_edit set note = 'x'", "permission denied"),
        ("begin read write; delete from ledger.merchant_rule", "permission denied"),
        (
            "begin read write; insert into ledger.merchant_rule (n, prefix, category)"
            " values (1, 'TESCO', 'x')",
            "permission denied",
        ),
        (
            "begin read write; insert into ledger.merchant_alias (at, prefix, name)"
            " values ('2020-01-01', 'TESCO', 'X')",
            "permission denied",
        ),
        ("begin read write; update ledger.label set category = 'x'", "permission denied"),
        ("begin read write; update ledger.entry_share set pence = 1", "permission denied"),
        ("begin read write; delete from ledger.entry_share", "permission denied"),
        ("begin read write; update ledger.entry_part set pence = 1", "permission denied"),
        ("begin read write; update ledger.iou set pence = 1", "permission denied"),
        ("begin read write; delete from ledger.iou", "permission denied"),
        (
            "begin read write; insert into ledger.iou (n, iou, person, pence, day)"
            " values (1, 1, 'Sam', 100, '2026-10-01')",
            "permission denied",
        ),
        (
            "begin read write; insert into ledger.booked_from values ('amex', 'a', 'b')",
            "permission denied",
        ),
        ("begin read write; update ledger.transaction set pence = 0", "permission denied"),
        (
            "begin read write; insert into ledger.merchant_rule (prefix, flow) values ('TESCO', 'reversed')",
            "check constraint",
        ),
        (
            "begin read write; insert into ledger.merchant_rule (prefix, category) values ('tesco', 'x')",
            "check constraint",
        ),
    ],
)
def test_the_role_only_adds_chat_rows(dsn, sql, error):
    with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.Error, match=error):
        conn.execute(sql.encode())
