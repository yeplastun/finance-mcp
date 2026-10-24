"""Periods as the model names them; the server does every date calculation.

`2026-09`, `2026-Q3`, `2026`, `2026-09-14`, `this-month`, `last-month`, `this-quarter`,
`last-quarter`, `this-year`, `last-year`, `last-30d` (ending today), `last-6m` (the six whole
months before this one), and `A..B` between any two of those (`2026-07..2026-09`; `2026-09..`
runs to today). Nothing runs past today, so this month is always "so far".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from mcpkit.errors import ToolFailure

HELP = "use e.g. 2026-09, 2026-Q3, 2026, this-month, last-30d, last-6m or 2026-07..2026-09"
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def add_months(first: date, n: int) -> date:
    y, m = divmod(first.year * 12 + first.month - 1 + n, 12)
    return date(y, m + 1, 1)


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive

    @property
    def months(self) -> int | None:
        """Calendar months touched, for a period starting on the 1st (the last may be partial)."""
        if self.start.day != 1:
            return None
        n = (self.end.year - self.start.year) * 12 + self.end.month - self.start.month
        return n + (self.end.day > 1)

    def back(self, k: int) -> Period:
        """The same span k spans earlier. Months move by months, so 1–12 Oct pairs with 1–12 Sep."""
        if self.months is None:
            span = self.end - self.start
            return Period(self.start - k * span, self.end - k * span)
        start = add_months(self.start, -k * self.months)
        if self.end.day == 1:
            return Period(start, add_months(self.end, -k * self.months))
        return Period(start, start + (self.end - self.start))

    @property
    def label(self) -> str:
        s, last = self.start, self.end - timedelta(days=1)
        whole = s.day == 1 and self.end.day == 1
        if whole and add_months(s, 1) == self.end:
            return f"{MONTH_NAMES[s.month - 1]} {s.year}"
        if whole and s.month % 3 == 1 and add_months(s, 3) == self.end:
            return f"Q{s.month // 3 + 1} {s.year}"
        if whole and s.month == 1 and add_months(s, 12) == self.end:
            return str(s.year)
        if s == last:
            return f"{s.day} {MONTH_NAMES[s.month - 1]} {s.year}"
        if (s.year, s.month) == (last.year, last.month):
            return f"{s.day}–{last.day} {MONTH_NAMES[s.month - 1]} {s.year}"
        if s.year == last.year:
            return f"{s.day} {MONTH_NAMES[s.month - 1]} – {last.day} {MONTH_NAMES[last.month - 1]} {s.year}"
        return f"{s.day} {MONTH_NAMES[s.month - 1]} {s.year} – {last.day} {MONTH_NAMES[last.month - 1]} {last.year}"


def _one(text: str, today: date) -> Period:
    month = date(today.year, today.month, 1)
    quarter = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
    year = date(today.year, 1, 1)
    named = {
        "today": Period(today, today + timedelta(days=1)),
        "yesterday": Period(today - timedelta(days=1), today),
        "this-month": Period(month, add_months(month, 1)),
        "last-month": Period(add_months(month, -1), month),
        "this-quarter": Period(quarter, add_months(quarter, 3)),
        "last-quarter": Period(add_months(quarter, -3), quarter),
        "this-year": Period(year, add_months(year, 12)),
        "last-year": Period(add_months(year, -12), year),
    }
    if text in named:
        return named[text]
    try:
        if m := re.fullmatch(r"last-(\d{1,4})d", text):
            return Period(today - timedelta(days=int(m[1]) - 1), today + timedelta(days=1))
        if m := re.fullmatch(r"last-(\d{1,3})m", text):
            return Period(add_months(month, -int(m[1])), month)
        if m := re.fullmatch(r"(\d{4})-q([1-4])", text):
            start = date(int(m[1]), int(m[2]) * 3 - 2, 1)
            return Period(start, add_months(start, 3))
        if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text):
            day = date(int(m[1]), int(m[2]), int(m[3]))
            return Period(day, day + timedelta(days=1))
        if m := re.fullmatch(r"(\d{4})-(\d{2})", text):
            start = date(int(m[1]), int(m[2]), 1)
            return Period(start, add_months(start, 1))
        if m := re.fullmatch(r"(\d{4})", text):
            start = date(int(m[1]), 1, 1)
            return Period(start, add_months(start, 12))
    except ValueError:  # month 13, day 31 in a 30-day month
        pass
    raise ToolFailure(f"can't read period {text!r}: {HELP}")


def parse(text: str, today: date) -> Period:
    text = text.strip().lower()
    if ".." in text:
        a, _, b = text.partition("..")
        start = _one(a.strip(), today).start
        end = _one(b.strip(), today).end if b.strip() else today + timedelta(days=1)
    else:
        one = _one(text, today)
        start, end = one.start, one.end
    end = min(end, today + timedelta(days=1))
    if start >= end:
        raise ToolFailure(f"period {text!r} is empty or in the future (today is {today})")
    return Period(start, end)
