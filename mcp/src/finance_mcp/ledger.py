"""The ledger (../finance) as the finance tools read it: SQL, then compact text or JSON.

Every number is computed in SQL over `ledger.entry_line` (about 8k rows), so nothing is cached
and nothing goes stale: one line per entry, or per part of an entry split across categories. Amounts are integer pence in GBP: negative is money out, on cards too.
"Spending" is the spend and housing flows; transfers between own accounts, loans, money with
friends (`people`) and investments are flows of their own and never count as spending. Spending
counts the user's share of a split payment (`pence + shared`); every other total is what the bank moved.
"""

from __future__ import annotations

import asyncio
import difflib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import psycopg

from mcpkit.errors import BackendError, ToolFailure
from mcpkit.periods import Period, parse

LONDON = ZoneInfo("Europe/London")
SPENDING = ["spend", "housing"]
OUT_POSITIVE = {"spending", "spend", "housing"}  # shown as money spent, not as a signed net
NOT_IN_OVERVIEW = {"spend", "housing", "transfer", "reversed"}
BASELINE = 6  # spans averaged for "usual"
STALE = timedelta(hours=13)  # as finance health: syncs run at 08, 16 and 23
KEYS = {
    "category": "coalesce(category, 'Uncategorised')",
    "merchant": "coalesce(merchant, description)",
    "account": "account_id",
    "trip": "trip",
    "month": "to_char(day, 'YYYY-MM')",
    "week": "to_char(date_trunc('week', day), 'YYYY-MM-DD')",  # the Monday it starts
    "day": "to_char(day, 'YYYY-MM-DD')",
    "flow": "coalesce(flow, 'unlabelled')",
    "person": "person",
    # A category in no group of the config's [groups] stands alone.
    "group": "coalesce((select g.name from ledger.category_group g where g.category = entry_line.category),"
    " category, 'Uncategorised')",
    "tag": "tag",  # one row per tag (from unnest below), so an entry counts under each of its tags
    "whose": "whose",  # spending only: personal or joint
}
WHOSE = ("personal", "joint")
TIME = ("month", "week", "day")  # chronological rows, no usual or shares


class Account(NamedTuple):
    id: str
    label: str
    kind: str
    name: str
    synced: datetime


def money(pence: int | float, exact: bool = False) -> str:
    sign = "-" if pence < 0 else ""
    return f"{sign}£{abs(pence) / 100:,.2f}" if exact else f"{sign}£{round(abs(pence) / 100):,}"


def plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def pounds(pence: int | float) -> float:
    return round(pence / 100, 2)


def change(now: int, usual: float | None) -> int | None:
    """Percent change against usual; None when there is nothing usual to compare with."""
    return round((now - usual) / abs(usual) * 100) if usual else None


def pct(value: int | None, now: int) -> str:
    if value is None:
        return "new" if now else ""
    return f"{value:+d}%"


def when(at: datetime) -> str:
    local = at.astimezone(LONDON)
    return f"{local.day} {local:%b %H:%M}"


def call(tool: str, **kwargs: Any) -> str:
    """The exact next call, so the model never guesses arguments."""
    args = ", ".join(
        f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in kwargs.items() if v is not None
    )
    return f"{tool}({args})"


def like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def suggest(what: str, value: str, known: Sequence[str]) -> ToolFailure:
    close = difflib.get_close_matches(value, known, n=3, cutoff=0.4)
    close += [k for k in known if value.lower() in k.lower() and k not in close][: 3 - len(close)]
    hint = f"did you mean {' or '.join(close)}?" if close else f"known: {', '.join(sorted(known))}"
    return ToolFailure(f"no {what} {value!r}: {hint}")


def baseline_note(period: Period, n: int) -> str:
    if n == 0:
        return "no earlier history to compare with"
    one, many = {1: ("month", "months"), 3: ("quarter", "quarters"), 12: ("year", "years")}.get(
        period.months or 0, ("period of the same length", "periods of the same length")
    )
    if n == 1:
        return f"usual = the same span in the previous {one}"
    return f"usual = average of the same span in the {n} previous {many}"


class Ledger:
    def __init__(self, dsn: str, now: Callable[[], datetime]) -> None:
        self.dsn = dsn
        self.now = now
        # The finmcp role allows 4 connections; queue calls past 3 instead of failing them.
        self.slots = asyncio.Semaphore(3)

    def today(self) -> date:
        return self.now().astimezone(LONDON).date()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[psycopg.AsyncConnection]:
        try:
            async with (
                self.slots,
                await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as conn,
            ):
                yield conn
        except psycopg.Error as exc:
            first = str(exc).strip().partition("\n")[0]
            raise BackendError(f"the ledger database failed: {first}") from None

    @staticmethod
    async def rows(
        conn: psycopg.AsyncConnection, sql: str, params: Sequence[Any] = ()
    ) -> list[tuple]:
        cur = await conn.execute(sql.encode(), params)
        return await cur.fetchall()

    async def accounts(self, conn: psycopg.AsyncConnection) -> list[Account]:
        found = await self.rows(
            conn,
            "select a.id, a.provider, a.name, a.kind, r.fetched_at from ledger.account a"
            " join ledger.run r on r.id = a.last_run order by a.provider, a.name",
        )
        providers = [provider for _, provider, *_ in found]
        return [
            Account(
                id,
                provider.removeprefix("ob-").title()
                + (f" {name}" if providers.count(provider) > 1 else ""),
                "card" if kind == "cards" else "account",
                name,
                synced,
            )
            for id, provider, name, kind, synced in found
        ]

    def synced(self, accounts: list[Account]) -> tuple[str, dict]:
        if not accounts:
            return "Never synced", {"newest": None, "stale": []}
        newest = max(a.synced for a in accounts)
        stale = [a for a in accounts if self.now() - a.synced > STALE]
        line = f"Synced {when(newest)}"
        if stale:
            line += " · stale: " + ", ".join(f"{a.label} last {when(a.synced)}" for a in stale)
        return line, {"newest": newest.isoformat(), "stale": [a.label for a in stale]}

    async def spans(self, conn: psycopg.AsyncConnection, period: Period) -> list[Period]:
        """The earlier spans for "usual", only those wholly inside the history."""
        [(first,)] = await self.rows(conn, "select min(day) from ledger.entry")
        earlier = (period.back(k) for k in range(1, BASELINE + 1))
        return [s for s in earlier if first is not None and s.start >= first]

    async def flow_filter(self, conn: psycopg.AsyncConnection, flow: str) -> tuple[str, list]:
        if flow == "spending":
            return "flow = any(%s)", [SPENDING]
        if flow == "all":
            return "true", []
        flows = [
            f
            for (f,) in await self.rows(
                conn, "select distinct flow from ledger.entry_line where flow is not null"
            )
        ]
        if flow not in flows:
            raise suggest("flow", flow, ["spending", "all", *flows])
        return "flow = %s", [flow]

    async def known_category(self, conn: psycopg.AsyncConnection, category: str) -> str:
        known = [
            c
            for (c,) in await self.rows(
                conn, "select distinct category from ledger.entry_line where category is not null"
            )
        ]
        exact = [c for c in known if c.lower() == category.lower()]
        if not exact:
            raise suggest("category", category, known)
        return exact[0]

    async def known_group(self, conn: psycopg.AsyncConnection, group: str) -> tuple[str, list[str]]:
        """A group's stored name and its categories."""
        found = await self.rows(conn, "select name, category from ledger.category_group")
        exact = {n for n, _ in found if n.lower() == group.lower()}
        if not exact:
            raise suggest("group", group, sorted({n for n, _ in found}))
        [name] = exact
        return name, [c for n, c in found if n == name]

    async def known_tag(self, conn: psycopg.AsyncConnection, tag: str) -> str:
        known = [
            t for (t,) in await self.rows(conn, "select distinct unnest(tags) from ledger.entry")
        ]
        exact = [t for t in known if t.lower() == tag.strip().lower()]
        if not exact:
            raise suggest("tag", tag, known)
        return exact[0]

    async def known_person(self, conn: psycopg.AsyncConnection, person: str) -> str:
        """The stored spelling of a person, matched case-insensitively."""
        known = [
            p
            for (p,) in await self.rows(
                conn,
                "select distinct person from ledger.entry where person is not null"
                " union select distinct person from ledger.chat_share"
                " union select distinct person from ledger.chat_iou",
            )
        ]
        exact = [p for p in known if p.lower() == person.strip().lower()]
        if not exact:
            # The name stays off the first line, which is all the journal keeps.
            raise ToolFailure("no such person\n" + str(suggest("person", person, known)))
        return exact[0]

    async def grouped(
        self,
        conn: psycopg.AsyncConnection,
        by: str,
        flow: str,
        period: Period,
        spans: list[Period],
        category: str | None = None,
        categories: list[str] | None = None,
        whose: str | None = None,
    ) -> list[tuple[str, int, int, int]]:
        """(key, pence now, pence over all earlier spans, entries now), biggest first."""
        where, params = await self.flow_filter(conn, flow)
        if whose:
            where += " and whose = %s"
            params = [*params, whose]
        if category:
            where += " and category = %s"
            params = [*params, category]
        if categories is not None:  # a group's
            where += " and category = any(%s)"
            params = [*params, categories]
        windows = [period, *spans]
        within = " or ".join("(day >= %s and day < %s)" for _ in windows)
        amount = "pence + shared" if flow in OUT_POSITIVE else "pence"
        rows = await self.rows(
            conn,
            f"""select {KEYS[by]} as key,
                coalesce(sum({amount}) filter (where day >= %s and day < %s), 0)::bigint,
                coalesce(sum({amount}) filter (where not (day >= %s and day < %s)), 0)::bigint,
                count(*) filter (where day >= %s and day < %s)
            from ledger.entry_line{", unnest(tags) tag" if by == "tag" else ""}
            where {where} and ({within}) and {KEYS[by]} is not null
            group by 1""",
            [
                *[period.start, period.end] * 3,
                *params,
                *[d for w in windows for d in (w.start, w.end)],
            ],
        )
        return sorted(rows, key=lambda r: (-abs(r[1]), r[0]))

    # --- tools ----------------------------------------------------------------------------------

    async def overview(self, period: str) -> tuple[str, dict]:
        p = parse(period, self.today())
        so_far = p.end > self.today()
        async with self.connect() as conn:
            accounts = await self.accounts(conn)
            spans = await self.spans(conn, p)
            cats = await self.grouped(conn, "category", "spending", p, spans)
            whose = {
                k: now for k, now, _, _ in await self.grouped(conn, "whose", "spending", p, [])
            }
            flows = await self.rows(
                conn,
                "select flow, sum(pence)::bigint from ledger.entry_line where day >= %s and day < %s"
                " and flow is not null group by 1 order by 2 desc",
                [p.start, p.end],
            )
            # One-offs: the largest payments from merchants paid at most once in the 120 days
            # before, so rent, bills and the weekly shop never qualify.
            one_offs = await self.rows(
                conn,
                """with recent as (select merchant, count(*) as n from ledger.entry
                        where day >= %s and day < %s group by 1),
                    seen as (select distinct merchant from ledger.entry where day < %s)
                select e.day, e.pence + e.shared, coalesce(e.merchant, e.description), e.category,
                       s.merchant is null
                from ledger.entry_line e
                left join recent r on r.merchant = e.merchant
                left join seen s on s.merchant = e.merchant
                where e.flow = any(%s) and e.pence < 0 and e.day >= %s and e.day < %s and coalesce(r.n, 0) < 2
                order by e.pence + e.shared, e.day limit 3""",
                [p.start - timedelta(days=120), p.start, p.start, SPENDING, p.start, p.end],
            )
            [(first,)] = await self.rows(conn, "select min(day) from ledger.entry")
            [(uncategorised, uncategorised_out)] = await self.rows(
                conn,
                "select count(*), coalesce(sum(pence) filter (where pence < 0), 0)::bigint"
                " from ledger.entry_line where category = 'Uncategorised' and day >= %s and day < %s",
                [p.start, p.end],
            )
            trips = await self.rows(
                conn,
                """select t.name, t.starts, t.ends, coalesce(sum(e.pence + case when e.flow = any(%s)
                    then e.shared else 0 end), 0)::bigint
                from ledger.trip t left join ledger.entry_line e on e.trip = t.name
                where t.starts < %s and t.ends >= %s group by 1, 2, 3 order by 2""",
                [SPENDING, p.end, p.start],
            )
        spent = -sum(now for _, now, _, _ in cats)
        usual = -sum(before for _, _, before, _ in cats) / len(spans) if spans else None
        sync_line, sync = self.synced(accounts)
        known_before = (
            first is not None and (p.start - first).days >= 90
        )  # otherwise everything is new
        floor = max(spent, usual or 0) * 0.05  # too small to call out
        one_offs = [
            (d, pence, m, c, new and known_before)
            for d, pence, m, c, new in one_offs
            if -pence >= floor
        ]
        data = {
            "period": {
                "label": p.label,
                "from": str(p.start),
                "to": str(p.end - timedelta(days=1)),
                "so_far": so_far,
            },
            "spent": pounds(spent),
            "usual": pounds(usual) if usual is not None else None,
            "change_pct": change(spent, usual),
            "whose": {w: pounds(-whose[w]) for w in WHOSE if w in whose},
            "baseline": baseline_note(p, len(spans)),
            "flows": {f: pounds(total) for f, total in flows if f not in NOT_IN_OVERVIEW},
            "categories": [
                {
                    "category": k,
                    "spent": pounds(-now),
                    "usual": pounds(-before / len(spans)) if spans else None,
                    "change_pct": change(-now, -before / len(spans) if spans else None),
                    "entries": n,
                }
                for k, now, before, n in cats
                if now
            ],
            "one_offs": [
                {
                    "day": str(d),
                    "amount": pounds(-pence),
                    "merchant": m,
                    "category": c,
                    "new_merchant": new,
                }
                for d, pence, m, c, new in one_offs
            ],
            "trips": [
                {"trip": name, "from": str(a), "to": str(b), "spent": pounds(-pence)}
                for name, a, b, pence in trips
            ],
            "uncategorised": {"entries": uncategorised, "out": pounds(-uncategorised_out)},
            "synced": sync,
        }

        top, rest = data["categories"][:6], data["categories"][6:]
        lines = [f"Finance · {p.label}{', so far' if so_far else ''}"]
        head = f"Spent {money(spent)}"
        if usual is not None:
            head += f" · usual {money(usual)}{' by this point' if so_far else ''} ({pct(change(spent, usual), spent)})"
        lines.append(head)
        if len(whose) > 1:
            lines.append(
                "Of it: " + " · ".join(f"{w} {money(-whose[w])}" for w in WHOSE if w in whose)
            )
        others = [f"{f} {money(total)}" for f, total in flows if f not in NOT_IN_OVERVIEW]
        if others:
            lines.append("Other flows: " + ", ".join(others) + " (own-account transfers left out)")
        if top:
            lines.append("Top categories: spent · usual · change" if spans else "Top categories")
            for c in top:
                line = f"- {c['category']} {money(c['spent'] * 100)}"
                if c["usual"] is not None:
                    line += f" · {money(c['usual'] * 100)} · {pct(c['change_pct'], 1)}"
                lines.append(line)
            if rest:
                lines.append(
                    f"+{len(rest)} more categories {money(sum(c['spent'] for c in rest) * 100)}"
                )
        unusual = [
            f"- {m} {money(-pence)} ({d:%-d %b}, {c}{', new merchant' if new else ''})"
            for d, pence, m, c, new in one_offs
        ]
        unusual += [
            f"- trip: {name} ({Period(a, b + timedelta(days=1)).label}) {money(-pence)}"
            for name, a, b, pence in trips
        ]
        if unusual:
            lines += ["Unusual", *unusual]
        if uncategorised:
            lines.append(
                f"Uncategorised: {plural(uncategorised, 'entry', 'entries')}"
                + (f", {money(-uncategorised_out)} out" if uncategorised_out else "")
                + " → "
                + call("finance_transactions", period=period, category="Uncategorised")
            )
        lines.append(data["baseline"])
        lines.append(sync_line)
        drill = [call("finance_breakdown", period=period, by="category")]
        if top:  # the category that moved most against usual, else the biggest
            moved = max(top, key=lambda c: abs(c["spent"] - (c["usual"] or 0)))
            drill.append(call("finance_transactions", period=period, category=moved["category"]))
        lines.append("→ " + " · ".join(drill))
        return "\n".join(lines), data

    async def breakdown(
        self,
        period: str,
        by: str,
        flow: str,
        top: int,
        category: str | None = None,
        group: str | None = None,
        whose: str | None = None,
    ) -> tuple[str, dict]:
        p = parse(period, self.today())
        async with self.connect() as conn:
            accounts = await self.accounts(conn)
            if category:
                category = await self.known_category(conn, category)
            categories = None
            if group:
                group, categories = await self.known_group(conn, group)
            spans = [] if by in TIME else await self.spans(conn, p)
            rows = await self.grouped(conn, by, flow, p, spans, category, categories, whose)
        if by in TIME:
            rows.sort()
        sign = -1 if flow in OUT_POSITIVE else 1
        shares = flow != "all" and by not in TIME
        names = {a.id: a.label for a in accounts}
        total = sign * sum(now for _, now, _, _ in rows)
        items = [
            {
                by: names.get(key, key) if by == "account" else key,
                "amount": pounds(sign * now),
                "share_pct": round(sign * now / total * 100)
                if shares and total > 0 and sign * now > 0
                else None,
                "usual": pounds(sign * before / len(spans)) if spans else None,
                "change_pct": change(sign * now, sign * before / len(spans) if spans else None),
                "entries": n,
            }
            for key, now, before, n in rows
            if now or n
        ]
        sync_line, sync = self.synced(accounts)
        usual = sign * sum(before for _, _, before, _ in rows) / len(spans) if spans else None
        data = {
            "period": {
                "label": p.label,
                "from": str(p.start),
                "to": str(p.end - timedelta(days=1)),
            },
            "flow": flow,
            "category": category,
            "group": group,
            "whose": whose,
            "by": by,
            "total": pounds(total),
            "usual": pounds(usual) if usual is not None else None,
            "rows": items,
            "baseline": baseline_note(p, len(spans)) if by not in TIME else None,
            "synced": sync,
        }

        title = (
            "All flows, net"
            if flow == "all"
            else flow.capitalize() + ("" if flow in OUT_POSITIVE else ", net")
        )
        within = " ".join(x for x in (whose, category or group) if x)
        head = f"{title}{f' in {within}' if within else ''} by {by} · {p.label}: {money(total)}"
        if usual is not None:
            head += f" (usual {money(usual)}, {pct(change(total, usual), total)})"
        lines = [head]
        for item in items[:top]:
            bits = [f"- {item[by]} {money(item['amount'] * 100)}"]
            if item["share_pct"] is not None:
                bits.append(f"{item['share_pct']}%")
            if spans:
                bits.append(pct(item["change_pct"], item["entries"]))
            lines.append(" · ".join(b for b in bits if b) + f" ({item['entries']})")
        if len(items) > top:
            rest = items[top:]
            lines.append(f"+{len(rest)} more {money(sum(i['amount'] for i in rest) * 100)}")
        if by in TIME and items:
            lines.append(f"Average per {by}: {money(total / len(items))}")
        if not items:
            lines.append("Nothing in this period.")
        if data["baseline"]:
            lines.append(data["baseline"])
        lines.append(sync_line)
        if items:
            first = items[0][by]
            if by == "week":
                drill = {"period": f"{first}..{date.fromisoformat(first) + timedelta(days=6)}"}
            else:
                drill = {
                    "month": {"period": first},
                    "day": {"period": first},
                    "flow": {"period": period, "flow": first},
                }.get(by, {"period": period, by: first})
            if by != "flow" and flow != "all":
                drill["flow"] = flow
            if category and by != "category":
                drill["category"] = category
            if whose and by != "whose":
                drill["whose"] = whose
            if by == "group":  # one level down: the group's categories
                drill = {
                    "period": period,
                    "by": "category",
                    "flow": drill.get("flow"),
                    "group": first,
                    "whose": whose,
                }
                lines.append("→ " + call("finance_breakdown", **drill))
            else:
                lines.append("→ " + call("finance_transactions", **drill))
        return "\n".join(lines), data

    async def transactions(
        self,
        period: str | None,
        text: str | None,
        category: str | None,
        merchant: str | None,
        account: str | None,
        flow: str,
        trip: str | None,
        person: str | None,
        tag: str | None,
        whose: str | None,
        added: str | None,
        min_amount: float | None,
        max_amount: float | None,
        limit: int,
        offset: int,
    ) -> tuple[str, dict]:
        p = parse(period, self.today()) if period else None
        async with self.connect() as conn:
            accounts = await self.accounts(conn)
            where, params = await self.flow_filter(conn, flow)
            conds, args = [where], list(params)
            if p:
                conds.append("day >= %s and day < %s")
                args += [p.start, p.end]
            if added:  # London days of the sync that first brought the entry in
                a = parse(added, self.today())
                conds.append("added >= %s and added < %s")
                args += [datetime.combine(d, time(), LONDON) for d in (a.start, a.end)]
            if category:
                conds.append("category = %s")
                args.append(await self.known_category(conn, category))
            if account:
                ids = [a.id for a in accounts if account.lower() in f"{a.label} {a.name}".lower()]
                if not ids:
                    raise suggest("account", account, [a.label for a in accounts])
                conds.append("account_id = any(%s)")
                args.append(ids)
            if person:
                conds.append("person = %s")
                args.append(await self.known_person(conn, person))
            if tag:
                conds.append("%s = any(tags)")
                args.append(await self.known_tag(conn, tag))
            if whose:
                conds.append("whose = %s")
                args.append(whose)
            for value, sql in (
                (merchant, "merchant ilike %s"),
                (trip, "trip ilike %s"),
                (text, "(description ilike %s or merchant ilike %s or note ilike %s)"),
            ):
                if value:
                    conds.append(sql)
                    args += [like(value)] * sql.count("%s")
            if min_amount is not None:
                conds.append("abs(pence) >= %s")
                args.append(round(min_amount * 100))
            if max_amount is not None:
                conds.append("abs(pence) <= %s")
                args.append(round(max_amount * 100))
            where = " and ".join(conds)
            [(count, out, into)] = await self.rows(
                conn,
                "select count(*), coalesce(sum(pence) filter (where pence < 0), 0)::bigint,"
                f" coalesce(sum(pence) filter (where pence > 0), 0)::bigint from ledger.entry_line where {where}",
                args,
            )
            found = await self.rows(
                conn,
                "select num, day, pence, coalesce(merchant, description), category, account_id, flow, trip,"
                f" status, tags, note, person, shared, was_pending_as, whole, whose from ledger.entry_line"
                f" where {where} order by day desc, at desc, id, category limit %s offset %s",
                [*args, limit, offset],
            )
        names = {a.id: a.label for a in accounts}
        sync_line, sync = self.synced(accounts)
        entries = [
            {
                "ref": num,
                "day": str(day),
                "amount": pounds(pence),
                "merchant": m,
                "category": c,
                "account": names.get(acc, acc),
                "flow": f,
                "trip": t,
                "pending": status == "pending",
                "tags": tags or [],
                "note": note,
                "person": who,
                "your_share": pounds(pence + shared) if shared else None,
                "was_pending_as": was,
                "part_of": pounds(whole) if pence != whole else None,
                "whose": w,
            }
            for num, day, pence, m, c, acc, f, t, status, tags, note, who, shared, was, whole, w in found
        ]
        filters = {
            "period": period,
            "text": text,
            "category": category,
            "merchant": merchant,
            "account": account,
            "flow": None if flow == "all" else flow,
            "trip": trip,
            "person": person,
            "tag": tag,
            "whose": whose,
            "added": added,
            "min_amount": min_amount,
            "max_amount": max_amount,
        }
        data = {
            "matched": count,
            "out": pounds(out),
            "in": pounds(into),
            "offset": offset,
            "entries": entries,
            "synced": sync,
        }

        scope = p.label if p else "all time"
        totals = [
            f"out {money(-out, exact=True)}" if out else "",
            f"in {money(into, exact=True)}" if into else "",
        ]
        lines = [" · ".join(x for x in [plural(count, "entry", "entries"), scope, *totals] if x)]
        if count > len(entries):
            lines[0] += f" · showing {offset + 1}–{offset + len(entries)}" if entries else ""
        for e in entries:
            line = f"#{e['ref']}  {e['day']}  {money(e['amount'] * 100, exact=True)}  {e['merchant']}  {e['category']}"
            if e["flow"] not in SPENDING:
                line += f" ({e['flow']})"
            line += f"  {e['account']}"
            extras = [f"person {e['person']}"] if e["person"] else []
            extras += [f"trip {e['trip']}"] if e["trip"] else []
            extras += ["pending"] if e["pending"] else []
            if e["was_pending_as"]:  # linked on the amount alone: worth a look
                extras.append(f"was pending as {e['was_pending_as']}")
            if e["your_share"] is not None:
                extras.append(f"your share {money(e['your_share'] * 100, exact=True)}")
            if e["part_of"] is not None:
                extras.append(f"part of {money(e['part_of'] * 100, exact=True)}")
            extras += [e["whose"]] if e["whose"] == "joint" else []  # personal goes unsaid
            extras += e["tags"]
            extras += [e["note"]] if e["note"] else []
            lines.append(line + "".join(f" · {x}" for x in extras))
        if not count:
            lines.append("Nothing matches: widen the period or drop a filter.")
        lines.append(sync_line)
        if offset + len(entries) < count:
            lines.append(
                "→ " + call("finance_transactions", **filters, limit=limit, offset=offset + limit)
            )
        return "\n".join(lines), data

    async def notes(self) -> str:
        """The user's own notes from the ledger config, the one column of it this role may read."""
        async with self.connect() as conn:
            found = await self.rows(conn, "select notes from ledger.config")
        return (found[0][0] if found else "") or "No notes yet."

    async def debts(self, person: str | None) -> tuple[str, dict]:
        """Who owes whom: shares of the user's payments and open IOUs, less `loan` entries with that person."""
        async with self.connect() as conn:
            accounts = await self.accounts(conn)
            if person:
                person = await self.known_person(conn, person)
            moves = await self.rows(
                conn,
                """select s.person, e.num, e.day, coalesce(e.merchant, e.description), s.pence, 'share'
                from ledger.chat_share s join ledger.entry e
                    on (e.account_id, e.edit_id, e.edit_status) = (s.account_id, s.id, s.status)
                where e.flow = any(%s)
                union all
                select person, num, day, coalesce(merchant, description), -pence, 'loan'
                from ledger.entry_line where flow = 'loan' and person is not null
                union all
                select person, iou, day, note, pence, 'iou' from ledger.chat_iou
                order by 3, 2""",
                [SPENDING],
            )
            [(unassigned,)] = await self.rows(
                conn,
                "select count(*) from ledger.entry_line where flow = 'loan' and person is null",
            )
        sync_line, sync = self.synced(accounts)

        def owes(who: str, pence: int) -> str:
            if pence > 0:
                return f"{who} owes you {money(pence, exact=True)}"
            return f"You owe {who} {money(-pence, exact=True)}" if pence else f"Settled with {who}"

        if person:
            mine = [m for m in moves if m[0] == person]
            balance, items = 0, []
            for _, num, day, merchant, owed, kind in mine:
                balance += owed
                items.append(
                    {
                        "ref": None if kind == "iou" else num,
                        "iou": num if kind == "iou" else None,
                        "day": str(day),
                        "owed": pounds(owed),
                        "merchant": merchant,
                        "share": kind == "share",
                        "balance": pounds(balance),
                    }
                )
            lines = [owes(person, balance)]
            if len(items) > 20:
                lines.append(f"+{len(items) - 20} earlier")
            lines += [
                f"{f'IOU {i["iou"]}' if i['iou'] else f'#{i["ref"]}'}  {i['day']}"
                f"  {'+' if i['owed'] > 0 else '-'}"
                f"{money(abs(i['owed']) * 100, exact=True)}  {i['merchant']}"
                f"{' (share)' if i['share'] else ''}  → {money(i['balance'] * 100, exact=True)}"
                for i in items[-20:]
            ]
            if any(i["iou"] for i in items):
                lines.append(
                    "A loan entry with them settles an IOU; close one by hand: finance_iou(close=N)"
                )
            lines.append(sync_line)
            return "\n".join(lines), {
                "person": person,
                "balance": pounds(balance),
                "moves": items,
                "synced": sync,
            }

        totals: dict[str, int] = {}
        for who, _, _, _, owed, _ in moves:
            totals[who] = totals.get(who, 0) + owed
        open_ = sorted(((w, p) for w, p in totals.items() if p), key=lambda x: (-abs(x[1]), x[0]))
        lines = ["Owed, by person"] if open_ else ["Nobody owes anything: splits and loans settle"]
        lines += [f"- {owes(w, p)}" for w, p in open_]
        if unassigned:
            lines.append(
                f"{plural(unassigned, 'loan entry has', 'loan entries have')} no person, so"
                " count in no balance → " + call("finance_transactions", flow="loan")
            )
        lines.append(sync_line)
        if open_:
            lines.append("→ " + call("finance_debts", person=open_[0][0]))
        return "\n".join(lines), {
            "balances": [{"person": w, "they_owe": pounds(p)} for w, p in open_],
            "loans_without_person": unassigned,
            "synced": sync,
        }

    async def account_list(self) -> tuple[str, dict]:
        async with self.connect() as conn:
            accounts = await self.accounts(conn)
            counts = {
                acc: (n, first)
                for acc, n, first in await self.rows(
                    conn, "select account_id, count(*), min(day) from ledger.entry group by 1"
                )
            }
        sync_line, sync = self.synced(accounts)
        items = []
        for a in accounts:
            n, first = counts.get(a.id, (0, None))
            items.append(
                {
                    "account": a.label,
                    "kind": a.kind,
                    "name": a.name,
                    "entries": n,
                    "since": str(first) if first else None,
                    "synced": a.synced.isoformat(),
                }
            )
        lines = [f"{len(items)} accounts (balances not collected yet)"]
        lines += [
            f"- {i['account']} ({i['kind']}) · {i['name']} · {i['entries']:,} entries since {i['since']}"
            f" · synced {when(a.synced)}"
            for i, a in zip(items, accounts, strict=True)
        ]
        lines.append(sync_line)
        return "\n".join(lines), {"accounts": items, "synced": sync}
