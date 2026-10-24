import glob
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from finance import ledger
from finance.ledger import Tx

CONFIG = """
abroad = "TOKYO"
not_trip = ["Rent"]

[[rule]]
flow = "transfer"
category = "Pot move"
match = '\\|UK_RETAIL_POT$'

[[rule]]
flow = "transfer"
category = "Own account"
match = "^ME\\\\||AMERICAN EXP|PAYMENT RECEIVED"

[[rule]]
flow = "income"
category = "Payday"
match = "FRIEND"
direction = "in"
min = 900

[[rule]]
flow = "loan"
category = "Loan"
match = "FRIEND"

[[rule]]
flow = "housing"
category = "Rent"
match = "LANDLORD"

[[rule]]
flow = "spend"
category = "Travel"
match = "AIRBNB|EASYJET"

[merchant_names]
"^GOOGLE ?\\\\*?CLOUD" = "GOOGLE CLOUD"

[merchants]
"GOOGLE CLOUD" = "Software"

[trip_kinds]
"Flights" = "EASYJET"
"Stay" = "AIRBNB"

[category_kinds]
"Groceries" = "Food"

[[trip]]
name = "2026-07 Beach holiday"
from = 2026-07-10
to = 2026-07-15
joint = true

[[trip]]
name = "2026-09 Work"
from = 2026-09-01
to = 2026-09-03

[[trip]]
name = "2026-06 Weekend away"
from = 2026-06-06
to = 2026-06-07
domestic = true

[[override]]
day = 2026-05-01
merchant = "EASYJET"
amount = -120.50
trip = "2026-09 Work"
tags = ["reimbursed"]

[[override]]
day = 2026-01-01
merchant = "NOTHING"
amount = -1
trip = ""

[whose]
joint = ["Groceries"]

[whose.weekends]
joint = ["Eating out"]

[groups]
"Food" = ["Groceries", "Eating out"]
"""

NOTED = 'notes = """\nKim is a friend.\n"""\n' + CONFIG


def tx(n, day, pence, merchant, account="a", ref=None, category=None, address=None, status="booked"):
    return Tx(account, f"t{n}", status, date.fromisoformat(day), pence, merchant, merchant, ref, category, address)


def labelled(txs, config_text=CONFIG):
    path = Path(tempfile.mkdtemp()) / "ledger.toml"
    path.write_text(config_text)
    labels, report = ledger.enrich_rows(txs, ledger.load_config(path))
    return {label.id: label for label in labels}, report


def test_pence_parsing_is_exact_and_rejects_sub_penny():
    assert ledger.to_pence(Decimal("1406.59")) == 140659
    assert ledger.to_pence(Decimal("-0.1")) == -10
    with pytest.raises(ValueError):
        ledger.to_pence(Decimal("1.005"))


def test_effective_day_reads_barclays_date_across_year_end_and_leap_day():
    assert ledger.effective_day("2026-10-14T00:00:00Z", "SHOP ON 10 OCT CPM") == date(2026, 10, 10)
    assert ledger.effective_day("2027-01-02T00:00:00Z", "SHOP ON 31 DEC") == date(2026, 12, 31)
    assert ledger.effective_day("2027-03-02T00:00:00Z", "SHOP ON 29 FEB") == date(2027, 3, 2)
    assert ledger.effective_day("2026-10-14T00:00:00Z", "NO DATE HERE") == date(2026, 10, 14)


def test_fx_detail_from_barclays_description():
    text = "CAFE PARIS AMOUNT IN EUR 20.00 ON 15 JAN VISA 1.150000 FINAL GBP AMOUNT INCLUDES NON-STERLING TRANS FEE £0.50"
    assert ledger.fx(text) == ("EUR", Decimal("20.00"), 50)
    assert ledger.fx("TESCO STORES") == (None, None, None)


def test_rules_direction_and_minimum_then_merchant_map_and_abroad():
    labels, _ = labelled(
        [
            tx(1, "2026-03-21", 150000, "FRIEND"),
            tx(2, "2026-03-22", -2000, "FRIEND"),
            tx(3, "2026-03-23", -748, "GOOGLE*CLOUD X1"),
            tx(4, "2026-03-24", -900, "SHOP", address="CHUO-KU TOKYO"),
            tx(5, "2026-03-25", -100, "SOMEWHERE"),
        ]
    )
    assert (labels["t1"].flow, labels["t1"].category) == ("income", "Payday")
    assert (labels["t2"].flow, labels["t2"].category) == ("loan", "Loan")
    assert (labels["t3"].merchant, labels["t3"].category, labels["t3"].rule) == ("GOOGLE CLOUD", "Software", "merchant")
    assert (labels["t4"].category, labels["t4"].rule) == ("Travel", "abroad")
    assert (labels["t5"].flow, labels["t5"].category) == ("spend", "Uncategorised")


def test_transfers_pair_by_amount_and_provider_ref():
    labels, report = labelled(
        [
            tx(1, "2026-04-01", -5000, "ME", account="bank"),
            tx(2, "2026-04-02", 5000, "ME", account="monzo"),
            tx(3, "2026-04-05", -300, "SAVINGS", account="monzo", ref="tx_1", category="uk_retail_pot"),
            tx(4, "2026-04-05", 300, "SAVINGS", account="pot", ref="tx_1", category="uk_retail_pot"),
            tx(5, "2026-04-05", 300, "SAVINGS", account="pot2", ref="tx_2", category="uk_retail_pot"),
            tx(6, "2026-04-20", -7000, "AMERICAN EXP", account="bank"),
        ]
    )
    assert (labels["t1"].pair_account_id, labels["t1"].pair_id) == ("monzo", "t2")
    assert labels["t3"].pair_id == "t4" and labels["t4"].pair_id == "t3"
    assert labels["t5"].pair_id is None
    assert report["unpaired_transfers"] == {"Pot move": 1, "Own account": 1}


def test_same_day_cancelling_payments_are_reversed():
    labels, _ = labelled(
        [
            tx(1, "2026-08-20", -200000, "LANDLORD STO"),
            tx(2, "2026-08-20", 200000, "LANDLORD UNP"),
            tx(3, "2026-08-22", -200000, "LANDLORD FT"),
        ]
    )
    assert labels["t1"].flow == labels["t2"].flow == "reversed"
    assert labels["t3"].flow == "housing"


def seen(n, day, pence, merchant, run="r1", account="amex"):
    return ledger.Seen(
        account, f"t{n}", date.fromisoformat(day), pence, merchant, run, datetime(2026, 10, n, tzinfo=UTC)
    )


def test_pending_rows_match_the_booked_row_they_became():
    pending = [
        seen(1, "2026-10-12", -585, "TESCO"),
        seen(2, "2026-10-12", -585, "TESCO"),  # two alike: each takes its own booked row
        seen(3, "2026-10-09", 390, "SQUAREUP"),  # Square renames the merchant on booking
        seen(4, "2026-10-12", -2000, "UBER"),  # a hold that never booked
        seen(5, "2026-10-12", -700, "PRET", run="r2"),
        seen(6, "2026-10-01", -1200, "CAFE"),
    ]
    booked = [
        seen(11, "2026-10-12", -585, "TESCO"),
        seen(12, "2026-10-13", -585, "TESCO"),
        seen(13, "2026-10-12", -585, "WAITROSE"),
        seen(14, "2026-10-09", 390, "CORNER CAFE"),
        seen(15, "2026-10-12", -1999, "UBER"),  # the amount changed: not this one
        seen(16, "2026-10-12", -700, "PRET", run="r1"),  # in the bank's list before the pending row was
        seen(17, "2026-10-08", -1200, "CAFE"),  # too long after
        seen(18, "2026-10-12", -585, "TESCO", account="monzo"),
    ]
    assert ledger.match_pending(pending, booked) == [
        ("amex", "t11", "t1"),
        ("amex", "t12", "t2"),
        ("amex", "t14", "t3"),
    ]


def test_trips_window_exclusions_booked_ahead_and_overrides():
    labels, report = labelled(
        [
            tx(1, "2026-07-16", -4000, "CAFE"),
            tx(2, "2026-07-12", -200000, "LANDLORD"),
            tx(3, "2026-03-01", -30000, "AIRBNB"),
            tx(4, "2026-05-01", -12050, "EASYJET"),
            tx(5, "2026-07-11", -500, "TESCO"),
            tx(6, "2026-07-11", -999, "AIRBNB", status="pending"),
            tx(7, "2026-06-07", -500, "CAFE"),
            tx(8, "2026-06-08", -500, "CAFE"),
        ]
    )
    assert (labels["t1"].trip, labels["t1"].trip_kind) == ("2026-07 Beach holiday", "Other on trip")
    assert labels["t2"].trip is None
    assert (labels["t3"].trip, labels["t3"].trip_kind, labels["t3"].tags) == ("2026-07 Beach holiday", "Stay", ["booked-ahead"])
    assert (labels["t4"].trip, labels["t4"].tags, labels["t4"].rule) == ("2026-09 Work", ["reimbursed"], "override 1")
    assert labels["t6"].trip is None
    assert (labels["t7"].trip, labels["t8"].trip) == ("2026-06 Weekend away", None)  # domestic: no day after
    assert report["overrides_not_matching"] == [2]


def test_a_category_in_two_groups_fails():
    with pytest.raises(ValueError, match="two groups"):
        labelled([], CONFIG + '"Shops" = ["Groceries"]\n')


def test_whose_takes_personal_or_joint_once_per_category():
    with pytest.raises(ValueError, match="twice"):
        labelled([], CONFIG.replace('joint = ["Groceries"]', 'joint = ["Groceries"]\npersonal = ["Groceries"]'))
    with pytest.raises(ValueError, match="personal or joint"):
        labelled([], CONFIG.replace('joint = ["Groceries"]', 'hers = ["Groceries"]'))
    with pytest.raises(ValueError, match=r"\[whose.weekends\] twice"):
        labelled([], CONFIG.replace('joint = ["Eating out"]', 'joint = ["Eating out"]\npersonal = ["Eating out"]'))


def test_override_naming_unknown_trip_fails():
    with pytest.raises(ValueError, match="trip"):
        labelled([tx(1, "2026-05-01", -12050, "EASYJET")], CONFIG.replace('trip = "2026-09 Work"', 'trip = "Mars"'))


def _bindir() -> Path | None:
    """PostgreSQL's binaries: on PATH, or where Ubuntu and Homebrew keep them."""
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    for pattern in (
        "/usr/lib/postgresql/*/bin",
        "/opt/homebrew/opt/postgresql@*/bin",
        "/usr/local/opt/postgresql@*/bin",
    ):
        if hits := sorted(glob.glob(pattern)):
            return Path(hits[-1])
    return None


@pytest.fixture(scope="module")
def dsn():
    bindir = _bindir()
    if bindir is None:
        if os.environ.get("CI"):
            pytest.fail("PostgreSQL binaries are required in CI")
        pytest.skip("PostgreSQL binaries not installed")
    root = Path(tempfile.mkdtemp(prefix="ledger-pg-"))
    env = {**os.environ, "LC_ALL": "C"}  # macOS: postmaster refuses to start without a valid locale
    try:
        subprocess.run(
            [bindir / "initdb", "-D", root / "data", "-U", "postgres", "-A", "trust", "--no-sync", "-E", "UTF8"],
            check=True,
            capture_output=True,
            env=env,
        )
        (root / "sock").mkdir()
        subprocess.run(
            [
                bindir / "pg_ctl",
                "-D",
                root / "data",
                "-l",
                root / "log",
                "-w",
                "start",
                "-o",
                f"-k {root / 'sock'} -c listen_addresses='' -c fsync=off",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        yield f"host={root / 'sock'} user=postgres dbname=postgres"
    finally:
        subprocess.run([bindir / "pg_ctl", "-D", root / "data", "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def write_run(data, name, kind, booked, pending):
    folder = data / name / "account-1"
    folder.mkdir(parents=True)
    account = {
        "kind": kind,
        "account_id": "acc1",
        "currency": "GBP",
        "display_name": "Card",
        "provider": {"provider_id": "ob-amex", "display_name": "Amex"},
    }
    for file, value in (
        ("account.json", account),
        ("transactions.json", {"results": booked}),
        ("pending.json", {"results": pending}),
        ("complete.json", {"bank": "ob-amex"}),
    ):
        (folder / file).write_text(json.dumps(value))


def raw(n, amount, day="2026-10-01"):
    return {
        "transaction_id": f"id{n}",
        "timestamp": f"{day}T00:00:00+00:00",
        "amount": amount,
        "currency": "GBP",
        "description": f"SHOP {n}",
        "meta": {"provider_merchant_name": f"SHOP {n}"},
    }


def test_ingest_and_enrich_in_postgres(dsn, tmp_path):
    data = tmp_path / "data"
    write_run(data, "20261010T080000Z-aaaa", "cards", [raw(1, 12.34)], [raw(9, 5.0), raw(8, 7.0)])
    write_run(data, "20261011T080000Z-bbbb", "cards", [raw(1, 12.34), raw(2, -3.5), raw(3, 5.0)], [raw(8, 7.0)])
    (data / "20261011T090000Z-cccc" / "account-1").mkdir(parents=True)  # incomplete run: ignored
    config = tmp_path / "ledger.toml"
    config.write_text(CONFIG)
    with ledger.connect(dsn) as conn:
        assert ledger.migrate(conn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
        assert ledger.migrate(conn) == []
        assert ledger.ingest(conn, data) == (3, 7)
        assert ledger.ingest(conn, data) == (0, 0)
        report = ledger.enrich(conn, ledger.load_config(config))
        assert ledger.enrich(conn, ledger.load_config(config))["pending_booked"] == 0  # links are made once
        ledger.save_config(conn, CONFIG)
        ledger.save_config(conn, CONFIG)
        saved = conn.execute("select text, notes from ledger.config").fetchall()
        groups = conn.execute("select category, name from ledger.category_group order by 1").fetchall()
        whose = conn.execute("select category, whose, weekends from ledger.category_whose order by 1").fetchall()
        joint = conn.execute("select name from ledger.trip where joint").fetchall()
        ledger.save_config(conn, NOTED)
        noted = conn.execute("select text, notes from ledger.config").fetchall()
        rows = conn.execute("select id, status, pence, flow, whose from ledger.entry order by id").fetchall()
        first = conn.execute("select first_run, last_run from ledger.transaction where id = 'id1'").fetchone()
        nums = dict(conn.execute("select id, num from ledger.transaction where id in ('id3', 'id9')").fetchall())
        booked = conn.execute(
            "select num, added, edit_id, edit_status, was_pending_as from ledger.entry where id = 'id3'"
        ).fetchone()
    # Card amounts flip sign: spending is negative. Pending id9 booked as id3: one entry under id9's ref. It was
    # linked on the amount alone (SHOP 9 became SHOP 3), so it says so and stays new. Pending id8 is still listed.
    assert rows == [  # only spending has a whose
        ("id1", "booked", -1234, "spend", "personal"),
        ("id2", "booked", 350, "income", None),
        ("id3", "booked", -500, "spend", "personal"),
        ("id8", "pending", -700, "spend", "personal"),
    ]
    assert booked == (nums["id9"], datetime(2026, 10, 11, 8, tzinfo=UTC), "id9", "pending", "SHOP 9")
    assert first == ("20261010T080000Z-aaaa", "20261011T080000Z-bbbb")
    assert report["vanished_booked"] == 0 and report["labelled"] == 4 and report["pending_booked"] == 1
    assert saved == [(CONFIG, "")]
    assert groups == [("Eating out", "Food"), ("Groceries", "Food")]
    assert whose == [("Eating out", None, "joint"), ("Groceries", "joint", None)]
    assert joint == [("2026-07 Beach holiday",)]
    assert noted == [(NOTED, "Kim is a friend.")]


def test_a_chat_category_that_is_never_on_a_trip_takes_the_entry_off_it(dsn, tmp_path):
    data = tmp_path / "data"  # the database is the module's, so these are entries of their own
    write_run(data, "20261012T080000Z-dddd", "cards", [raw(21, 9.99, "2026-07-12"), raw(22, 5.0, "2026-07-12")], [])
    config = tmp_path / "ledger.toml"
    config.write_text(CONFIG)
    trips = "select id, trip from ledger.entry where id in ('id21', 'id22') order by id"
    with ledger.connect(dsn) as conn:
        ledger.migrate(conn)
        ledger.ingest(conn, data)
        ledger.enrich(conn, ledger.load_config(config))
        before = conn.execute(trips).fetchall()
        conn.execute(
            "insert into ledger.merchant_rule (prefix, category) values ('SHOP 21', 'Rent'), ('SHOP 22', 'Rent')"
        )
        ruled = conn.execute(trips).fetchall()
        conn.execute("insert into ledger.entry_edit (account_id, id, trip) values ('acc1', 'id22', '2026-07 Beach holiday')")
        named = conn.execute(trips).fetchall()
    assert before == [("id21", "2026-07 Beach holiday"), ("id22", "2026-07 Beach holiday")]
    assert ruled == [("id21", None), ("id22", None)]  # Rent is in not_trip
    assert named == [("id21", None), ("id22", "2026-07 Beach holiday")]  # unless chat names the trip


def test_example_config_loads_and_labels():
    config = ledger.load_config(Path(__file__).resolve().parents[1] / "ledger.example.toml")
    labels, report = ledger.enrich_rows([tx(1, "2026-05-01", -30000, "BOOKING.COM")], config)
    assert (labels[0].trip, report["overrides_not_matching"]) == ("2026-07 Beach holiday", [])
