"""Exchange trading-session calendar.

All lookbacks ("21 sessions ago", "252 sessions ago", "next session's open") are
measured in NYSE sessions from this calendar -- never in calendar days, and never
in "rows of a particular stock's price history" (a stock with a missing bar does
not shift the lookback dates of other stocks).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def to_iso(d: date | datetime | str) -> str:
    if isinstance(d, str):
        return d[:10]
    return d.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class SessionTimes:
    open_utc: datetime
    close_utc: datetime


class TradingCalendar:
    """Ordered list of trading sessions (ISO date strings) with optional open/close times."""

    def __init__(self, sessions: list[str], times: dict[str, SessionTimes] | None = None, name: str = "custom"):
        if sorted(sessions) != list(sessions) or len(set(sessions)) != len(sessions):
            raise ValueError("sessions must be strictly increasing")
        self.name = name
        self.sessions: list[str] = list(sessions)
        self._index = {s: i for i, s in enumerate(self.sessions)}
        self._times = times or {}

    # -- lookups ---------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.sessions)

    def is_session(self, s: str) -> bool:
        return s in self._index

    def index(self, s: str) -> int:
        try:
            return self._index[s]
        except KeyError:
            raise KeyError(f"{s} is not a {self.name} trading session") from None

    def offset(self, s: str, n: int) -> str | None:
        """Session `n` sessions after (n>0) or before (n<0) `s`; None if outside calendar."""
        i = self.index(s) + n
        return self.sessions[i] if 0 <= i < len(self.sessions) else None

    def next_session(self, s: str) -> str | None:
        """First session strictly after date `s` (which need not be a session)."""
        i = bisect.bisect_right(self.sessions, s)
        return self.sessions[i] if i < len(self.sessions) else None

    def previous_session(self, s: str) -> str | None:
        """Last session strictly before date `s`."""
        i = bisect.bisect_left(self.sessions, s) - 1
        return self.sessions[i] if i >= 0 else None

    def session_on_or_after(self, s: str) -> str | None:
        return s if s in self._index else self.next_session(s)

    def session_on_or_before(self, s: str) -> str | None:
        return s if s in self._index else self.previous_session(s)

    def between(self, start: str, end: str) -> list[str]:
        lo = bisect.bisect_left(self.sessions, start)
        hi = bisect.bisect_right(self.sessions, end)
        return self.sessions[lo:hi]

    def is_month_end(self, s: str) -> bool:
        """True if `s` is the last trading session of its calendar month.

        Requires knowing the following session; at the calendar's final session we
        cannot know, so we return False rather than guess.
        """
        nxt = self.offset(s, 1)
        return nxt is not None and nxt[:7] != s[:7]

    def month_end_sessions(self, start: str, end: str) -> list[str]:
        return [s for s in self.between(start, end) if self.is_month_end(s)]

    def next_month_end(self, after_or_on: str) -> str | None:
        for s in self.sessions[bisect.bisect_left(self.sessions, after_or_on):]:
            if self.is_month_end(s):
                return s
        return None

    # -- wall-clock helpers ------------------------------------------------------------------
    def times(self, s: str) -> SessionTimes:
        if s in self._times:
            return self._times[s]
        # Fallback for synthetic calendars: 09:30-16:00 New York time.
        d = date.fromisoformat(s)
        o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY).astimezone(UTC)
        c = datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY).astimezone(UTC)
        return SessionTimes(o, c)

    def latest_completed_session(self, now: datetime, settle: timedelta = timedelta(hours=1)) -> str | None:
        """Most recent session whose close (+ settle buffer for EOD data) is before `now`."""
        today = now.astimezone(NY).strftime("%Y-%m-%d")
        s = self.session_on_or_before(today)
        while s is not None and self.times(s).close_utc + settle > now:
            s = self.previous_session(s)
        return s

    def market_clock(self, now: datetime) -> dict:
        today = now.astimezone(NY).strftime("%Y-%m-%d")
        status = "closed"
        current = today if self.is_session(today) else None
        if current:
            t = self.times(current)
            if t.open_utc <= now < t.close_utc:
                status = "open"
            elif now < t.open_utc:
                status = "pre_open"
            else:
                status = "after_close"
        nxt = self.next_session(today)
        upcoming = current if status in ("pre_open", "open") else nxt
        return {
            "now_utc": now.astimezone(UTC).isoformat(timespec="seconds"),
            "now_new_york": now.astimezone(NY).isoformat(timespec="seconds"),
            "status": status,
            "session_today": current,
            "next_open_utc": self.times(upcoming).open_utc.isoformat() if upcoming and status != "open" else None,
            "next_close_utc": self.times(upcoming).close_utc.isoformat() if upcoming else None,
            "calendar": self.name,
            "source": "exchange_calendars XNYS schedule (calendar only; no live quotes)",
        }


@lru_cache(maxsize=2)
def xnys_calendar(start: str = "2014-01-01") -> TradingCalendar:
    """NYSE calendar from the maintained `exchange_calendars` package (holidays, early closes)."""
    import exchange_calendars as xc

    cal = xc.get_calendar("XNYS", start=start)
    sched = cal.schedule
    sessions: list[str] = []
    times: dict[str, SessionTimes] = {}
    for ts, row in sched.iterrows():
        s = ts.strftime("%Y-%m-%d")
        sessions.append(s)
        times[s] = SessionTimes(row["open"].to_pydatetime(), row["close"].to_pydatetime())
    return TradingCalendar(sessions, times, name="XNYS")
