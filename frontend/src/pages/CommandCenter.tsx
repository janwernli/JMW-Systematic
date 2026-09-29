import { Badge, Button, Group, Text } from "@mantine/core";
import { Link } from "react-router";
import { IconArrowsExchange } from "@tabler/icons-react";
import { useQuality, useSummary, useStatus } from "../api/hooks";
import type { S } from "../api/client";
import { DataTable } from "../components/DataTable";
import { DrawdownChart, EquityChart } from "../components/charts";
import { AdvanceControls, InitPortfolio } from "../components/PaperControls";
import { Empty, ErrorView, KV, Kpi, Loading, Panel, Signed, StaleBanner } from "../components/ui";
import { fmtPct, fmtPx, fmtTs, fmtUsd } from "../lib/format";

export function CommandCenter() {
  const { data, isLoading, error, refetch } = useSummary();
  const { data: status } = useStatus();
  if (isLoading) return <div className="page"><Loading what="command center" /></div>;
  if (error || !data) return <div className="page"><ErrorView error={error} retry={refetch} /></div>;
  const p = data.portfolio;
  const bench = data.benchmark_symbol;
  const series = data.nav_series;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Command Center</h1>
          <div className="page-sub">
            Internal virtual portfolio · valued at the close of {data.valuation_session ?? "—"} · Paper Simulation on{" "}
            {status?.data_label ?? "—"}
          </div>
        </div>
        {p && <AdvanceControls />}
      </div>
      <StaleBanner reason={data.data_is_stale ? data.data_stale_reason : null} />

      {!p ? (
        <Panel title="Virtual portfolio"><InitPortfolio /></Panel>
      ) : (
        <>
          <div className="kpi-strip">
            <Kpi label="Virtual NAV" value={fmtUsd(data.nav, true)} sub={`cash + marked positions · ${data.valuation_session}`}
              tip="NAV = cash + Σ shares × raw close of the valuation session (carried-forward close if a stock had no bar)." />
            <Kpi label="Cash" value={fmtUsd(data.cash, true)} sub={`${fmtPct((data.cash ?? 0) / (data.nav ?? 1), 1)} of NAV · ${data.positions} positions`} />
            <Kpi label="Day" value={fmtPct(data.daily_return, 2, true)} tone={data.daily_return}
              sub={`${data.daily_pnl != null && data.daily_pnl >= 0 ? "+" : ""}${fmtUsd(data.daily_pnl, true)} vs prior close`} />
            <Kpi label="Since inception" value={fmtPct(data.inception_return, 2, true)} tone={data.inception_return}
              sub={`gross ${fmtPct(data.gross_inception_return, 2, true)} · since ${p.inception_session}`}
              tip="Net of assumed slippage/commissions. Gross adds back cumulative costs (not compounded)." />
            <Kpi label={`${bench ?? "Benchmark"} same period`} value={fmtPct(data.benchmark_inception_return, 2, true)}
              tone={data.benchmark_inception_return}
              sub={data.benchmark_return_basis === "total_return" ? "total return" : "PRICE RETURN ONLY"}
              tip="Benchmark total-return index from raw closes + dividends over the portfolio's life." />
            <Kpi label="Difference" value={fmtPct(data.return_difference, 2, true)} tone={data.return_difference}
              sub="simple difference, not alpha" tip="Portfolio return minus benchmark return over the same sessions. Not a risk-adjusted or statistically estimated alpha." />
            {data.mode === "long_short" && (
              <Kpi label="Gross / net" value={`${fmtPct((data.long_gross ?? 0) + (data.short_gross ?? 0), 0)} / ${fmtPct(data.net_exposure, 1, true)}`}
                sub={`long ${fmtPct(data.long_gross, 0)} · short ${fmtPct(data.short_gross, 0)}`}
                tip="Long and |short| market value as a share of NAV at the valuation close. Beta-neutral sizing, not dollar-neutral." />
            )}
            <Kpi label="Drawdown" value={fmtPct(data.drawdown, 2)} sub={`max ${fmtPct(data.max_drawdown, 2)}`}
              tip="NAV / running peak NAV − 1, from daily closes since inception." />
            <Kpi label="Next rebalance" value={data.next_rebalance.signal_session ?? "—"}
              sub={
                data.next_rebalance.pending_plan_status === "proposed"
                  ? "PLAN AWAITING DECISION"
                  : data.next_rebalance.fill_session
                  ? `signal close → fill open ${data.next_rebalance.fill_session}`
                  : "—"
              }
              tip="Signals are frozen after the close of the last NYSE session of the month; fills simulate at the next session's open." />
          </div>

          {data.next_rebalance.pending_plan_status === "proposed" && (
            <Group justify="space-between" className="panel" p={8} style={{ borderColor: "#5a4712", background: "#1c1a12" }}>
              <Text size="xs">
                <Badge color="yellow" size="xs" mr={6}>Decision</Badge>
                Rebalance plan #{data.next_rebalance.pending_plan_id} from the {data.next_rebalance.signal_session} signal awaits
                your review. Advancing stops until it is applied or skipped (fills at the {data.next_rebalance.fill_session} open).
              </Text>
              <Button component={Link} to="/rebalance" size="xs" color="yellow" variant="light" leftSection={<IconArrowsExchange size={14} />}>
                Open Rebalance Desk
              </Button>
            </Group>
          )}

          <div className="grid-12">
            <Panel className="span-8" title="Portfolio equity curve" label="Paper Simulation"
              source={`Daily NAV in USD at each session close since ${p.inception_session}. ${bench} line: the benchmark's total-return index scaled to the same starting capital. Scroll/pinch to zoom.`}>
              {series.length > 1 ? (
                <EquityChart points={series} benchmarkName={bench} showGross height={372} />
              ) : (
                <Empty title="Only the inception close so far">Advance the portfolio to build a history.</Empty>
              )}
            </Panel>
            <div className="span-4" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <DataStatus />
              <Panel title="Session & timestamps" source="Session status comes from the XNYS calendar; values are end-of-day. No live quotes are shown anywhere.">
                <KV rows={[
                  ["Valuation session", data.valuation_session ?? "—"],
                  ["Latest stored bar", data.data_end ?? "—"],
                  ["Market clock", status ? `${status.market_clock.status.replace("_", " ")} · ${status.market_clock.now_new_york.slice(11, 16)} ET` : "—"],
                  ["Next signal (month-end close)", data.next_rebalance.signal_session ?? "—"],
                  ["Next fill (open)", data.next_rebalance.fill_session ?? "—"],
                  ["Cumulative assumed costs", fmtUsd(p.cum_costs, true)],
                ]} />
              </Panel>
            </div>

            <Panel className="span-8" title="Drawdown" label="Paper Simulation"
              source="Drawdown = NAV ÷ running peak NAV − 1 (percent), daily closes. Benchmark drawdown on its total-return index.">
              {series.length > 1 ? (
                <DrawdownChart sessions={series.map((s) => s.session)} strategy={series.map((s) => s.drawdown)}
                  benchmark={benchDrawdown(series)} benchmarkName={bench} />
              ) : <Empty title="No drawdown history yet" />}
            </Panel>
            <Panel className="span-4" title="Alerts" source="Latest warnings and errors from the immutable audit log (data imports, valuation, rebalances).">
              {data.alerts.length === 0 ? <Empty title="No warnings" /> : (
                <div style={{ maxHeight: 190, overflow: "auto" }}>
                  {data.alerts.map((a) => (
                    <div key={a.id} className="check-row">
                      <Badge size="xs" color={a.level === "error" ? "red" : "yellow"} variant="light" style={{ flexShrink: 0 }}>{a.level}</Badge>
                      <div>
                        <Text size="xs">{a.message}</Text>
                        <Text size="10px" c="dimmed">{a.session ?? ""} · {fmtTs(a.ts)} · {a.category}</Text>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            <Panel className="span-6" title="Top movers (1 session)" label="Paper Simulation" pad={false}
              source={`Holdings ranked by absolute 1-session total return (close ${data.valuation_session} vs prior close, incl. dividends). P&L ≈ market value × return.`}>
              <DataTable<S["Mover"]> data={data.top_movers} maxHeight={260} empty="No holdings"
                cols={[
                  { id: "symbol", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
                  { id: "ret", header: "1D return", align: "right", value: (r) => r.return_1d, cell: (r) => <Signed v={r.return_1d}>{fmtPct(r.return_1d, 2, true)}</Signed> },
                  { id: "pnl", header: "1D P&L", align: "right", value: (r) => r.pnl_1d, cell: (r) => <Signed v={r.pnl_1d}>{fmtUsd(r.pnl_1d, true)}</Signed> },
                  { id: "w", header: "Weight", align: "right", value: (r) => r.weight, cell: (r) => fmtPct(r.weight, 2) },
                ]} />
            </Panel>
            <Panel className="span-6" title="Recent simulated fills" label="Paper Simulation" pad={false}
              source="Latest fills in the internal ledger: open price ± assumed slippage, whole shares. Not broker executions.">
              <DataTable<S["FillModel"]> data={data.recent_fills} maxHeight={260} empty="No fills yet"
                cols={[
                  { id: "s", header: "Session", value: (r) => r.session },
                  { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
                  { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "buy" ? "blue" : "orange"}>{{ open_short: "short", close_short: "cover" }[r.position_effect ?? ""] ?? r.side}</Badge> },
                  { id: "q", header: "Shares", align: "right", value: (r) => r.shares },
                  { id: "px", header: "Fill px", align: "right", value: (r) => r.fill_price, cell: (r) => fmtPx(r.fill_price) },
                  { id: "v", header: "Value", align: "right", value: (r) => r.gross_value, cell: (r) => fmtUsd(r.gross_value, true) },
                ]} />
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}

function benchDrawdown(series: S["NavPoint"][]) {
  let peak = -Infinity;
  return series.map((s) => {
    if (s.benchmark_nav == null) return null;
    peak = Math.max(peak, s.benchmark_nav);
    return s.benchmark_nav / peak - 1;
  });
}

export function DataStatus() {
  const { data: q, isLoading, error } = useQuality();
  return (
    <Panel title="Data status" label={q?.provider.data_label}
      source="From the data-quality check: latest import, coverage of the stored bars, and whether the latest completed NYSE session is present.">
      {isLoading ? <Loading /> : error || !q ? <ErrorView error={error} /> : (
        <KV rows={[
          ["Provider / feed", `${q.provider.name} · ${q.provider.feed}`],
          ["Coverage", `${q.coverage_start ?? "—"} → ${q.coverage_end ?? "—"}`],
          ["Freshness", q.is_stale ? <span className="warn">STALE</span> : <span className="up">✓ current</span>],
          ["Universe / eligible", `${q.universe_count} / ${q.eligible_count ?? "—"}`],
          ["Missing bars / opens", `${q.missing_bars} / ${q.missing_opens}`],
          ["Last successful import", fmtTs(q.latest_successful_import?.finished_at)],
        ]} />
      )}
    </Panel>
  );
}
