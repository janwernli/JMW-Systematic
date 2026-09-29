"""Alpaca adapter against a mocked HTTP transport: pagination, retries, adjustment, timestamps, actions."""

import httpx
import pytest

from app.data.alpaca_provider import AlpacaProvider, classify_asset
from app.data.provider import ProviderError


def make(handler):
    return AlpacaProvider("key", "secret", feed="sip", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_bars_pagination_raw_adjustment_and_session_dates():
    calls = []

    def handler(req: httpx.Request):
        calls.append(dict(req.url.params))
        assert req.headers["APCA-API-KEY-ID"] == "key"
        if "page_token" not in req.url.params:
            return httpx.Response(200, json={"bars": {"AAA": [
                {"t": "2024-01-02T05:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}]},
                "next_page_token": "p2"})
        return httpx.Response(200, json={"bars": {"AAA": [
            {"t": "2024-01-03T05:00:00Z", "o": 10.5, "h": 12, "l": 10, "c": 11, "v": 2000}],
            "BBB": [{"t": "2024-07-01T04:00:00Z", "o": 5, "h": 5, "l": 5, "c": 5, "v": 1}]}, "next_page_token": None})

    df = make(handler).fetch_bars(["AAA", "BBB"], "2024-01-01", "2024-12-31")
    assert len(calls) == 2 and calls[1]["page_token"] == "p2"
    assert calls[0]["adjustment"] == "raw" and calls[0]["feed"] == "sip" and calls[0]["timeframe"] == "1Day"
    assert list(df["session"]) == ["2024-01-02", "2024-01-03", "2024-07-01"]  # NY session dates (EST and EDT)
    assert df["close"].tolist() == [10.5, 11, 5]


def test_rate_limit_and_server_error_are_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "0"})
        if n["i"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    assert make(handler).fetch_bars(["AAA"], "2024-01-01", "2024-01-31").empty
    assert n["i"] == 3


def test_auth_error_is_explained():
    with pytest.raises(ProviderError, match="credentials"):
        make(lambda req: httpx.Response(403, text="forbidden")).fetch_bars(["AAA"], "2024-01-01", "2024-01-31")


def test_missing_key_is_rejected():
    with pytest.raises(ProviderError):
        AlpacaProvider("", "")


def test_corporate_actions_mapping():
    def handler(req):
        assert req.url.path == "/v1/corporate-actions"
        return httpx.Response(200, json={"corporate_actions": {
            "forward_splits": [{"symbol": "AAA", "ex_date": "2024-06-10", "old_rate": 1, "new_rate": 4}],
            "reverse_splits": [{"symbol": "BBB", "ex_date": "2024-03-01", "old_rate": 10, "new_rate": 1}],
            "cash_dividends": [{"symbol": "AAA", "ex_date": "2024-02-09", "rate": 0.24, "foreign": False},
                               {"symbol": "AAA", "ex_date": "2024-02-09", "rate": 1.0, "special": True, "foreign": False}],
        }, "next_page_token": None})

    df = make(handler).fetch_corporate_actions(["AAA", "BBB"], "2024-01-01", "2024-12-31").set_index(["symbol", "action_type"])
    assert df.loc[("AAA", "split"), "ratio"] == 4.0
    assert df.loc[("BBB", "split"), "ratio"] == pytest.approx(0.1)
    assert df.loc[("AAA", "cash_dividend"), "amount"] == pytest.approx(1.24)


def test_asset_classification_heuristic():
    assert classify_asset("AAPL", "Apple Inc. Common Stock") == "common_stock"
    assert classify_asset("SPY", "SPDR S&P 500 ETF Trust") == "etf"
    assert classify_asset("XYZ.PRA", "XYZ Corp 6.5% Series A Preferred") == "preferred"
    assert classify_asset("ABCW", "ABC Acquisition Corp Warrants") == "warrant"


@pytest.mark.parametrize("symbol,name,kind", [
    ("AAPL", "Apple Inc. Common Stock", "common_stock"),
    ("JPM", "JPMorgan Chase & Co.", "common_stock"),
    ("BRK.B", "BERKSHIRE HATHAWAY Class B", "common_stock"),
    ("GPMT", "Granite Point Mortgage Trust Inc. Common Stock", "common_stock"),
    ("GAB", "The Gabelli Equity Trust Inc.", "fund"),
    ("QQQ", "Invesco QQQ Trust, Series 1", "fund"),
    ("FXY", "Invesco CurrencyShares Japanese Yen Trust", "etf"),
    ("GLDI", "UBS AG ETRACS Gold Shares Covered Call ETNs due February 2, 2033", "etf"),
    ("GSK", "GSK plc American Depositary Shares (Each representing two Ordinary Shares)", "adr"),
    ("GPJA", "Georgia Power Company Series 2017A 5.00 Percent Junior Subordinated Notes", "debt"),
    ("GSRV", "GSR V Acquisition Corp. Class A ordinary shares", "spac"),
])
def test_classifier_on_real_alpaca_names(symbol, name, kind):
    assert classify_asset(symbol, name) == kind


def test_frozen_universe_skips_reselection_and_bar_end_is_after_bar_stamp():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/v2/assets":
            return httpx.Response(200, json=[{"symbol": "AAA", "name": "AAA Inc. Common Stock", "exchange": "NYSE",
                                              "status": "active", "tradable": True}])
        assert req.url.params["end"] == "2024-03-28T12:00:00Z"  # includes the 2024-03-28 bar stamped 04:00Z
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    p = make(handler)
    inst = p.list_instruments(["AAA", "OLD"])
    from app.data.alpaca_provider import SECTOR_ETFS
    assert [i.symbol for i in inst] == ["AAA", "OLD", "SPY"] + SECTOR_ETFS   # reference series always included
    assert seen == ["/v2/assets"]  # no dollar-volume re-selection requests
    assert "Frozen" in inst[0].metadata["selection"]
    p.fetch_bars(["AAA"], "2024-01-01", "2024-03-28")


def test_corporate_action_requests_are_windowed_by_year():
    from app.data.alpaca_provider import _year_windows

    w = _year_windows("2016-01-01", "2018-06-30")
    assert w[0] == ("2016-01-01", "2016-12-31") and w[-1] == ("2018-01-01", "2018-06-30") and len(w) == 3


def test_relative_paths_resolve_from_project_root():
    from app.config import REPO_ROOT, Settings

    s = Settings(_env_file=None, DATABASE_PATH="./data/x.db", ALPACA_UNIVERSE_FILE="universe.txt")
    assert s.database_path == (REPO_ROOT / "data" / "x.db").resolve()
    assert s.alpaca_universe_file == (REPO_ROOT / "universe.txt").resolve()
