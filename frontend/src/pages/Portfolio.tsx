import { useMemo } from "react";
import { Badge, Text, Tooltip } from "@mantine/core";
import { usePositions, useStatus } from "../api/hooks";
import { ApiError, type S } from "../api/client";
import { DataTable } from "../components/DataTable";
import { EChart, baseOption, valueAxis } from "../components/EChart";
import { InitPortfolio } from "../components/PaperControls";
import { Empty, ErrorView, KV, Kpi, Loading, Panel, Signed } from "../components/ui";
import { C } from "../lib/colors";
import { fmtNum, fmtPct, fmtPx, fmtUsd } from "../lib/format";

type Row = S["PositionRow"];

export function Portfolio() {
  const { data: status } = useStatus();
  const { data, isLoading, error, refetch } = usePositions(!!status?.has_portfolio);
  if (status && !status.has_portfolio) return <div className="page"><Panel title="Portfolio"><InitPortfolio /></Panel></div>;
  if (isLoading || !status) return <div className="page"><Loading what="positions" /></div>;
  if (error instanceof ApiError && error.code === "no_portfolio") return <div className="page"><Panel title="Portfolio"><InitPortfolio /></Panel></div>;
  if (error || !data) return <div className="page"><ErrorView error={error} retry={refetch} /></div>;
  const c = data.concentration;
  const stale = data.rows.filter((r) => r.stale_mark).length;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Model Portfolio</h1>
          <div className="page-sub">{data.valuation_note} · Paper Simulation</div>
        </div>
        <Badge variant="light" color="teal" size="sm">Valuation timestamp: close of {data.valuation_session}</Badge>
      </div>

      <div className="kpi-strip">
        <Kpi label="NAV" value={fmtUsd(data.nav, true)} sub="cash + Σ shares × mark" />
        <Kpi label="Cash" value={fmtUsd(data.cash, true)} sub={`${fmtPct(data.cash_weight, 1)} of NAV (incl. short proceeds)`} />
        {(
          <Kpi label="Gross / net" value={`${fmtPct(data.long_gross + data.short_gross, 0)} / ${fmtPct(data.net_exposure, 1, true)}`}
            sub={`long ${fmtPct(data.long_gross, 1)} · short ${fmtPct(data.short_gross, 1)}`}
            tip="Long market value / NAV and |short market value| / NAV at the valuation close. The book targets beta neutrality, so net dollar exposure can differ from zero." />
        )}
        <Kpi label="Positions" value={fmtNum(data.rows.length)} sub={stale ? `${stale} stale mark(s)` : "all marked at the valuation close"} />
        <Kpi label="Largest" value={fmtPct(c.largest_weight, 2)} sub={c.largest_symbol ?? "—"} />
        <Kpi label="Top 5 / Top 10" value={`${fmtPct(c.top5_weight, 1)} / ${fmtPct(c.top10_weight, 1)}`} sub="share of NAV" />
        <Kpi label="Effective N" value={c.effective_n ? c.effective_n.toFixed(1) : "—"} sub={`HHI ${c.hhi.toFixed(4)} of invested`}
          tip="HHI = Σ w² using weights of the invested amount; effective number of positions = 1 / HHI." />
      </div>

      <Panel title="Positions" label="Paper Simulation" pad={false}
        source={`Shares from the internal ledger. Mark = raw close of ${data.valuation_session} (or last available close, flagged). Target = equal weight in the latest frozen plan (${data.plan_signal_session ?? "none"}). Drift = current − target weight. Contribution = unrealized P&L ÷ NAV. 1D = total return vs prior close.`}>
        {data.rows.length === 0 ? <Empty title="No positions">The portfolio holds only cash. Apply a rebalance plan to invest.</Empty> : (
          <DataTable<Row>
            data={data.rows}
            maxHeight={430}
            initialSort={[{ id: "value", desc: true }]}
            rowKey={(r) => r.symbol}
            cols={[
              { id: "symbol", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
              { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "long" ? "blue" : "orange"}>{r.side}</Badge> },
              { id: "name", header: "Name", value: (r) => r.name, cell: (r) => <Text size="xs" truncate maw={180}>{r.name}</Text> },
              { id: "sector", header: "Sector", value: (r) => r.sector },
              { id: "shares", header: "Shares", align: "right", value: (r) => r.shares, cell: (r) => fmtNum(r.shares) },
              { id: "mark", header: "Mark $", align: "right", value: (r) => r.mark_price, cell: (r) => (
                <span>{fmtPx(r.mark_price)}{r.stale_mark && <Tooltip label={`Carried from ${r.mark_session}`}><Badge ml={4} size="xs" color="yellow" variant="light">stale</Badge></Tooltip>}</span>) },
              { id: "value", header: "Mkt value", align: "right", value: (r) => Math.abs(r.market_value), cell: (r) => fmtUsd(r.market_value, true), tip: "Signed: shorts are negative" },
              { id: "w", header: "Weight", align: "right", value: (r) => r.weight, cell: (r) => fmtPct(r.weight, 2, true) },
              { id: "tw", header: "Target", align: "right", value: (r) => r.target_weight, cell: (r) => fmtPct(r.target_weight, 2, true) },
              { id: "drift", header: "Drift", align: "right", value: (r) => r.drift, cell: (r) => <Signed v={r.drift}>{fmtPct(r.drift, 2, true)}</Signed> },
              { id: "basis", header: "Cost basis", align: "right", value: (r) => r.cost_basis, cell: (r) => fmtUsd(r.cost_basis, true), tip: "Signed, average-cost: longs = cash paid incl. costs; shorts = −(net sale proceeds)" },
              { id: "pnl", header: "Unreal. P&L", align: "right", value: (r) => r.unrealized_pnl, cell: (r) => <Signed v={r.unrealized_pnl}>{fmtUsd(r.unrealized_pnl, true)}</Signed> },
              { id: "pct", header: "P&L %", align: "right", value: (r) => r.unrealized_pct, cell: (r) => <Signed v={r.unrealized_pct}>{fmtPct(r.unrealized_pct, 1, true)}</Signed> },
              { id: "contrib", header: "Contrib.", align: "right", value: (r) => r.contribution, cell: (r) => <Signed v={r.contribution}>{fmtPct(r.contribution, 2, true)}</Signed> },
              { id: "r1", header: "1D", align: "right", value: (r) => r.return_1d, cell: (r) => <Signed v={r.return_1d}>{fmtPct(r.return_1d, 2, true)}</Signed> },
              { id: "open", header: "Opened", value: (r) => r.opened_session },
            ]}
          />
        )}
      </Panel>

      <div className="grid-12">
        <Panel className="span-6" title="Drift from target weight" label="Paper Simulation"
          source="Signed current weight − signed target weight per holding, percentage points of NAV (for shorts, negative drift = larger short than target). Drift accumulates between monthly rebalances as prices move.">
          {data.rows.length ? <DriftChart rows={data.rows} /> : <Empty title="No positions" />}
        </Panel>
        <Panel className="span-3" title="Sector exposure (net)" source={`${data.sector_note} Net = long minus short weight per sector.`}>
          {data.sector_exposure ? <SectorChart rows={data.sector_exposure} /> : <Empty title="Unavailable">{data.sector_note}</Empty>}
        </Panel>
        <Panel className="span-3" title="Concentration" source="Weights are shares of NAV unless stated. Equal-weight target with N holdings implies ~1/N each.">
          <KV rows={[
            ["Largest position", `${c.largest_symbol ?? "—"} · ${fmtPct(c.largest_weight, 2)}`],
            ["Top 5 weight", fmtPct(c.top5_weight, 2)],
            ["Top 10 weight", fmtPct(c.top10_weight, 2)],
            ["HHI (invested)", c.hhi.toFixed(4)],
            ["Effective N", c.effective_n ? c.effective_n.toFixed(1) : "—"],
            ["Cash weight", fmtPct(data.cash_weight, 2)],
            ["Positions", fmtNum(data.rows.length)],
          ]} />
        </Panel>
      </div>
    </div>
  );
}

function DriftChart({ rows }: { rows: Row[] }) {
  const option = useMemo(() => {
    const sorted = [...rows].filter((r) => r.drift != null).sort((a, b) => (a.drift ?? 0) - (b.drift ?? 0));
    const base = baseOption();
    return {
      ...base,
      grid: { left: 48, right: 12, top: 12, bottom: 56 },
      tooltip: { ...(base.tooltip as object), trigger: "item", formatter: (p: { name: string; value: number }) => `${p.name}: ${p.value > 0 ? "+" : ""}${p.value.toFixed(2)} pp` },
      xAxis: { type: "category", data: sorted.map((r) => r.symbol), axisLabel: { rotate: 90, fontSize: 9, color: C.muted, interval: 0 }, axisTick: { show: false }, axisLine: { lineStyle: { color: C.axis } } },
      yAxis: valueAxis((v: number) => `${v.toFixed(1)}pp`, { scale: false }),
      series: [{
        type: "bar", barMaxWidth: 10,
        data: sorted.map((r) => ({ value: +((r.drift ?? 0) * 100).toFixed(3), itemStyle: { color: C.strategy, borderRadius: (r.drift ?? 0) >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3] } })),
      }],
    };
  }, [rows]);
  return <EChart option={option} height={260} />;
}

function SectorChart({ rows }: { rows: S["SectorExposure"][] }) {
  const option = useMemo(() => {
    const r = [...rows].reverse();
    const base = baseOption();
    return {
      ...base,
      grid: { left: 130, right: 44, top: 6, bottom: 20 },
      tooltip: { ...(base.tooltip as object), trigger: "item", formatter: (p: { name: string; value: number; dataIndex: number }) => `${p.name}: ${p.value.toFixed(2)}% of NAV · ${r[p.dataIndex].positions} positions` },
      xAxis: valueAxis((v: number) => `${v}%`, { scale: false }),
      yAxis: { type: "category", data: r.map((x) => x.sector), axisLabel: { color: C.text2, fontSize: 10 }, axisTick: { show: false }, axisLine: { lineStyle: { color: C.axis } } },
      series: [{ type: "bar", barMaxWidth: 12, data: r.map((x) => +(x.weight * 100).toFixed(2)), itemStyle: { color: C.strategy, borderRadius: [0, 3, 3, 0] },
        label: { show: true, position: "right", color: C.text2, fontSize: 10, formatter: (p: { value: number }) => `${p.value.toFixed(1)}%` } }],
    };
  }, [rows]);
  return <EChart option={option} height={Math.max(160, rows.length * 22 + 30)} />;
}
