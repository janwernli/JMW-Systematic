# JMW Systematic: long-short momentum with Alpaca paper trading

This is a locally hosted research terminal and **fully automated paper-trading system** for one strategy: a US-equities, long-short, beta- and sector-neutral cross-sectional momentum strategy.

- **Research.** Event-ordered monthly backtests of a composite momentum signal (plain 12–1 is selectable for comparison), with a Fama-French 5 + momentum alpha test.
- **Paper trading.** A daily automation job trades your **Alpaca PAPER account** (no real money). That account is the source of truth for the traded portfolio.
- **Model track.** An internal model ledger simulates the same strategy with theoretical fills. It is identical to the backtest to the cent, and the Alpaca account is compared against it.

> **Paper trading only.** The broker adapter refuses any host other than `paper-api.alpaca.markets`, so nothing here can trade real money.
> Historical results on Alpaca data are **survivorship-biased** (see [Limitations](#10-limitations)). This is not investment advice.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Install](#2-install)
3. [Configure Alpaca and SEC](#3-configure-alpaca-and-sec)
4. [Import data](#4-import-data)
5. [Start the app and open the dashboard](#5-start-the-app-and-open-the-dashboard)
6. [Automated paper trading](#6-automated-paper-trading)
7. [Research: backtests, comparison, alpha test](#7-research-backtests-comparison-alpha-test)
8. [Strategy rules](#8-strategy-rules)
9. [Execution, accounting and metrics](#9-execution-accounting-and-metrics)
10. [Limitations](#10-limitations)
11. [Norgate Data (point-in-time Russell 1000)](#11-norgate-data-point-in-time-russell-1000)
12. [Architecture and data flow](#12-architecture-and-data-flow)
13. [Tests](#13-tests)
14. [Git and GitHub](#14-git-and-github)
15. [Configuration reference](#15-configuration-reference)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Quick start

```bash
npm run setup              # once: Python venv + packages, frontend packages, creates .env
# edit .env: Alpaca keys, SEC_USER_AGENT (see section 3)
npm run import-data        # first import: universe, ~10 years of daily bars, splits/dividends, SEC sectors
npm run daily:dry          # dry run of the daily cycle against your paper account (sends nothing)
npm run schedule:install   # Windows: run the cycle automatically at 14:00 and 23:30 every day
npm run start              # dashboard at http://127.0.0.1:8765
```

## 2. Install

You need three programs:

- **Python 3.12** (3.11+ works): https://www.python.org/downloads/
- **Node.js 24 LTS** (20+ works): https://nodejs.org/. Use the **ARM64** installer on Windows-on-ARM PCs (e.g. Snapdragon).
- **Git**

On **Windows (PowerShell)**, npm scripts are blocked by default. Allow them once, or type `npm.cmd` instead of `npm`:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
cd "C:\Coding\JMW Trading Strategy"
npm run setup
```

On **macOS / Linux**, run `npm run setup`. On Ubuntu you may first need `sudo apt install python3-venv`.

`npm run setup` does the following:

1. Creates `backend/.venv`.
2. Installs the pinned Python packages (`backend/requirements*.txt`, including SciPy for the sector-neutral optimizer).
3. Installs the pinned frontend packages.
4. Copies `.env.example` to `.env` if it doesn't exist.

## 3. Configure Alpaca and SEC

Edit `.env` in the project root. The file is git-ignored and never committed.

```ini
MARKET_DATA_PROVIDER=alpaca
ALPACA_API_KEY_ID=...            # Alpaca paper-trading keys (dashboard -> API keys). Also used for market data.
ALPACA_API_SECRET_KEY=...
ALPACA_DATA_FEED=sip             # consolidated volume (free plan: SIP history except the last 15 minutes)
SEC_USER_AGENT=YourName research you@example.com   # SEC requires a name + contact e-mail
BROKER_TRADING_ENABLED=true      # master switch; false = orders are computed but never sent
```

The keys stay on the local server and are never sent to the browser. If you ever paste your keys somewhere public, including a chat, regenerate them in the Alpaca dashboard and update `.env`.

## 4. Import data

```bash
npm run import-data          # incremental refresh after the first import
npm run import-data -- --full # re-download the full history for the frozen universe
npm run sectors              # re-classify all sectors from SEC EDGAR
```

**The first import:**

- selects the universe: the 600 most liquid active US common stocks by recent dollar volume, or `ALPACA_UNIVERSE_FILE`
- adds SPY and the 11 SPDR sector ETFs (XLB, XLC, XLE, XLF, XLI, XLK, XLP, XLRE, XLU, XLV, XLY) as reference series
- downloads raw daily bars from 2016 plus splits and cash dividends
- classifies sectors from **SEC EDGAR SIC codes**

**After that, the universe is frozen.** Refreshes only re-fetch the last 10 sessions for the same symbols. Requests always end at the latest *completed* NYSE session, so a half-finished day is never stored.

**Classifying common stock.** Alpaca has no field for this, so a documented name/symbol heuristic excludes ETFs/ETNs, funds and trusts, notes, preferreds, ADRs, SPACs, warrants, units and rights.

## 5. Start the app and open the dashboard

```bash
npm run start      # builds the frontend, serves everything at http://127.0.0.1:8765
npm run dev        # development: backend :8765 + live-reloading frontend at http://127.0.0.1:5173
```

Both bind to localhost only. Stop with **Ctrl+C**.

| Screen | What it shows |
|---|---|
| **Command Center** | Alpaca paper equity and automation status strip. Model portfolio KPIs, equity/drawdown curves, data status, alerts. |
| **Alpaca Paper** | Automation switch, **Dry run now** / **Run cycle now**, account, equity (Alpaca vs model vs SPY), actual positions vs model targets with drift, run log, every order sent to Alpaca with its status. |
| **Universe & Rankings** | Every stock with its composite score, residual momentum, 12–1, FIP, sector, vol, beta, eligibility and long/short book. A stock drawer shows signal inputs and price charts. |
| **Model Portfolio** | The internal model ledger's positions, drift, sector exposure and concentration. |
| **Rebalance Desk** | Frozen month-end plans: targets, estimated orders, cash reconciliation, rule checks, and sizing diagnostics (gross, betas, ex-ante vol, sector-neutral nets, hard-to-borrow screen, crash guard). |
| **Research Lab** | Backtest settings (composite or plain 12-1), live config warnings, results, exposure, **Factor alpha** tab, monthly heatmap, trades, rebalance log, run comparison. |
| **Ledger & Diagnostics** | Model ledger orders, fills and cash; audit trail; data quality and ingestion; reproducibility. |

## 6. Automated paper trading

### What the daily cycle does (`npm run daily`)

The cycle is idempotent, so running it several times a day is safe.

1. **Data.** Refreshes end-of-day bars and fills in missing SEC sectors. If the data doesn't reach the latest completed NYSE session, the cycle **sends no orders**.
2. **Sync.** Pulls the Alpaca account, positions, order statuses and daily equity history.
3. **Model.** Advances the internal model ledger and automatically applies its month-end plan.
4. **Stops.** Checks each **actual** short position. If its close is at least 1.5 × its average entry price, a cover order is placed for the next open.
5. **Rebalance.** For up to 5 sessions after a month-end signal, reconciles the **actual** positions to the frozen targets:
   - Order size is `trunc(target weight × account equity ÷ signal close)`.
   - A long-to-short flip is split: close now, open at the next run. Alpaca rejects flipping a position in one order.
   - New shorts require Alpaca's `shortable` and `easy_to_borrow` flags; otherwise they are skipped and recorded.
   - Orders that expired unfilled are retried.
6. **Submit.** Orders are sent only when **all** of these hold: `BROKER_TRADING_ENABLED=true`, the dashboard's automation switch is ON, the data is fresh, and the run is not a dry run.
   - Between **19:00 and 09:28 ET** they are sent as market-on-open orders (opening auction).
   - If that window was missed but the fill session is trading, they are sent as a regular market order, flagged as late.
   - Otherwise they stay planned for the next run.
   - Every order carries a deterministic `client_order_id`, so it can never be sent twice.

### Schedule (Windows)

```bash
npm run schedule:install   # registers two tasks for your user
npm run schedule:remove    # removes them
```

| Task | Local time (Europe/Zurich) | New York time | Purpose |
|---|---|---|---|
| `JMW-Systematic daily 1400` | 14:00 | 08:00 ET (09:00 during the few DST-mismatch weeks) | Stops, month-end rebalance, opening-auction orders |
| `JMW-Systematic sync 2330` | 23:30 | 17:30 ET | Syncs fills and equity after the close. No orders: outside the auction window. |

- The tasks run while you are logged on. **StartWhenAvailable** catches up after sleep.
- Output goes to `logs/daily.log`.
- **The PC must be on around 14:00** on the first trading day of each month. If it wakes after 09:28 ET, orders go out as late market orders.
- On macOS/Linux, use cron instead:

  ```
  0 14 * * 1-5  cd /path/to/repo && node scripts/py.mjs -m app daily --trigger schedule >> logs/daily.log 2>&1
  30 23 * * 1-5 cd /path/to/repo && node scripts/py.mjs -m app daily --trigger schedule >> logs/daily.log 2>&1
  ```

### Month-end timeline (example: September → October 2026)

| When | What happens |
|---|---|
| **Wed 30 Sep, 16:00 ET** | Close of the last session of September. That close is the signal. |
| **Wed 30 Sep, 23:30** | Sync only. The signal is not processed yet: bars are only trusted one hour after the close. |
| **Thu 1 Oct, 14:00 (08:00 ET)** | Refreshes data, freezes the signal, sizes orders from account equity and submits market-on-open orders. |
| **Thu 1 Oct, 09:30 ET** | Orders fill in Alpaca's opening auction. |
| **Thu 1 Oct, 23:30** | Syncs fills. The model ledger fills at the same open ± assumed slippage. |

### Safety switches

- `BROKER_TRADING_ENABLED` in `.env`: the master switch.
- **Automation** switch on the Alpaca Paper page: a kill switch that takes effect immediately. It does not cancel orders already at Alpaca; cancel those in the Alpaca dashboard.
- The adapter only accepts the paper host. There is no code path to a live-money account.

## 7. Research: backtests, comparison, alpha test

In the **Research Lab**:

1. Pick **Composite signal** or **Plain 12-1**. Adjust books, sizing, sector neutrality, the hard-to-borrow screen and short-risk parameters.
2. Click **Run backtest**. Tick 2–4 saved runs to compare them.
3. Open the **Factor alpha** tab. It regresses the run's monthly returns minus RF on the **Fama-French 5 factors (2×3) + momentum**:
   - Factors come from the Kenneth R. French Data Library, downloaded and cached weekly under `data/factors/`.
   - Results show alpha (monthly × 12), **Newey-West t-stats** (Bartlett kernel, lag = ⌊4·(T/100)^(2/9)⌋), all factor betas and R².
   - This is reported for the full sample and for each half (each half needs at least 24 months).
   - The first month is dropped because it is partial. Months not yet published by the library are excluded and noted.

**Results on the current Alpaca data (2017-01 → 2026-09, default settings).** These are illustrations only; see Limitations.

| | Composite | Plain 12-1 |
|---|---|---|
| Net CAGR / vol / max DD | 4.1% / 8.3% / −14.1% | 3.5% / 8.3% / −12.4% |
| Realized beta to SPY | 0.00 | −0.01 |
| Alpha (FF5 + Mom), full sample | +0.7%/yr, t = 0.5 | +0.1%/yr, t = 0.1 |
| Alpha, 1st half / 2nd half | −3.3% (t −2.1) / +4.8% (t 2.5) | −2.8% (t −1.4) / +2.7% (t 1.2) |
| Momentum-factor loading | 0.34 (t 7) | 0.35 (t 7) |

## 8. Strategy rules

These are the defaults; everything is adjustable in the Research Lab. The live/model copy is changed under **Rebalance Desk → Paper config** and applies to future plans only.

| Rule | Definition |
|---|---|
| Universe | Point-in-time US common stocks (listed at t, not yet delisted). Raw close > $5, 20-session average dollar volume ≥ $5M, at least 252 valid bars, valid bars at t, t−21 and t−252. With Norgate, the stock must also be a Russell 1000 member at t. |
| Timing | Signals are computed and frozen after the close of the last NYSE session of each month. Only data up to t is used. Fills happen at the next session's open. |
| **Signal (composite)** | 0.60 · z(residual momentum) + 0.25 · z(sector-demeaned 12–1) + 0.15 · z(frog-in-the-pan). Each component is winsorized at mean ± 3σ and then z-scored across eligible stocks. If a component is missing for a stock, its weight is dropped. |
| Residual momentum | Monthly total returns. Over the last 36 months, regress r = a + b_m·r_market + b_s·r_sectorETF + e (market only if the sector ETF lacks history, e.g. XLC before 2018-06). Score = Σ e over months t−11…t−1 ÷ sd(those residuals). The intercept absorbs a constant drift, so only a *recent* idiosyncratic trend scores. |
| Sector-demeaned 12–1 | TR(t−21)/TR(t−252) − 1 minus the mean of eligible stocks in the same sector |
| Frog-in-the-pan | ID = sgn(PRET)·(%neg − %pos days) over the 12–1 window; the score is sgn(PRET)·(−ID) = %up days − %down days. Smooth winners rank high and smooth losers rank low. |
| Plain 12–1 (selectable) | TR(t−21)/TR(t−252) − 1 on NYSE session offsets |
| Books | Long the top 10% and short the bottom 10% of eligible stocks by score, 50–100 names per side. Buffer: held names stay while in the top/bottom 30%. |
| Short eligibility | Raw close > $10. Not stopped out since the last signal. Not in the **bottom 20% of eligible stocks by 60-session dollar volume** (hard-to-borrow stand-in). Live orders also require Alpaca's easy-to-borrow flag. |
| Weights | 1 / realized vol (126 sessions), capped at 2% per long and **1.5% per short**. Excess is redistributed. |
| Beta neutral | Short gross = long gross × β_long / β_short. Betas use 252 sessions vs SPY, shrunk 33% toward 1 (β = 1 if history is too short). |
| Vol target | 10% ex-ante (trailing 126-session returns of the proposed book). Bounded to 50–75% gross per side and 150% total. |
| **Sector neutral** | \|long − short\| ≤ 2% of NAV per sector (11 sectors from SEC SIC). A quadratic program (SciPy SLSQP, with an exact LP feasibility check) stays as close as possible to the inverse-vol weights while keeping gross, beta neutrality and caps. If caps make that impossible, **gross is reduced** and reported. Unclassified stocks are unconstrained. |
| Crash guard | If SPY's 24-month return < 0 and its 6-month realized vol > 20%, the short book is multiplied by 0.5. |
| Costs (assumed) | 10 bps slippage, $0 commission. **Borrow fee 0.5%/yr accrued per calendar day (ACT/360)**; Monday pays for the weekend. Short proceeds earn 0%. |
| Stop-loss | Short close ≥ 1.5 × average entry → cover at the next open. No re-short until the next signal. |
| **Capacity warning** | If `min_names_per_side × max_short_weight < max_side_gross` (or the long equivalent), the config emits a `ConfigWarning`. The warning is shown in the UI, plan checks and run diagnostics: the per-name caps can prevent the vol target from being reached. |

## 9. Execution, accounting and metrics

**Event order each session** (identical in the backtest and the model ledger; a test verifies they match to the cent):

1. Pre-open: splits and dividends. Shorts pay dividends and split fractions.
2. Open: stop-loss covers, then the rebalance. Order of trades: reduce longs → short sales → covers → buys in rank order.
3. Close: delisting close-outs.
4. Close: borrow fee (ACT/360), then mark to market. NAV = cash + long value + short value, where short value is negative.
5. Close: stop-loss checks.
6. Month-end: freeze the signal and create the plan.

**Accounting details:**

- Whole shares only.
- Signed average cost basis: longs record cash paid; shorts record −(net proceeds).
- Valuation uses **raw** prices, and dividends go through cash, so they are never double counted.

**Metrics:**

| Metric | Definition |
|---|---|
| CAGR | Only shown when the sample covers at least 365 days |
| Volatility | sd(daily returns) × √252 |
| Max drawdown | Largest peak-to-trough fall in NAV |
| Turnover | (buys + sells) / 2 / NAV at the open |
| Gross / net exposure | (long value + \|short value\|) / NAV and (long − \|short\|) / NAV |
| Realized beta | cov(strategy, SPY) / var(SPY) over the backtest |
| Gross NAV | Net NAV + cumulative slippage, commission and borrow fees |
| Return difference vs SPY | A simple difference, not alpha. The alpha test is the factor regression in section 7. |

## 10. Limitations

- **Alpaca data is not point-in-time.** The universe is *today's* 600 most liquid stocks, and delisted names are missing.
  - Long-winner backtests are strongly inflated: an earlier long-only run on this data showed +2,798%.
  - The short book is biased the other way, because only losers that survived are in the data.
  - Treat all historical results on this data as illustrations. Use Norgate (section 11) for an unbiased study.
- **SEC SIC sectors** are current codes, not point-in-time. The SIC → sector mapping is approximate; for example, SIC 7370 puts Alphabet and Meta in Information Technology.
- **The hard-to-borrow screen** in backtests is a liquidity stand-in. Real borrow availability, recalls, locates and hard-to-borrow fees are not modelled. Live orders check Alpaca's easy-to-borrow flag instead.
- **Costs are assumptions.** Opening-auction liquidity, market impact and interest on short proceeds are not modelled.
- **Sizes are small.** With $100k and 50–100 names per side, many positions are only a few shares, so rounding noise is noticeable.
- **Stops are checked on daily closes** and fill at the next open, which can gap past the stop.
- **Automation depends on this PC being on** at the scheduled times. Missed windows fall back to late market orders or the next run.
- **The factor data lags** by about 1–2 months, and the most recent months are excluded from the alpha test.

## 11. Norgate Data (point-in-time Russell 1000)

`MARKET_DATA_PROVIDER=norgate` switches to `NorgateProvider`. It requires:

- a Norgate Data subscription that includes historical index constituents
- the Norgate Data Updater running on Windows
- `pip install norgatedata` in `backend/.venv`

What it provides:

- The **Russell 1000 Current & Past** watchlist, including delisted stocks.
- Unadjusted bars. Splits are derived from capital-adjusted vs unadjusted closes, and dividends come from the Dividend column.
- **Point-in-time index membership intervals**, which drive eligibility (`not_in_index`). The survivorship warning disappears.

Tell the scheduler which provider to trade on by keeping `MARKET_DATA_PROVIDER` set. Paper orders still go to Alpaca, and symbols are matched by ticker.

**Status:** implemented against Norgate's documented Python API and unit-tested with a simulated `norgatedata` module. **It has not been run against a real Norgate installation**, because none was available.

## 12. Architecture and data flow

```
 Alpaca Market Data / Norgate ──► data/ (provider, store, panel)      SEC EDGAR ──► data/sectors.py
                                         │                             Ken French ──► backtest/factors.py
                                         ▼
          strategy/  composite.py · signals.py · long_short.py (books, sizing, sector QP) · execution.py
               │                                   │
               ▼                                   ▼
   backtest/engine.py + runner.py         ledger/paper.py (model ledger)
               │                                   │
               └──────────► automation.py ◄────────┘──► broker/alpaca_paper.py ──► Alpaca PAPER account
                                   │
                    SQLite data/momentum.db (migrations, append-only ledger triggers)
                                   │
            FastAPI api/ ──► React + TypeScript dashboard (Mantine, ECharts, TanStack)
```

The tables that were added for this version:

- `universe_membership`: point-in-time index intervals.
- `broker_orders`: every order with its client id, status, fills and reason.
- `broker_equity`, `broker_positions`, `broker_account_snapshots`: the Alpaca mirror.
- `automation_runs`: a step-by-step log of each cycle.
- `app_settings`: the automation switch.
- Signal rows now also store the composite components, sector and 60-day ADV.

## 13. Tests

```bash
npm test              # backend pytest suite + frontend type-check
npm run test:backend
```

There are 127 backend tests, built on constructed datasets with hand-checkable results. Beyond the earlier coverage (lookbacks, no look-ahead, calendar, splits/dividends, whole-share cash, costs, restart persistence, model ledger = backtest), they cover:

- **Composite signal:** winsorizing, the FIP arithmetic, residual momentum rewarding recent idiosyncratic drift but not beta, sector demeaning, and ranking by composite vs 12–1.
- **Books and sizing:** the hard-to-borrow screen, the sector-neutral QP (limits, gross, beta, caps, and the reduced-gross fallback), the capacity warning, and loading of legacy configs.
- **Data sources:** the SIC → sector mapping, the SEC client (User-Agent, and sectors surviving a refresh), and Norgate's split/dividend/membership/delisting mapping.
- **Costs:** ACT/360 borrow fees.
- **Alpha test:** Ken French parsing and Newey-West (matches OLS and White's estimator at lag 0), plus the full/half-sample alpha test.
- **Automation:** opening-auction submission sized from equity, idempotency, dry run, both kill switches, stale-data blocking, stop-loss on actual shorts, flip splitting, the submission-window rules, and the paper-host lock.

## 14. Git and GitHub

The repository is pushed to the private GitHub repo `janwernli/JMW-Systematic` (`origin`).

```bash
git add -A && git commit -m "Describe the change" && git push
```

`.gitignore` excludes the following, so they are never committed: `.env` (keys), `data/` (database, factor cache), `logs/`, virtual environments, `node_modules` and build output.

## 15. Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8765` | Local server |
| `DATABASE_PATH` | `./data/momentum.db` | SQLite file |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Logging |
| `MARKET_DATA_PROVIDER` | `alpaca` | `alpaca` or `norgate` |
| `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` | — | Paper keys (data + trading) |
| `ALPACA_DATA_FEED` | `sip` | `sip` or `iex` |
| `ALPACA_HISTORY_START` / `ALPACA_MAX_SYMBOLS` / `ALPACA_UNIVERSE_FILE` | `2016-01-01` / `600` / — | First-import universe |
| `ALPACA_PAPER_TRADING_URL` | `https://paper-api.alpaca.markets` | Any other host is refused |
| `BROKER_TRADING_ENABLED` | `false` | Master switch for sending paper orders |
| `SEC_USER_AGENT` | — | "Name contact@email" (SEC requirement) |
| `NORGATE_INDEX` / `NORGATE_HISTORY_START` | `Russell 1000` / `2000-01-01` | Norgate provider |

## 16. Troubleshooting

| Symptom | Fix |
|---|---|
| `npm.ps1 cannot be loaded` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `npm.cmd`. |
| Port 8765 in use | Set `APP_PORT=8766` in `.env`. |
| Automation run says **STALE** | The data doesn't reach the latest completed session yet. It usually resolves at the next run; check `logs/daily.log`. |
| Order `rejected` | Its reason is shown on the Alpaca Paper page, e.g. a market-on-open order outside 19:00–09:28 ET or insufficient buying power. |
| Short `skipped` | Not easy-to-borrow at Alpaca at order time. It is recorded and retried at the next signal. |
| `SEC_USER_AGENT must be set` | Add a name and e-mail to `.env`, then run `npm run sectors`. |
| Norgate error | Install the Norgate Data Updater and run `pip install norgatedata` in `backend/.venv`. |
