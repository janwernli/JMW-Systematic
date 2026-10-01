import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Alert, Badge, Button, Checkbox, Group, Modal, Progress, Select, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPlayerPlay, IconRobot } from "@tabler/icons-react";
import type { S } from "../api/client";
import { DataTable } from "../components/DataTable";
import { KV } from "../components/ui";
import { useLaunchVariantStudy, usePaperStrategy, useSwitchApply, useSwitchPreview, useVariantStudy, useVariants } from "../api/hooks";
import { EChart, baseOption, lineSeries, timeAxis, valueAxis, zoom } from "../components/EChart";
import { Empty, ErrorView, Loading, Panel, Signed } from "../components/ui";
import { C } from "../lib/colors";
import { MONTHS, fmtNum, fmtPct, fmtTs, fmtUsd } from "../lib/format";

type Report = S["VariantReport"];
type Period = S["PeriodStats"];

// Four validated series colours for the variants; SPY is drawn as a dashed neutral line.
const VARIANT_COLORS = [C.strategy, C.benchmark, C.gross, C.series4];
const SPY_STYLE = { lineStyle: { width: 1.5, color: C.text2, type: [5, 4] }, itemStyle: { color: C.text2 } };

const ratio = (v?: number | null) => (v == null ? "—" : v.toFixed(2));
const tstat = (v?: number | null) => (v == null ? "" : ` (t ${v.toFixed(2)})`);

export function Variants() {
  const { data, isLoading, error, refetch } = useVariants();
  const launch = useLaunchVariantStudy();
  const [studyId, setStudyId] = useState<number | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  useEffect(() => {
    if (data && studyId == null && data.studies.length) setStudyId(data.studies[0].id);
  }, [data, studyId]);
  const study = useVariantStudy(studyId);
  const paper = usePaperStrategy();
  const [switchTo, setSwitchTo] = useState<S["VariantDef"] | null>(null);

  if (isLoading) return <div className="page"><Loading what="strategy variants" /></div>;
  if (error || !data) return <div className="page"><ErrorView error={error} retry={refetch} /></div>;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Strategy Variants</h1>
          <div className="page-sub">
            Four pre-defined variants, each run once on the same data and period. Nothing here is tuned to the backtest.
            Paper trading currently uses <b>{paper.data?.name ?? "…"}</b>; switch with “Use for paper trading”.
          </div>
        </div>
        <Group gap={8} align="flex-end">
          <TextInput size="xs" label="Start" placeholder={data.default_start} value={start} w={120}
            onChange={(e) => setStart(e.currentTarget.value)} />
          <TextInput size="xs" label="End" placeholder={data.default_end} value={end} w={120}
            onChange={(e) => setEnd(e.currentTarget.value)} />
          <Button leftSection={<IconPlayerPlay size={14} />} loading={launch.isPending}
            onClick={() => launch.mutate({ start_date: start || null, end_date: end || null },
              { onSuccess: (s) => setStudyId(s.id) })}>
            Run all four
          </Button>
        </Group>
      </div>

      {switchTo && <SwitchModal variant={switchTo} onClose={() => setSwitchTo(null)} />}

      <div className="grid-12">
        <Panel className="span-12" title="Variant definitions" pad={false}
          source="Everything not listed is the current default configuration. The 130/30 per-name long cap follows from the spec (130% / the 50-name minimum book), not from tuning.">
          <table className="dt">
            <thead><tr><th>Variant</th><th>What it is</th><th>Changes vs. default</th><th>Paper trading</th></tr></thead>
            <tbody>
              {data.variants.map((v, i) => (
                <tr key={v.key}>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <span style={{ display: "inline-block", width: 10, height: 3, background: VARIANT_COLORS[i], marginRight: 6, verticalAlign: 3 }} />
                    <b>{v.name}</b>
                  </td>
                  <td style={{ whiteSpace: "normal" }}><Text size="xs">{v.description}</Text>
                    {v.config_warnings.map((w, k) => <Text key={k} size="10px" c="yellow.5">{w}</Text>)}</td>
                  <td className="num" style={{ whiteSpace: "normal" }}>
                    {Object.keys(v.overrides).length ? Object.entries(v.overrides).map(([k, val]) => `${k}=${String(val)}`).join(", ") : "none (defaults)"}
                  </td>
                  <td>
                    {paper.data?.key === v.key
                      ? <Badge color="teal" variant="light" leftSection={<IconRobot size={11} />}>Paper strategy</Badge>
                      : <Button size="compact-xs" variant="default" onClick={() => setSwitchTo(v)}>Use for paper trading…</Button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        {!data.studies.length ? (
          <div className="span-12"><Empty title="No comparison yet">Click “Run all four” to backtest every variant on the same period.</Empty></div>
        ) : (
          <div className="span-12">
            <Group gap={8} mb={6}>
              <Select size="xs" w={360} value={studyId ? String(studyId) : null} onChange={(v) => setStudyId(v ? Number(v) : null)}
                data={data.studies.map((s) => ({ value: String(s.id), label: `#${s.id} · ${s.start_date} → ${s.end_date} · ${fmtTs(s.created_at)}${s.complete ? "" : " · running"}` }))} />
            </Group>
            {study.isLoading || !study.data ? <Loading what="comparison" /> : <StudyView res={study.data} />}
          </div>
        )}
      </div>
    </div>
  );
}

function StudyView({ res }: { res: S["StudyResult"] }) {
  const running = res.study.runs.filter((r) => r.status !== "completed");
  const done = res.results.filter((r) => r.report);
  const colorOf = Object.fromEntries(res.study.runs.map((r, i) => [r.key, VARIANT_COLORS[i % VARIANT_COLORS.length]]));
  return (
    <Stack gap={10}>
      {running.length > 0 && (
        <Panel title="Backtests" label="Backtest">
          {res.study.runs.map((r) => (
            <Group key={r.key} gap={8} wrap="nowrap" mb={4}>
              <Text size="xs" w={120}>{r.name}</Text>
              <Progress value={(r.progress ?? 0) * 100} w={260} size="sm" color={r.status === "failed" ? "red" : "blue"} />
              <Badge size="xs" variant="light" color={r.status === "failed" ? "red" : r.status === "completed" ? "teal" : "gray"}>{r.status}</Badge>
              {r.error && <Text size="10px" c="red.4">{r.error}</Text>}
            </Group>
          ))}
        </Panel>
      )}
      {res.notes.map((n, i) => <Alert key={i} color="yellow" variant="light" p="xs"><Text size="xs">{n}</Text></Alert>)}
      {done.length > 0 && (
        <>
          <ComparisonTable results={done} colorOf={colorOf} />
          <div className="grid-12">
            <Panel className="span-12" title="Equity: growth of $1 (log scale)" label="Backtest"
              source="Each variant's net NAV (after slippage, borrow fees and margin interest) divided by starting capital, with SPY total return on the same sessions.">
              <EquityChart series={res.series} results={done} colorOf={colorOf} />
            </Panel>
            <Panel className="span-12" title="Drawdown" label="Backtest" source="NAV / running peak − 1.">
              <DrawdownChart series={res.series} results={done} colorOf={colorOf} />
            </Panel>
            <MarginPanel results={done} />
          </div>
        </>
      )}
    </Stack>
  );
}

type Row = { label: ReactNode; tip?: string; cell: (r: Report) => ReactNode };

function ComparisonTable({ results, colorOf }: { results: S["StudyVariantResult"][]; colorOf: Record<string, string> }) {
  const halfRows = (i: 0 | 1, name: string): Row[] => [
    { label: `${name}: CAGR`, cell: (r) => { const h: Period | undefined = r.halves[i]; return h ? <Signed v={h.cagr}>{fmtPct(h.cagr, 1, true)}</Signed> : "—"; } },
    { label: `${name}: Sharpe`, cell: (r) => ratio(r.halves[i]?.sharpe) },
    { label: `${name}: alpha`, cell: (r) => { const a = r.halves[i]?.alpha; return a ? <span><Signed v={a.alpha_annual}>{fmtPct(a.alpha_annual, 1, true)}</Signed><Text span size="10px" c={Math.abs(a.t_alpha) >= 2 ? undefined : "dimmed"}>{tstat(a.t_alpha)}</Text></span> : "n/a"; } },
  ];
  const rows: (Row | string)[] = [
    "Returns and risk",
    { label: "CAGR", cell: (r) => <Signed v={r.cagr}>{fmtPct(r.cagr, 2, true)}</Signed> },
    { label: "Volatility (ann.)", cell: (r) => fmtPct(r.ann_vol, 1) },
    { label: "Sharpe", tip: "Mean excess return × 252 / (stdev × √252). Excess = r − RF on the capital not held as idle cash (see note below).", cell: (r) => ratio(r.sharpe) },
    { label: "Sortino", tip: "Mean excess return × 252 / (downside deviation × √252), target 0.", cell: (r) => ratio(r.sortino) },
    { label: "Max drawdown", cell: (r) => <span className="down">{fmtPct(r.max_drawdown, 1)}</span> },
    { label: "  peak → trough", cell: (r) => <Text size="10px">{r.max_dd_peak} → {r.max_dd_trough}</Text> },
    { label: "  recovered", cell: (r) => <Text size="10px">{r.max_dd_recovery ?? "not yet"}</Text> },
    { label: "Worst month", cell: (r) => r.worst_month ? <span><span className="down">{fmtPct(r.worst_month.value, 1)}</span> <Text span size="10px" c="dimmed">{MONTHS[r.worst_month.month - 1]} {r.worst_month.year}</Text></span> : "—" },
    "Market exposure",
    { label: "Beta to SPY", tip: "Daily returns vs. SPY total return.", cell: (r) => ratio(r.beta) },
    { label: "Correlation to SPY", cell: (r) => ratio(r.correlation) },
    { label: "Avg long / short gross", cell: (r) => `${fmtPct(r.avg_long_gross, 0)} / ${fmtPct(r.avg_short_gross, 0)}` },
    { label: "Avg net exposure", cell: (r) => fmtPct(r.avg_net_exposure, 0, true) },
    "Factor alpha (FF5 + momentum, Newey-West)",
    { label: "Alpha (ann.)", tip: "Intercept × 12 of the monthly regression on Mkt-RF, SMB, HML, RMW, CMA, Mom. |t| > 2 is conventionally significant.", cell: (r) => r.alpha ? <span><Signed v={r.alpha.alpha_annual}>{fmtPct(r.alpha.alpha_annual, 2, true)}</Signed><Text span size="10px" c={Math.abs(r.alpha.t_alpha) >= 2 ? undefined : "dimmed"}>{tstat(r.alpha.t_alpha)}</Text></span> : "n/a" },
    { label: "Market / momentum loading", cell: (r) => r.alpha ? `${ratio(r.alpha.beta_mkt)} / ${ratio(r.alpha.beta_mom)}` : "—" },
    "Trading and costs",
    { label: "Turnover (one-way, per year)", cell: (r) => r.annualized_turnover == null ? "—" : `${r.annualized_turnover.toFixed(1)}×` },
    { label: "Total costs", tip: "Slippage + commissions + borrow fees + margin-loan interest over the whole run.", cell: (r) => (
      <Tooltip label={`slippage/commission ${fmtUsd(r.slippage_commission)} · borrow ${fmtUsd(r.borrow_fees)} · margin interest ${fmtUsd(r.margin_interest)}`}>
        <span>{fmtUsd(r.total_costs)}</span>
      </Tooltip>) },
    { label: "Margin loan (min cash)", tip: "Lowest cash / NAV. Negative = a margin loan, charged RF + an ASSUMED 2.5% spread.", cell: (r) => fmtPct(r.min_cash_weight, 1, true) },
    "Margin checks (flags only)",
    { label: "Days gross > 2.0×", cell: (r) => r.margin ? <span className={r.margin.gross_breach_days ? "warn" : ""}>{fmtNum(r.margin.gross_breach_days)}</span> : "—" },
    { label: "Days maintenance breached", tip: "Equity below: shorts max(30% of value, $5/share) + longs 25% of value.", cell: (r) => r.margin ? <span className={r.margin.maintenance_breach_days ? "down" : ""}>{fmtNum(r.margin.maintenance_breach_days)}</span> : "—" },
    { label: "Max gross / min equity ÷ requirement", cell: (r) => r.margin ? `${ratio(r.margin.max_gross_exposure)}× / ${ratio(r.margin.min_equity_to_requirement)}×` : "—" },
    "First half / second half",
    ...halfRows(0, "1st half"),
    ...halfRows(1, "2nd half"),
  ];
  const first = results[0].report!;
  return (
    <Panel title="Side by side" label="Backtest" pad={false}
      source={`${first.start} → ${first.end}. ${first.rf_note} Halves: performance split at the middle session (${first.halves[0]?.end}); alpha halves split the factor months in half.`}>
      <div style={{ overflowX: "auto" }}>
        <table className="dt dense" style={{ minWidth: 760 }}>
          <thead>
            <tr><th />{results.map((r) => (
              <th key={r.key} style={{ textAlign: "right" }}>
                <span style={{ display: "inline-block", width: 10, height: 3, background: colorOf[r.key], marginRight: 6, verticalAlign: 3 }} />{r.name}
              </th>))}</tr>
          </thead>
          <tbody>
            {rows.map((row, i) => typeof row === "string" ? (
              <tr key={i}><td colSpan={results.length + 1} style={{ paddingTop: 10 }}><Text size="10px" fw={600} c="dimmed" tt="uppercase">{row}</Text></td></tr>
            ) : (
              <tr key={i}>
                <td>{row.tip ? <Tooltip multiline w={320} label={row.tip}><span style={{ borderBottom: "1px dotted" }}>{row.label}</span></Tooltip> : row.label}</td>
                {results.map((r) => <td key={r.key} className="num" style={{ textAlign: "right" }}>{row.cell(r.report!)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Text size="10px" c="dimmed" p="xs">{first.alpha_model}</Text>
    </Panel>
  );
}

function EquityChart({ series, results, colorOf }: { series: S["StudySeriesPoint"][]; results: S["StudyVariantResult"][]; colorOf: Record<string, string> }) {
  const option = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => (v == null ? "—" : `$${v.toFixed(2)}`) },
      xAxis: timeAxis(series.map((p) => p.session)),
      yAxis: valueAxis((v: number) => `$${v.toFixed(1)}`, { type: "log", logBase: 2 }),
      dataZoom: zoom,
      series: [
        ...results.map((r) => lineSeries(r.name, series.map((p) => p.navs[r.key] ?? null), colorOf[r.key])),
        lineSeries("SPY", series.map((p) => p.benchmark ?? null), C.text2, SPY_STYLE),
      ],
    };
  }, [series, results, colorOf]);
  return <EChart option={option} height={340} />;
}

function DrawdownChart({ series, results, colorOf }: { series: S["StudySeriesPoint"][]; results: S["StudyVariantResult"][]; colorOf: Record<string, string> }) {
  const option = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => fmtPct(v, 1) },
      xAxis: timeAxis(series.map((p) => p.session)),
      yAxis: valueAxis((v: number) => `${(v * 100).toFixed(0)}%`, { max: 0 }),
      dataZoom: zoom,
      series: [
        ...results.map((r) => lineSeries(r.name, series.map((p) => p.drawdowns[r.key] ?? null), colorOf[r.key], { lineStyle: { width: 1.5, color: colorOf[r.key] } })),
        lineSeries("SPY", series.map((p) => p.benchmark_drawdown ?? null), C.text2, SPY_STYLE),
      ],
    };
  }, [series, results, colorOf]);
  return <EChart option={option} height={260} />;
}

function MarginPanel({ results }: { results: S["StudyVariantResult"][] }) {
  const flagged = results.filter((r) => r.report?.margin && r.report.margin.flagged_days.length);
  const lim = results[0].report?.margin?.limits;
  return (
    <Panel className="span-12" title="Margin flags" label="Backtest"
      source={lim ? `Checked at every close; flags only (nothing is liquidated in the backtest). Gross exposure > ${lim.max_gross}× equity, or equity below the maintenance requirement: shorts max(${(lim.short_maint_pct * 100).toFixed(0)}% of value, $${lim.short_maint_min_per_share}/share), longs ${(lim.long_maint_pct * 100).toFixed(0)}% of value.` : undefined}>
      {!flagged.length ? <Text size="xs" c="dimmed">No variant breached a margin limit on any day.</Text> : flagged.map((r) => {
        const m = r.report!.margin!;
        return (
          <div key={r.key} style={{ marginBottom: 8 }}>
            <Text size="xs" fw={600}>{r.name}: {m.gross_breach_days} day(s) gross &gt; 2×, {m.maintenance_breach_days} maintenance breach(es)
              {m.first_gross_breach ? ` · first gross breach ${m.first_gross_breach}` : ""} · max gross {ratio(m.max_gross_exposure)}× on {m.max_gross_session}</Text>
            <Text size="10px" c="dimmed">
              {m.flagged_days.slice(0, 12).map((f) => `${f.session} (${f.breaches.join("+")}, ${f.gross_exposure.toFixed(2)}×)`).join(" · ")}
              {m.flagged_days.length > 12 || m.flagged_days_truncated ? " · …" : ""}
            </Text>
          </div>
        );
      })}
    </Panel>
  );
}

function SwitchModal({ variant, onClose }: { variant: S["VariantDef"]; onClose: () => void }) {
  const [replanNow, setReplanNow] = useState(true);
  const pv = useSwitchPreview();
  const apply = useSwitchApply();
  const { mutate } = pv;
  useEffect(() => { mutate({ variant: variant.key, replan_now: replanNow }); }, [mutate, variant.key, replanNow]);
  const d = pv.data;
  const b = d?.book;
  const blocked = typeof d?.replan.blocked === "string" ? (d.replan.blocked as string) : null;
  const buys = d?.orders.filter((o) => o.side === "buy").reduce((a, o) => a + o.value, 0) ?? 0;
  const sells = d?.orders.filter((o) => o.side === "sell").reduce((a, o) => a + o.value, 0) ?? 0;
  return (
    <Modal opened onClose={onClose} size="80rem" centered title={`Use “${variant.name}” for paper trading`}>
      <Stack gap={10}>
        <Text size="xs" c="dimmed">
          Changes the active model portfolio's configuration (a new config version); the Alpaca PAPER account follows the
          model's plans. Same logic as <code>python -m app paper-config --variant {variant.key}</code>. Research variants are unaffected.
        </Text>
        <Checkbox size="xs" checked={replanNow} onChange={(e) => setReplanNow(e.currentTarget.checked)}
          label="Replan now: rebuild the latest month-end plan with the new config and trade it at the next 08:00 ET run (otherwise from the next month-end)" />
        {pv.isPending || !d ? <Loading what="preview (target book and orders)" /> : (
          <>
            <Group gap={8}>
              <Badge variant="light">from: {d.current.name}</Badge><Text size="xs">→</Text><Badge color="teal" variant="light">{d.variant.name}</Badge>
              <Badge variant="outline" color={d.rebalance_mode === "approve" ? "blue" : "orange"}>rebalance mode: {d.rebalance_mode}</Badge>
              {!d.trading_enabled && <Badge variant="outline" color="yellow">BROKER_TRADING_ENABLED=false: nothing is sent</Badge>}
            </Group>
            <Alert color={blocked ? "red" : d.replan.action === "replan" ? "teal" : "gray"} variant="light" p="xs">
              <Text size="xs"><b>Replan: {String(d.replan.action).replace("_", " ")}.</b> {String(d.replan.detail ?? "")}</Text>
              {blocked && <Text size="xs" c="red.4" mt={4}>{blocked}</Text>}
            </Alert>
            {d.config_warnings.map((w, i) => <Text key={i} size="10px" c="yellow.5">{w}</Text>)}
            <div className="grid-12">
              <Panel className="span-5" title="Settings that change">
                {Object.keys(d.changes).length === 0 ? <Text size="xs" c="dimmed">None: already this variant.</Text> : (
                  <table className="dt dense"><tbody>
                    {Object.entries(d.changes).map(([k, [a, bb]]) => (
                      <tr key={k}><td>{k}</td><td className="num muted">{String(a)}</td><td className="num">→ {String(bb)}</td></tr>
                    ))}
                  </tbody></table>
                )}
              </Panel>
              <Panel className="span-7" title={b ? `Target book (signal ${b.signal_session})` : "Target book"}>
                {!b ? <Text size="xs" c="dimmed">No month-end signal available.</Text> : (
                  <KV rows={[
                    ["SPY core", fmtPct(b.spy_weight, 0)],
                    ["Longs / shorts", `${b.longs} / ${b.shorts}`],
                    ["Overlay long / short gross", `${fmtPct(b.long_gross, 1)} / ${fmtPct(b.short_gross, 1)}`],
                    ["Total gross / net", `${fmtPct(b.total_gross, 1)} / ${fmtPct(b.net_exposure, 1, true)}`],
                    ["Est. overlay beta", b.overlay_beta == null ? "—" : b.overlay_beta.toFixed(3)],
                    ["Crash guard", b.crash_guard ? "ON (overlay short book scaled)" : "off"],
                    ["Max sector net", fmtPct(b.max_sector_net, 2)],
                    ...(b.binding.length ? [["Binding", b.binding.join("; ")] as [string, string]] : []),
                    ...(b.blocked_reason ? [["BLOCKED", b.blocked_reason] as [string, string]] : []),
                  ]} />
                )}
              </Panel>
            </div>
            <Panel title={`Orders (${d.orders.length}): buy ${fmtUsd(buys)} · sell ${fmtUsd(sells)}`} pad={false} source={d.orders_basis ?? undefined}>
              <DataTable<S["PreviewOrder"]> data={d.orders} maxHeight={300} empty="No orders"
                cols={[
                  { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
                  { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "buy" ? "blue" : "orange"}>{r.side}</Badge> },
                  { id: "eff", header: "Effect", value: (r) => r.effect, cell: (r) => r.effect.replace("_", " ") },
                  { id: "qty", header: "Qty", align: "right", value: (r) => r.qty, cell: (r) => fmtNum(r.qty) },
                  { id: "val", header: "≈ Value", align: "right", value: (r) => r.value, cell: (r) => fmtUsd(r.value) },
                  { id: "why", header: "Reason", value: (r) => r.why, cell: (r) => <Text size="10px" c="dimmed" truncate maw={380}>{r.why}</Text> },
                ]} />
            </Panel>
            <Group justify="flex-end">
              <Button variant="default" onClick={onClose}>Cancel</Button>
              <Button color="teal" disabled={!!blocked} loading={apply.isPending}
                onClick={() => apply.mutate({ variant: variant.key, replan_now: replanNow, expected_orders: d.orders.length },
                  { onSuccess: onClose })}>
                Switch paper trading to {d.variant.name}
              </Button>
            </Group>
          </>
        )}
      </Stack>
    </Modal>
  );
}
