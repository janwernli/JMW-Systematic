"""Market-data persistence (SQLite), import pipeline, data versioning and quality report."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from ..calendar import TradingCalendar
from ..db import Database, log_event, utcnow
from .panel import Panel, build_panel
from .provider import MarketDataProvider, ProviderError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------------------------
def run_import(
    db: Database,
    provider: MarketDataProvider,
    calendar: TradingCalendar,
    start: str | None = None,
    end: str | None = None,
    symbols: list[str] | None = None,
) -> dict:
    """Fetch instruments, raw bars and corporate actions from `provider` and store them.

    Bars are upserted, so re-running an import is idempotent. Every import is
    recorded in `data_imports` with coverage, counts, warnings and provenance.
    """
    info = provider.info
    d_start, d_end = provider.default_history_range()
    start = start or d_start
    end = end or d_end
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO data_imports (provider, feed, started_at, status, requested_start, requested_end, provenance_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (info.key, info.feed, utcnow(), "running", start, end, json.dumps(info.to_dict())),
        )
        import_id = cur.lastrowid
    warnings: list[str] = []
    try:
        instruments = provider.list_instruments(symbols)
        symbols = [i.symbol for i in instruments]
        bars = provider.fetch_bars(symbols, start, end)
        actions = provider.fetch_corporate_actions(symbols, start, end)
        membership = provider.index_membership(symbols)

        # ---- validation -------------------------------------------------------------------
        if not bars.empty:
            not_session = ~bars["session"].map(calendar.is_session)
            if not_session.any():
                warnings.append(f"Dropped {int(not_session.sum())} bars dated on non-trading days.")
                bars = bars[~not_session]
            dup = bars.duplicated(["symbol", "session"], keep="last")
            if dup.any():
                warnings.append(f"Dropped {int(dup.sum())} duplicate bars.")
                bars = bars[~dup]
            bad = ~(bars["close"] > 0)
            if bad.any():
                warnings.append(f"Dropped {int(bad.sum())} bars without a positive close.")
                bars = bars[~bad]
            no_open = int(bars["open"].isna().sum())
            if no_open:
                warnings.append(f"{no_open} bars have no opening price (fills on those sessions will not execute).")
        missing_syms = sorted(set(symbols) - set(bars["symbol"])) if not bars.empty else symbols
        if missing_syms:
            warnings.append(f"{len(missing_syms)} instruments returned no bars in the requested range.")

        with db.transaction() as conn:
            ids = _upsert_instruments(conn, info.key, instruments, import_id)
            n_bars = _upsert_bars(conn, ids, bars, import_id)
            n_act = _upsert_actions(conn, ids, actions, import_id)
            if membership is not None and not membership.empty:
                conn.execute("DELETE FROM universe_membership WHERE instrument_id IN "
                             "(SELECT id FROM instruments WHERE provider=?)", (info.key,))
                conn.executemany(
                    "INSERT OR REPLACE INTO universe_membership (instrument_id, index_name, start_session, end_session,"
                    " import_id) VALUES (?,?,?,?,?)",
                    [(ids[m.symbol], m.index_name, m.start, m.end if isinstance(m.end, str) else None, import_id)
                     for m in membership.itertuples(index=False) if m.symbol in ids])
            cov_start = bars["session"].min() if not bars.empty else None
            cov_end = bars["session"].max() if not bars.empty else None
            status = "succeeded" if not missing_syms or len(missing_syms) < len(symbols) else "failed"
            conn.execute(
                "UPDATE data_imports SET finished_at=?, status=?, coverage_start=?, coverage_end=?, symbols_requested=?,"
                " symbols_loaded=?, bars_loaded=?, actions_loaded=?, warnings_json=? WHERE id=?",
                (utcnow(), status, cov_start, cov_end, len(symbols), len(symbols) - len(missing_syms), n_bars, n_act,
                 json.dumps(warnings), import_id),
            )
            log_event(conn, "info" if status == "succeeded" else "error", "data_import",
                      f"{info.name}: imported {n_bars} bars / {n_act} corporate actions for {len(symbols)} instruments",
                      payload={"import_id": import_id, "coverage": [cov_start, cov_end], "warnings": warnings})
    except ProviderError as e:
        _fail_import(db, import_id, str(e))
        raise
    except Exception as e:  # noqa: BLE001 - recorded, then re-raised
        log.exception("import failed")
        _fail_import(db, import_id, f"{type(e).__name__}: {e}")
        raise
    invalidate_panel_cache()
    return db.query_one("SELECT * FROM data_imports WHERE id=?", (import_id,))


def _fail_import(db: Database, import_id: int, msg: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE data_imports SET finished_at=?, status='failed', error=? WHERE id=?",
                     (utcnow(), msg, import_id))
        log_event(conn, "error", "data_import", f"Data import failed: {msg}", payload={"import_id": import_id})


def _upsert_instruments(conn, provider: str, records, import_id: int) -> dict[str, int]:
    now = utcnow()
    for r in records:
        conn.execute(
            "INSERT INTO instruments (provider, symbol, name, exchange, asset_type, asset_type_source, sector, sector_source,"
            " is_benchmark, list_date, delist_date, active, metadata_json, import_id, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(provider, symbol) DO UPDATE SET name=excluded.name, exchange=excluded.exchange,"
            " asset_type=excluded.asset_type, asset_type_source=excluded.asset_type_source,"
            " sector=COALESCE(excluded.sector, instruments.sector),"
            " sector_source=COALESCE(excluded.sector_source, instruments.sector_source), is_benchmark=excluded.is_benchmark,"
            " list_date=COALESCE(instruments.list_date, excluded.list_date), delist_date=excluded.delist_date,"
            " active=excluded.active, metadata_json=excluded.metadata_json, import_id=excluded.import_id,"
            " updated_at=excluded.updated_at",
            (provider, r.symbol, r.name, r.exchange, r.asset_type, r.asset_type_source, r.sector, r.sector_source,
             int(r.is_benchmark), r.list_date, r.delist_date, int(r.active), json.dumps(r.metadata), import_id, now),
        )
    return {row[0]: row[1] for row in conn.execute("SELECT symbol, id FROM instruments WHERE provider=?", (provider,))}


def _nz(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def _upsert_bars(conn, ids: dict[str, int], bars: pd.DataFrame, import_id: int) -> int:
    if bars.empty:
        return 0
    b = bars[bars["symbol"].isin(ids)]
    vals = b[["open", "high", "low", "close", "volume"]].astype(float)
    vals = vals.astype(object).where(vals.notna(), None)
    rows = list(zip(b["symbol"].map(ids), b["session"], *(vals[c] for c in vals.columns), [import_id] * len(b)))
    conn.executemany(
        "INSERT INTO bars (instrument_id, session, open, high, low, close, volume, import_id) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(instrument_id, session) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,"
        " close=excluded.close, volume=excluded.volume, import_id=excluded.import_id",
        rows,
    )
    return len(rows)


def _upsert_actions(conn, ids: dict[str, int], actions: pd.DataFrame, import_id: int) -> int:
    if actions.empty:
        return 0
    rows = [
        (ids[s], d, t, _nz(r), _nz(a), import_id)
        for s, d, t, r, a in actions[["symbol", "ex_date", "action_type", "ratio", "amount"]].itertuples(index=False)
        if s in ids
    ]
    conn.executemany(
        "INSERT INTO corporate_actions (instrument_id, ex_date, action_type, ratio, amount, import_id) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(instrument_id, ex_date, action_type) DO UPDATE SET ratio=excluded.ratio, amount=excluded.amount,"
        " import_id=excluded.import_id",
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
def load_membership(db: Database, provider: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT i.symbol, m.index_name, m.start_session AS start, m.end_session AS end FROM universe_membership m"
        " JOIN instruments i ON i.id=m.instrument_id WHERE i.provider=?", db.conn, params=(provider,))


def load_frames(db: Database, provider: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    conn = db.conn
    inst = pd.read_sql_query(
        "SELECT symbol, name, exchange, asset_type, asset_type_source, sector, sector_source, is_benchmark,"
        " list_date, delist_date, active FROM instruments WHERE provider=?", conn, params=(provider,))
    bars = _load_bars(db, provider)
    acts = pd.read_sql_query(
        "SELECT i.symbol, a.ex_date, a.action_type, a.ratio, a.amount FROM corporate_actions a"
        " JOIN instruments i ON i.id=a.instrument_id WHERE i.provider=?", conn, params=(provider,))
    return inst, bars, acts


def _load_bars(db: Database, provider: str, chunk: int = 200_000) -> pd.DataFrame:
    """All bars of `provider`, streamed in chunks into numpy arrays.

    `read_sql_query` materializes one Python tuple (plus a str per symbol/session) per row before
    building the frame, which peaks at ~750 MB for 1.5M bars. Streaming into preallocated arrays
    with integer codes keeps the peak near the size of the final frame (small enough for a 1 GiB VM).
    Symbol and session come back as categoricals; values stay float64, so results are identical.
    """
    ids = dict(db.conn.execute("SELECT id, symbol FROM instruments WHERE provider=?", (provider,)).fetchall())
    n = db.conn.execute("SELECT COUNT(*) FROM bars b JOIN instruments i ON i.id=b.instrument_id WHERE i.provider=?",
                        (provider,)).fetchone()[0]
    inst_ids = sorted(ids)
    inst_code = {iid: k for k, iid in enumerate(inst_ids)}
    sess_code: dict[str, int] = {}
    sym = np.empty(n, dtype=np.int32)
    sess = np.empty(n, dtype=np.int32)
    vals = np.empty((n, 5), dtype=float)
    cur = db.conn.execute(
        "SELECT b.instrument_id, b.session, b.open, b.high, b.low, b.close, b.volume FROM bars b"
        " JOIN instruments i ON i.id=b.instrument_id WHERE i.provider=?", (provider,))
    k = 0
    while k < n:
        rows = cur.fetchmany(chunk)
        if not rows:
            break
        m = len(rows)
        cols = list(zip(*rows))
        sym[k:k + m] = [inst_code[i] for i in cols[0]]
        sess[k:k + m] = [sess_code.setdefault(x, len(sess_code)) for x in cols[1]]
        for c in range(5):
            vals[k:k + m, c] = np.array(cols[2 + c], dtype=float)   # None -> nan
        k += m
        del rows, cols
    sym, sess, vals = sym[:k], sess[:k], vals[:k]
    frame = pd.DataFrame(vals, columns=["open", "high", "low", "close", "volume"])
    # Ordered, sorted session categories so min()/max()/comparisons behave like the plain strings.
    sess_cats = sorted(sess_code)
    remap = np.empty(len(sess_code), dtype=np.int32)
    remap[[sess_code[x] for x in sess_cats]] = np.arange(len(sess_cats), dtype=np.int32)
    frame.insert(0, "session", pd.Categorical.from_codes(remap[sess] if len(sess) else sess, categories=sess_cats,
                                                         ordered=True))
    frame.insert(0, "symbol", pd.Categorical.from_codes(sym, categories=[ids[i] for i in inst_ids]))
    return frame


_version_cache: dict[tuple, str] = {}


def data_version(db: Database, provider: str) -> str:
    """Content fingerprint of the stored data for `provider` (changes whenever bars/actions change).

    Cached per latest import record: every write to bars/actions goes through `run_import`,
    which creates a new data_imports row, so the cache key changes whenever data can change.
    """
    stamp = db.query_one("SELECT MAX(id) id, MAX(COALESCE(finished_at, started_at)) ts, COUNT(*) n FROM data_imports "
                         "WHERE provider=?", (provider,))
    key = (str(db.path), provider, stamp["id"], stamp["ts"], stamp["n"])
    if key in _version_cache:
        return _version_cache[key]
    ver = _compute_data_version(db, provider)
    _version_cache.clear()
    _version_cache[key] = ver
    return ver


def _compute_data_version(db: Database, provider: str) -> str:
    row = db.query_one(
        "SELECT COUNT(*) n, MIN(b.session) lo, MAX(b.session) hi, ROUND(SUM(b.close), 4) sc,"
        " ROUND(SUM(COALESCE(b.open, 0)), 4) so, ROUND(SUM(b.volume), 0) sv"
        " FROM bars b JOIN instruments i ON i.id=b.instrument_id WHERE i.provider=?", (provider,))
    act = db.query_one(
        "SELECT COUNT(*) n, ROUND(SUM(COALESCE(ratio,0)) + SUM(COALESCE(amount,0)), 6) s FROM corporate_actions a"
        " JOIN instruments i ON i.id=a.instrument_id WHERE i.provider=?", (provider,))
    inst = db.query_one(
        "SELECT COUNT(*) n, GROUP_CONCAT(symbol || asset_type || COALESCE(delist_date,''), ',') s FROM"
        " (SELECT * FROM instruments WHERE provider=? ORDER BY symbol)", (provider,))
    mem = db.query_one(
        "SELECT COUNT(*) n, MAX(m.start_session) s, MAX(COALESCE(m.end_session, '')) e,"
        " SUM(CASE WHEN m.end_session IS NULL THEN 1 ELSE 0 END) open"
        " FROM universe_membership m JOIN instruments i ON i.id=m.instrument_id WHERE i.provider=?", (provider,))
    payload = json.dumps([provider, row, act, inst] + ([mem] if mem and mem["n"] else []), sort_keys=True, default=str)
    return f"{provider}-{row['hi'] or 'empty'}-{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


_panel_cache: dict[tuple[str, str], Panel] = {}
_panel_lock = threading.Lock()


def invalidate_panel_cache() -> None:
    with _panel_lock:
        _panel_cache.clear()
    _version_cache.clear()   # membership changes do not create an import record


def get_panel(db: Database, provider: str, calendar: TradingCalendar, benchmark: str | None) -> Panel:
    """Build (or reuse) the full panel for the current data version."""
    ver = data_version(db, provider)
    key = (provider, ver)
    with _panel_lock:
        if key in _panel_cache:
            return _panel_cache[key]
        inst, bars, acts = load_frames(db, provider)
        if bars.empty:
            raise ProviderError(f"No market data stored for provider '{provider}'. Run `npm run import-data` first.")
        membership = load_membership(db, provider)
        panel = build_panel(bars, acts, inst, calendar, benchmark, membership=membership if not membership.empty else None)
        if db.scalar("SELECT 1 FROM fundamental_facts WHERE provider=? LIMIT 1", (provider,)):
            from .fundamentals import PointInTimeFundamentals

            panel.fundamentals = PointInTimeFundamentals.load(db, provider)
        _panel_cache.clear()
        _panel_cache[key] = panel
        return panel


# ---------------------------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------------------------
def quality_report(db: Database, provider: MarketDataProvider, calendar: TradingCalendar, cfg_min_cov: float = 0.9,
                   now: datetime | None = None) -> dict:
    info = provider.info
    now = now or datetime.now(UTC)
    last_import = db.query_one("SELECT * FROM data_imports WHERE provider=? ORDER BY id DESC LIMIT 1", (info.key,))
    last_ok = db.query_one(
        "SELECT * FROM data_imports WHERE provider=? AND status='succeeded' ORDER BY id DESC LIMIT 1", (info.key,))
    stats = db.query_one(
        "SELECT MIN(b.session) lo, MAX(b.session) hi, COUNT(*) n FROM bars b JOIN instruments i ON i.id=b.instrument_id"
        " WHERE i.provider=?", (info.key,))
    warnings: list[dict] = []
    if not stats or not stats["n"]:
        return {
            "provider": info.to_dict(), "has_data": False, "latest_import": last_import, "latest_successful_import": last_ok,
            "coverage_start": None, "coverage_end": None, "expected_latest_session": None, "is_stale": True,
            "stale_reason": "No market data stored yet.", "instruments": 0, "universe_count": 0, "missing_bars": 0,
            "missing_opens": 0, "stale_symbols": [], "latest_session_coverage": 0.0,
            "warnings": [{"level": "error", "message": "No market data stored yet."}],
        }
    hi = stats["hi"]
    # Fixed test fixtures are judged against their own end date; live providers against the exchange calendar.
    expected = hi if info.is_demo else calendar.latest_completed_session(now)
    lag = 0
    if expected and hi < expected:
        lag = len(calendar.between(hi, expected)) - 1
    is_stale = lag > 0
    stale_reason = None
    if is_stale:
        stale_reason = (f"Latest stored session is {hi}; the latest completed NYSE session is {expected} "
                        f"({lag} session(s) behind). Rebalances whose signal session has no data are disabled.")
        warnings.append({"level": "warning", "message": stale_reason})

    inst = db.query(
        "SELECT i.symbol, i.asset_type, i.is_benchmark, i.active, i.delist_date, MIN(b.session) first, MAX(b.session) last,"
        " COUNT(b.session) n, SUM(CASE WHEN b.open IS NULL THEN 1 ELSE 0 END) no_open"
        " FROM instruments i LEFT JOIN bars b ON b.instrument_id=i.id WHERE i.provider=? GROUP BY i.id", (info.key,))
    missing_bars = 0
    stale_symbols = []
    for r in inst:
        if not r["first"]:
            continue
        end = r["delist_date"] or r["last"]
        expected_n = len(calendar.between(r["first"], max(end, r["last"])))
        missing_bars += max(expected_n - r["n"], 0)
        if r["delist_date"] is None and r["last"] < hi:
            stale_symbols.append({"symbol": r["symbol"], "last_bar": r["last"]})
    universe = sum(1 for r in inst if r["asset_type"] == "common_stock" and not r["is_benchmark"]
                   and r["first"] and r["first"] <= hi and (r["delist_date"] is None or r["delist_date"] >= hi))
    with_bar_latest = db.scalar(
        "SELECT COUNT(*) FROM bars b JOIN instruments i ON i.id=b.instrument_id WHERE i.provider=? AND b.session=?"
        " AND i.asset_type='common_stock' AND i.is_benchmark=0", (info.key, hi))
    coverage = (with_bar_latest / universe) if universe else 0.0
    if coverage < cfg_min_cov:
        warnings.append({"level": "warning",
                         "message": f"Only {coverage:.0%} of the universe has a bar on {hi} (threshold {cfg_min_cov:.0%})."})
    if stale_symbols:
        warnings.append({"level": "info", "message": f"{len(stale_symbols)} active symbols have no bar on {hi}."})
    if not info.point_in_time_universe:
        warnings.append({"level": "error", "message": "SURVIVORSHIP BIAS: " + info.survivorship_note})
    if last_import and last_import["status"] == "failed":
        warnings.append({"level": "error", "message": f"Latest import #{last_import['id']} failed: {last_import['error']}"})
    for w in json.loads(last_ok["warnings_json"]) if last_ok else []:
        warnings.append({"level": "info", "message": f"Import #{last_ok['id']}: {w}"})
    bench_row = next((r for r in inst if r["is_benchmark"]), None)
    return {
        "provider": info.to_dict(),
        "has_data": True,
        "latest_import": last_import,
        "latest_successful_import": last_ok,
        "coverage_start": stats["lo"],
        "coverage_end": hi,
        "expected_latest_session": expected,
        "is_stale": is_stale,
        "stale_reason": stale_reason,
        "instruments": len(inst),
        "universe_count": universe,
        "total_bars": stats["n"],
        "missing_bars": missing_bars,
        "missing_opens": int(sum(r["no_open"] or 0 for r in inst)),
        "stale_symbols": stale_symbols[:200],
        "latest_session_coverage": coverage,
        "benchmark_symbol": bench_row["symbol"] if bench_row else None,
        "warnings": warnings,
    }
