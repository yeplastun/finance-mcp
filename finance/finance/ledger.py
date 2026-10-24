"""The ledger in PostgreSQL: ingest raw runs from data/, then label them from the personal config.

Ingest only adds: run, account and transaction rows, money in integer pence. Enrich rebuilds label and trip
from the transactions plus `.local/ledger.toml` (rules naming people stay out of git), so a config edit
followed by `python -m finance ledger` relabels everything. It also links each pending row to the booked row
it became (`booked_from`), and those links, unlike labels, are kept once made.
"""

import hashlib
import json
import re
import tomllib
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
from psycopg.types.json import Jsonb

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
REAL_DATE = re.compile(r"\bON (\d{2}) ([A-Z]{3})\b")
FX = re.compile(r"AMOUNT IN (?!GBP)([A-Z]{3}) ([\d,]+(?:\.\d+)?)(?:.*?NON-STERLING TRANS FEE £([\d,]+\.\d{2}))?")
PAIR_DAYS = 4  # a card payment or bank transfer lands on the other account within this many days
BOOKED_AHEAD_DAYS = 150
BOOKS_WITHIN = (-1, 5)  # a pending card payment books this many days from its own date


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS) -> list[int]:
    """Apply numbered NNN_name.sql files not yet applied; an edited applied file stops everything."""
    conn.execute("create schema if not exists ledger")  # the database is shared, so the table lives in ours
    conn.execute(
        "create table if not exists ledger.schema_migrations (version integer primary key,"
        " checksum text not null, applied_at timestamptz not null default now())"
    )
    applied = dict(conn.execute("select version, checksum from ledger.schema_migrations").fetchall())
    done = []
    for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        version, sql = int(path.name[:3]), path.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(f"applied migration {path.name} was edited; add a new migration instead")
            continue
        with conn.transaction():
            conn.execute(sql.encode())
            conn.execute(
                "insert into ledger.schema_migrations (version, checksum) values (%s, %s)", (version, checksum)
            )
        done.append(version)
    return done


# --- ingest -------------------------------------------------------------------------------------------


def to_pence(amount: Decimal) -> int:
    value = amount * 100
    if value != value.to_integral_value():
        raise ValueError("amount has sub-penny precision")
    return int(value)


def effective_day(timestamp: str, description: str) -> date:
    """Barclays posts card payments days later; its description carries the real date ('ON 10 OCT')."""
    posted = date.fromisoformat(timestamp[:10])
    found = REAL_DATE.search(description.upper())
    if not found or found.group(2) not in MONTHS:
        return posted
    for year in (posted.year, posted.year - 1):
        try:
            real = date(year, MONTHS[found.group(2)], int(found.group(1)))
        except ValueError:  # 29 FEB in a non-leap year, or garbage
            continue
        if real <= posted:
            return real
    return posted


def fx(description: str) -> tuple[str | None, Decimal | None, int | None]:
    found = FX.search(description.upper())
    if not found:
        return None, None, None
    fee = to_pence(Decimal(found.group(3).replace(",", ""))) if found.group(3) else None
    return found.group(1), Decimal(found.group(2).replace(",", "")), fee


def _read(path: Path) -> Any:
    return json.loads(path.read_text(), parse_float=Decimal)


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=float))  # Decimal back to the same JSON number


def transaction_row(t: dict, account_id: str, status: str, card: bool, run: str) -> tuple:
    meta = t.get("meta") or {}
    description = t.get("description") or ""
    pence = to_pence(Decimal(str(t["amount"]))) * (-1 if card else 1)
    return (
        account_id,
        t["transaction_id"],
        status,
        t["timestamp"],
        effective_day(t["timestamp"], description),
        pence,
        t["currency"],
        description,
        meta.get("provider_merchant_name") or meta.get("counter_party_preferred_name"),
        meta.get("provider_id"),
        *fx(description),
        _jsonb(t),
        run,
        run,
    )


UPSERT = """insert into ledger.transaction (account_id, id, status, at, day, pence, currency, description, merchant_raw,
    provider_ref, fx_currency, fx_amount, fee_pence, raw, first_run, last_run)
    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    on conflict (account_id, id, status) do update set at = excluded.at, day = excluded.day, pence = excluded.pence,
    currency = excluded.currency, description = excluded.description, merchant_raw = excluded.merchant_raw,
    provider_ref = excluded.provider_ref, fx_currency = excluded.fx_currency, fx_amount = excluded.fx_amount,
    fee_pence = excluded.fee_pence, raw = excluded.raw, last_run = excluded.last_run"""


def ingest(conn: psycopg.Connection, data: Path) -> tuple[int, int]:
    """Load runs not yet in the ledger, oldest first. Returns (runs, transaction rows written)."""
    done = {name for (name,) in conn.execute("select id from ledger.run")}
    runs = written = 0
    for run in sorted(p for p in data.iterdir() if p.is_dir() and p.name not in done):
        fetched = datetime.strptime(run.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("insert into ledger.run (id, fetched_at) values (%s, %s)", (run.name, fetched))
            for folder in sorted(run.glob("account-*")):
                if not (folder / "complete.json").exists():
                    continue  # the fetch failed part-way; what's missing from it proves nothing
                account = _read(folder / "account.json")
                card = account["kind"] == "cards"
                cur.execute(
                    """insert into ledger.account (id, provider, kind, name, currency, raw, last_run)
                    values (%s, %s, %s, %s, %s, %s, %s) on conflict (id) do update set provider = excluded.provider,
                    kind = excluded.kind, name = excluded.name, currency = excluded.currency, raw = excluded.raw,
                    last_run = excluded.last_run""",
                    (
                        account["account_id"],
                        account["provider"]["provider_id"],
                        account["kind"],
                        account.get("display_name") or account["provider"]["display_name"],
                        account["currency"],
                        _jsonb(account),
                        run.name,
                    ),
                )
                for status, name in (("booked", "transactions.json"), ("pending", "pending.json")):
                    if (folder / name).exists():
                        rows = [
                            transaction_row(t, account["account_id"], status, card, run.name)
                            for t in _read(folder / name)["results"]
                        ]
                        cur.executemany(UPSERT, rows)
                        written += len(rows)
        runs += 1
    return runs, written


def save_config(conn: psycopg.Connection, text: str) -> None:
    """Keep a copy of the personal config in the database, so its backup covers it, and its notes apart for the
    finance MCP, which may read them but not the config."""
    conn.execute(
        "insert into ledger.config (id, text, notes) values (true, %s, %s) on conflict (id) do update"
        " set text = excluded.text, notes = excluded.notes, saved_at = now()"
        " where ledger.config.text <> excluded.text or ledger.config.notes <> excluded.notes",
        (text, tomllib.loads(text).get("notes", "").strip()),
    )


# --- enrich -------------------------------------------------------------------------------------------


class Tx(NamedTuple):
    account_id: str
    id: str
    status: str
    day: date
    pence: int
    description: str
    merchant_raw: str | None
    provider_ref: str | None
    provider_category: str | None
    address: str | None


class Label(NamedTuple):
    account_id: str
    id: str
    status: str
    merchant: str
    flow: str
    category: str
    rule: str
    trip: str | None
    trip_kind: str | None
    tags: list[str]
    note: str | None
    pair_account_id: str | None
    pair_id: str | None


def whose_defaults(table: dict, where: str) -> dict[str, str]:
    out = {}
    for kind, categories in table.items():
        if kind not in ("personal", "joint"):
            raise ValueError(f"{where} {kind!r}: use personal or joint")
        for category in categories:
            if category in out:
                raise ValueError(f"category {category!r} is in {where} twice")
            out[category] = kind
    return out


def load_config(path: Path) -> dict:
    """The personal config (see ledger.example.toml), with regexes compiled and amounts in pence."""
    config = tomllib.loads(path.read_text(), parse_float=Decimal)
    for rule in config.setdefault("rule", []):
        rule["match"] = re.compile(rule["match"])
        rule["min"] = to_pence(Decimal(rule.get("min", 0)))
        rule["max"] = to_pence(Decimal(rule["max"])) if "max" in rule else None
    config["merchant_names"] = [(re.compile(p), name) for p, name in config.get("merchant_names", {}).items()]
    config.setdefault("merchants", {})
    config["abroad"] = re.compile(config["abroad"], re.IGNORECASE) if config.get("abroad") else None
    config["trip_kinds"] = [(kind, re.compile(p)) for kind, p in config.get("trip_kinds", {}).items()]
    config.setdefault("category_kinds", {})
    config["not_trip"] = set(config.get("not_trip", []))
    config.setdefault("trip", [])
    groups = config.pop("groups", {})
    config["groups"] = {}  # category -> its group
    for name, categories in groups.items():
        for category in categories:
            if category in config["groups"]:
                raise ValueError(f"category {category!r} is in two groups")
            config["groups"][category] = name
    whose = config.pop("whose", {})
    weekends = whose.pop("weekends", {})
    config["whose"] = whose_defaults(whose, "[whose]")  # category -> personal or joint
    config["whose_weekends"] = whose_defaults(weekends, "[whose.weekends]")  # the same, on Saturdays and Sundays
    for override in config.setdefault("override", []):
        override["pence"] = to_pence(Decimal(override["amount"]))
    return config


def canonical(merchant_raw: str | None, description: str, names: list) -> str:
    merchant = (merchant_raw or description).strip().upper()
    return next((name for rx, name in names if rx.search(merchant)), merchant)


def classify(tx: Tx, merchant: str, config: dict) -> tuple[str, str, str, list[str]]:
    """(flow, category, rule, tags). Rules match "MERCHANT|DESCRIPTION|PROVIDER_CATEGORY"; first match wins."""
    key = f"{merchant}|{tx.description.upper()}|{(tx.provider_category or '').upper()}"
    size = abs(tx.pence)
    for n, rule in enumerate(config["rule"], 1):
        direction = rule.get("direction")
        if (
            rule["match"].search(key)
            and size >= rule["min"]
            and (rule["max"] is None or size <= rule["max"])
            and (direction is None or (direction == "in") == (tx.pence > 0))
        ):
            return rule["flow"], rule["category"], f"rule {n}", list(rule.get("tags", []))
    if merchant in config["merchants"]:
        return "spend", config["merchants"][merchant], "merchant", []
    if tx.pence < 0 and config["abroad"] and config["abroad"].search(f"{tx.address or ''}|{tx.description}"):
        return "spend", "Travel", "abroad", []
    return ("spend" if tx.pence < 0 else "income"), "Uncategorised", "default", []


def enrich_rows(txs: list[Tx], config: dict) -> tuple[list[Label], dict]:
    """Label every transaction; returns the labels and a report that holds counts only (safe to print)."""
    labels: dict[tuple[str, str, str], dict[str, Any]] = {}
    for tx in txs:
        merchant = canonical(tx.merchant_raw, tx.description, config["merchant_names"])
        flow, category, rule, tags = classify(tx, merchant, config)
        labels[(tx.account_id, tx.id, tx.status)] = {
            "merchant": merchant,
            "flow": flow,
            "category": category,
            "rule": rule,
            "tags": tags,
            "trip": None,
            "trip_kind": None,
            "note": None,
            "pair": (None, None),
            "tx": tx,
        }
    booked = [v for k, v in labels.items() if k[2] == "booked"]
    report: dict[str, Any] = {"labelled": len(labels), "overrides_not_matching": []}

    # Overrides: manual answers for booked transactions picked by day + merchant prefix + amount. Exactly
    # `count` (default 1) must match, so a typo or a re-issued transaction shows in the report instead.
    trip_set = set()
    for n, o in enumerate(config["override"], 1):
        hits = [
            v
            for v in booked
            if v["tx"].day == o["day"]
            and v["tx"].pence == o["pence"]
            and v["merchant"].startswith(o["merchant"].upper())
        ]
        if len(hits) != o.get("count", 1):
            report["overrides_not_matching"].append(n)
            continue
        for hit in hits:
            for field in ("flow", "category", "note", "trip_kind"):
                hit[field] = o.get(field, hit[field])
            hit["tags"] = sorted(set(hit["tags"]) | set(o.get("tags", [])))
            hit["rule"] = f"override {n}"
            if "trip" in o:  # "" means: not part of any trip
                hit["trip"] = o["trip"] or None
                trip_set.add(id(hit))

    # Same account, same day, same category, amounts cancelling out: a bounced payment or an instant refund.
    groups = defaultdict(list)
    for v in booked:
        if v["flow"] in ("spend", "housing"):
            groups[(v["tx"].account_id, v["tx"].day, v["category"])].append(v)
    for group in groups.values():
        for v in group:
            match = next(
                (
                    w
                    for w in group
                    if w is not v
                    and w["flow"] != "reversed"
                    and v["flow"] != "reversed"
                    and w["tx"].pence == -v["tx"].pence
                ),
                None,
            )
            if match:
                v["flow"] = match["flow"] = "reversed"

    # Transfers between own accounts: pair each outgoing leg with an incoming one of the same amount.
    incoming = defaultdict(list)
    for v in booked:
        if v["flow"] == "transfer" and v["tx"].pence > 0:
            incoming[v["tx"].pence].append(v)
    for v in sorted((v for v in booked if v["flow"] == "transfer" and v["tx"].pence < 0), key=lambda v: v["tx"].day):
        out = v["tx"]
        candidates = [
            w
            for w in incoming[-out.pence]
            if w["pair"] == (None, None)
            and w["tx"].account_id != out.account_id
            and abs((w["tx"].day - out.day).days) <= PAIR_DAYS
        ]
        if candidates:
            w = min(
                candidates, key=lambda w: (w["tx"].provider_ref != out.provider_ref, abs((w["tx"].day - out.day).days))
            )
            v["pair"], w["pair"] = (w["tx"].account_id, w["tx"].id), (out.account_id, out.id)
    unpaired = defaultdict(int)
    for v in booked:
        if v["flow"] == "transfer" and v["pair"] == (None, None):
            unpaired[v["category"]] += 1
    report["unpaired_transfers"] = dict(unpaired)

    # Trips: spending between departure and the day after return, plus travel booked up to 150 days ahead. A
    # domestic trip (in the UK) takes neither: no late foreign charges follow it, and booked-ahead travel belongs to
    # the next trip abroad; its own bookings are named with overrides or from chat.
    trips = sorted(
        (t["from"], t["to"] + timedelta(days=0 if t.get("domestic") else 1), t["name"], bool(t.get("domestic")))
        for t in config["trip"]
    )
    for v in booked:
        tx = v["tx"]
        if id(v) not in trip_set:
            if v["flow"] != "spend":
                continue
            v["trip"] = next((n for a, b, n, _ in trips if a <= tx.day <= b), None)
            if v["trip"] and v["category"] in config["not_trip"]:
                v["trip"] = None
            if v["trip"] is None and v["category"] == "Travel":
                v["trip"] = next(
                    (
                        n
                        for a, _, n, home in trips
                        if not home and tx.day < a <= tx.day + timedelta(days=BOOKED_AHEAD_DAYS)
                    ),
                    None,
                )
                if v["trip"]:
                    v["tags"] = sorted(set(v["tags"]) | {"booked-ahead"})
        if v["trip"]:
            v["trip_kind"] = (
                v["trip_kind"]
                or next((k for k, rx in config["trip_kinds"] if rx.search(v["merchant"])), None)
                or config["category_kinds"].get(v["category"], "Other on trip")
            )
    unknown = {v["trip"] for v in booked if v["trip"]} - {n for _, _, n, _ in trips}
    if unknown:
        raise ValueError(f"{len(unknown)} override(s) name a trip that isn't in [[trip]]")
    report["on_trips"] = sum(1 for v in booked if v["trip"])
    report["uncategorised"] = sum(1 for v in booked if v["category"] == "Uncategorised")

    result = [
        Label(
            *k,
            v["merchant"],
            v["flow"],
            v["category"],
            v["rule"],
            v["trip"],
            v["trip_kind"],
            v["tags"],
            v["note"],
            *v["pair"],
        )
        for k, v in labels.items()
    ]
    return result, report


class Seen(NamedTuple):
    account_id: str
    id: str
    day: date
    pence: int
    merchant: str
    first_run: str
    at: datetime


def match_pending(pending: list[Seen], booked: list[Seen]) -> list[tuple[str, str, str]]:
    """(account_id, booked id, pending id) for each pending row and the booked row it became.

    Banks give the booked row a new ID, so: same account and amount, booked from a day before to five days
    after, by a run no earlier than the one that first listed the pending row; the same merchant first (Square
    can rename it), then the nearest day. A payment whose amount changed on booking (a tip, an exchange rate)
    finds nothing and simply drops out of the bank's pending list.
    """
    free = defaultdict(list)
    for b in booked:
        free[(b.account_id, b.pence)].append(b)
    found = []
    for p in sorted(pending, key=lambda p: (p.at, p.id)):
        candidates = [
            b
            for b in free[(p.account_id, p.pence)]
            if BOOKS_WITHIN[0] <= (b.day - p.day).days <= BOOKS_WITHIN[1] and b.first_run >= p.first_run
        ]
        if candidates:
            b = min(candidates, key=lambda b: (b.merchant != p.merchant, abs((b.day - p.day).days), b.at, b.id))
            free[(p.account_id, p.pence)].remove(b)
            found.append((p.account_id, b.id, p.id))
    return found


SEEN = "select account_id, id, day, pence, coalesce(merchant_raw, description), first_run, at from ledger.transaction t"


def link_pending(conn: psycopg.Connection) -> int:
    """Add booked_from links for pending rows that have booked. A link is never redrawn, so chat labels keyed
    on the pending row can't move to another entry later."""
    pending = [
        Seen(*row)
        for row in conn.execute(
            SEEN + " where status = 'pending' and not exists (select from ledger.booked_from b"
            " where (b.account_id, b.pending_id) = (t.account_id, t.id))"
        )
    ]
    if not pending:
        return 0
    booked = [
        Seen(*row)
        for row in conn.execute(
            SEEN + " where status = 'booked' and day >= %s and not exists (select from ledger.booked_from b"
            " where (b.account_id, b.id) = (t.account_id, t.id))",
            (min(p.day for p in pending) + timedelta(days=BOOKS_WITHIN[0]),),
        )
    ]
    links = match_pending(pending, booked)
    with conn.cursor() as cur:
        cur.executemany("insert into ledger.booked_from (account_id, id, pending_id) values (%s, %s, %s)", links)
    return len(links)


def enrich(conn: psycopg.Connection, config: dict) -> dict:
    txs = [
        Tx(*row)
        for row in conn.execute("""
        select t.account_id, t.id, t.status, t.day, t.pence, t.description, t.merchant_raw, t.provider_ref,
               t.raw->'meta'->>'provider_category', t.raw->'meta'->>'address'
        from ledger.transaction t join ledger.account a on a.id = t.account_id
        where t.status = 'booked' or t.last_run = a.last_run""")
    ]
    labels, report = enrich_rows(txs, config)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from ledger.label")
        cur.execute("delete from ledger.trip")
        cur.execute("delete from ledger.category_group")
        cur.execute("delete from ledger.category_whose")
        cur.execute("delete from ledger.not_trip")
        cur.executemany("insert into ledger.not_trip values (%s)", [(c,) for c in sorted(config["not_trip"])])
        cur.executemany("insert into ledger.category_group values (%s, %s)", list(config["groups"].items()))
        whose, weekends = config["whose"], config["whose_weekends"]
        cur.executemany(
            "insert into ledger.category_whose values (%s, %s, %s)",
            [(c, whose.get(c), weekends.get(c)) for c in sorted(whose.keys() | weekends.keys())],
        )
        cur.executemany(
            "insert into ledger.trip (name, starts, ends, purpose, joint) values (%s, %s, %s, %s, %s)",
            [(t["name"], t["from"], t["to"], t.get("purpose"), bool(t.get("joint"))) for t in config["trip"]],
        )
        with cur.copy(
            "copy ledger.label (account_id, id, status, merchant, flow, category, rule, trip, trip_kind,"
            " tags, note, pair_account_id, pair_id) from stdin"
        ) as copy:
            for label in labels:
                copy.write_row(label)
        report["pending_booked"] = link_pending(conn)
    # Booked rows the latest window should have listed again but didn't: re-issued IDs would double count.
    report["vanished_booked"] = conn.execute("""
        select count(*) from ledger.transaction t join ledger.account a on a.id = t.account_id
        join ledger.run r on r.id = a.last_run
        where t.status = 'booked' and t.last_run <> a.last_run and t.at >= r.fetched_at - interval '85 days'""").fetchone()[0]
    return report
