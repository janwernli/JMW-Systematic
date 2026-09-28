# Momentum Terminal

A locally hosted **US-equities cross-sectional momentum** research terminal and **internal paper simulator**.

- Historical **backtests** of one precisely defined strategy (12–1 month momentum, top 50, equal weight, monthly).
- A forward-running **internal virtual portfolio** funded with $100,000 of simulated cash.
- Research and operational dashboards (six screens).
- A market-data adapter interface (demo fixture built in, Alpaca optional). **No broker orders are ever placed.**

> **Not investment advice. No real money, no brokerage connection, no live orders.**
> Every number in the UI carries one of these labels: **Demo Data**, **Delayed Market Data**, **Backtest**, **Paper Simulation**.
> The built-in dataset is **synthetic**: fictional tickers that contain a digit (e.g. `KUGU7`), which no real US common stock uses.
> Its results say nothing about real-world performance.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Install dependencies](#2-install-dependencies)
3. [Start the backend and frontend](#3-start-the-backend-and-frontend)
4. [Open the dashboard](#4-open-the-dashboard)
5. [Load the demo and run a backtest](#5-load-the-demo-and-run-a-backtest)
6. [Initialize and advance the virtual portfolio](#6-initialize-and-advance-the-virtual-portfolio)
7. [Add a market-data key later (Alpaca)](#7-add-a-market-data-key-later-alpaca)
8. [Run the tests](#8-run-the-tests)
9. [Commit and push to a private GitHub repository](#9-commit-and-push-to-a-private-github-repository)
10. [Architecture](#10-architecture)
11. [Database and data flow](#11-database-and-data-flow)
12. [Strategy rules (v1)](#12-strategy-rules-v1)
13. [Execution, accounting and metrics](#13-execution-accounting-and-metrics)
14. [Limitations of the data and the backtest](#14-limitations-of-the-data-and-the-backtest)
15. [Roadmap: optional broker paper-account integration](#15-roadmap-optional-broker-paper-account-integration)
16. [Configuration reference](#16-configuration-reference)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Quick start

```bash
npm run setup      # once: Python venv + packages, frontend packages, creates .env (demo mode)
npm run dev        # starts backend (127.0.0.1:8765) and frontend (127.0.0.1:5173)
```

Open **http://127.0.0.1:5173**. The first start builds the demo database, which takes about 20–30 seconds.
The details for each step follow.

## 2. Install dependencies

You need three programs. Install each one once.

| Tool | Version | Where |
|---|---|---|
| **Python** | 3.12 (3.11+ works) | https://www.python.org/downloads/ |
| **Node.js** | 24 LTS (20+ works) | https://nodejs.org/ – choose the **LTS** installer. On Windows-on-ARM PCs (e.g. Snapdragon), pick the **ARM64** `.msi`. |
| **Git** | any recent | https://git-scm.com/downloads |

Docker, cloud accounts and paid data are not needed for the demo.

### Windows (PowerShell)

1. During the Python installation, tick **"Add python.exe to PATH"**.
2. Windows PowerShell blocks `npm` scripts by default. Allow them once for your user:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```
   Alternatively, type `npm.cmd` instead of `npm` in every command below.
3. In the project folder, run:
   ```powershell
   cd "C:\Coding\JMW Trading Strategy"
   npm run setup
   ```

### macOS / Linux (Terminal)

```bash
cd ~/path/to/momentum-terminal
npm run setup
```

On Ubuntu/Debian you may first need `sudo apt install python3-venv`.

### What `npm run setup` does

It runs the same steps on every operating system (see `scripts/setup.mjs`):

1. Creates `backend/.venv` with Python 3.11+.
2. Runs `pip install -r backend/requirements-dev.txt`. Versions are pinned in `backend/requirements.txt`.
3. Runs `npm install` in `frontend/`. Versions are pinned in `frontend/package.json` and the lockfile.
4. Copies `.env.example` to `.env` if `.env` does not exist yet. The default is demo mode, with no keys needed.

<details><summary>Manual setup without the helper</summary>

```bash
# backend
cd backend
python -m venv .venv                       # Windows: py -3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt          # Windows: .venv\Scripts\pip install -r requirements-dev.txt
# frontend
cd ../frontend && npm install
# config
cd .. && cp .env.example .env              # Windows: copy .env.example .env
```
</details>

## 3. Start the backend and frontend

```bash
npm run dev
```

This starts both processes, and **Ctrl+C** stops both:

| Process | Address | Notes |
|---|---|---|
| FastAPI backend | http://127.0.0.1:8765 (`/api/...`) | Logs are structured: JSON by default, or `LOG_FORMAT=text`. |
| Vite frontend | http://127.0.0.1:5173 | Proxies `/api` to the backend. |

Both processes bind to **localhost only**, so nothing is reachable from other computers.

To run the two processes separately, use two terminals:

```bash
npm run dev:api     # backend only
npm run dev:web     # frontend only
```

To run a single-port production build, use:

```bash
npm run start       # builds the frontend, then FastAPI serves it at http://127.0.0.1:8765
```

## 4. Open the dashboard

Go to **http://127.0.0.1:5173** (or http://127.0.0.1:8765 after `npm run start`).
A striped **DEMO DATA** ribbon at the top confirms you are looking at the synthetic dataset.

| Screen | What it shows |
|---|---|
| **Command Center** | Virtual NAV, cash, day and inception return, benchmark comparison, drawdown, next rebalance, data status, equity and drawdown curves, top movers, recent fills, alerts, market/session clock |
| **Universe & Rankings** | Searchable, sortable universe with price, ADV, 12–1 momentum, rank, eligibility reason, position, and current / model / plan weights. Click a row to open the stock drawer: signal inputs, TR-index chart, raw price chart, corporate actions. |
| **Portfolio** | Positions, marks with stale flags, weights, drift from target, contribution, sector exposure (only where metadata is reliable), concentration |
| **Rebalance Desk** | Frozen signal timestamp, target portfolio, current vs target weights, proposed sells and buys, estimated turnover and costs, cash reconciliation, rule checks, and the **Apply to internal virtual portfolio only** button |
| **Research Lab** | Backtest settings, launch and progress, equity, drawdown, rolling metrics, monthly heatmap, trades, rebalance log, assumptions, reproducibility, and comparison of up to 4 saved runs |
| **Ledger & Diagnostics** | Every simulated order, fill and cash event, the audit trail, data quality and ingestion history, reproducibility metadata, and portfolio administration |

Every panel has an ⓘ tooltip. It explains where the values come from, their units and timestamps, and the formulas used.

## 5. Load the demo and run a backtest

The demo loads **automatically on first start** (`DEMO_AUTOSEED=true`). The seed does three things:

1. It imports the deterministic synthetic dataset:
   - 140 fictional common stocks, 8 ETFs, funds and preferreds that the universe filter must exclude, and a synthetic benchmark `MKT0` that stands in for SPY.
   - Real NYSE sessions from 2016-01-04 to 2026-09-25.
   - Splits, reverse splits, dividends, IPOs, delistings, trading halts and missing opening prints.
2. It runs one default backtest ("Default v1 (demo seed)").
3. It creates a demo virtual portfolio funded at the close of 2025-12-31.
   - Its monthly plans through July 2026 are applied automatically, and this is labelled in the audit log.
   - **The 2026-08-31 plan is left waiting for your decision.**

To run your own backtest:

1. Open **Research Lab**.
2. Adjust the start and end dates, holdings (top N), minimum ADV, minimum price, slippage (bps), commission and initial capital.
3. Click **Run backtest**. A progress bar shows while it runs, which takes a few seconds on the demo.
4. The result tabs are: Equity & drawdown · Rolling metrics · Monthly returns · Trades · Rebalances · Assumptions & reproducibility.
5. To compare runs, tick the boxes of 2–4 saved runs.

Every run stores its full config, the data version (a content fingerprint), the provider, its results, timestamps, and package and git versions.
The same config on the same data version reproduces identical numbers; a test checks this.

## 6. Initialize and advance the virtual portfolio

The virtual portfolio lives in the local SQLite database (`data/momentum.db`) and **persists across restarts**.

**In the UI (demo walkthrough):**

1. **Rebalance Desk** shows plan #9 with signal 2026-08-31 and fill 2026-09-01, status *proposed*.
   Review the rule checks, the cash reconciliation and the proposed orders.
2. Click **Apply to internal virtual portfolio only**, tick the confirmation, and apply.
   A plan can be applied **once**; the database rejects a second application.
   Alternatively, **Skip** the plan; the decision is recorded.
3. Click **Advance to latest data** (on the Command Center or Rebalance Desk).
   - The portfolio processes each session in order: pre-open corporate actions, then fills at the open, then close valuation, then the month-end signal.
   - It **stops automatically** whenever a new month-end plan needs your decision.
   - **Advance 1 session** steps a single day.
4. The demo data ends on 2026-09-25, so advancing stops there with "no newer data".

To start over, open **Ledger & Diagnostics → Portfolio admin**. You can archive the portfolio (its history is kept) and re-create the demo portfolio.
To initialize a new portfolio with your own capital and inception session, use the form on the Command Center when no portfolio exists.

**From the command line** (the backend server may be running or stopped):

```bash
npm run advance                         # advance through all stored sessions (stops at decisions)
npm run advance -- --until 2026-09-15   # advance up to a given session
```

Changing strategy settings in **Research Lab** never touches the paper portfolio.
The paper portfolio has its own config, edited via **Rebalance Desk → Paper config**.
Each edit creates a new config version that applies to **future plans only**.
Fills, cash transactions, NAV rows and position snapshots are append-only; SQLite triggers reject updates and deletes.

## 7. Add a market-data key later (Alpaca)

The app ships one real-data adapter: **Alpaca Market Data**. It is isolated behind the `MarketDataProvider` interface in `backend/app/data/provider.py`.
It only *reads* data: bars, corporate actions and the asset list. It never calls an order or account endpoint.

1. Create a free account at https://alpaca.markets and generate **API keys** in the dashboard.
   Paper-trading keys are fine; they are only used to read data here.
2. Edit `.env` in the project root. Keys stay on the server and are never sent to the browser.
   ```ini
   MARKET_DATA_PROVIDER=alpaca
   ALPACA_API_KEY_ID=your-key-id
   ALPACA_API_SECRET_KEY=your-secret
   ALPACA_DATA_FEED=sip
   ALPACA_HISTORY_START=2016-01-01
   # optional: one symbol per line; otherwise the top ALPACA_MAX_SYMBOLS by *recent* dollar volume
   # ALPACA_UNIVERSE_FILE=./universe.txt
   ALPACA_MAX_SYMBOLS=600
   ```
3. Import the history. This takes several minutes for hundreds of symbols because of rate limits.
   ```bash
   npm run import-data
   ```
4. Restart `npm run dev`.
   - The UI switches to **Delayed Market Data** and shows a red **SURVIVORSHIP BIAS** ribbon.
   - Initialize a new virtual portfolio from the Command Center. Demo and Alpaca portfolios are kept separately.
5. After each trading day, click **Refresh data** (Command Center, visible in live mode) or run `npm run import-data`, then **Advance**.

**What Alpaca provides.** These are per Alpaca's public docs, so verify them for your plan because they can change:

| Item | Details |
|---|---|
| Endpoints | `GET /v2/stocks/bars` (raw daily bars, multi-symbol, paginated via `next_page_token`) · `GET /v1/corporate-actions` (splits, reverse splits, cash dividends by ex-date) · `GET /v2/assets` (asset list) |
| Historical coverage | Stock bars from 2016 onward |
| Free plan | About 200 requests/min. SIP (consolidated) historical data except the latest 15 minutes. The IEX feed covers only a small share of volume, which would break the dollar-volume filter, so **SIP is the default**. |
| Adjustments | The app requests `adjustment=raw` and applies splits and dividends itself from the corporate-actions endpoint |
| Rate limits | The adapter retries on HTTP 429 using the `X-RateLimit-Reset` header, and retries 5xx and network errors with exponential backoff |
| Timestamps | Daily bars are mapped to the New York session date |
| Missing features | No ETF/share-class field: common stocks are identified by a documented **name/symbol heuristic**. No sector data: sector exposure is hidden. Not a point-in-time security master: delisted names are largely missing. |

**Consequence:** backtests on Alpaca data are **survivorship-biased**, and the UI says so everywhere.
An unbiased whole-market study needs a point-in-time dataset with delisted securities and historical membership (for example CRSP, Norgate, or Sharadar via Nasdaq Data Link).
Any such source can be added by implementing the four methods of `MarketDataProvider`.

## 8. Run the tests

```bash
npm test                 # backend pytest suite + frontend type-check
npm run test:backend     # backend only
npm run build            # production frontend build (tsc + vite)
```

The suite has 59 tests, built on small constructed datasets with hand-computed expected numbers. It covers:

- `test_signals.py`: the exact 12–1 lookback (t−21 / t−252 sessions), measuring by exchange sessions rather than a stock's own rows, the history requirement, splits and dividends inside the lookback, the raw-price filter, the liquidity window, tie-breaks, exclusions, **no look-ahead**, and coverage blocking.
- `test_execution.py`: whole shares and residual cash, **sell-before-buy**, slippage and commission arithmetic, no substitution of a missing open, cash-limited partial fills, and no-leverage invariants.
- `test_accounting.py`: split cash-in-lieu, reverse splits, a **dividend credited once** with a continuous NAV, splits and dividends on the same day, delisting cash-outs, and stale marks.
- `test_calendar.py`: NYSE holidays (Good Friday, New Year), early closes, session offsets, and the latest completed session.
- `test_backtest.py`: fills at the next open after a holiday, no trading without an open, no look-ahead at the NAV level, gross = net + costs, NAV = cash + positions, repeatable runs, and metric formulas.
- `test_ledger.py`: **persistence across a restart**, duplicate-application prevention, DB-level immutability, config changes that affect only future plans, cash reconciliation, and a paper ledger that **exactly matches the backtest**.
- `test_data_quality.py`: stale-data detection, a rebalance blocked on insufficient coverage, a halt on a session with no bars, and the missing-key error state.
- `test_alpaca_provider.py`: pagination, raw adjustment, session dates, 429/5xx retries, auth errors, and corporate-action mapping, all against a mocked HTTP transport.
- `test_api.py`: the end-to-end demo flow over HTTP, including the demo paper ledger matching a backtest exactly.

## 9. Commit and push to a private GitHub repository

The repository already has a local history.

**Before any commit, check that nothing private is staged:**

```bash
git status --short
git check-ignore -v .env data/momentum.db backend/.venv frontend/node_modules frontend/dist
```

`.gitignore` excludes the following: `.env` (secrets), `data/` and `*.db` (local database and portfolio state), virtual environments, `node_modules`, build output, caches, logs and `.tools/`.

**Option A: GitHub CLI** (https://cli.github.com)

```bash
gh auth login
gh repo create momentum-terminal --private --source . --remote origin --push
```

**Option B: website**

1. On https://github.com/new, create a repository, choose **Private**, and do **not** add a README or .gitignore.
2. Then run:
   ```bash
   git remote add origin https://github.com/<your-user>/momentum-terminal.git
   git push -u origin main
   ```

For later changes, use:

```bash
git add -A
git commit -m "Describe the change"
git push
```

## 10. Architecture

```
┌──────────────────────────── Browser (localhost:5173) ─────────────────────────────┐
│ React + TypeScript · Mantine · ECharts · TanStack Query/Table                      │
│ Renders only. Types generated from the backend OpenAPI schema (src/api/schema.d.ts)│
└───────────────────────────────────────┬────────────────────────────────────────────┘
                                        │ JSON over /api (Vite proxy in dev)
┌───────────────────────────────────────▼────────────────────────────────────────────┐
│ FastAPI (backend/app/api) – typed Pydantic schemas, errors {error, message}          │
│   system · universe · portfolio/paper · rebalance · research · ledger routes         │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ services.AppContext – wires settings, DB, calendar, provider, ledger, run executor    │
├───────────────┬───────────────┬──────────────────┬───────────────┬───────────────────┤
│ data/         │ strategy/     │ backtest/        │ ledger/       │ calendar.py       │
│ provider.py   │ config.py     │ engine.py        │ paper.py      │ XNYS sessions via │
│ demo_provider │ signals.py    │ metrics.py       │ plans, orders,│ exchange_calendars│
│ alpaca_provider│ execution.py │ runner.py        │ fills, cash,  │                   │
│ panel.py      │ (shared by    │ (persisted runs) │ snapshots     │                   │
│ store.py      │  backtest AND │                  │               │                   │
│ (import, QA)  │  paper ledger)│                  │               │                   │
├───────────────┴───────────────┴──────────────────┴───────────────┴───────────────────┤
│ SQLite (data/momentum.db) – versioned SQL migrations, append-only triggers, WAL      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **No trading logic in the UI.** Signals, sizing, fills, accounting and metrics are pure Python modules.
- **One execution path.** `strategy/execution.py` handles sizing, fills, costs, corporate actions and marking, and both the backtest and the paper ledger use it.
  A test proves that the paper ledger reproduces the backtest NAV to the cent.
- **Provider isolation.** Everything outside `data/*_provider.py` sees only raw bars plus explicit corporate actions.

Project layout:

```
backend/
  app/            api/ (routes, schemas) · data/ · strategy/ · backtest/ · ledger/ · db/ (migrations)
  tests/          pytest suite
  requirements*.txt, pyproject.toml
frontend/
  src/            api/ (client, hooks, generated types) · components/ · pages/ · lib/ · styles/
scripts/          setup.mjs · dev.mjs · py.mjs  (cross-platform helpers)
.env.example      configuration template
```

To regenerate the frontend types after changing an API schema, run `npm run gen:api`.

## 11. Database and data flow

```
Provider ──► store.run_import ──► instruments · bars (raw OHLCV) · corporate_actions · data_imports
                                          │  (validation: non-sessions, duplicates, bad closes, missing opens)
                                          ▼
                              panel.build_panel  (sessions × symbols, causal total-return index, marks)
                                          │
              ┌───────────────────────────┴───────────────────────────┐
              ▼                                                       ▼
   backtest.run_backtest                                   ledger.PaperLedger.advance
   → backtest_runs / _nav / _rebalances /                  → paper_nav · position_snapshots · positions
     _fills / _cash_events · signal_sets/_rows               cash_transactions · paper_orders · paper_fills
                                                              rebalance_plans · plan_orders · signal_sets/_rows
                                   system_events (audit trail for everything)
```

| Table | Purpose |
|---|---|
| `instruments` | Symbol master: asset type (and how it was determined), sector (and its source), list and delist dates |
| `bars` | Raw daily OHLCV per NYSE session |
| `corporate_actions` | Splits (new shares per old) and cash dividends (USD/share) by ex-date |
| `data_imports` | Every ingestion: range, coverage, counts, warnings, provenance JSON |
| `strategy_configs` | Every config version, by canonical hash |
| `signal_sets`, `signal_rows` | Frozen signals: inputs, momentum, eligibility reason, rank and target weight per stock (immutable) |
| `backtest_runs` (+ `_nav`, `_rebalances`, `_fills`, `_cash_events`) | Inputs, data version, metrics, assumptions, warnings, reproducibility and results for each run |
| `paper_portfolios` | Virtual portfolio(s): capital, cash, as-of session, config version |
| `rebalance_plans`, `plan_orders` | Month-end plans with estimates and rule checks. Status moves proposed → applied → executed, or skipped / blocked. |
| `paper_orders`, `paper_fills` | Internal orders (created on apply) and immutable simulated fills |
| `cash_transactions` | Every cash movement with running balance (immutable) |
| `positions`, `position_snapshots`, `paper_nav` | Current holdings; daily immutable snapshots and NAV |
| `system_events` | Immutable audit log |

Migrations live in `backend/app/db/migrations/NNNN_*.sql`. They are applied automatically and idempotently at startup.

## 12. Strategy rules (v1)

All defaults are adjustable in the Research Lab. The paper portfolio's copy of these settings is changed via **Paper config**.

| Rule | Definition |
|---|---|
| Universe | Point-in-time US **common stocks**: listed at the signal session and not delisted before it. ETFs, funds, preferreds, warrants, units, rights and the benchmark are excluded, using the provider's field or a documented heuristic. |
| Direction | Long-only. No leverage, no short selling. |
| Capital | $100,000 of virtual cash |
| Signal | `momentum = TR(t−21) / TR(t−252) − 1`, where `t` is the signal session, offsets are **NYSE sessions**, and `TR` is the causal total-return index (split- and dividend-adjusted close) |
| Timing | Computed and **frozen after the close** of the last NYSE session of each month. Only data ≤ t is read. |
| History | At least 252 valid bars before t, plus valid bars at t−252, t−21 and t |
| Price filter | Raw (unadjusted, point-in-time) close at t **> $5** |
| Liquidity | Mean of close × volume over the 20 sessions ending at t **≥ $5,000,000**. The window must be complete. |
| Data sufficiency | At least 90% of the universe must have a bar at t; otherwise the rebalance is **blocked** with the reason shown |
| Ranking | Momentum descending. Ties broken by ADV descending, then symbol ascending (deterministic). |
| Selection | Top 50, or all eligible stocks if fewer |
| Weighting | Equal weight, whole shares, limited by available cash. The residual cash is displayed. |
| Rebalance | Monthly. Fills are simulated at the **next session's open**. If a stock has no opening price, it is **not traded**; the signal close is never substituted. |
| Costs (assumed) | 10 bps adverse slippage on buys and sells, $0 commission. These are configurable assumptions, not observed execution costs. |
| Benchmark | SPY total return (demo: synthetic `MKT0`). If dividends are unavailable, it is labelled **price return only**. |

## 13. Execution, accounting and metrics

**Event order per session** (identical in the backtest and the paper ledger):

1. **Pre-open.** Ex-date splits adjust share counts; fractional shares are paid as cash-in-lieu at the prior mark ÷ ratio.
   Cash dividends are credited on shares held × amount, on the ex-date. The pay-date lag is not modelled.
2. **Open.** An applied or pending plan executes.
   - Compute `NAV_open = cash + Σ shares × open`. A holding without an open is valued at its pre-open mark and flagged.
   - `target_shares = floor((w × NAV_open − commission) / (open × (1 + slippage)))`
   - **Sells first**, in symbol order: full exits, then trims. Fill price is `open × (1 − slippage)`.
   - **Buys second**, in rank order: fill price is `open × (1 + slippage)`. If cash runs short, the largest affordable whole-share quantity is bought.
   - What remains is residual cash.
3. **Close.** A holding on its final trading session is converted to cash at its last close. This is an assumption; real delisting proceeds can be lower.
4. **Close.** NAV = cash + Σ shares × raw close. If a stock has no bar, the last close is carried forward, restated for any actions, and flagged as stale.
5. **After close.** On the last session of the month, signals are formed and frozen, and a plan is created.

**No double counting of dividends.** Signals use the total-return index. Valuation uses **raw** prices, and dividends are credited to cash explicitly. Adjusted prices are never used for valuation.

**Metrics** (the formulas are also shown in the Research Lab):

| Metric | Formula |
|---|---|
| Daily return | `r_t = NAV_t / NAV_{t−1} − 1` |
| Total return | `NAV_end / NAV_start − 1` |
| CAGR | `(NAV_end / NAV_start)^(365.25 / days) − 1`, **only reported if the sample is ≥ 365 calendar days** |
| Annualized volatility | `stdev(r_t) × √252` |
| Return/vol | `mean(r_t) × 252 / vol` (risk-free rate = 0) |
| Drawdown | `NAV_t / max(NAV_0..t) − 1` |
| Turnover (one-way) | `(buys + sells) / 2 / NAV_open` |
| Difference vs benchmark | Strategy total return − benchmark total return. This is **a simple difference, not a statistically estimated alpha**. |
| Gross NAV | Net NAV + cumulative slippage and commissions (not compounded) |
| Monthly return | Last NAV of month / last NAV of the prior month − 1 |

## 14. Limitations of the data and the backtest

- **The demo data is fabricated.** It is survivorship-free by construction, but it is not a market. Nothing about it says anything about real momentum returns.
- **Alpaca data is not point-in-time.**
  - The universe comes from today's asset list, and optionally from today's liquidity ranking, so delisted losers are missing.
  - Backtests on it are survivorship-biased and look-ahead-biased in their selection. The UI shows a permanent warning.
- **Security classification** on Alpaca is heuristic (name/symbol patterns), so some ETFs or preferreds may slip through, or some common stocks may be excluded.
- **Costs are assumptions**, not observed executions:
  - Opening auctions can be less liquid than assumed.
  - Market impact is not modelled.
  - There are no borrow or fee effects, because the strategy is long-only.
- **Dividends** are credited on the ex-date; the real pay date is later. **Delisting proceeds** are assumed to be the last close.
- **Taxes, fees, cash interest and FX** are not modelled.
- **Corporate actions** beyond splits and cash dividends (spin-offs, mergers, symbol changes, rights) are not modelled.
- Daily bars only; there are no intraday data or live quotes.
- One strategy, one rebalance frequency and one account. The engine is kept simple and auditable on purpose.

## 15. Roadmap: optional broker paper-account integration

This version deliberately has **no order routing**. A future, opt-in integration with a broker's *paper* account, such as Alpaca paper trading, should look like this:

1. **A separate `BrokerAdapter` interface** in its own module, next to `MarketDataProvider`, with read-only account and positions calls first.
2. **Reconciliation before orders.** Show internal-ledger vs broker-paper positions and cash side by side and flag differences, still without sending anything.
3. **Explicit, per-plan submission.**
   - A second, separately confirmed action ("Submit to broker PAPER account") on an already-applied plan.
   - Orders use market-on-open (OPG) or limit orders, with idempotent client order IDs derived from the plan ID, so a plan can never be submitted twice.
4. **Hard guards.**
   - Refuse any base URL that is not the broker's paper endpoint.
   - Use an environment flag that defaults to off.
   - Never store keys outside `.env`.
   - Log every request and response in `system_events`.
5. **Fill import.** Store broker paper fills in their own table. Never overwrite the internal simulated fills.
   Compare the two to measure the real slippage against the 10 bps assumption.
6. **Live-money trading remains out of scope.**

## 16. Configuration reference

All settings are read from `.env` in the project root; see `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `APP_HOST` | `127.0.0.1` | Bind address. Keep this at localhost. |
| `APP_PORT` | `8765` | Backend port. The Vite proxy reads the same value. |
| `APP_CORS_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173` | Allowed dev origins |
| `DATABASE_PATH` | `./data/momentum.db` | SQLite file (git-ignored) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Structured logs (`text` for human-readable) |
| `MARKET_DATA_PROVIDER` | `demo` | `demo` or `alpaca` |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | — | Server-side only |
| `ALPACA_DATA_FEED` | `sip` | `sip` or `iex` |
| `ALPACA_HISTORY_START` | `2016-01-01` | First date to import |
| `ALPACA_UNIVERSE_FILE` | — | Optional symbol list |
| `ALPACA_MAX_SYMBOLS` | `600` | Universe size when no file is given |
| `DEMO_AUTOSEED` | `true` | Seed the demo data, backtest and portfolio on first start |

## 17. Troubleshooting

| Symptom | Fix |
|---|---|
| `npm : File ...npm.ps1 cannot be loaded because running scripts is disabled` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `npm.cmd`. |
| `port 8765 ... already in use` | Set another port in `.env`, e.g. `APP_PORT=8766`, and restart. The frontend follows automatically. |
| `Python virtual environment not found` | Run `npm run setup`. |
| npm warns about `esbuild` install scripts not being approved | Harmless. esbuild ships its binary as a platform package. If the build fails, run `npm --prefix frontend install-scripts approve esbuild`. |
| UI shows "Backend / data provider problem" | The backend is not running, or `MARKET_DATA_PROVIDER=alpaca` is set without keys. Check the terminal log. |
| You want a clean slate | Stop the app, delete `data/momentum.db*`, and start again. The demo will re-seed. |
