import { useEffect, useMemo, useState } from "react";
import { Alert, Badge, Button, Checkbox, Code, Divider, Group, NumberInput, Progress, SegmentedControl, SimpleGrid, Stack, Switch, Tabs, Text, TextInput } from "@mantine/core";
import { IconPlayerPlay } from "@tabler/icons-react";
import { useQueries } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { api, type S, type StrategyConfig } from "../api/client";
import { useLaunchRun, useResearchDefaults, useRun, useRunAlpha, useRunMonthly, useRunRebalances, useRunSeries, useRunTrades, useRuns, type AlphaFit } from "../api/hooks";
import { DataTable } from "../components/DataTable";
import { EChart, baseOption, lineSeries, timeAxis, valueAxis, zoom } from "../components/EChart";
import { DrawdownChart, EquityChart, MonthlyHeatmap } from "../components/charts";
import { Empty, ErrorView, KV, Kpi, Loading, Panel, Signed } from "../components/ui";
import { C, SERIES } from "../lib/colors";
import { fmtMillions, fmtNum, fmtPct, fmtPx, fmtTs, fmtUsd } from "../lib/format";

type Metrics = {
  start_session: string; end_session: string; sessions: number; calendar_days: number; cagr_meaningful: boolean;
  final_nav: number; initial_capital: number;
  net: Stat; gross: Stat; benchmark: Stat | null; return_difference: number | null; cagr_difference: number | null;
  rebalances_executed: number; rebalances_blocked: number; avg_turnover: number | null; annualized_turnover: number | null;
  total_costs: number; cost_drag: number | null; avg_positions: number; avg_cash_weight: number;
  benchmark_symbol: string | null; benchmark_return_basis: string | null;
  mode?: string; avg_long_gross?: number; avg_short_gross?: number; avg_gross_exposure?: number; avg_net_exposure?: number;
  max_gross_exposure?: number; realized_beta?: number; correlation?: number; borrow_fees?: number;
  short_dividends_paid?: number; stop_losses?: number; crash_guard_months?: number;
};
type Stat = { total_return: number | null; cagr: number | null; ann_vol: number | null; ret_vol_ratio: number | null; max_drawdown: number | null; max_dd_peak: string | null; max_dd_trough: string | null };

export function ResearchLab() {
  const defaults = useResearchDefaults();
  const runs = useRuns();
  const [selected, setSelected] = useState<number | null>(null);
  const [compare, setCompare] = useState<number[]>([]);
  useEffect(() => {
    if (selected == null && runs.data?.length) setSelected(runs.data[0].id);
  }, [runs.data, selected]);

  if (defaults.isLoading || runs.isLoading) return <div className="page"><Loading what="research lab" /></div>;
  if (defaults.error || !defaults.data) return <div className="page"><ErrorView error={defaults.error} retry={defaults.refetch} /></div>;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Research Lab</h1>
          <div className="page-sub">Event-ordered monthly backtests · every run stores its inputs, data version, results and timestamps</div>
        </div>
      </div>
      {defaults.data.survivorship_warning && (
        <Alert color="red" variant="filled" title="Survivorship-biased universe">{defaults.data.survivorship_warning}</Alert>
      )}
      <div className="grid-12">
        <div className="span-3" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <SettingsForm d={defaults.data} onLaunched={(id) => setSelected(id)} />
          <Panel title="Saved runs" pad={false} source="All runs on the current provider, newest first. Tick up to 4 to compare.">
            <div style={{ maxHeight: 420, overflow: "auto" }}>
              {(runs.data ?? []).map((r) => (
                <div key={r.id} onClick={() => setSelected(r.id)} style={{ padding: "6px 10px", borderBottom: "1px solid var(--grid)", cursor: "pointer", background: selected === r.id ? "#1d2329" : undefined }}>
                  <Group justify="space-between" wrap="nowrap" gap={6}>
                    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                      <Checkbox size="xs" checked={compare.includes(r.id)} aria-label={`Compare run ${r.id}`}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setCompare((c) => (e.currentTarget.checked ? [...c, r.id].slice(-4) : c.filter((x) => x !== r.id)))} />
                      <Text size="xs" fw={600} truncate>#{r.id} {r.name}</Text>
                    </Group>
                    <RunBadge r={r} />
                  </Group>
                  {r.status === "running" || r.status === "queued" ? (
                    <Progress value={r.progress * 100} size="xs" mt={4} animated />
                  ) : r.headline ? (
                    <Text size="10px" c="dimmed" className="mono" mt={2}>
                      net {fmtPct(r.headline.net_total_return as number, 1, true)} · bench {fmtPct(r.headline.benchmark_total_return as number, 1, true)} · maxDD {fmtPct(r.headline.max_drawdown as number, 1)}
                    </Text>
                  ) : r.error ? <Text size="10px" c="red">{r.error}</Text> : null}
                </div>
              ))}
              {!runs.data?.length && <Empty title="No runs yet">Configure and launch a backtest.</Empty>}
            </div>
          </Panel>
        </div>
        <div className="span-9" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {compare.length >= 2 ? (
            <Compare ids={compare} onClear={() => setCompare([])} />
          ) : selected != null ? (
            <RunView id={selected} />
          ) : (
            <Panel title="Results"><Empty title="Select or launch a run" /></Panel>
          )}
        </div>
      </div>
    </div>
  );
}

function RunBadge({ r }: { r: S["RunSummary"] }) {
  const color = { completed: "teal", running: "blue", queued: "gray", failed: "red" }[r.status];
  return <Badge size="xs" variant="light" color={color}>{r.status}</Badge>;
}

function SettingsForm({ d, onLaunched }: { d: S["ResearchDefaults"]; onLaunched: (id: number) => void }) {
  const [cfg, setCfg] = useState<StrategyConfig>({ ...d.config, start_date: d.earliest_start, end_date: d.latest_end });
  const [name, setName] = useState("");
  const launch = useLaunchRun();
  const num = (k: keyof StrategyConfig) => (v: string | number) => setCfg((c) => ({ ...c, [k]: v === "" ? c[k] : Number(v) }));
  // percentages are edited as % in the form and stored as fractions
  const pct = (k: keyof StrategyConfig) => ({
    value: +(((cfg[k] as number) ?? 0) * 100).toFixed(4),
    onChange: (v: string | number) => setCfg((c) => ({ ...c, [k]: v === "" ? c[k] : Number(v) / 100 })),
  });
  const ls = true;
  const warn = configWarnings(cfg);
  return (
    <Panel title="Backtest settings" label="Backtest"
      source={`Dates are NYSE sessions within stored data (${d.earliest_start} … ${d.latest_end}); the first ${d.config.lookback_sessions} sessions are needed for the lookback. Costs and borrow fees are ASSUMPTIONS, not observed executions.`}>
      <Stack gap={6}>
        <SegmentedControl fullWidth value={cfg.signal} onChange={(v) => setCfg((c) => ({ ...c, signal: v as StrategyConfig["signal"] }))}
          data={[{ value: "composite", label: "Composite signal" }, { value: "momentum_12_1", label: "Plain 12-1" }]} />
        {cfg.signal === "composite" && (
          <SimpleGrid cols={3} spacing={6}>
            <NumberInput label="Residual %" {...pct("w_residual")} min={0} max={100} suffix="%" />
            <NumberInput label="Sector-dm %" {...pct("w_sector_demeaned")} min={0} max={100} suffix="%" />
            <NumberInput label="FIP %" {...pct("w_fip")} min={0} max={100} suffix="%" />
          </SimpleGrid>
        )}
        <SimpleGrid cols={2} spacing={6}>
          <TextInput label="Start" type="date" value={cfg.start_date ?? ""} min={d.earliest_start} max={d.latest_end}
            onChange={(e) => { const v = e.currentTarget.value; setCfg((c) => ({ ...c, start_date: v || null })); }} />
          <TextInput label="End" type="date" value={cfg.end_date ?? ""} min={d.earliest_start} max={d.latest_end}
            onChange={(e) => { const v = e.currentTarget.value; setCfg((c) => ({ ...c, end_date: v || null })); }} />
          <NumberInput label="Initial capital $" value={cfg.initial_capital} onChange={num("initial_capital")} min={1000} step={10000} thousandSeparator="," />
          <NumberInput label="Min ADV $" value={cfg.min_adv_usd} onChange={num("min_adv_usd")} min={0} step={1e6} thousandSeparator="," />
          <NumberInput label="Min price $" value={cfg.min_price} onChange={num("min_price")} min={0} decimalScale={2} />
          <NumberInput label="Slippage (bps)" value={cfg.slippage_bps} onChange={num("slippage_bps")} min={0} max={500} />
          <NumberInput label="Commission $/fill" value={cfg.commission_per_order} onChange={num("commission_per_order")} min={0} decimalScale={2} />
        </SimpleGrid>
        {ls && (
          <>
            <Divider label="Books" labelPosition="left" />
            <SimpleGrid cols={2} spacing={6}>
              <NumberInput label="Long top %" {...pct("long_pct")} min={1} max={50} suffix="%" />
              <NumberInput label="Short bottom %" {...pct("short_pct")} min={1} max={50} suffix="%" />
              <NumberInput label="Min names / side" value={cfg.min_names_per_side} onChange={num("min_names_per_side")} min={1} max={1000} />
              <NumberInput label="Max names / side" value={cfg.max_names_per_side} onChange={num("max_names_per_side")} min={1} max={1000} />
              <NumberInput label="Buffer exit %" {...pct("buffer_exit_pct")} min={1} max={50} suffix="%" />
              <NumberInput label="Short min price $" value={cfg.short_min_price} onChange={num("short_min_price")} min={0} decimalScale={2} />
            </SimpleGrid>
            <Divider label="Sizing" labelPosition="left" />
            <SimpleGrid cols={2} spacing={6}>
              <NumberInput label="Vol target %" {...pct("target_vol")} min={1} max={100} suffix="%" />
              <NumberInput label="Total gross cap %" {...pct("max_total_gross")} min={10} max={400} suffix="%" />
              <NumberInput label="Min gross / side %" {...pct("min_side_gross")} min={0} max={200} suffix="%" />
              <NumberInput label="Max gross / side %" {...pct("max_side_gross")} min={1} max={200} suffix="%" />
              <NumberInput label="Cap per long %" {...pct("max_long_weight")} min={0.1} max={100} decimalScale={2} suffix="%" />
              <NumberInput label="Cap per short %" {...pct("max_short_weight")} min={0.1} max={100} decimalScale={2} suffix="%" />
              <NumberInput label="Max sector net %" {...pct("max_sector_net")} min={0} max={100} decimalScale={1} suffix="%" disabled={!cfg.sector_neutral} />
              <NumberInput label="HTB exclude %" {...pct("htb_exclude_pct")} min={0} max={90} suffix="%" />
            </SimpleGrid>
            <Switch size="xs" label="Sector neutral (|long − short| per sector)" checked={cfg.sector_neutral}
              onChange={(e) => { const v = e.currentTarget.checked; setCfg((c) => ({ ...c, sector_neutral: v })); }} />
            <Divider label="Short risk" labelPosition="left" />
            <SimpleGrid cols={2} spacing={6}>
              <NumberInput label="Borrow fee %/yr" {...pct("borrow_fee_annual")} min={0} max={100} decimalScale={2} suffix="%" />
              <NumberInput label="Short stop-loss %" value={cfg.short_stop_loss == null ? "" : +(cfg.short_stop_loss * 100).toFixed(2)}
                onChange={(v) => setCfg((c) => ({ ...c, short_stop_loss: v === "" ? null : Number(v) / 100 }))} min={1} max={1000} suffix="%" placeholder="off" />
              <NumberInput label="Crash: market vol >" {...pct("crash_market_vol_threshold")} min={1} max={200} suffix="%" disabled={!cfg.crash_guard} />
              <NumberInput label="Crash: short scale" value={cfg.crash_short_scale} onChange={num("crash_short_scale")} min={0} max={1} step={0.1} decimalScale={2} disabled={!cfg.crash_guard} />
            </SimpleGrid>
            <Switch size="xs" label="Cash earns RF (Ken French, ACT/360) — alpha test then uses r − RF" checked={cfg.cash_interest}
              onChange={(e) => { const v = e.currentTarget.checked; setCfg((c) => ({ ...c, cash_interest: v })); }} />
            <Switch size="xs" label="Crash guard (bear market + high vol → shrink short book)" checked={cfg.crash_guard}
              onChange={(e) => { const v = e.currentTarget.checked; setCfg((c) => ({ ...c, crash_guard: v })); }} />
          </>
        )}
        {warn.map((w, i) => <Alert key={i} color="yellow" variant="light" p={6}><Text size="10px">{w}</Text></Alert>)}
        <TextInput label="Run name (optional)" value={name} onChange={(e) => setName(e.currentTarget.value)}
          placeholder={`${cfg.signal === "composite" ? "Composite" : "12-1"} L/S vol ${Math.round(cfg.target_vol * 100)}%`} />
        <Button leftSection={<IconPlayerPlay size={14} />} loading={launch.isPending}
          onClick={() => launch.mutate({ config: cfg, name: name || null }, { onSuccess: (r) => onLaunched(r.id) })}>
          Run backtest
        </Button>
        <Text size="10px" c="dimmed">Benchmark: {d.benchmark_symbol ?? "none"} · Data: {d.data_label} · Rebalance: monthly, signal after last session's close, fills at next open.</Text>
      </Stack>
    </Panel>
  );
}

/** Mirrors StrategyConfig.config_warnings() on the server so the warning shows while editing. */
function configWarnings(c: StrategyConfig): string[] {
  const out: string[] = [];
  const capS = c.min_names_per_side * c.max_short_weight;
  if (capS < c.max_side_gross - 1e-12)
    out.push(`Short caps limit the short book: ${c.min_names_per_side} × ${(c.max_short_weight * 100).toFixed(2)}% = ${(capS * 100).toFixed(1)}% < max side gross ${(c.max_side_gross * 100).toFixed(0)}% — the vol target may be unreachable.`);
  const capL = c.min_names_per_side * c.max_long_weight;
  if (capL < c.max_side_gross - 1e-12)
    out.push(`Long caps limit the long book: ${c.min_names_per_side} × ${(c.max_long_weight * 100).toFixed(2)}% = ${(capL * 100).toFixed(1)}% < max side gross.`);
  return out;
}

function RunView({ id }: { id: number }) {
  const [params] = useSearchParams();
  const { data: run, error, refetch } = useRun(id);
  const ready = run?.status === "completed";
  const series = useRunSeries(id, ready);
  const monthly = useRunMonthly(id, ready);
  if (error) return <ErrorView error={error} retry={refetch} />;
  if (!run) return <Loading what="run" />;
  if (run.status === "queued" || run.status === "running")
    return (
      <Panel title={`Run #${run.id} · ${run.name}`} label="Backtest">
        <Text size="xs" mb={6}>{run.progress_note ?? "Queued"}</Text>
        <Progress value={run.progress * 100} animated />
      </Panel>
    );
  if (run.status === "failed") return <Alert color="red" title={`Run #${run.id} failed`}>{run.error}</Alert>;
  const m = run.metrics as unknown as Metrics;
  const bench = m.benchmark_symbol;
  const pts = series.data ?? [];

  return (
    <>
      <div className="kpi-strip">
        <Kpi label="Net total return" value={fmtPct(m.net.total_return, 1, true)} tone={m.net.total_return} sub={`${m.start_session} → ${m.end_session}`} />
        <Kpi label="CAGR (net)" value={m.cagr_meaningful ? fmtPct(m.net.cagr, 2, true) : "n/a"} tone={m.cagr_meaningful ? m.net.cagr : undefined}
          sub={m.cagr_meaningful ? `gross ${fmtPct(m.gross.cagr, 2, true)}` : "sample < 1 year"}
          tip="(NAV_end/NAV_start)^(365.25/days) − 1. Only reported for samples of at least 365 calendar days." />
        <Kpi label="Ann. volatility" value={fmtPct(m.net.ann_vol, 1)} sub={`ret/vol ${m.net.ret_vol_ratio?.toFixed(2) ?? "—"} (rf = 0)`} tip="stdev(daily returns) × √252" />
        <Kpi label="Max drawdown" value={fmtPct(m.net.max_drawdown, 1)} sub={`${m.net.max_dd_peak} → ${m.net.max_dd_trough}`} />
        <Kpi label={`${bench ?? "Benchmark"} return`} value={fmtPct(m.benchmark?.total_return, 1, true)} tone={m.benchmark?.total_return}
          sub={m.benchmark_return_basis === "total_return" ? `total return · CAGR ${fmtPct(m.benchmark?.cagr, 2)}` : "PRICE RETURN ONLY"} />
        <Kpi label="Difference" value={fmtPct(m.return_difference, 1, true)} tone={m.return_difference} sub="simple difference, not alpha" />
        <Kpi label="Turnover" value={fmtPct(m.avg_turnover, 1)} sub={`per rebalance · ${m.annualized_turnover ? `${fmtPct(m.annualized_turnover, 0)}/yr` : "—"}`} tip="One-way: (buys + sells) / 2 / NAV at the rebalance open." />
        <Kpi label="Assumed costs" value={fmtUsd(m.total_costs + (m.borrow_fees ?? 0))}
          sub={m.borrow_fees ? `trading ${fmtUsd(m.total_costs)} · borrow ${fmtUsd(m.borrow_fees)}` : `drag ${fmtPct(m.cost_drag, 2)} of capital`} />
        {(
          <>
            <Kpi label="Avg gross / net" value={`${fmtPct(m.avg_gross_exposure, 0)} / ${fmtPct(m.avg_net_exposure, 0, true)}`}
              sub={`long ${fmtPct(m.avg_long_gross, 0)} · short ${fmtPct(m.avg_short_gross, 0)}`}
              tip="(long market value + |short market value|) / NAV and (long − |short|) / NAV, averaged over daily closes." />
            <Kpi label="Realized beta" value={m.realized_beta?.toFixed(2) ?? "—"} sub={`corr ${m.correlation?.toFixed(2) ?? "—"} vs ${bench ?? "benchmark"}`}
              tip="cov(strategy, benchmark) / var(benchmark) of daily returns over the run (descriptive, not a forecast)." />
            <Kpi label="Short stops / crash guard" value={`${m.stop_losses ?? 0} / ${m.crash_guard_months ?? 0}`}
              sub={`stop-loss covers · months guard ON · dividends paid ${fmtUsd(m.short_dividends_paid ?? 0)}`} />
          </>
        )}
      </div>

      <Tabs defaultValue={params.get("tab") ?? "equity"} keepMounted={false} variant="outline" radius="sm">
        <Tabs.List>
          <Tabs.Tab value="equity">Equity & drawdown</Tabs.Tab>
          <Tabs.Tab value="exposure">Exposure</Tabs.Tab>
          <Tabs.Tab value="alpha">Factor alpha</Tabs.Tab>
          <Tabs.Tab value="rolling">Rolling metrics</Tabs.Tab>
          <Tabs.Tab value="monthly">Monthly returns</Tabs.Tab>
          <Tabs.Tab value="trades">Trades</Tabs.Tab>
          <Tabs.Tab value="rebalances">Rebalances</Tabs.Tab>
          <Tabs.Tab value="assumptions">Assumptions & reproducibility</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="equity" pt={10}>
          {!pts.length ? <Loading what="series" /> : (
            <Stack gap={10}>
              <Panel title="Equity curve (USD)" label="Backtest"
                source={`Daily NAV at each close, ${m.start_session} to ${m.end_session}. Net = after assumed costs; gross adds back cumulative costs; ${bench} = total-return index scaled to the same starting capital.`}>
                <EquityChart points={pts} benchmarkName={bench} showGross netName="Net NAV" height={320} />
              </Panel>
              <Panel title="Drawdown" label="Backtest" source="NAV ÷ running peak − 1, percent, daily closes.">
                <DrawdownChart sessions={pts.map((p) => p.session)} strategy={pts.map((p) => p.drawdown)} benchmark={pts.map((p) => p.benchmark_drawdown)} benchmarkName={bench} />
              </Panel>
            </Stack>
          )}
        </Tabs.Panel>
        <Tabs.Panel value="exposure" pt={10}>
          {!pts.length ? <Loading /> : <ExposureChart pts={pts} />}
        </Tabs.Panel>
        <Tabs.Panel value="alpha" pt={10}>
          <AlphaPanel id={id} />
        </Tabs.Panel>
        <Tabs.Panel value="rolling" pt={10}>
          {!pts.length ? <Loading /> : <Rolling pts={pts} bench={bench} />}
        </Tabs.Panel>
        <Tabs.Panel value="monthly" pt={10}>
          <Panel title="Monthly return history" label="Backtest" source="Calendar-month returns of net NAV and of the benchmark (same capital base).">
            {monthly.data ? <MonthlyHeatmap rows={monthly.data} /> : <Loading />}
          </Panel>
        </Tabs.Panel>
        <Tabs.Panel value="trades" pt={10}><Trades id={id} /></Tabs.Panel>
        <Tabs.Panel value="rebalances" pt={10}><Rebalances id={id} /></Tabs.Panel>
        <Tabs.Panel value="assumptions" pt={10}>
          <div className="grid-12">
            <Panel className="span-7" title="Assumptions & formulas" label="Backtest">
              {run.assumptions.map((a, i) => (
                <div className="check-row" key={i}><Badge size="xs" variant="outline" color="gray" style={{ flexShrink: 0 }}>{String(a.key)}</Badge><Text size="xs">{String(a.text)}</Text></div>
              ))}
              <Text size="xs" fw={600} mt={8}>Metric definitions</Text>
              <Code block fz="10px" mt={4}>{`daily return      r_t = NAV_t / NAV_{t-1} - 1
total return      NAV_end / NAV_start - 1
CAGR              (NAV_end/NAV_start)^(365.25/days) - 1   (only if days >= 365)
ann. volatility   stdev(r_t) * sqrt(252)
return/vol        mean(r_t) * 252 / ann. volatility        (risk-free = 0)
drawdown          NAV_t / max(NAV_0..t) - 1
turnover          (buys + sells) / 2 / NAV at the open
difference        strategy total return - benchmark total return (not alpha)
gross NAV         net NAV + cumulative slippage & commission (not compounded)`}</Code>
              {run.warnings.length > 0 && (
                <Alert color="yellow" variant="light" mt={8} title={`${run.warnings.length} warning(s)`}>
                  <div style={{ maxHeight: 140, overflow: "auto" }}>{run.warnings.map((w, i) => <Text key={i} size="10px">{w}</Text>)}</div>
                </Alert>
              )}
            </Panel>
            <Panel className="span-5" title="Reproducibility" source="Re-running the same config on the same data version reproduces identical results.">
              <KV rows={[
                ["Run id", `#${run.id}`], ["Created", fmtTs(run.created_at)], ["Finished", fmtTs(run.finished_at)],
                ["Provider / label", `${run.provider} · ${run.data_label}`], ["Data version", <span style={{ fontSize: 10 }}>{run.data_version}</span>],
                ...Object.entries(run.repro ?? {}).filter(([k]) => !["note", "data_version", "provider"].includes(k)).map(([k, v]) => [k, String(v)] as [string, string]),
              ]} />
              <Text size="xs" fw={600} mt={8}>Config</Text>
              <Code block fz="10px">{JSON.stringify(run.config, null, 2)}</Code>
            </Panel>
          </div>
        </Tabs.Panel>
      </Tabs>
    </>
  );
}

function AlphaPanel({ id }: { id: number }) {
  const { data, isLoading, error } = useRunAlpha(id, true);
  if (isLoading) return <Loading what="factor regression (downloads Ken French data on first use)" />;
  if (error || !data) return <ErrorView error={error} />;
  const cols: [string, AlphaFit | null][] = [["Full sample", data.full], ["First half", data.first_half], ["Second half", data.second_half]];
  const factors = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"];
  const t = (v?: number) => (v == null ? "—" : v.toFixed(2));
  return (
    <Panel title="Fama-French 5 + momentum regression" label="Backtest"
      source={`${data.model}. Source: ${data.source}. Alpha is the monthly intercept (×12 annualized); |t| > 2 is conventionally significant, but multiple testing and a biased universe (Alpaca: today's liquid stocks) make even that weak evidence.`}>
      <div style={{ overflowX: "auto" }}>
        <table className="dt" style={{ minWidth: 640 }}>
          <thead>
            <tr><th />{cols.map(([n, f]) => <th key={n} style={{ textAlign: "right" }}>{n}{f ? ` (${f.start}–${f.end}, n=${f.months})` : ""}</th>)}</tr>
          </thead>
          <tbody>
            <tr>
              <td><b>Alpha (annualized)</b></td>
              {cols.map(([n, f]) => <td key={n} className="num" style={{ textAlign: "right" }}>{f ? <Signed v={f.alpha_annual}>{fmtPct(f.alpha_annual, 2, true)}</Signed> : "n/a"}</td>)}
            </tr>
            <tr>
              <td>t-stat (Newey-West)</td>
              {cols.map(([n, f]) => <td key={n} className="num" style={{ textAlign: "right" }}>{f ? <span className={Math.abs(f.t_alpha) >= 2 ? "" : "muted"}>{t(f.t_alpha)}</span> : "—"}</td>)}
            </tr>
            {factors.map((fac) => (
              <tr key={fac}>
                <td>β {fac}</td>
                {cols.map(([n, f]) => <td key={n} className="num" style={{ textAlign: "right" }}>{f ? `${t(f.betas[fac])} (t ${t(f.t_betas[fac])})` : "—"}</td>)}
              </tr>
            ))}
            <tr><td>R²</td>{cols.map(([n, f]) => <td key={n} className="num" style={{ textAlign: "right" }}>{f?.r2 != null ? f.r2.toFixed(2) : "—"}</td>)}</tr>
            <tr><td>NW lags</td>{cols.map(([n, f]) => <td key={n} className="num" style={{ textAlign: "right" }}>{f ? f.nw_lags : "—"}</td>)}</tr>
          </tbody>
        </table>
      </div>
      {data.notes.map((n, i) => <Text key={i} size="10px" c="dimmed" mt={4}>{n}</Text>)}
    </Panel>
  );
}

function ExposureChart({ pts }: { pts: S["RunSeriesPoint"][] }) {
  const option = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => fmtPct(v, 1) },
      xAxis: timeAxis(pts.map((p) => p.session)), yAxis: valueAxis((v: number) => `${(v * 100).toFixed(0)}%`), dataZoom: zoom,
      series: [
        lineSeries("Long gross", pts.map((p) => p.long_gross ?? null), C.strategy),
        lineSeries("Short gross", pts.map((p) => p.short_gross ?? null), C.benchmark, { lineStyle: { width: 1.5, color: C.benchmark } }),
        lineSeries("Net exposure", pts.map((p) => p.net_exposure ?? null), C.gross, { lineStyle: { width: 1.5, color: C.gross, type: [4, 3] } }),
      ],
    };
  }, [pts]);
  return (
    <Panel title="Exposure (% of NAV)" label="Backtest"
      source="Daily long market value / NAV, |short market value| / NAV, and net = long − short. Sizing targets beta neutrality (not dollar neutrality), so net dollar exposure is usually non-zero.">
      <EChart option={option} height={300} />
    </Panel>
  );
}

function Rolling({ pts, bench }: { pts: S["RunSeriesPoint"][]; bench: string | null }) {
  const dates = pts.map((p) => p.session);
  const retOpt = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => fmtPct(v, 1, true) },
      xAxis: timeAxis(dates), yAxis: valueAxis((v: number) => `${(v * 100).toFixed(0)}%`), dataZoom: zoom,
      series: [
        lineSeries("Strategy 12-month return", pts.map((p) => p.rolling_return), C.strategy),
        lineSeries(`${bench ?? "Benchmark"} 12-month return`, pts.map((p) => p.rolling_bench_return), C.benchmark, { lineStyle: { width: 1.5, color: C.benchmark } }),
      ],
    };
  }, [pts, bench, dates]);
  const volOpt = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => fmtPct(v, 1) },
      xAxis: timeAxis(dates), yAxis: valueAxis((v: number) => `${(v * 100).toFixed(0)}%`), dataZoom: zoom,
      series: [lineSeries("Strategy 63-session annualized volatility", pts.map((p) => p.rolling_vol), C.strategy)],
    };
  }, [pts, dates]);
  return (
    <Stack gap={10}>
      <Panel title="Rolling 12-month (252-session) return" label="Backtest" source="NAV_t / NAV_{t−252} − 1 for the strategy and the benchmark. Blank until 252 sessions of history exist.">
        <EChart option={retOpt} height={260} />
      </Panel>
      <Panel title="Rolling 63-session volatility (annualized)" label="Backtest" source="stdev of daily returns over the trailing 63 sessions × √252.">
        <EChart option={volOpt} height={200} />
      </Panel>
    </Stack>
  );
}

function Trades({ id }: { id: number }) {
  const [offset, setOffset] = useState(0);
  const [sym, setSym] = useState("");
  const { data, isLoading } = useRunTrades(id, true, offset, sym.trim().toUpperCase());
  return (
    <Panel title="Simulated trades" label="Backtest" pad={false} source="Every simulated fill: open price (reference), fill price after assumed slippage, whole shares, commission. Newest first."
      right={<TextInput placeholder="Filter symbol" value={sym} onChange={(e) => { setSym(e.currentTarget.value); setOffset(0); }} w={140} />}>
      {isLoading || !data ? <Loading /> : (
        <>
          <DataTable<S["FillModel"]> data={data.rows} maxHeight={460}
            cols={[
              { id: "s", header: "Fill session", value: (r) => r.session },
              { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
              { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "buy" ? "blue" : "orange"}>{r.side}</Badge> },
              { id: "why", header: "Reason", value: (r) => r.reason },
              { id: "eff", header: "Effect", value: (r) => r.position_effect, cell: (r) => (r.position_effect ?? "").replace("_", " ") },
              { id: "q", header: "Shares", align: "right", value: (r) => r.shares, cell: (r) => fmtNum(r.shares) },
              { id: "ref", header: "Open", align: "right", value: (r) => r.ref_price, cell: (r) => fmtPx(r.ref_price) },
              { id: "fp", header: "Fill", align: "right", value: (r) => r.fill_price, cell: (r) => fmtPx(r.fill_price) },
              { id: "v", header: "Value", align: "right", value: (r) => r.gross_value, cell: (r) => fmtUsd(r.gross_value, true) },
              { id: "c", header: "Slip + comm", align: "right", value: (r) => r.slippage_cost + r.commission, cell: (r) => fmtUsd(r.slippage_cost + r.commission, true) },
            ]} />
          <Group justify="space-between" p={6}>
            <Text size="10px" c="dimmed">{data.offset + 1}–{Math.min(data.offset + data.limit, data.total)} of {fmtNum(data.total)}</Text>
            <Group gap={4}>
              <Button size="compact-xs" variant="default" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Prev</Button>
              <Button size="compact-xs" variant="default" disabled={offset + 100 >= data.total} onClick={() => setOffset(offset + 100)}>Next</Button>
            </Group>
          </Group>
        </>
      )}
    </Panel>
  );
}

function Rebalances({ id }: { id: number }) {
  const { data } = useRunRebalances(id, true);
  if (!data) return <Loading />;
  return (
    <Panel title="Rebalance log" label="Backtest" pad={false} source="One row per month-end signal. warmup = insufficient history; blocked = data insufficient (see warnings). Long-short columns show book sizes, gross per side after sizing, ex-ante volatility and the crash-guard state.">
      <DataTable<S["RunRebalance"]> data={data} maxHeight={480}
        cols={[
          { id: "sig", header: "Signal", value: (r) => r.signal_session },
          { id: "fill", header: "Fill", value: (r) => r.fill_session },
          { id: "st", header: "Status", value: (r) => r.status, cell: (r) => <Badge size="xs" variant="light" color={r.status === "executed" ? "teal" : r.status === "blocked" ? "red" : "gray"}>{r.status}</Badge> },
          { id: "u", header: "Universe", align: "right", value: (r) => r.universe_count },
          { id: "e", header: "Eligible", align: "right", value: (r) => r.eligible_count },
          { id: "n", header: "Selected", align: "right", value: (r) => r.selected_count },
          { id: "nav", header: "NAV @ open", align: "right", value: (r) => r.nav_at_open, cell: (r) => fmtUsd(r.nav_at_open) },
          { id: "to", header: "Turnover", align: "right", value: (r) => r.turnover, cell: (r) => fmtPct(r.turnover, 1) },
          { id: "c", header: "Costs", align: "right", value: (r) => (r.slippage_cost ?? 0) + (r.commission ?? 0), cell: (r) => fmtUsd((r.slippage_cost ?? 0) + (r.commission ?? 0), true) },
          { id: "cash", header: "Cash after", align: "right", value: (r) => r.cash_after, cell: (r) => fmtUsd(r.cash_after, true) },
          { id: "ls", header: "L / S names", align: "right", value: (r) => (r.diagnostics?.long_names as number) ?? null,
            cell: (r) => (r.diagnostics?.long_names != null ? `${r.diagnostics.long_names} / ${r.diagnostics.short_names}` : "—") },
          { id: "g", header: "Gross L / S", align: "right", value: (r) => (r.diagnostics?.long_gross as number) ?? null,
            cell: (r) => (r.diagnostics?.long_gross != null ? `${fmtPct(r.diagnostics.long_gross as number, 0)} / ${fmtPct(r.diagnostics.short_gross as number, 0)}` : "—") },
          { id: "xv", header: "Ex-ante vol", align: "right", value: (r) => (r.diagnostics?.ex_ante_vol as number) ?? null,
            cell: (r) => (r.diagnostics?.ex_ante_vol != null ? fmtPct(r.diagnostics.ex_ante_vol as number, 1) : "—") },
          { id: "cg", header: "Crash", value: (r) => ((r.diagnostics?.crash_guard as { active?: boolean })?.active ? "on" : ""),
            cell: (r) => ((r.diagnostics?.crash_guard as { active?: boolean })?.active ? <Badge size="xs" color="red" variant="light">ON</Badge> : "") },
          { id: "un", header: "Unfilled", align: "right", value: (r) => r.unfilled.length, cell: (r) => (r.unfilled.length ? <span className="warn" title={r.unfilled.map((u) => `${u.symbol}: ${u.reason}`).join("\n")}>{r.unfilled.length}</span> : "0") },
        ]} />
    </Panel>
  );
}

function Compare({ ids, onClear }: { ids: number[]; onClear: () => void }) {
  const runs = useQueries({ queries: ids.map((id) => ({ queryKey: ["run", id], queryFn: () => api.get<S["RunDetail"]>(`/research/runs/${id}`) })) });
  const series = useQueries({ queries: ids.map((id) => ({ queryKey: ["runSeries", id], queryFn: () => api.get<S["RunSeriesPoint"][]>(`/research/runs/${id}/series`), staleTime: Infinity })) });
  const loaded = runs.every((r) => r.data) && series.every((s) => s.data);
  const option = useMemo(() => {
    if (!loaded) return null;
    const allDates = [...new Set(series.flatMap((s) => s.data!.map((p) => p.session)))].sort();
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => (v == null ? "—" : v.toFixed(1)) },
      xAxis: timeAxis(allDates), yAxis: valueAxis((v: number) => v.toFixed(0)), dataZoom: zoom,
      series: series.map((s, i) => {
        const first = s.data![0].nav;
        const m = new Map(s.data!.map((p) => [p.session, (p.nav / first) * 100]));
        return lineSeries(`#${ids[i]} ${runs[i].data!.name}`, allDates.map((d) => m.get(d) ?? null), SERIES[i]);
      }),
    };
  }, [loaded, series, runs, ids]);
  if (!loaded) return <Loading what="comparison" />;
  const rows = runs.map((r) => ({ run: r.data!, m: r.data!.metrics as unknown as Metrics }));
  return (
    <>
      <Panel title={`Comparing ${ids.length} runs`} label="Backtest" right={<Button size="compact-xs" variant="subtle" onClick={onClear}>Clear comparison</Button>}
        source="Net NAV of each run indexed to 100 at its own first session (one common axis). Runs may cover different periods and configs — see the table.">
        <EChart option={option!} height={320} />
      </Panel>
      <Panel title="Metrics side by side" pad={false}>
        <DataTable<(typeof rows)[number]> data={rows}
          cols={[
            { id: "id", header: "Run", value: (r) => r.run.id, cell: (r) => <b>#{r.run.id} {r.run.name}</b> },
            { id: "per", header: "Period", value: (r) => r.m.start_session, cell: (r) => `${r.m.start_session} → ${r.m.end_session}` },
            { id: "sig", header: "Signal", value: (r) => r.run.config.signal, cell: (r) => (r.run.config.signal === "composite" ? "Composite" : "12-1") },
            { id: "sn", header: "Sector-neutral", value: (r) => String(r.run.config.sector_neutral), cell: (r) => (r.run.config.sector_neutral ? `±${(r.run.config.max_sector_net * 100).toFixed(0)}%` : "off") },
            { id: "slip", header: "Slip bps", align: "right", value: (r) => r.run.config.slippage_bps },
            { id: "adv", header: "Min ADV", align: "right", value: (r) => r.run.config.min_adv_usd, cell: (r) => fmtMillions(r.run.config.min_adv_usd) },
            { id: "tr", header: "Net TR", align: "right", value: (r) => r.m.net.total_return, cell: (r) => <Signed v={r.m.net.total_return}>{fmtPct(r.m.net.total_return, 1, true)}</Signed> },
            { id: "cagr", header: "CAGR", align: "right", value: (r) => r.m.net.cagr, cell: (r) => (r.m.cagr_meaningful ? fmtPct(r.m.net.cagr, 2) : "n/a") },
            { id: "vol", header: "Vol", align: "right", value: (r) => r.m.net.ann_vol, cell: (r) => fmtPct(r.m.net.ann_vol, 1) },
            { id: "dd", header: "Max DD", align: "right", value: (r) => r.m.net.max_drawdown, cell: (r) => fmtPct(r.m.net.max_drawdown, 1) },
            { id: "b", header: "Bench TR", align: "right", value: (r) => r.m.benchmark?.total_return, cell: (r) => fmtPct(r.m.benchmark?.total_return, 1, true) },
            { id: "to", header: "Turnover", align: "right", value: (r) => r.m.avg_turnover, cell: (r) => fmtPct(r.m.avg_turnover, 1) },
            { id: "c", header: "Costs", align: "right", value: (r) => r.m.total_costs, cell: (r) => fmtUsd(r.m.total_costs) },
            { id: "dv", header: "Data version", value: (r) => r.run.data_version, cell: (r) => <Text size="10px" className="mono">{r.run.data_version}</Text> },
          ]} />
      </Panel>
    </>
  );
}
