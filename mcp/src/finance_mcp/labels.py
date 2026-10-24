"""Labels set from chat: entries by ref, rules and renames by merchant, splits, parts, IOUs, undo, the change list.

The finance service's migrations 003, 005, 008, 009 and 012 hold the tables; this server may only insert into them
(deploy/finance.sql). Each row is the target's whole chat state, so the newest row wins and a row
of nulls hands the target back to the config. The rows one call writes share a transaction id; the
change's number is the smallest `n` among them, and an undo writes back the state before it.
Everything here runs in a read-write transaction, under one advisory lock, so two calls can't
merge from the same stale state. Every read path stays read-only.

An entry's labels, shares and parts key on its first row (the view's `edit_id`, `edit_status`): a pending
entry keeps them when it books, because the booked row reads them from the pending one.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal

import psycopg

from mcpkit.errors import ToolFailure

from .ledger import SPENDING, Ledger, call, money, plural, suggest, when

# As the chat tables' check constraints (finance migrations 003, 005); a rule can't set `reversed`.
SetFlow = Literal[
    "spend",
    "housing",
    "income",
    "transfer",
    "loan",
    "people",
    "investment",
    "tax",
    "reimbursement",
    "reversed",
]
ENTRY_FIELDS = ("flow", "category", "person", "trip", "tags", "note", "whose")
RULE_FIELDS = ("flow", "category", "person", "whose")
TAG = re.compile(r"[a-z0-9-]{1,30}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SHOWN = 10  # entries listed in a reply; the rest are counted
Key = tuple[str, str, str]  # account_id, id, status of the row an entry's chat state keys on

# One row per chat-table row, whatever the table: what the change list and undo read. A share's
# person, a part's category and an IOU's number are part of its key (`who`), not a value.
CHAT = """with x as (
    select 'entry' as kind, n, at, xact, account_id, id, status, null::text as prefix,
           null::text as who, flow, category, person, trip, tags, note, null::text as name,
           null::bigint as pence, whose, null::date as day from ledger.entry_edit
    union all
    select 'share', n, at, xact, account_id, id, status, null, person, null, null, null, null, null,
           null, null, pence, null, null from ledger.entry_share
    union all
    select 'part', n, at, xact, account_id, id, status, null, category, flow, null, null, null, null,
           null, null, pence, null, null from ledger.entry_part
    union all
    select 'rule', n, at, xact, null, null, null, prefix, null, flow, category, person, null, null,
           null, null, null, whose, null from ledger.merchant_rule
    union all
    select 'rename', n, at, xact, null, null, null, prefix, null, null, null, null, null, null, null,
           name, null, null, null from ledger.merchant_alias
    union all
    select 'iou', n, at, xact, null, null, null, null, iou::text, null, null, person, null, null, note,
           null, pence, null, day from ledger.iou)"""
VALUES = "flow, category, person, trip, tags, note, name, pence, whose, day"
IOU_FIELDS = ("person", "pence", "day", "note")


def tidy(text: str) -> str:
    return " ".join(CONTROL.sub(" ", text).split())


def describe(state: dict[str, Any]) -> str:
    bits = []
    for field in ENTRY_FIELDS:
        value = state.get(field)
        if value is None:
            continue
        if field == "tags":
            bits.append("tags " + (", ".join(value) if value else "none"))
        elif field == "trip" and value == "":
            bits.append("no trip")
        elif field == "note" and value == "":
            bits.append("no note")
        else:
            bits.append(f"{field} {value}")
    return ", ".join(bits) or "back to automatic"


def changed(before: dict[str, Any], after: dict[str, Any]) -> str:
    """`category Uncategorised → Lunch, person Kim` for the fields that moved."""
    bits = []
    for field in ENTRY_FIELDS:
        old, new = before.get(field), after.get(field)
        if old == new:
            continue
        if field == "tags":
            old, new = ", ".join(old or []) or "none", ", ".join(new or []) or "none"
        old, new = old if old not in (None, "") else "–", new if new not in (None, "") else "–"
        bits.append(f"{field} {old} → {new}")
    return ", ".join(bits) or "unchanged"


def split(shares: dict[str, int], entry: dict) -> str:
    """`split: Sam £50.00, Jo £50.00, yours £50.00`, from the entry as it is now."""
    if not shares:
        return "split removed"
    owed = ", ".join(f"{who} {money(p, exact=True)}" for who, p in shares.items())
    return f"split: {owed}, yours {money(-(entry['pence'] + entry['shared']), exact=True)}"


def parted(parts: dict[str, tuple[str, int]], entry: dict) -> str:
    """`parts: Gifts £15.00, rest £15.00 Partner advances`, from the entry as it is now."""
    if not parts:
        return "parts removed"
    moved = ", ".join(f"{c} {money(p, exact=True)}" for c, (_, p) in parts.items())
    rest = abs(entry["pence"]) - entry["parted"]
    return f"parts: {moved}, rest {money(rest, exact=True)} {entry['category']}"


def iou_text(person: str, pence: int) -> str:
    owed = money(abs(pence), exact=True)
    return f"{person} owes you {owed}" if pence > 0 else f"you owe {person} {owed}"


def to_pence(amounts: dict[str, float], what: str = "share") -> dict[str, int]:
    out = {}
    for who, pounds in amounts.items():
        p = round(pounds * 100)
        if not 0 < p <= 10_000_000 or abs(pounds * 100 - p) > 1e-6:
            raise ToolFailure(f"{what} {pounds}: pounds above 0, up to 100,000, at most 2 decimals")
        out[who] = p
    return out


class Labels:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    @asynccontextmanager
    async def writing(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.ledger.connect() as conn, conn.transaction():
            # The role defaults to read-only; only this path opens a write.
            await conn.execute(b"set transaction read write")
            await conn.execute(b"select pg_advisory_xact_lock(hashtext('ledger.chat'))")
            yield conn

    async def rows(
        self, conn: psycopg.AsyncConnection, sql: str, params: Sequence[Any] = ()
    ) -> list[tuple]:
        return await self.ledger.rows(conn, sql, params)

    # --- names ----------------------------------------------------------------------------------

    async def name(
        self,
        conn: psycopg.AsyncConnection,
        what: str,
        value: str,
        new_name: bool,
    ) -> str:
        """A category or person as already spelled, or a new one when new_name is set."""
        value = tidy(value)
        if not value:
            raise ToolFailure(f"{what} is empty")
        sources = ["entry", "chat_rule"] + (["chat_share", "chat_iou"] if what == "person" else [])
        known = [
            k
            for (k,) in await self.rows(
                conn,
                " union ".join(
                    f"select distinct {what} from ledger.{s} where {what} is not null"
                    for s in sources
                ),
            )
        ]
        exact = [k for k in known if k.lower() == value.lower()]
        if exact:
            return exact[0]
        if new_name:
            return value
        hint = str(suggest(what, value, known)) + "; or pass new_name=true to start a new one"
        if what == "person":  # keep the name off the first line, which is all the journal keeps
            raise ToolFailure("no such person\n" + hint)
        raise ToolFailure(hint)

    async def trip(self, conn: psycopg.AsyncConnection, value: str) -> str:
        value = tidy(value)
        if value == "":
            return ""
        trips = [t for (t,) in await self.rows(conn, "select name from ledger.trip")]
        exact = [t for t in trips if t.lower() == value.lower()]
        if not exact:
            raise suggest("trip", value, trips)
        return exact[0]

    @staticmethod
    def tags(values: list[str]) -> list[str]:
        out = []
        for value in values:
            tag = "-".join(tidy(value).lower().split())
            if not TAG.fullmatch(tag):
                raise ToolFailure(
                    f"tag {value!r}: use up to 30 letters, digits or dashes, like 'work-trip'"
                )
            if tag not in out:
                out.append(tag)
        return out

    @staticmethod
    def prefix(value: str, what: str = "merchant", shortest: int = 3) -> str:
        prefix = tidy(value).upper()
        if len(prefix) < shortest:
            raise ToolFailure(f"{what} needs at least {shortest} characters")
        return prefix

    # --- finance_label ------------------------------------------------------------------------

    async def label(
        self,
        refs: list[int] | None,
        merchant: str | None,
        fields: dict[str, Any],
        rename: str | None,
        new_name: bool,
        clear: bool,
        undo: int | None,
        shares: dict[str, float] | None = None,
        parts: dict[str, float] | None = None,
    ) -> tuple[str, dict]:
        given = {k: v for k, v in fields.items() if v is not None}
        amounts = shares is not None or parts is not None
        if undo is not None:
            if refs or merchant or given or rename or clear or amounts:
                raise ToolFailure("undo takes nothing else: finance_label(undo=<change>)")
            return await self.undo(undo)
        if (refs is None) == (merchant is None):
            raise ToolFailure(
                "name entries with refs=[…] (the #numbers finance_transactions shows)"
                " or a merchant with merchant=…, one of the two"
            )
        if clear and (given or rename or amounts):
            raise ToolFailure("clear=true takes no fields: it hands the labels back to the config")
        if not clear and not given and rename is None and not amounts:
            raise ToolFailure(
                "say what to set: flow, category, person, trip, tags, note, whose, shares or parts"
            )
        if refs is not None:
            if rename is not None:
                raise ToolFailure("rename is for a merchant: finance_label(merchant=…, rename=…)")
            return await self.label_entries(refs, given, shares, parts, new_name, clear)
        assert merchant is not None
        extra = sorted(set(given) - set(RULE_FIELDS))
        extra += [k for k, v in (("shares", shares), ("parts", parts)) if v is not None]
        if extra:
            raise ToolFailure(
                f"{', '.join(extra)} can only be set on entries (refs); a merchant rule sets"
                " flow, category, person and whose"
            )
        if given.get("flow") == "reversed":
            raise ToolFailure(
                "reversed is for entries (refs): a hold that won't book, or a refunded charge"
            )
        return await self.label_merchant(merchant, given, rename, new_name, clear)

    async def clean(
        self, conn: psycopg.AsyncConnection, given: dict[str, Any], new_name: bool
    ) -> dict[str, Any]:
        out = dict(given)
        if "category" in out:
            out["category"] = await self.name(conn, "category", out["category"], new_name)
        if "person" in out:
            out["person"] = await self.name(conn, "person", out["person"], new_name)
        if "trip" in out:
            out["trip"] = await self.trip(conn, out["trip"])
        if "tags" in out:
            out["tags"] = self.tags(out["tags"])
        if "note" in out:
            out["note"] = tidy(out["note"])
        return out

    async def label_entries(
        self,
        refs: list[int],
        given: dict[str, Any],
        shares: dict[str, float] | None,
        parts: dict[str, float] | None,
        new_name: bool,
        clear: bool,
    ) -> tuple[str, dict]:
        refs = list(dict.fromkeys(refs))
        async with self.writing() as conn:
            given = await self.clean(conn, given, new_name)
            owe: dict[str, int] | None = None
            if shares is not None:
                owe = {
                    await self.name(conn, "person", who, new_name): p
                    for who, p in to_pence(shares).items()
                }
            move: dict[str, tuple[str, int]] | None = None
            if parts is not None:
                move = {}
                for c, p in to_pence(parts, "part").items():
                    c = await self.name(conn, "category", c, new_name)
                    move[c] = (await self.part_flow(conn, c), p)
            entries = await self.entries(conn, refs)
            missing = [r for r in refs if r not in entries]
            if missing:
                raise ToolFailure(
                    "no entry "
                    + ", ".join(f"#{r}" for r in missing)
                    + ": refs come from finance_transactions"
                )
            keys: dict[int, Key] = {
                r: (entries[r]["account_id"], entries[r]["edit_id"], entries[r]["edit_status"])
                for r in refs
            }
            current = await self.entry_states(conn, list(keys.values()))
            relabelled = []
            for r in refs:
                old = {k: v for k, v in current.get(keys[r], {}).items() if v is not None}
                new = {} if clear else {**old, **given}
                if old != new:
                    relabelled.append(r)
                    await conn.execute(
                        b"insert into ledger.entry_edit (account_id, id, status, flow, category,"
                        b" person, trip, tags, note, whose)"
                        b" values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [*keys[r], *(new.get(f) for f in ENTRY_FIELDS)],
                    )
            resplit = await self.write_shares(conn, refs, keys, owe) if owe is not None else []
            reparted = await self.write_parts(conn, refs, keys, move) if move is not None else []
            done = [r for r in refs if r in relabelled or r in resplit or r in reparted]
            if not done:
                return "Nothing changed: those entries already have these labels.", {"change": None}
            change = await self.last_change(conn)
            after = await self.entries(conn, done)
            pairs = await self.pair_refs(conn, [entries[r] for r in done])
        lines = [f"Change {change}: {plural(len(done), 'entry', 'entries')} relabelled"]
        for r in done[:SHOWN]:
            e = entries[r]
            bits = [changed(e, after[r])] if r in relabelled else []
            bits += [split(owe or {}, after[r])] if r in resplit else []
            bits += [parted(move or {}, after[r])] if r in reparted else []
            lines.append(
                f"#{r}  {e['day']}  {money(e['pence'], exact=True)}  {e['merchant']}: "
                + "; ".join(bits)
            )
        if len(done) > SHOWN:
            lines.append(f"+{len(done) - SHOWN} more")
        for r in done:
            other = pairs.get(r)
            if entries[r]["flow"] == "transfer" and after[r]["flow"] != "transfer" and other:
                lines.append(
                    f"Note: #{r} was one leg of an own-account transfer; the other leg #{other} is still a transfer"
                )
        pending = [f"#{r}" for r in done if entries[r]["status"] == "pending"]
        if pending:
            lines.append(f"Note: {', '.join(pending)} pending; this goes with it when it books")
        lines.append("→ undo: " + call("finance_label", undo=change))
        data = {
            "change": change,
            "entries": [
                {
                    "ref": r,
                    "before": self.public(entries[r]),
                    "after": self.public(after[r]),
                    "shares": {k: v / 100 for k, v in owe.items()}
                    if owe is not None and r in resplit
                    else None,
                    "parts": {c: {"flow": f, "amount": p / 100} for c, (f, p) in move.items()}
                    if move is not None and r in reparted
                    else None,
                }
                for r in done
            ],
        }
        return "\n".join(lines), data

    async def write_shares(
        self,
        conn: psycopg.AsyncConnection,
        refs: list[int],
        keys: dict[int, Key],
        owe: dict[str, int],
    ) -> list[int]:
        """Make each entry's split exactly `owe`; the refs whose split changed. Checked against the
        entries as this call left them, so a flow set in the same call counts."""
        entries = await self.entries(conn, refs)
        now = await self.share_states(conn, list(keys.values()))
        changed_refs = []
        for r in refs:
            e = entries[r]
            if owe and (e["flow"] not in SPENDING or e["pence"] >= 0):
                raise ToolFailure(
                    f"#{r} isn't a payment out (spend or housing), so nobody owes a share of it"
                )
            if owe and sum(owe.values()) >= -e["pence"] - e["parted"]:
                raise ToolFailure(
                    f"#{r}: shares of {money(sum(owe.values()), exact=True)} leave nothing of"
                    f" {money(-e['pence'] - e['parted'], exact=True)} as yours; your share must"
                    " stay above £0"
                )
            was = now.get(keys[r], {})
            for who in sorted(set(was) | set(owe)):
                if was.get(who) != owe.get(who):
                    await conn.execute(
                        b"insert into ledger.entry_share (account_id, id, status, person, pence)"
                        b" values (%s, %s, %s, %s, %s)",
                        [*keys[r], who, owe.get(who)],
                    )
                    if r not in changed_refs:
                        changed_refs.append(r)
        return changed_refs

    async def write_parts(
        self,
        conn: psycopg.AsyncConnection,
        refs: list[int],
        keys: dict[int, Key],
        move: dict[str, tuple[str, int]],
    ) -> list[int]:
        """Make each entry's parts exactly `move`; the refs whose parts changed. Checked against the
        entries as this call left them, so shares set in the same call count."""
        entries = await self.entries(conn, refs)
        now = await self.part_states(conn, list(keys.values()))
        changed_refs = []
        for r in refs:
            e = entries[r]
            if move and e["flow"] == "reversed":
                raise ToolFailure(f"#{r} is reversed and counts nowhere, so it has no parts")
            moved = sum(p for _, p in move.values())
            if move and moved + e["shared"] >= abs(e["pence"]):
                raise ToolFailure(
                    f"#{r}: parts of {money(moved, exact=True)}"
                    + (f" and shares of {money(e['shared'], exact=True)}" if e["shared"] else "")
                    + f" leave nothing of {money(abs(e['pence']), exact=True)} as"
                    f" {e['category']}; some of it must stay"
                )
            was = now.get(keys[r], {})
            for c in sorted(set(was) | set(move)):
                if was.get(c) != move.get(c):
                    flow, pence = move.get(c, (None, None))
                    await conn.execute(
                        b"insert into ledger.entry_part (account_id, id, status, category, flow,"
                        b" pence) values (%s, %s, %s, %s, %s, %s)",
                        [*keys[r], c, flow, pence],
                    )
                    if r not in changed_refs:
                        changed_refs.append(r)
        return changed_refs

    async def part_flow(self, conn: psycopg.AsyncConnection, category: str) -> str:
        """A part takes its category's flow: the one most entries in that category have."""
        found = await self.rows(
            conn,
            "select flow from ledger.entry_line where category = %s and flow is not null"
            " and flow <> 'reversed' group by flow order by count(*) desc, flow limit 1",
            [category],
        )
        if not found:
            raise ToolFailure(
                f"no entry is {category} yet, so a part can't take its flow: label one entry first"
            )
        return found[0][0]

    async def entries(self, conn: psycopg.AsyncConnection, refs: list[int]) -> dict[int, dict]:
        found = await self.rows(
            conn,
            "select num, account_id, id, status, day, pence, coalesce(merchant, description),"
            " flow, category, person, trip, tags, note, pair_account_id, pair_id, edit_id,"
            " edit_status, shared, (select coalesce(sum(p.pence), 0)::bigint from ledger.chat_part p"
            " where (p.account_id, p.id, p.status) = (e.account_id, e.edit_id, e.edit_status)),"
            " coalesce(whose, whose_set) from ledger.entry e where num = any(%s)",
            [refs],
        )
        keys = [
            "num",
            "account_id",
            "id",
            "status",
            "day",
            "pence",
            "merchant",
            "flow",
            "category",
            "person",
            "trip",
            "tags",
            "note",
            "pair_account_id",
            "pair_id",
            "edit_id",
            "edit_status",
            "shared",
            "parted",
            "whose",
        ]
        return {row[0]: dict(zip(keys, row, strict=True)) for row in found}

    @staticmethod
    def public(entry: dict) -> dict:
        return {f: entry[f] for f in ENTRY_FIELDS}

    @staticmethod
    def unnest(keys: list[Key]) -> list[list[str]]:
        return [[k[i] for k in keys] for i in range(3)]

    async def entry_states(self, conn: psycopg.AsyncConnection, keys: list[Key]) -> dict[Key, dict]:
        """The current chat state of each entry, newest row first (cleared rows count)."""
        found = await self.rows(
            conn,
            "select distinct on (account_id, id, status) account_id, id, status, flow, category,"
            " person, trip, tags, note, whose from ledger.entry_edit where (account_id, id, status) in"
            " (select * from unnest(%s::text[], %s::text[], %s::text[]))"
            " order by account_id, id, status, n desc",
            self.unnest(keys),
        )
        return {(a, i, s): dict(zip(ENTRY_FIELDS, rest, strict=True)) for a, i, s, *rest in found}

    async def share_states(
        self, conn: psycopg.AsyncConnection, keys: list[Key]
    ) -> dict[Key, dict[str, int]]:
        found = await self.rows(
            conn,
            "select account_id, id, status, person, pence from ledger.chat_share"
            " where (account_id, id, status) in (select * from unnest(%s::text[], %s::text[], %s::text[]))",
            self.unnest(keys),
        )
        out: dict[Key, dict[str, int]] = defaultdict(dict)
        for a, i, s, who, p in found:
            out[(a, i, s)][who] = p
        return out

    async def part_states(
        self, conn: psycopg.AsyncConnection, keys: list[Key]
    ) -> dict[Key, dict[str, tuple[str, int]]]:
        found = await self.rows(
            conn,
            "select account_id, id, status, category, flow, pence from ledger.chat_part"
            " where (account_id, id, status) in (select * from unnest(%s::text[], %s::text[], %s::text[]))",
            self.unnest(keys),
        )
        out: dict[Key, dict[str, tuple[str, int]]] = defaultdict(dict)
        for a, i, s, c, f, p in found:
            out[(a, i, s)][c] = (f, p)
        return out

    async def pair_refs(self, conn: psycopg.AsyncConnection, entries: list[dict]) -> dict[int, int]:
        paired = [e for e in entries if e["pair_id"]]
        if not paired:
            return {}
        found = await self.rows(
            conn,
            "select account_id, id, num from ledger.entry where status = 'booked'"
            " and (account_id, id) in (select * from unnest(%s::text[], %s::text[]))",
            [[e["pair_account_id"] for e in paired], [e["pair_id"] for e in paired]],
        )
        nums = {(a, i): n for a, i, n in found}
        return {
            e["num"]: nums[(e["pair_account_id"], e["pair_id"])]
            for e in paired
            if (e["pair_account_id"], e["pair_id"]) in nums
        }

    async def last_change(self, conn: psycopg.AsyncConnection) -> int:
        [(change,)] = await self.rows(
            conn, CHAT + " select min(n) from x where xact = pg_current_xact_id()"
        )
        return change

    async def matches(
        self, conn: psycopg.AsyncConnection, prefix: str, column: str
    ) -> list[tuple[str, str, str, str]]:
        """(merchant, flow, category, rule) of every entry whose `column` starts with prefix."""
        return await self.rows(
            conn,
            f"select merchant, flow, category, rule from ledger.entry where ledger.prefix_of(%s, {column})",
            [prefix],
        )

    async def label_merchant(
        self,
        merchant: str,
        given: dict[str, Any],
        rename: str | None,
        new_name: bool,
        clear: bool,
    ) -> tuple[str, dict]:
        prefix = self.prefix(merchant)
        lines: list[str] = []
        data: dict[str, Any] = {"prefix": prefix}
        async with self.writing() as conn:
            given = await self.clean(conn, given, new_name)
            [(alias_now,)] = await self.rows(
                conn,
                "select (select name from ledger.merchant_alias where prefix = %s order by n desc limit 1)",
                [prefix],
            )
            if clear:
                new_alias = None
            elif rename is not None:
                new_alias = self.prefix(rename, "rename", 2)
            else:
                new_alias = alias_now
            rules_before = await self.rules_matching(conn)
            touched: list[str] = []  # what this call wrote: the alias prefix, rule:<prefix>

            async def set_alias() -> None:
                if new_alias == alias_now:
                    return
                before = await self.matches(conn, prefix, "config_merchant")
                await conn.execute(
                    b"insert into ledger.merchant_alias (prefix, name) values (%s, %s)",
                    [prefix, new_alias],
                )
                touched.append(prefix)
                data["rename"] = new_alias
                if not new_alias:
                    lines.append(f"Rename of {prefix} removed")
                    return
                lines.append(
                    f"Renamed merchants starting {prefix} → {new_alias}:"
                    f" {plural(len(before), 'entry', 'entries')}"
                )
                merchants = Counter(m for m, *_ in before)
                if len(merchants) > 1:
                    lines.append("Merged: " + self.top(merchants))

            async def set_rules() -> None:
                # Rules match renamed merchants, so a rule set with a rename, or on a merchant
                # renamed before, goes on the new name; clearing removes both.
                targets = [prefix, alias_now] if clear else [new_alias or prefix]
                for target in dict.fromkeys(t for t in targets if t):
                    now = {
                        k: v
                        for k, v in (await self.rule_state(conn, target)).items()
                        if v is not None
                    }
                    new = {} if clear else {**now, **given}
                    if now == new:
                        continue
                    before = await self.matches(conn, target, "merchant")
                    await conn.execute(
                        b"insert into ledger.merchant_rule (prefix, flow, category, person, whose)"
                        b" values (%s, %s, %s, %s, %s)",
                        [target, *(new.get(f) for f in RULE_FIELDS)],
                    )
                    touched.append(f"rule:{target}")
                    lines.extend(self.rule_lines(target, new, before))
                    data["rule"] = {"prefix": target, **new, "entries": len(before)}

            if clear:  # count each rule's entries before the rename that feeds it goes
                await set_rules()
                await set_alias()
            else:
                await set_alias()
                if given:
                    await set_rules()
            if not touched:
                return "Nothing changed: that merchant already has these labels.", {"change": None}
            removed = {t.removeprefix("rule:") for t in touched if t.startswith("rule:")}
            lost = sorted(rules_before - await self.rules_matching(conn) - removed)
            if lost:
                lines.append(
                    f"Note: rule {', '.join(lost)} now matches nothing; set it again on the new name"
                )
            change = await self.last_change(conn)
        data["change"] = change
        lines.insert(0, f"Change {change}")
        lines.append("→ undo: " + call("finance_label", undo=change))
        return "\n".join(lines), data

    @staticmethod
    def top(merchants: Counter[str]) -> str:
        shown = ", ".join(f"{m} ({n})" for m, n in merchants.most_common(5))
        return shown + (f" +{len(merchants) - 5} more" if len(merchants) > 5 else "")

    async def rule_state(self, conn: psycopg.AsyncConnection, prefix: str) -> dict[str, Any]:
        found = await self.rows(
            conn,
            "select flow, category, person, whose from ledger.merchant_rule where prefix = %s"
            " order by n desc limit 1",
            [prefix],
        )
        return dict(zip(RULE_FIELDS, found[0], strict=True)) if found else {}

    async def rules_matching(self, conn: psycopg.AsyncConnection) -> set[str]:
        return {
            p
            for (p,) in await self.rows(
                conn,
                "select r.prefix from ledger.chat_rule r where exists"
                " (select from ledger.entry e where ledger.prefix_of(r.prefix, e.merchant))",
            )
        }

    @staticmethod
    def rule_lines(prefix: str, rule: dict[str, Any], before: list[tuple]) -> list[str]:
        if not rule:
            return [
                f"Rule for {prefix} removed: {plural(len(before), 'entry', 'entries')} back to the config's labels"
            ]
        lines = [
            f"Rule: merchants starting {prefix} → {describe(rule)}."
            f" {plural(len(before), 'entry', 'entries')} now, and new ones as they come"
        ]
        if not before:
            lines[0] = (
                f"Rule: merchants starting {prefix} → {describe(rule)}. Nothing matches yet; new ones will"
            )
            return lines
        merchants = Counter(m for m, *_ in before)
        if len(merchants) > 1:
            lines.append("Merchants: " + Labels.top(merchants))
        was = Counter(f"{c} ({f})" for _, f, c, _ in before)
        lines.append("Before: " + ", ".join(f"{k} ×{n}" for k, n in was.most_common(4)))
        kept = Counter(
            "set in chat on its own" if r == "chat" else "a toml override"
            for _, _, _, r in before
            if r == "chat" or r.startswith("override")
        )
        for why, n in kept.items():
            lines.append(f"Kept: {plural(n, 'entry', 'entries')} with {why} keep their own labels")
        transfers = sum(1 for _, f, _, _ in before if f == "transfer")
        if transfers and rule.get("flow") not in (None, "transfer"):
            lines.append(
                f"Note: {plural(transfers, 'entry was an', 'entries were')} own-account"
                f" transfer{'s' if transfers > 1 else ''}; now {rule['flow']}"
            )
        return lines

    # --- finance_iou ------------------------------------------------------------------------------

    async def iou(
        self,
        person: str | None,
        amount: float | None,
        note: str | None,
        day: date | None,
        close: int | None,
        new_name: bool,
    ) -> tuple[str, dict]:
        """Start an IOU (amount > 0: the person owes the user, < 0: the user owes them) or close one by hand."""
        async with self.writing() as conn:
            if close is not None:
                if person or amount or note or day:
                    raise ToolFailure("close takes nothing else: finance_iou(close=<IOU>)")
                found = await self.rows(
                    conn,
                    "select person, pence, day, note from ledger.chat_iou where iou = %s",
                    [close],
                )
                if not found:
                    raise ToolFailure(f"no open IOU {close}: finance_debts lists them")
                who, pence, day, note = found[0]
                await conn.execute(
                    b"insert into ledger.iou (iou, person, pence, day, note) values (%s, %s, null, %s, %s)",
                    [close, who, day, note],
                )
                number, text = close, f"IOU {close} closed: {iou_text(who, pence)} no more"
            else:
                if not (person and amount and note and tidy(note)):
                    raise ToolFailure(
                        "an IOU needs person, amount (+ they owe the user, - the user owes them) and note"
                    )
                who = await self.name(conn, "person", person, new_name)
                pence = round(amount * 100)
                if pence == 0:
                    raise ToolFailure("amount rounds to £0")
                [(number,)] = await self.rows(conn, "select nextval('ledger.iou_id')")
                await conn.execute(
                    b"insert into ledger.iou (iou, person, pence, day, note) values (%s, %s, %s, %s, %s)",
                    [number, who, pence, day or self.ledger.today(), tidy(note)],
                )
                text = f"IOU {number}: {iou_text(who, pence)} ({tidy(note)})"
            change = await self.last_change(conn)
        return (
            f"Change {change}: {text}\n→ balances: {call('finance_debts', person=who)}"
            f"\n→ undo: {call('finance_label', undo=change)}",
            {"change": change, "iou": number},
        )

    # --- undo and the change list -------------------------------------------------------------

    async def undo(self, change: int) -> tuple[str, dict]:
        """Write back, for every target of the change, the state before it.

        Refused when a target has moved on since (a later change set something else), so an undo
        never silently discards newer labels; undoing changes newest first always works.
        """
        async with self.writing() as conn:
            rows = await self.rows(
                conn,
                CHAT + f" select kind, n, account_id, id, status, prefix, who, {VALUES}"
                " from x where xact = (select xact from x where n = %s) order by n",
                [change],
            )
            if not rows or rows[0][1] != change:
                raise ToolFailure(f"no change {change}: finance_changes lists them")
            target = " and ".join(
                [
                    "x.kind = %s",
                    *(
                        f"x.{c} is not distinct from %s"
                        for c in ("account_id", "id", "status", "prefix", "who")
                    ),
                ]
            )
            restored: Counter[str] = Counter()
            for kind, n, account_id, id, status, prefix, who, *state in rows:
                key = [kind, account_id, id, status, prefix, who]
                [(latest, *now)] = await self.rows(
                    conn,
                    CHAT + ", c as (select xact, min(n) as change from x group by xact)"
                    f" select c.change, {VALUES} from x join c using (xact) where {target}"
                    " order by x.n desc limit 1",
                    key,
                )
                if now != state:
                    raise ToolFailure(
                        f"change {latest} changed the same labels since; undo that first"
                    )
                table, keys, fields = {
                    "entry": ("entry_edit", ("account_id", "id", "status"), ENTRY_FIELDS),
                    "share": ("entry_share", ("account_id", "id", "status", "person"), ("pence",)),
                    "part": (
                        "entry_part",
                        ("account_id", "id", "status", "category"),
                        ("flow", "pence"),
                    ),
                    "rule": ("merchant_rule", ("prefix",), RULE_FIELDS),
                    "rename": ("merchant_alias", ("prefix",), ("name",)),
                    "iou": ("iou", ("iou",), IOU_FIELDS),
                }[kind]
                previous = await self.rows(
                    conn,
                    CHAT + f" select {', '.join(fields)} from x where {target} and x.n < %s"
                    " order by x.n desc limit 1",
                    [*key, n],
                )
                if kind == "iou" and not previous:  # undoing its start closes it
                    previous = [(state[2], None, state[9], state[5])]
                await conn.execute(
                    f"insert into ledger.{table} ({', '.join((*keys, *fields))})"
                    f" values ({', '.join(['%s'] * (len(keys) + len(fields)))})".encode(),
                    [
                        *{
                            "entry": (account_id, id, status),
                            "share": (account_id, id, status, who),
                            "part": (account_id, id, status, who),
                            "iou": (who,),  # its number as text; PostgreSQL casts it
                        }.get(kind, (prefix,)),
                        *(previous[0] if previous else [None] * len(fields)),
                    ],
                )
                restored[kind] += 1
            new = await self.last_change(conn)
        names = {
            "entry": ("entry", "entries"),
            "share": ("share", "shares"),
            "part": ("part", "parts"),
            "rule": ("rule", "rules"),
            "rename": ("rename", "renames"),
            "iou": ("IOU", "IOUs"),
        }
        what = ", ".join(plural(n, *names[kind]) for kind, n in restored.items())
        return (
            f"Change {new}: undid change {change} ({what} back as they were)\n"
            f"→ undo the undo: {call('finance_label', undo=new)}",
            {"change": new, "undid": change},
        )

    async def changes(self, limit: int) -> tuple[str, dict]:
        async with self.ledger.connect() as conn:
            rows = await self.rows(
                conn,
                CHAT
                + """, c as (select xact, min(n) as change from x group by xact
                    order by change desc limit %s)
                select c.change, x.kind, x.at, e.num, x.prefix, x.flow, x.category, x.person, x.trip,
                       x.tags, x.note, x.name, x.who, x.pence, x.whose
                from x join c using (xact)
                left join ledger.entry e
                    on (e.account_id, e.edit_id, e.edit_status) = (x.account_id, x.id, x.status)
                order by c.change desc, x.n""",
                [limit],
            )
        groups: dict[int, list[tuple]] = {}
        for row in rows:
            groups.setdefault(row[0], []).append(row)
        items, lines = [], []
        for change, group in groups.items():
            at = when(group[0][2])
            parts = []
            entries = [r for r in group if r[1] == "entry"]
            if entries:
                states = {
                    describe(dict(zip(ENTRY_FIELDS, (*r[5:11], r[14]), strict=True)))
                    for r in entries
                }
                refs = ", ".join(f"#{r[3]}" for r in entries[:5]) + (
                    f" +{len(entries) - 5}" if len(entries) > 5 else ""
                )
                parts.append(f"{refs}: " + (states.pop() if len(states) == 1 else "labels differ"))
            for r in group:
                if r[1] == "rule":
                    parts.append(
                        f"rule {r[4]}: "
                        + describe(dict(zip(RULE_FIELDS, (*r[5:8], r[14]), strict=True))).replace(
                            "back to automatic", "removed"
                        )
                    )
                elif r[1] == "rename":
                    parts.append(f"rename {r[4]} → {r[11]}" if r[11] else f"rename {r[4]} removed")
                elif r[1] == "share":
                    owes = f"owes {money(r[13], exact=True)}" if r[13] else "share removed"
                    parts.append(f"#{r[3]}: {r[12]} {owes}")
                elif r[1] == "part":
                    size = f"{money(r[13], exact=True)} ({r[5]})" if r[13] else "removed"
                    parts.append(f"#{r[3]}: part {r[12]} {size}")
                elif r[1] == "iou":
                    parts.append(f"IOU {r[12]}: " + (iou_text(r[7], r[13]) if r[13] else "closed"))
            lines.append(f"- change {change} · {at} · " + "; ".join(parts))
            items.append({"change": change, "at": group[0][2].isoformat(), "what": parts})
        if not lines:
            return "No labels set from chat yet.", {"changes": []}
        lines.insert(0, "Labels set from chat, newest first")
        lines.append("→ undo one: finance_label(undo=<change>)")
        return "\n".join(lines), {"changes": items}
