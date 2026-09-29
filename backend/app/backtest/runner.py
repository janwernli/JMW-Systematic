"""Persisted backtest runs: inputs, data snapshot/version, results, timestamps, reproducibility."""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from importlib.metadata import version as pkg_version

import pandas as pd

from .. import __version__
from ..calendar import TradingCalendar
from ..config import REPO_ROOT
from ..data.panel import Panel
from ..data.provider import ProviderInfo
from ..db import Database, log_event, utcnow
from ..ledger.paper import store_config, store_signal_set
from ..strategy.config import StrategyConfig
from .engine import MARGIN, BacktestResult, run_backtest
from .metrics import compute_metrics

log = logging.getLogger(__name__)


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                             text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def assumptions(cfg: StrategyConfig, info: ProviderInfo, benchmark: str | None) -> list[dict]:
    ls = [
            {"key": "signal", "text": ("Composite rank signal: 60% residual momentum (36-month market + sector-ETF "
                                       "regression, 11-month residual sum / residual vol), 25% sector-demeaned 12-1, "
                                       "15% frog-in-the-pan; each winsorized at +/-3 sd and z-scored."
                                       if cfg.signal == "composite" else "Plain 12-1 momentum ranking.")},
            {"key": "sectors", "text": ("Sector-neutral: |long - short| <= "
                                        f"{cfg.max_sector_net:.0%} of NAV per sector (SEC EDGAR SIC -> 11 sectors)."
                                        if cfg.sector_neutral else "No sector-neutrality constraint.")},
            {"key": "htb", "text": f"Hard-to-borrow stand-in: the least liquid {cfg.htb_exclude_pct:.0%} of eligible "
                                   f"stocks by {cfg.htb_adv_window}-session dollar volume are never shorted."},
            {"key": "books", "text": f"Long the top {cfg.long_pct:.0%} and short the bottom {cfg.short_pct:.0%} of eligible "
                                     f"stocks by 12-1 momentum, {cfg.min_names_per_side}-{cfg.max_names_per_side} names per "
                                     f"side. Buffer: held names stay while in the top/bottom {cfg.buffer_exit_pct:.0%}."},
            {"key": "weights", "text": f"Inverse {cfg.vol_lookback_sessions}-session realized-vol weights within each side, "
                                       f"capped at {cfg.max_long_weight:.0%} per long and {cfg.max_short_weight:.0%} per "
                                       "short (excess redistributed)."},
            {"key": "neutrality", "text": f"Beta-neutral: short gross = long gross x beta_long / beta_short, betas from "
                                          f"{cfg.beta_lookback_sessions} sessions vs. the benchmark, shrunk "
                                          f"{cfg.beta_shrink:.0%} toward 1."},
            {"key": "vol_target", "text": f"Gross scaled to a {cfg.target_vol:.0%} annualized ex-ante vol (trailing "
                                          f"{cfg.vol_lookback_sessions} sessions), within {cfg.min_side_gross:.0%}-"
                                          f"{cfg.max_side_gross:.0%} per side and {cfg.max_total_gross:.0%} total. Per-name "
                                          "caps and neutrality take priority over the minimum gross."},
            {"key": "crash_guard", "text": (f"Crash guard: short book x {cfg.crash_short_scale:g} when the benchmark's "
                                            f"{cfg.crash_market_lookback_sessions}-session return < 0 and its "
                                            f"{cfg.vol_lookback_sessions}-session vol > {cfg.crash_market_vol_threshold:.0%}."
                                            if cfg.crash_guard else "Crash guard disabled.")},
            {"key": "shorts", "text": f"Shorts need a raw close > ${cfg.short_min_price:g}. ASSUMED borrow fee "
                                      f"{cfg.borrow_fee_annual:.2%}/yr accrued per calendar day (/360) on short market value; short proceeds "
                                      "are held as cash earning 0%; shorts pay dividends on the ex-date; no locates, recalls "
                                      "or hard-to-borrow costs are modelled."},
            {"key": "stop_loss", "text": (f"Short stop-loss: when a close is {cfg.short_stop_loss:.0%} above the average short "
                                          "entry, the short is covered at the next open (automatic) and may not be re-shorted "
                                          "until the next monthly signal." if cfg.short_stop_loss else "No short stop-loss.")},
    ]
    return ls + [
        {"key": "signal", "text": f"12-1 momentum = TR(t-{cfg.skip_sessions}) / TR(t-{cfg.lookback_sessions}) - 1, "
                                  "computed after the close of the last NYSE session of each month and frozen."},
        {"key": "fills", "text": "Orders fill at the next session's OPEN. If a stock has no opening price, it is not "
                                 "traded (no substitution of the signal close)."},
        {"key": "slippage", "text": f"ASSUMED adverse slippage of {cfg.slippage_bps:g} bps on buys and sells vs. the open "
                                    "(not observed execution cost)."},
        {"key": "commission", "text": f"ASSUMED commission ${cfg.commission_per_order:g} per fill + "
                                      f"{cfg.commission_bps:g} bps of traded value."},
        {"key": "sizing", "text": "Signed target weights of NAV at the open; whole shares only; order: reduce longs, "
                                  "short sales, covers, then buys in rank order limited by cash."},
        {"key": "dividends", "text": "Cash dividends are credited to cash on the ex-date (pay-date lag ignored); prices used "
                                     "for valuation are raw (unadjusted), so dividends are never double counted."},
        {"key": "splits", "text": "Splits adjust share counts on the ex-date; fractional shares are paid as cash-in-lieu."},
        {"key": "delisting", "text": "Holdings are converted to cash at their last available close on the delisting "
                                     "session. Real delisting proceeds may be materially lower."},
        {"key": "gross", "text": "Gross NAV = net NAV + cumulative slippage, commissions and borrow fees (not compounded)."},
        {"key": "cash", "text": ("Positive cash (incl. short proceeds) earns the Ken French RF, ACT/360; the alpha test uses r - RF."
                                 if cfg.cash_interest else
                                 "Cash earns no interest; the alpha test therefore uses raw returns (RF not subtracted).")},
        {"key": "benchmark", "text": f"Benchmark {benchmark or 'n/a'}: "
                                     + ("total return (dividends reinvested via the same TR index)."
                                        if info.benchmark_return_basis == "total_return"
                                        else "PRICE RETURN ONLY (dividends not included); comparison is biased against "
                                             "the benchmark.")},
        {"key": "universe", "text": info.survivorship_note},
    ]


def margin_summary(res: BacktestResult, keep: int = 100) -> dict:
    """Days on which the book breached the 2x gross limit or the maintenance requirement (see engine.MARGIN)."""
    nav = res.nav
    flags = res.margin_flags
    gross = nav["gross_exposure"].replace([float("inf")], float("nan")) if "gross_exposure" in nav else None
    cushion = (nav["nav"] / nav["margin_requirement"]).where(nav["margin_requirement"] > 0)         if "margin_requirement" in nav else None
    by_kind = {k: [f["session"] for f in flags if k in f["breaches"]] for k in ("gross", "maintenance")}
    return {
        "limits": dict(MARGIN),
        "gross_breach_days": len(by_kind["gross"]), "maintenance_breach_days": len(by_kind["maintenance"]),
        "first_gross_breach": by_kind["gross"][0] if by_kind["gross"] else None,
        "first_maintenance_breach": by_kind["maintenance"][0] if by_kind["maintenance"] else None,
        "max_gross_exposure": None if gross is None or gross.isna().all() else float(gross.max()),
        "max_gross_session": None if gross is None or gross.isna().all() else str(gross.idxmax()),
        "min_equity_to_requirement": None if cushion is None or cushion.isna().all() else float(cushion.min()),
        "min_equity_to_requirement_session": None if cushion is None or cushion.isna().all() else str(cushion.idxmin()),
        "flagged_days": flags[:keep], "flagged_days_truncated": len(flags) > keep,
    }


def create_run(db: Database, cfg: StrategyConfig, info: ProviderInfo, data_version: str, name: str | None) -> int:
    with db.transaction() as conn:
        cfg_id = store_config(conn, cfg)
        imp = conn.execute("SELECT MAX(id) FROM data_imports WHERE provider=? AND status='succeeded'",
                           (info.key,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO backtest_runs (name, status, progress, config_id, config_json, provider, data_label,"
            " data_version, data_import_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name or f"{'Composite' if cfg.signal == 'composite' else '12-1'} L/S vol {cfg.target_vol:.0%} / "
                      f"{cfg.slippage_bps:g}bps", "queued", 0.0, cfg_id, cfg.canonical_json(),
             info.key, info.data_label, data_version, imp, utcnow()))
        run_id = cur.lastrowid
        log_event(conn, "info", "backtest", f"Backtest #{run_id} queued", run_id=run_id,
                  payload={"config": cfg.model_dump(), "data_version": data_version})
    return run_id


def execute_run(db: Database, run_id: int, panel: Panel, calendar: TradingCalendar, info: ProviderInfo) -> None:
    row = db.query_one("SELECT * FROM backtest_runs WHERE id=?", (run_id,))
    cfg = StrategyConfig.model_validate_json(row["config_json"])
    with db.transaction() as conn:
        conn.execute("UPDATE backtest_runs SET status='running', started_at=? WHERE id=?", (utcnow(), run_id))

    def progress(frac: float, session: str) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE backtest_runs SET progress=?, progress_note=? WHERE id=?",
                         (round(frac * 0.9, 4), f"Simulating {session}", run_id))

    try:
        rf = None
        borrows = cfg.core_beta > 0 or (cfg.sizing == "fixed" and cfg.fixed_long_gross > cfg.fixed_short_gross)
        if cfg.cash_interest or borrows:
            from .factors import RateSource, load_factors

            try:
                rf = RateSource(load_factors(REPO_ROOT / "data" / "factors"))
            except Exception:  # noqa: BLE001
                if cfg.cash_interest:
                    raise   # interest on cash needs RF; margin interest falls back to the spread (warned)
        res = run_backtest(panel, calendar, cfg, progress, rf=rf)
        if rf is not None and rf.carried:
            res.warnings.append(f"RF not yet published for {', '.join(sorted(rf.carried))}; latest month carried forward.")
        persist_result(db, run_id, res, info, row["data_version"])
    except Exception as e:  # noqa: BLE001
        log.exception("backtest failed", extra={"run_id": run_id})
        with db.transaction() as conn:
            conn.execute("UPDATE backtest_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                         (f"{type(e).__name__}: {e}", utcnow(), run_id))
            log_event(conn, "error", "backtest", f"Backtest #{run_id} failed: {e}", run_id=run_id)


def persist_result(db: Database, run_id: int, res: BacktestResult, info: ProviderInfo, data_version: str) -> dict:
    cfg = res.config
    nav = res.nav
    reb_dicts = [{"status": r.status, "turnover": r.turnover, "slippage_cost": r.slippage_cost,
                  "commission": r.commission} for r in res.rebalances]
    metrics = compute_metrics(nav, reb_dicts, cfg.initial_capital)
    metrics["signal"] = cfg.signal
    metrics["borrow_fees"] = float(-sum(e.amount for _, e in res.cash_events if e.kind == "borrow_fee"))
    metrics["short_dividends_paid"] = float(-sum(e.amount for _, e in res.cash_events
                                                 if e.kind == "dividend" and e.amount < 0))
    metrics["stop_losses"] = len(res.stops)
    metrics["cash_interest"] = float(sum(e.amount for _, e in res.cash_events if e.kind == "interest"))
    metrics["margin_interest"] = float(-sum(e.amount for _, e in res.cash_events if e.kind == "margin_interest"))
    metrics["min_cash_weight"] = float((nav["cash"] / nav["nav"]).min())
    metrics["crash_guard_months"] = sum(1 for r in res.rebalances
                                        if r.signals.diagnostics.get("crash_guard", {}).get("active"))
    metrics["margin"] = margin_summary(res)
    metrics["benchmark_symbol"] = res.benchmark_symbol
    metrics["benchmark_return_basis"] = info.benchmark_return_basis if res.benchmark_symbol else None
    repro = {
        "app_version": __version__, "git_commit": git_commit(), "python": platform.python_version(),
        "numpy": pkg_version("numpy"), "pandas": pkg_version("pandas"),
        "exchange_calendars": pkg_version("exchange-calendars"), "platform": platform.platform(),
        "config_hash": cfg.config_hash(), "data_version": data_version, "provider": info.key, "feed": info.feed,
        "deterministic": True,
        "note": "Re-running the same config on the same data_version reproduces identical results.",
    }
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO backtest_nav (run_id, session, nav, gross_nav, cash, positions_value, benchmark_nav, positions,"
            " stale_marks, long_value, short_value) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, s, r.nav, r.gross_nav, r.cash, r.positions_value,
              None if pd.isna(r.benchmark_nav) else float(r.benchmark_nav), int(r.positions), int(r.stale_marks),
              float(r.long_value), float(r.short_value))
             for s, r in nav.iterrows()])
        for rec in res.rebalances:
            set_id = None
            if rec.status != "warmup":
                set_id = store_signal_set(conn, rec.signals, "backtest", cfg.config_hash(), data_version, run_id=run_id)
            sig = rec.signals
            rid = conn.execute(
                "INSERT INTO backtest_rebalances (run_id, signal_session, fill_session, signal_set_id, universe_count,"
                " eligible_count, selected_count, nav_at_open, buy_value, sell_value, turnover, slippage_cost, commission,"
                " cash_after, unfilled_json, status, diagnostics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, rec.signal_session, rec.fill_session, set_id, sig.universe_count, sig.eligible_count,
                 sig.selected_count, rec.nav_at_open, rec.buy_value, rec.sell_value, rec.turnover, rec.slippage_cost,
                 rec.commission, rec.cash_after, json.dumps(rec.unfilled), rec.status,
                 json.dumps(sig.diagnostics, default=str))).lastrowid
            conn.executemany(
                "INSERT INTO backtest_fills (run_id, rebalance_id, session, symbol, side, shares, ref_price, fill_price,"
                " gross_value, slippage_cost, commission, reason, position_effect) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, rid, rec.fill_session, f.symbol, f.side, f.shares, f.ref_price, f.fill_price, f.gross_value,
                  f.slippage_cost, f.commission, f.reason, f.effect) for f in rec.fills])
        conn.executemany(
            "INSERT INTO backtest_fills (run_id, rebalance_id, session, symbol, side, shares, ref_price, fill_price,"
            " gross_value, slippage_cost, commission, reason, position_effect) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, None, e.fill_session, f.symbol, f.side, f.shares, f.ref_price, f.fill_price, f.gross_value,
              f.slippage_cost, f.commission, f"stop_loss (trigger {e.trigger_session})", f.effect)
             for e in res.stops for f in e.fills])
        conn.executemany(
            "INSERT INTO backtest_cash_events (run_id, session, kind, symbol, amount, note) VALUES (?,?,?,?,?,?)",
            [(run_id, s, e.kind, e.symbol, e.amount, e.note) for s, e in res.cash_events])
        conn.execute(
            "UPDATE backtest_runs SET status='completed', progress=1.0, progress_note='Completed', finished_at=?,"
            " metrics_json=?, assumptions_json=?, warnings_json=?, repro_json=? WHERE id=?",
            (utcnow(), json.dumps(metrics, default=str), json.dumps(assumptions(cfg, info, res.benchmark_symbol)),
             json.dumps(res.warnings), json.dumps(repro), run_id))
        log_event(conn, "info", "backtest", f"Backtest #{run_id} completed: net total return "
                  f"{metrics['net']['total_return']:.2%} over {metrics['sessions']} sessions", run_id=run_id)
    return metrics
