"""Exchange-calendar boundaries (NYSE holidays, month ends, completed sessions)."""

from datetime import UTC, datetime

from app.calendar import xnys_calendar


def test_month_end_before_good_friday():
    cal = xnys_calendar()
    # Good Friday 2024-03-29: NYSE closed -> last session of March is Thursday 2024-03-28.
    assert not cal.is_session("2024-03-29")
    assert cal.is_month_end("2024-03-28")
    assert cal.next_session("2024-03-28") == "2024-04-01"


def test_fill_after_new_year_holiday():
    cal = xnys_calendar()
    assert cal.is_month_end("2025-12-31")
    assert cal.next_session("2025-12-31") == "2026-01-02"


def test_offsets_count_sessions_not_calendar_days():
    cal = xnys_calendar()
    # 21 sessions before 2024-12-31 skips Thanksgiving (11-28) and Christmas (12-25)
    assert cal.offset("2024-12-31", -21) == "2024-11-29"
    assert len(cal.between("2024-01-01", "2024-12-31")) == 252


def test_latest_completed_session_respects_close_and_settle_buffer():
    cal = xnys_calendar()
    # Friday 2026-09-25, 15:00 New York (before close) -> latest completed is Thursday.
    assert cal.latest_completed_session(datetime(2026, 9, 25, 19, 0, tzinfo=UTC)) == "2026-09-24"
    # Saturday -> Friday is complete.
    assert cal.latest_completed_session(datetime(2026, 9, 26, 15, 0, tzinfo=UTC)) == "2026-09-25"


def test_early_close_day_times():
    cal = xnys_calendar()
    t = cal.times("2026-11-27")  # day after Thanksgiving: 13:00 New York close
    assert t.close_utc.hour == 18
