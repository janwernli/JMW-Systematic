<img src="frontend/public/brand/jmw-logo-dark.png" alt="JMW Capital Partners" height="36">

# JMW Capital Partners Trading

Long-short momentum with Alpaca paper trading.

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
7. [Research: backtests, comparison, alpha test](#7-research-backtests-comparison-alpha-test) (incl. [strategy variants](#strategy-variants))
8. [Strategy rules](#8-strategy-rules)
9. [Execution, accounting and metrics](#9-execution-accounting-and-metrics)
10. [Limitations](#10-limitations)
11. [Norgate Data (point-in-time Russell 1000)](#11-norgate-data-point-in-time-russell-1000)
12. [Architecture and data flow](#12-architecture-and-data-flow)
13. [Tests](#13-tests)
14. [Git and GitHub](#14-git-and-github)
15. [Configuration reference](#15-configuration-reference)
16. [Troubleshooting](#16-troubleshooting)
17. [Running on an Azure VM](#17-running-on-an-azure-vm)

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
| **Strategy Variants** | The four pre-defined variants side by side on the same period: metrics table, equity and drawdown charts with SPY, margin flags. |
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
6. **Approval.** Set with **Rebalance mode** on the Alpaca Paper page (setting `rebalance_mode`):
   - **Approve** (default): the month-end plan's orders stay *planned / awaiting approval*. A panel on the Trading page shows the plan (signal date, order count, buy/sell value).
     - **Review & approve plan…** opens the **full order list**. **Approve all N orders** approves the whole plan at once and immediately runs a cycle.
     - The orders are sent as market-on-open orders if it is 19:00–09:28 ET, as market orders during the fill session, otherwise at the next scheduled run.
     - The approval covers the plan, so catch-up orders for the same plan (e.g. an order that expired unfilled) need no second approval.
     - If the order list changed since you opened it, approval is refused and you review the new list.
     - **Decline plan** skips that rebalance; its orders are never sent or regenerated.
   - **Auto**: rebalance orders are sent without asking.
   - **Stop-loss covers are automatic in both modes.**
7. **Submit.** Orders are sent only when **all** of these hold: `BROKER_TRADING_ENABLED=true`, the dashboard's automation switch is ON, the data is fresh, and the run is not a dry run.
   - Between **19:00 and 09:28 ET** they are sent as market-on-open orders (opening auction).
   - If that window was missed but the fill session is trading, they are sent as a regular market order, flagged as late.
   - Otherwise they stay planned for the next run.
   - Every order carries a deterministic `client_order_id`, so it can never be sent twice.

### Schedule (Windows)

For the always-on setup on an Azure VM with systemd timers, see [section 17](#17-running-on-an-azure-vm). Use one or the other, never both.

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
| **Thu 1 Oct, 14:00 (08:00 ET)** | Refreshes data, freezes the signal and sizes orders from account equity. In **Approve** mode the plan waits for you (ntfy: "plan awaiting approval"). Approve before **15:28 (09:28 ET)** to get the opening auction. In **Auto** mode the market-on-open orders are submitted directly. |
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
3. Open the **Factor alpha** tab. It regresses the run's monthly returns on the **Fama-French 5 factors (2×3) + momentum**:
   - **Cash interest.** The dependent variable is the return of the positions over the risk-free cost of the capital they tie up.
     - With **Cash earns RF** on (`cash_interest`), positive cash is credited the Ken French RF, per calendar day on ACT/360. The regression uses **r − RF**.
     - By default, backtest cash (including short proceeds) earns **no interest**. The regression then uses **r − RF × (1 − idle cash weight)**: RF is charged on every dollar except idle, non-interest-bearing cash.
       - For the market-neutral book (cash ≈ NAV) this is ≈ r. Subtracting the full RF would charge it for interest it never received.
       - For a net-long book (SPY core, 130/30; cash ≈ 0) it is ≈ r − RF. Not subtracting RF there would count the risk-free rate as alpha.
       - A margin loan is already charged RF + spread inside r, so borrowed dollars are charged RF once.
     - The model ledger credits the same interest, so it stays identical to the backtest.
   - Factors come from the Kenneth R. French Data Library, downloaded and cached weekly under `data/factors/`.
   - Results show alpha (monthly × 12), **Newey-West t-stats** (Bartlett kernel, lag = ⌊4·(T/100)^(2/9)⌋), all factor betas and R².
   - This is reported for the full sample and for each half (each half needs at least 24 months).
   - The first month is dropped because it is partial. Months not yet published by the library are excluded and noted.

**Results on the current Alpaca data (2017-01 → 2026-09).** These are illustrations only; see Limitations.

| | Composite | Plain 12-1 |
|---|---|---|
| Net CAGR / vol, cash earns nothing | 4.1% / 8.3% | 3.5% / 8.3% |
| Net CAGR, cash earns RF | 6.7% | 6.4% |
| Realized beta to SPY | 0.00 | −0.01 |
| Alpha (FF5 + Mom), full sample | +3.1%/yr, t = 2.1 | +2.5%/yr, t = 1.6 |
| Alpha, 1st half / 2nd half | −2.1% (t −1.4) / +8.4% (t 4.7) | −1.7% (t −0.9) / +6.3% (t 3.0) |
| Momentum-factor loading | 0.34 (t 7) | 0.35 (t 7) |

The alpha is essentially the same whether cash earns RF (regressing r − RF) or not: +3.08% vs +3.1% for the composite. That shows the two conventions are consistent. With the idle-cash rule the composite's no-interest alpha is +3.17% (t 2.09): the book averaged −3% net, so RF is added back on that small net short.

An earlier version subtracted RF although cash earned nothing, which understated alpha by roughly RF (about 2.4%/yr on average over the sample).

A full-sample t-stat near 2, entirely from the second half, on a survivorship-biased universe, is weak evidence.

### Strategy variants

The **Strategy Variants** page (`/variants`) runs four **pre-defined** variants on the same data version and period and shows them side by side. They are fixed structural alternatives, defined in `backend/app/backtest/variants.py` before being run; nothing is tuned to the backtest.

**Paper trading uses SPY + overlay** (switched on 2026-10-01; see [Switching the paper strategy](#switching-the-paper-strategy)). The other variants stay available for research. `StrategyConfig()` defaults are still Neutral 10%.

| Variant | Definition |
|---|---|
| **Neutral 10%** | The current defaults. |
| **Neutral 15%** | `target_vol` 0.15, `max_side_gross` 1.0, `max_total_gross` 2.0 (Alpaca Reg T limit). |
| **SPY + overlay** (paper) | `core_beta` 1.0: 100% of NAV in SPY, rebalanced monthly with the rest. Plus an overlay from the same composite signal: deciles, buffer, inverse-vol weights, sector-neutral.<br>Overlay sizing is **fixed** (`sizing` fixed):<br>• long book 30% of NAV (`fixed_long_gross`);<br>• short book sized for overlay beta neutrality, long × β_L / β_S ≈ 30%, capped at 40% (`max_side_gross`).<br>`max_total_gross` 1.6: if core + long + short would exceed it, both overlay sides are scaled down together, which keeps the beta ratio. The crash guard scales the overlay short book. The stop-loss (+50%), the $10 short floor, the HTB screen and the easy-to-borrow check are unchanged. Beta and sector neutrality apply to the overlay only. |
| **130/30** | `sizing` = fixed: long book 130%, short book 30%, same signal, `beta_neutral` off, no vol target, gross cap 1.6. |

The 130/30 variant needs two things that follow from its definition:

- **Sector neutrality is off.** A 100% net-long book cannot keep every sector within ±2% net, because the sector nets sum to +100%.
- **The per-name long cap is 2.6%** (= 130% ÷ the 50-name minimum book). With the default 2%, a ~60-name book can hold only ~120%.

**Margin loans.** SPY + overlay and 130/30 hold longs worth more than NAV plus short proceeds.

- In the backtest (as at Alpaca under Reg T), the difference is a **margin loan**. The loan is limited to what the targets need (net target − 100%, plus a 1% buffer for slippage and rounding).
- The loan is charged **RF + an ASSUMED 2.5% spread** per calendar day (ACT/360), set by `margin_debit_spread`.
- The market-neutral books never borrow.

**Margin checks.** At every close the backtest flags, but never liquidates:

- days when **gross exposure > 2.0× equity**;
- days when **equity < maintenance requirement**: shorts max(30% of market value, $5/share) + longs 25% of market value.

The flagged days are listed per run.

**Reported for each variant:**

- CAGR, volatility, Sharpe, Sortino;
- max drawdown with its peak, trough and recovery dates;
- worst month;
- beta and correlation to SPY;
- FF5 + momentum alpha with Newey-West t-stat;
- one-way turnover per year;
- total costs (slippage + commissions + borrow fees + margin interest);
- average long, short and net exposure;
- the margin checks;
- a first/second-half split.

Two charts go with the table: equity curves with SPY (log scale), and drawdowns. Sharpe and Sortino use the same excess return as the alpha test (RF charged on all capital except idle cash).

**Results on the current Alpaca data (2017-01-03 → 2026-09-29, comparison #2).** These are illustrations only. The universe is survivorship-biased, which flatters **long** exposure most, so the net-long variants are the most inflated.

| | Neutral 10% | Neutral 15% | SPY + overlay | 130/30 |
|---|---|---|---|---|
| CAGR / vol | 4.1% / 8.3% | 5.0% / 10.3% | 16.7% / 18.8% | 23.5% / 21.7% |
| Sharpe / Sortino | 0.54 / 0.73 | 0.53 / 0.72 | 0.79 / 1.11 | 0.98 / 1.37 |
| Max drawdown | −14.1% (2019-08 → 2021-07) | −19.1% | −33.4% (Mar 2020) | −35.0% (Mar 2020) |
| Worst month | −4.4% | −6.4% | −12.4% | −13.8% |
| Beta / correlation to SPY | 0.00 / 0.01 | 0.02 / 0.03 | 1.01 / 0.96 | 0.97 / 0.80 |
| Alpha (FF5 + Mom), t | +3.2%, 2.09 | +3.5%, 1.64 | +0.4%, 0.43 | +6.8%, 3.32 |
| Turnover / total costs | 3.4× / $9,994 | 4.0× / $11,837 | 3.6× / $18,787 | 3.5× / $23,222 |
| Days gross > 2× / maintenance breaches | 0 / 0 | 0 / 0 | 0 / 0 (max 1.64×) | 0 / 0 |
| 1st half: CAGR, alpha (t) | −1.8%, −2.0% (−1.3) | −2.8%, −3.3% (−1.6) | +16.5%, −2.3% (−2.7) | +21.4%, +0.8% (0.4) |
| 2nd half: CAGR, alpha (t) | +10.4%, +8.5% (4.7) | +13.4%, +10.2% (3.3) | +16.9%, +3.2% (2.9) | +25.5%, +11.3% (3.9) |

SPY itself returned about 15% a year over the same period.

- **Every variant's alpha comes from the second half.** None is significant in the first half.
- **The two net-long variants mostly carry market beta.** Their extra return over the neutral books is mainly SPY's return.
- **SPY + overlay (the paper strategy) has a full-sample alpha of essentially zero.** Its 30%/30% overlay is small next to the 100% SPY core, and its first-half alpha is significantly negative. It never breached a margin limit; it borrowed a little on margin ($1,729 interest over the run).
- **Comparison #1** used an earlier SPY + overlay definition: an 8% vol-target overlay, 200% gross cap, 57 days above 2×. It is kept in the comparison history.

### Switching the paper strategy

The model portfolio (internal ledger) and the Alpaca paper account always follow the **same** plans. The Alpaca account trades whatever the model ledger's latest plan targets, so switching the strategy is a config change on the active model portfolio:

- a new config version, logged under Ledger & Diagnostics;
- an ntfy message, "Paper strategy changed to …".

**From the dashboard:** go to *Strategy Variants*, click **Use for paper trading…**, review the settings, the target book and the full order list, then confirm. The Alpaca Paper page shows the active strategy at the top.

**From the command line (on the server):**

```bash
# Azure VM (systemd install)
cd ~/JMW-Systematic/backend
.venv/bin/python -m app paper-config --variant spy_overlay --replan-now --dry-run   # look first: changes nothing
.venv/bin/python -m app paper-config --variant spy_overlay --replan-now             # switch
systemctl list-timers 'jmw-daily*'                                                   # next run (08:00 / 17:30 ET)

# Windows PC
cd "C:\Coding\JMW Trading Strategy\backend"
.venv\Scripts\python.exe -m app paper-config --variant spy_overlay --replan-now --dry-run
.venv\Scripts\python.exe -m app paper-config --variant spy_overlay --replan-now
```

Run it where the scheduler runs, i.e. against the database that trades: on the VM if the VM runs `jmw-daily.timer`.

Variants: `neutral_10`, `neutral_15`, `spy_overlay`, `ext_130_30`.

- **`--dry-run`** prints the config changes, warnings, the target book and the full order list, and changes nothing.
  - The target book shows the SPY weight, the number of longs and shorts, overlay long and short gross, total gross, net exposure and the estimated overlay beta.
  - The orders are computed against the live Alpaca paper positions and equity (read-only), or the last sync.
- **Without `--replan-now`**, the new config applies from the next month-end plan.
- **`--replan-now`** makes the switch happen within one trading day.
  - It rebuilds the plan of the latest month-end signal the ledger has processed. The **ranking is the month-end close's**; only books and sizing change. The plan is priced at the latest close with current holdings and applied to the model ledger, filling at the next open.
  - Any unexecuted plan is **superseded**, and its unsent Alpaca drafts too.
  - At the next **08:00 ET** run, the daily cycle reconciles the Alpaca positions to the new plan: names not in the new target are sold or covered, SPY is bought, and the overlay is rebalanced.
  - **Approve mode** still applies: the new plan waits for **Approve** on the Trading page. In **auto** mode it is sent.
  - If the ledger has not processed the latest month-end yet (for example right after a month-end close), no replan is needed: the next daily run forms that month-end plan with the new config.
  - It is **refused** while rebalance orders are still working at Alpaca. Wait for the fills, or cancel them in Alpaca, then switch.
- The model ledger takes the same margin loans as the backtest: RF + `margin_debit_spread` on a negative cash balance. That keeps the theoretical track, the backtest and the Alpaca account consistent.

**Margin checks on the real account.** After every sync the daily cycle compares the Alpaca account with the limits:

- gross above the strategy cap (+5% drift) → **warning**;
- gross above 2× or equity below Alpaca's maintenance margin → **error**, with an ntfy alert.

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
| Crash guard | If SPY's 24-month return < 0 and its 6-month realized vol > 20%, the short book is multiplied by 0.5. This is applied **before** sector neutralization, so the ±2% sector limits hold for the book actually traded. With the guard on, those limits therefore cap how net-long the book can become. The final per-sector nets, after the guard, are reported with every plan. |
| Costs (assumed) | 10 bps slippage, $0 commission. **Borrow fee 0.5%/yr accrued per calendar day (ACT/360)**; Monday pays for the weekend. |
| Cash | By default cash, including short proceeds, earns nothing. Optionally (`cash_interest`), it earns the Ken French RF (monthly RF × 12, ACT/360). Unpublished recent months carry the latest value forward, with a warning. |
| Stop-loss | Short close ≥ 1.5 × average entry → cover at the next open. No re-short until the next signal. |
| **Capacity warning** | If `min_names_per_side × max_short_weight < max_side_gross` (or the long equivalent), the config emits a `ConfigWarning`. The warning is shown in the UI, plan checks and run diagnostics: the per-name caps can prevent the vol target from being reached. |

## 9. Execution, accounting and metrics

**Event order each session** (identical in the backtest and the model ledger; a test verifies they match to the cent):

1. Pre-open: splits and dividends. Shorts pay dividends and split fractions.
2. Open: stop-loss covers, then the rebalance. Order of trades: reduce longs → short sales → covers → buys in rank order.
3. Close: delisting close-outs.
4. Close: cash interest (optional, RF, ACT/360; none on the funding session), borrow fee (ACT/360), then mark to market. NAV = cash + long value + short value, where short value is negative.
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
- **SEC SIC sectors** (Alpaca provider) are current codes, not point-in-time. The SIC → sector mapping is approximate; for example, SIC 7370 puts Alphabet and Meta in Information Technology. The Norgate provider uses Norgate's GICS instead.
- **The hard-to-borrow screen** in backtests is a liquidity stand-in. Real borrow availability, recalls, locates and hard-to-borrow fees are not modelled. Live orders check Alpaca's easy-to-borrow flag instead.
- **Costs are assumptions.** Opening-auction liquidity, market impact and interest on short proceeds are not modelled.
- **Sizes are small.** With $100k and 50–100 names per side, many positions are only a few shares, so rounding noise is noticeable.
- **Stops are checked on daily closes** and fill at the next open, which can gap past the stop.
- **Automation depends on the scheduler's machine being on** (the Azure VM, or this PC) at the scheduled times. Missed windows fall back to late market orders or the next run.
- **The factor data lags** by about 1–2 months, and the most recent months are excluded from the alpha test.

## 11. Norgate Data (point-in-time Russell 1000)

`MARKET_DATA_PROVIDER=norgate` switches to `NorgateProvider`. It requires:

- a Norgate Data subscription that includes historical index constituents
- the Norgate Data Updater running on Windows
- `pip install norgatedata` in `backend/.venv`

What it provides:

- The **Russell 1000 Current & Past** watchlist, including delisted stocks.
- Unadjusted bars. Splits are derived from capital-adjusted vs unadjusted closes, and dividends come from the Dividend column.
- **Sectors from Norgate's own GICS classification.** This uses `classification_at_level(symbol, "GICS", "Name", 1)` for every symbol, including delisted ones, which keep their last classification. Legacy names such as "Telecommunication Services" are mapped to today's 11 sectors. SEC EDGAR is only used with the Alpaca provider.
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
- `app_settings`: the automation switch and `rebalance_mode` (`approve` | `auto`).
- `broker_plan_decisions`: your approve/decline decision per month-end plan.
- Signal rows now also store the composite components, sector and 60-day ADV.

## 13. Tests

```bash
npm test              # backend pytest suite + frontend type-check
npm run test:backend
```

There are 180 backend tests, built on constructed datasets with hand-checkable results. Beyond the earlier coverage (lookbacks, no look-ahead, calendar, splits/dividends, whole-share cash, costs, restart persistence, model ledger = backtest), they cover:

- **Composite signal:** winsorizing, the FIP arithmetic, residual momentum rewarding recent idiosyncratic drift but not beta, sector demeaning, and ranking by composite vs 12–1.
- **Books and sizing:** the hard-to-borrow screen, the sector-neutral QP (limits, gross, beta, caps, and the reduced-gross fallback), the capacity warning, and loading of legacy configs.
- **Data sources:** the SIC → sector mapping, the SEC client (User-Agent, and sectors surviving a refresh), and Norgate's split/dividend/membership/delisting mapping.
- **Costs:** ACT/360 borrow fees.
- **Alpha test:** Ken French parsing and Newey-West (matches OLS and White's estimator at lag 0), plus the full/half-sample alpha test.
- **Automation:** opening-auction submission sized from equity, idempotency, dry run, both kill switches, stale-data blocking, stop-loss on actual shorts, flip splitting, the submission-window rules, and the paper-host lock.
- **Follow-ups:** ACT/360 cash interest, the model ledger matching the backtest with interest on, the alpha regression matching the cash treatment, Norgate GICS sectors (including delisted symbols such as `ENRNQ-200411` all the way into the panel, and legacy names), SEC not being used for Norgate, and final sector nets after the crash guard.
- **Approval mode:** `approve` is the default (also for existing databases, via migration 0005). Rebalances are held while stop-losses are sent. Approving a plan sends every order and later catch-ups. A changed order list and a second decision are refused. A declined plan is never sent, even in `auto`. Stale drafts are superseded.
- **Strategy variants:**
  - The `StrategyConfig` defaults are unchanged (= Neutral 10%). The variant definitions match the spec.
  - The SPY core is bought first, is exempt from beta and sector neutrality, and counts toward the gross cap; the overlay is identical with and without it.
  - Fixed 130/30 sizing, including scaling to the gross cap; equal dollar sides without beta neutrality.
  - Margin loans only when the book needs them (exact cash arithmetic), ACT/360 debit interest, and no borrowing for neutral books.
  - The margin requirement rules and the engine's gross/maintenance flags.
  - The idle-cash RF basis, which matches r and r − RF at the extremes; the report's Sharpe, drawdown dates, costs and halves.
  - End to end: the four variants run through the API on an identical period.
- **Paper strategy switch:**
  - A new config version, the event log, the ntfy message and idempotency.
  - The dry run changes nothing: every table is compared before and after the CLI dry run.
  - Replan-now from a neutral book to SPY + overlay: the old plan is superseded and the month-end ranking reused; dropped names are sold or covered, SPY is bought, and only the new plan trades.
  - Approve vs auto mode; a refusal while orders are working; no replan when the month-end has not been processed yet.
  - The model ledger equals the backtest to the cent with a SPY core and a margin loan.
  - Account margin flags.
  - A spy_overlay sanity backtest (no margin flags) on the fixture, and on the locally stored real data when present.
- **CI:** `.github/workflows/tests.yml` runs the whole backend suite on every push. The real-data test is skipped there, because no market data is in the repo.
- **VM operations:** ntfy messages (off without a topic; summary; stale-data, error and approval alerts; never raises), consistent SQLite backups with 14-day rotation while the database is open, the `migrate` and `backup` commands, and the deploy files (localhost-only single-worker unit, weekday New York timers, safe `.env.example` defaults).

## 14. Git and GitHub

The repository is pushed to the GitHub repo `janwernli/JMW-Systematic` (`origin`). It can be public or private; for the VM, see [Making the repository private](#making-the-repository-private). Keys, the database and logs are never committed (see below).

Every push to `main` runs the **Frontend build** GitHub Action (`.github/workflows/frontend.yml`). It builds `frontend/dist` and publishes it as `frontend-dist.tar.gz`, with a SHA-256 file, on the rolling release `frontend-latest`. The Azure VM downloads it from there instead of running Node.

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
| `NTFY_TOPIC` | — (off) | ntfy topic for run summaries and alerts. Use a long random name. |
| `NTFY_SERVER` / `NTFY_TOKEN` | `https://ntfy.sh` / — | Own ntfy server or access token (optional) |

Strategy parameters that the variants use (StrategyConfig; defaults = Neutral 10%):

| Parameter | Default | Meaning |
|---|---|---|
| `core_beta` | 0.0 | SPY core, fraction of NAV (0–1); exempt from beta and sector neutrality; counts toward `max_total_gross` |
| `beta_neutral` | true | Short gross = long gross × β_L / β_S; off = equal dollar gross per side |
| `sizing` | `vol_target` | `vol_target` or `fixed` (`fixed_long_gross` / `fixed_short_gross`, default 1.30 / 0.30) |
| `margin_debit_spread` | 0.025 | ASSUMED margin-loan rate over RF, ACT/360 (only books with net target > 100% borrow) |
| `fixed_long_gross` / `fixed_short_gross` | 1.30 / 0.30 | Fixed sizing. With `beta_neutral` on, the short book is sized for overlay beta neutrality instead, capped at `max_side_gross` (SPY + overlay: 0.30 long, short ≤ 0.40) |

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
| VM: dashboard page is blank / 404 | No frontend build installed yet. Check that the *Frontend build* Action succeeded on GitHub, then run `bash deploy/update.sh`. |
| VM: `https://<machine>.<tailnet>.ts.net` unreachable | Run `tailscale status` and `sudo tailscale serve status`. Enable MagicDNS and HTTPS certificates in the Tailscale admin console, then re-run `bash deploy/setup.sh`. |
| VM: no ntfy messages | Check `NTFY_TOPIC` in `.env` and that the app is subscribed to exactly that topic. Failures are logged: `journalctl -u jmw-daily \| grep ntfy`. |

## 17. Running on an Azure VM

The VM runs the automated paper trading and the dashboard around the clock. Your Windows PC stays the research machine: Norgate backtests, with the Windows scripts unchanged.

| | Azure VM (Ubuntu 24.04, 1 GiB RAM + 2 GB swap) | Windows PC |
|---|---|---|
| Provider | Alpaca (SIP) | Norgate (backtests), or Alpaca |
| Runs | `jmw-backend` (API + dashboard), `jmw-daily.timer`, `jmw-backup.timer` | `npm run dev` / `npm start`, research |
| Dashboard | `https://<machine>.<tailnet>.ts.net` (Tailscale, HTTPS, private) | `http://127.0.0.1:8765` |
| Frontend | pre-built by GitHub Actions, downloaded | built locally |

> **Run the scheduler in one place only.** The VM and the PC must never both run the daily cycle against the same Alpaca paper account. Each has its own database, so neither knows about the other's planned orders. Remove the Windows tasks (`npm run schedule:remove`) before the VM takes over.

**Memory.** On the real data (600 symbols, ~1.5 M bars), loading the price panel peaks at about 230 MB, and the daily cycle takes about 15 s. The backend unit has `MemoryHigh=500M` / `MemoryMax=750M` and one worker. Long backtests in the Research Lab also work on the VM but are slow; run heavy research on the PC.

### 1. Clone and install Tailscale

```bash
ssh azureuser@<vm-public-ip>
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/janwernli/JMW-Systematic.git ~/JMW-Systematic
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # log in with the link it prints
```

If the repository is already private, create the deploy key first ([Making the repository private](#making-the-repository-private), step 1), then clone with `git clone git@github.com:janwernli/JMW-Systematic.git ~/JMW-Systematic`.

In the Tailscale admin console, enable **MagicDNS** and **HTTPS certificates** (DNS page) so `tailscale serve` can use `https://<machine>.<tailnet>.ts.net`.

### 2. Create `.env`

```bash
cd ~/JMW-Systematic
bash deploy/setup.sh       # first run: copies .env.example to .env (chmod 600) and stops
nano .env
```

Fill in the following (every variable is commented in `.env.example`):

- `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`: your **paper** keys.
- `ALPACA_DATA_FEED=sip`.
- `SEC_USER_AGENT`: name and e-mail.
- `NTFY_TOPIC`: a long random name, e.g. `jmw-$(openssl rand -hex 12)`. Subscribe to it in the ntfy app.
- Leave `BROKER_TRADING_ENABLED=false` for now.

### 3. Bring the database along (recommended)

The model ledger, the order history with its client order ids and your plan decisions live in `data/momentum.db`. Copying it keeps the VM's view of the Alpaca account continuous and avoids a 10-year first import on a small VM.

On the PC:

```powershell
npm run schedule:remove                 # the VM takes over the scheduling
npm run backup                          # consistent copy in %USERPROFILE%\backups\momentum-YYYY-MM-DD.db
scp $HOME\backups\momentum-2026-09-30.db azureuser@<vm>:~/JMW-Systematic/data/momentum.db
```

Without a copied database, the first run imports the full Alpaca history (slow, but it works), or you can run `cd backend && .venv/bin/python -m app import-data` yourself.

### 4. Run setup

```bash
bash deploy/setup.sh
```

The script is idempotent; re-run it at any time. It does the following:

1. Installs Python 3.12 + venv, sqlite3, ufw and unattended-upgrades, and creates 2 GB of swap if there is none.
2. Creates `backend/.venv` and installs `backend/requirements.txt`. It reinstalls only when the file changes.
3. Downloads the latest frontend build (release `frontend-latest`) after verifying its SHA-256.
4. Applies database migrations (`python -m app migrate`).
5. Installs and enables the systemd units:

   | Unit | What |
   |---|---|
   | `jmw-backend.service` | `uvicorn` on **127.0.0.1:8765 only**, 1 worker, `Restart=always`, memory-capped |
   | `jmw-daily.timer` | `python -m app daily --trigger schedule` at **08:00 and 17:30 America/New_York**, Mon–Fri, `Persistent=true` (a run missed while the VM was off is made up at boot) |
   | `jmw-backup.timer` | nightly 21:00 New York: SQLite online backup to `~/backups/momentum-YYYY-MM-DD.db`, keeps the last 14 |

6. Turns on unattended security upgrades. If a reboot is needed, it happens at 04:00 UTC, when no run is scheduled; everything restarts by itself.
7. Configures the ufw firewall: deny incoming except **OpenSSH** and the **tailscale0** interface.
8. Runs `tailscale serve --bg 8765` if Tailscale is installed and logged in. This serves the dashboard at `https://<machine>.<tailnet>.ts.net`, and the configuration persists across reboots.
9. Sets `chmod 600 .env`.

### 5. Check, then switch trading on

```bash
systemctl list-timers 'jmw-*'           # next runs (shown in UTC and as "left")
sudo systemctl start jmw-daily          # one run now; BROKER_TRADING_ENABLED=false -> nothing is sent
journalctl -u jmw-daily -n 100          # its output; an ntfy summary should arrive too
```

When the run looks right, set `BROKER_TRADING_ENABLED=true` in `.env` and run `sudo systemctl restart jmw-backend`. The daily cycle reads `.env` at every run; the backend reads it only at startup, so the restart makes the dashboard show the new value.

With the default **Approve** mode, month-end rebalances wait for you on the Trading page (ntfy tells you). Stop-loss covers are always sent automatically.

### Logs

```bash
journalctl -u jmw-backend -f            # API / dashboard
journalctl -u jmw-daily -n 200          # daily cycles (also on the Trading page, "Automation runs")
journalctl -u jmw-backup                # backups
```

### ntfy notifications

With `NTFY_TOPIC` set, every daily run sends a short summary:

- orders sent / planned / skipped / awaiting approval;
- account equity and the number of long and short positions;
- how far the data reaches.

It sends a **high-priority alert** on any error, on **stale data**, when a plan is waiting for your approval, and when a backup fails. With an empty `NTFY_TOPIC`, nothing is sent.

### Update

```bash
cd ~/JMW-Systematic && bash deploy/update.sh
```

The script does the following:

1. `git pull --ff-only`. If the deploy scripts themselves changed, it continues with the new version.
2. Reinstalls the requirements only if `requirements.txt` changed.
3. Downloads the new frontend build. It warns if the GitHub Action hasn't finished yet; just re-run it a few minutes later.
4. Waits for a running daily cycle to finish, then stops the backend and applies DB migrations.
5. Refreshes the systemd units, restarts the backend and checks `/api/health`.

### Restore a backup

```bash
sudo systemctl stop jmw-daily.timer jmw-backend
cd ~/JMW-Systematic
mkdir -p data/replaced && mv data/momentum.db* data/replaced/  # keep the current file (+ its -wal/-shm) aside
cp ~/backups/momentum-2026-10-14.db data/momentum.db
(cd backend && .venv/bin/python -m app migrate)                # an older backup may predate a migration
sudo systemctl start jmw-backend jmw-daily.timer
```

Orders placed after the backup date are not in the restored database. The Alpaca account stays the source of truth: the next run syncs positions and order statuses from Alpaca, and deterministic client order ids stop an order from being sent twice. If a restored month-end plan was already approved in the lost period, approve it again.

### Making the repository private

The VM then needs credentials for two things: `git pull`, and downloading the frontend build. Set both up **before** you switch the repo to private; they also work while it is public.

**1. `git pull` with a read-only deploy key** (on the VM):

```bash
ssh-keygen -t ed25519 -N "" -C "jmw-vm deploy key" -f ~/.ssh/jmw_deploy
cat ~/.ssh/jmw_deploy.pub         # copy this line
```

- On GitHub, go to the repo's **Settings → Deploy keys → Add deploy key**. Paste the line and leave **Allow write access** unticked.
- Then point the VM's checkout at SSH:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/jmw_deploy
  IdentitiesOnly yes
EOF
ssh -o StrictHostKeyChecking=accept-new -T git@github.com    # "Hi janwernli/JMW-Systematic! ..." = OK
cd ~/JMW-Systematic && git remote set-url origin git@github.com:janwernli/JMW-Systematic.git && git pull
```

**2. The frontend download with a read-only token:**

1. Go to **GitHub → Settings → Developer settings → Fine-grained personal access tokens → Generate new token**.
2. Set **Repository access** to *Only select repositories: JMW-Systematic*.
3. Set **Permissions** to *Contents: Read-only*.
4. Put the token in the VM's `.env` as `GITHUB_RELEASE_TOKEN=...`. Only the deploy scripts read it, and `.env` is `chmod 600`.
5. Run `bash deploy/update.sh`. It should say *"with token"* when downloading.
6. Note the token's expiry date. When it expires, `update.sh` warns that the download failed and keeps the previous build.

**3. Switch the repo to private** (GitHub → Settings → General → Danger zone), then run `bash deploy/update.sh` once more to check.

- The GitHub Action keeps working on a private repo. Each build (about a minute) counts against the free Actions minutes: 2,000 per month on the free plan.
- Your PC is unaffected: it pushes with your normal GitHub login.
