"""Application context: wires settings, database, calendar, data provider, model ledger, paper broker and
the background executor."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .backtest.runner import create_run, execute_run
from .calendar import TradingCalendar, xnys_calendar
from .config import Settings
from .data.panel import Panel
from .data.provider import MarketDataProvider, ProviderError
from .data.store import data_version, get_panel, run_import
from .db import Database, log_event
from .ledger.paper import PaperLedger
from .strategy.config import StrategyConfig

log = logging.getLogger(__name__)


def build_provider(settings: Settings) -> MarketDataProvider:
    if settings.market_data_provider == "norgate":
        from .data.norgate_provider import NorgateProvider

        return NorgateProvider(index_name=settings.norgate_index, history_start=settings.norgate_history_start)
    from .data.alpaca_provider import AlpacaProvider

    return AlpacaProvider(
        key_id=settings.alpaca_api_key_id.get_secret_value() if settings.alpaca_api_key_id else "",
        secret=settings.alpaca_api_secret_key.get_secret_value() if settings.alpaca_api_secret_key else "",
        feed=settings.alpaca_data_feed, data_url=settings.alpaca_data_base_url,
        trading_url=settings.alpaca_trading_base_url, history_start=settings.alpaca_history_start,
        universe_file=settings.alpaca_universe_file, max_symbols=settings.alpaca_max_symbols,
    )


class AppContext:
    def __init__(self, settings: Settings, calendar: TradingCalendar | None = None,
                 provider: MarketDataProvider | None = None, broker=None):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.calendar = calendar or xnys_calendar()
        self.provider_error: str | None = None
        self.provider: MarketDataProvider | None = provider
        if self.provider is None:
            try:
                self.provider = build_provider(settings)
            except ProviderError as e:
                self.provider_error = str(e)
                log.error("market data provider unavailable", extra={"error": str(e)})
        self._broker = broker
        self.broker_error: str | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker")
        key = self.provider.info.key if self.provider else settings.market_data_provider
        self._rf = None
        self.ledger = PaperLedger(self.db, self.calendar, key, self.panel, self.data_version, rf_fn=self.rf_source)

    def rf_source(self):
        """Ken French RF (cached weekly on disk); only used when a config enables cash_interest."""
        if self._rf is None:
            from .backtest.factors import RateSource, load_factors
            from .config import REPO_ROOT

            self._rf = RateSource(load_factors(REPO_ROOT / "data" / "factors"))
        return self._rf

    # ------------------------------------------------------------------ broker
    @property
    def broker(self):
        """Alpaca PAPER broker (None if keys are missing)."""
        if self._broker is None and self.broker_error is None:
            from .broker.alpaca_paper import AlpacaPaperBroker, BrokerError

            s = self.settings
            try:
                self._broker = AlpacaPaperBroker(
                    s.alpaca_api_key_id.get_secret_value() if s.alpaca_api_key_id else "",
                    s.alpaca_api_secret_key.get_secret_value() if s.alpaca_api_secret_key else "",
                    s.broker_paper_url)
            except BrokerError as e:
                self.broker_error = str(e)
        return self._broker

    def daily_cycle(self, trigger: str = "manual", dry_run: bool = False) -> dict:
        from .automation import DailyCycle

        return DailyCycle(self, self.broker).run(trigger=trigger, dry_run=dry_run)

    # ------------------------------------------------------------------ data access
    def require_provider(self) -> MarketDataProvider:
        if self.provider is None:
            raise ProviderError(self.provider_error or "No market data provider configured.")
        return self.provider

    def panel(self) -> Panel:
        p = self.require_provider()
        return get_panel(self.db, p.info.key, self.calendar, p.info.benchmark_symbol)

    def data_version(self) -> str:
        return data_version(self.db, self.require_provider().info.key)

    def has_data(self) -> bool:
        if self.provider is None:
            return False
        return bool(self.db.scalar(
            "SELECT 1 FROM bars b JOIN instruments i ON i.id=b.instrument_id WHERE i.provider=? LIMIT 1",
            (self.provider.info.key,)))

    def data_label(self) -> str:
        if self.provider:
            return self.provider.info.data_label
        return "End-of-Day Market Data" if self.settings.market_data_provider == "norgate" else "Delayed Market Data"

    def refresh_data(self, full: bool = False) -> dict:
        """Import from the provider.

        After the first import the universe is FROZEN: refreshes re-fetch the last 10 stored sessions onward for
        the same symbols (catching late corrections). Symbols the provider adds that have no history yet (e.g.
        reference sector ETFs) are back-filled with full history first. `full=True` re-fetches everything.
        Missing sectors are then filled from SEC EDGAR (if SEC_USER_AGENT is configured).
        """
        prov = self.require_provider()
        key = prov.info.key
        existing = [r["symbol"] for r in self.db.query(
            "SELECT symbol FROM instruments WHERE provider=? ORDER BY symbol", (key,))]
        start = None
        if existing and not full:
            hi = self.db.scalar("SELECT MAX(b.session) FROM bars b JOIN instruments i ON i.id=b.instrument_id "
                                "WHERE i.provider=?", (key,))
            if hi:
                start = self.calendar.offset(self.calendar.session_on_or_before(hi), -10)
            wanted = {i.symbol for i in prov.list_instruments(existing)}
            with_bars = {r["symbol"] for r in self.db.query(
                "SELECT DISTINCT i.symbol FROM instruments i JOIN bars b ON b.instrument_id=i.id WHERE i.provider=?", (key,))}
            new = sorted(wanted - with_bars)
            if new:
                log.info("backfilling new symbols", extra={"symbols": new})
                run_import(self.db, prov, self.calendar, symbols=new)
        result = run_import(self.db, prov, self.calendar, start=start, symbols=existing or None)
        self.fill_sectors()
        return result

    def fill_sectors(self, only_missing: bool = True) -> dict | None:
        """SEC EDGAR SIC sectors - Alpaca only (Norgate supplies its own GICS sectors)."""
        if not self.settings.sec_user_agent or self.provider is None or self.provider.info.key != "alpaca":
            return None
        from .data.sectors import SecClient, classify_instruments

        try:
            return classify_instruments(self.db, self.provider.info.key, SecClient(self.settings.sec_user_agent),
                                        only_missing=only_missing)
        except Exception as e:  # noqa: BLE001 - sectors are optional; never block a data refresh
            with self.db.transaction() as conn:
                log_event(conn, "warning", "data_import", f"SEC sector classification failed: {e}")
            return None

    # ------------------------------------------------------------------ backtests
    def submit_backtest(self, cfg: StrategyConfig, name: str | None) -> int:
        prov = self.require_provider()
        panel = self.panel()
        run_id = create_run(self.db, cfg, prov.info, self.data_version(), name)
        self.executor.submit(execute_run, self.db, run_id, panel, self.calendar, prov.info)
        return run_id

    def recover_interrupted_runs(self) -> None:
        with self.db.transaction() as conn:
            n = conn.execute("UPDATE backtest_runs SET status='failed', error='Interrupted by server restart' "
                             "WHERE status IN ('queued','running')").rowcount
            if n:
                log_event(conn, "warning", "backtest", f"{n} interrupted backtest run(s) marked failed after restart")
            conn.execute("UPDATE automation_runs SET status='error', summary='Interrupted' WHERE status='running'")
