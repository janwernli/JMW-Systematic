"""Application context: wires settings, database, calendar, provider, ledger and the run executor."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .backtest.runner import create_run, execute_run
from .calendar import TradingCalendar, xnys_calendar
from .config import Settings
from .data.demo_provider import DemoProvider
from .data.panel import Panel
from .data.provider import MarketDataProvider, ProviderError
from .data.store import data_version, get_panel, run_import
from .db import Database, log_event
from .ledger.paper import PaperLedger
from .strategy.config import StrategyConfig

log = logging.getLogger(__name__)

DEMO_PAPER_INCEPTION = "2025-12-31"
DEMO_PAPER_SEED_UNTIL = "2026-08-31"


def build_provider(settings: Settings) -> MarketDataProvider:
    if settings.market_data_provider == "alpaca":
        from .data.alpaca_provider import AlpacaProvider

        return AlpacaProvider(
            key_id=settings.alpaca_api_key_id.get_secret_value() if settings.alpaca_api_key_id else "",
            secret=settings.alpaca_api_secret_key.get_secret_value() if settings.alpaca_api_secret_key else "",
            feed=settings.alpaca_data_feed, data_url=settings.alpaca_data_base_url,
            trading_url=settings.alpaca_trading_base_url, history_start=settings.alpaca_history_start,
            universe_file=settings.alpaca_universe_file, max_symbols=settings.alpaca_max_symbols,
        )
    return DemoProvider()


class AppContext:
    def __init__(self, settings: Settings, calendar: TradingCalendar | None = None,
                 provider: MarketDataProvider | None = None):
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
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
        key = self.provider.info.key if self.provider else settings.market_data_provider
        self.ledger = PaperLedger(self.db, self.calendar, key, self.panel, self.data_version,
                                  is_demo=bool(self.provider and self.provider.info.is_demo))

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
        return "Demo Data" if self.settings.market_data_provider == "demo" else "Delayed Market Data"

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

    # ------------------------------------------------------------------ demo seed
    def seed_demo(self) -> None:
        """First-start bootstrap: demo data, one default backtest, and a demo virtual portfolio.

        The demo portfolio is funded at the close of 2025-12-31 and advanced with the
        monthly plans applied automatically through July; the 2026-08-31 plan is left
        *proposed* so the Rebalance Desk shows a live decision.
        """
        prov = self.provider
        if prov is None or not prov.info.is_demo:
            return
        if not self.has_data():
            log.info("seeding demo market data")
            run_import(self.db, prov, self.calendar)
        if not self.db.scalar("SELECT COUNT(*) FROM backtest_runs WHERE provider=?", (prov.info.key,)):
            cfg = StrategyConfig()
            run_id = create_run(self.db, cfg, prov.info, self.data_version(), "Default v1 (demo seed)")
            execute_run(self.db, run_id, self.panel(), self.calendar, prov.info)
        if not self.db.scalar("SELECT COUNT(*) FROM paper_portfolios WHERE provider=?", (prov.info.key,)):
            self.ledger.initialize(StrategyConfig(), inception_session=DEMO_PAPER_INCEPTION,
                                   name="Demo virtual portfolio")
            self.ledger.advance(until=DEMO_PAPER_SEED_UNTIL, auto_apply=True)
            with self.db.transaction() as conn:
                log_event(conn, "info", "paper", "Demo seed: plans through July 2026 were auto-applied; the August "
                          "plan awaits your decision in the Rebalance Desk.")
