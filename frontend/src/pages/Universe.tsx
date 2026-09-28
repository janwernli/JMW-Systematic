import { useEffect, useMemo, useState } from "react";
import { Badge, Drawer, Group, SegmentedControl, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconSearch } from "@tabler/icons-react";
import { useSearchParams } from "react-router";
import { useStock, useUniverse } from "../api/hooks";
import type { S } from "../api/client";
import { DataTable } from "../components/DataTable";
import { EChart, baseOption, lineSeries, timeAxis, valueAxis, zoom } from "../components/EChart";
import { Empty, ErrorView, KV, Kpi, Loading, Panel, Signed } from "../components/ui";
import { C } from "../lib/colors";
import { DASH, fmtMillions, fmtNum, fmtPct, fmtPx } from "../lib/format";

type Row = S["UniverseRow"];
type Filter = "all" | "eligible" | "selected" | "held" | "ineligible";

export function Universe() {
  const [basis, setBasis] = useState<"latest" | "plan">("latest");
  const [planSession, setPlanSession] = useState<string | null>(null);
  const { data, isLoading, error, refetch } = useUniverse(basis === "plan" ? planSession : null);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [params, setParams] = useSearchParams();
  const symbol = params.get("symbol");
  const setSymbol = (s: string | null) => setParams(s ? { symbol: s } : {}, { replace: true });

  const rows = useMemo(() => {
    if (!data) return [];
    const s = q.trim().toUpperCase();
    return data.rows.filter((r) => {
      if (s && !r.symbol.includes(s) && !(r.name ?? "").toUpperCase().includes(s) && !(r.sector ?? "").toUpperCase().includes(s)) return false;
      if (filter === "eligible") return r.eligible;
      if (filter === "selected") return r.selected;
      if (filter === "held") return r.position_shares > 0;
      if (filter === "ineligible") return !r.eligible;
      return true;
    });
  }, [data, q, filter]);

  useEffect(() => {
    if (!planSession && data?.plan_signal_session) setPlanSession(data.plan_signal_session);
  }, [data, planSession]);

  if (isLoading) return <div className="page"><Loading what="universe" /></div>;
  if (error || !data) return <div className="page"><ErrorView error={error} retry={refetch} /></div>;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Universe & Rankings</h1>
          <div className="page-sub">
            12–1 momentum ranking after the close of {data.signal_session}
            {data.is_month_end ? " (month-end signal session)" : " (indicative, not a rebalance date)"} · {data.data_label}
          </div>
        </div>
        <SegmentedControl value={basis} onChange={(v) => setBasis(v as "latest" | "plan")}
          data={[
            { value: "latest", label: "Latest stored session" },
            { value: "plan", label: `Latest plan signal${data.plan_signal_session || planSession ? ` (${data.plan_signal_session ?? planSession})` : ""}`, disabled: !planSession },
          ]} />
      </div>

      <div className="kpi-strip">
        <Kpi label="Universe (point-in-time)" value={fmtNum(data.universe_count)} sub="common stocks listed at the session" />
        <Kpi label="Eligible" value={fmtNum(data.eligible_count)} sub={`price > $${data.config.min_price}, ADV ≥ ${fmtMillions(data.config.min_adv_usd)}`} />
        <Kpi label="Selected" value={fmtNum(data.selected_count)} sub={`top ${data.config.top_n}, equal weight`} />
        <Kpi label="Bar coverage" value={fmtPct(data.coverage, 1)} sub={`min ${fmtPct(data.config.min_session_coverage, 0)} to rebalance`} />
        <Kpi label="Status" value={data.blocked_reason ? "BLOCKED" : "OK"} sub={data.blocked_reason ?? "data sufficient for ranking"} />
      </div>

      <Panel title="Rankings" label={data.data_label} pad={false}
        source={<>{data.basis_note} Momentum = TR(t−{data.config.skip_sessions}) ÷ TR(t−{data.config.lookback_sessions}) − 1 using NYSE session offsets. Ties: higher ADV, then symbol. Close = raw (unadjusted) close; ADV = mean of close × volume over {data.config.adv_window} sessions (USD). Weights: current = paper portfolio; model = this ranking; plan = latest frozen plan.</>}
        right={
          <Group gap={6}>
            <TextInput placeholder="Search symbol, name, sector" leftSection={<IconSearch size={13} />} value={q}
              onChange={(e) => setQ(e.currentTarget.value)} w={220} aria-label="Search" />
            <SegmentedControl value={filter} onChange={(v) => setFilter(v as Filter)}
              data={["all", "eligible", "selected", "held", "ineligible"].map((v) => ({ value: v, label: v }))} />
          </Group>
        }>
        <DataTable<Row>
          data={rows}
          maxHeight="calc(100vh - 290px)"
          onRowClick={(r) => setSymbol(r.symbol)}
          rowKey={(r) => r.symbol}
          rowClass={(r) => (r.position_shares > 0 ? "row-held" : !r.eligible ? "row-dim" : undefined)}
          initialSort={[{ id: "rank", desc: false }]}
          cols={[
            { id: "rank", header: "Rank", align: "right", value: (r) => r.rank, cell: (r) => r.rank ?? DASH, width: 50 },
            { id: "symbol", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
            { id: "name", header: "Name", value: (r) => r.name, cell: (r) => <Text size="xs" truncate maw={220}>{r.name}</Text> },
            { id: "sector", header: "Sector", value: (r) => r.sector },
            { id: "close", header: "Close $", align: "right", value: (r) => r.close_raw, cell: (r) => fmtPx(r.close_raw), tip: "Raw close at the signal session (USD)" },
            { id: "adv", header: "ADV20", align: "right", value: (r) => r.adv20, cell: (r) => fmtMillions(r.adv20), tip: "Trailing average daily dollar volume (USD millions)" },
            { id: "mom", header: "12–1 mom", align: "right", value: (r) => r.momentum, cell: (r) => <Signed v={r.momentum}>{fmtPct(r.momentum, 1, true)}</Signed> },
            { id: "elig", header: "Eligibility", value: (r) => r.reason, cell: (r) => r.eligible
                ? <Badge size="xs" variant="light" color={r.selected ? "blue" : "gray"}>{r.selected ? "selected" : "eligible"}</Badge>
                : <Tooltip label={r.reason_text}><Badge size="xs" variant="outline" color="gray">{r.reason.replaceAll("_", " ")}</Badge></Tooltip> },
            { id: "pos", header: "Shares", align: "right", value: (r) => r.position_shares, cell: (r) => (r.position_shares ? fmtNum(r.position_shares) : "") },
            { id: "cw", header: "Cur wt", align: "right", value: (r) => r.current_weight, cell: (r) => (r.current_weight ? fmtPct(r.current_weight, 2) : "") },
            { id: "mw", header: "Model wt", align: "right", value: (r) => r.model_weight, cell: (r) => (r.model_weight ? fmtPct(r.model_weight, 2) : "") },
            { id: "pw", header: "Plan wt", align: "right", value: (r) => r.plan_target_weight, cell: (r) => (r.plan_target_weight ? fmtPct(r.plan_target_weight, 2) : ""), tip: "Target weight in the latest frozen rebalance plan" },
          ]}
        />
        <Text size="10px" c="dimmed" p={6}>
          {rows.length} of {data.rows.length} instruments shown · reasons: {Object.entries(data.reason_counts).map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`).join(" · ")}
        </Text>
      </Panel>

      <StockDrawer symbol={symbol} session={data.signal_session} onClose={() => setSymbol(null)} />
    </div>
  );
}

function StockDrawer({ symbol, session, onClose }: { symbol: string | null; session: string; onClose: () => void }) {
  const { data, isLoading, error } = useStock(symbol, session);
  return (
    <Drawer opened={!!symbol} onClose={onClose} position="right" size="xl" title={<Text fw={600}>{symbol} · signal inputs</Text>}
      styles={{ content: { background: "#101317" }, header: { background: "#101317" } }}>
      {isLoading ? <Loading /> : error || !data ? <ErrorView error={error} /> : <StockBody d={data} />}
    </Drawer>
  );
}

function StockBody({ d }: { d: S["StockDetail"] }) {
  const s = d.signal;
  const option = useMemo(() => {
    const dates = d.prices.map((p) => p.session);
    const marks = [s.session_t252, s.session_t21, s.signal_session].filter(Boolean).map((x, i) => ({
      xAxis: x, label: { formatter: ["t−252", "t−21", "t"][i], color: C.text2, fontSize: 10 },
    }));
    const base = baseOption();
    return {
      ...base,
      grid: { left: 48, right: 16, top: 44, bottom: 28 },
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => (v == null ? DASH : v.toFixed(2)) },
      xAxis: timeAxis(dates),
      yAxis: valueAxis((v: number) => v.toFixed(0)),
      dataZoom: zoom,
      series: [
        lineSeries("Total-return index (first bar = 100)", d.prices.map((p) => p.tr_index), C.strategy, {
          markLine: { symbol: "none", silent: true, lineStyle: { color: "#4b525a", type: "dashed" }, data: marks },
        }),
      ],
    };
  }, [d, s]);
  const rawOption = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => (v == null ? DASH : `$${v.toFixed(2)}`) },
      xAxis: timeAxis(d.prices.map((p) => p.session)),
      yAxis: valueAxis((v: number) => `$${v.toFixed(0)}`),
      dataZoom: zoom,
      series: [lineSeries("Raw close (USD, unadjusted)", d.prices.map((p) => p.close), C.muted, { lineStyle: { width: 1.2, color: "#a9b0b8" } })],
    };
  }, [d]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <Text size="xs" c="dimmed">
        {d.name} · {d.exchange ?? DASH} · {d.asset_type.replaceAll("_", " ")}
        {d.sector ? ` · ${d.sector} (${d.sector_source})` : ""} · listed {d.list_date ?? DASH}
        {d.delist_date ? ` · delisted ${d.delist_date}` : ""} · {d.data_label}
      </Text>
      <Panel title="Frozen signal inputs" source={s.formula}>
        <Table fz="xs" withRowBorders={false} verticalSpacing={3} className="mono">
          <Table.Thead>
            <Table.Tr><Table.Th>Point</Table.Th><Table.Th>Session</Table.Th><Table.Th ta="right">Raw close</Table.Th><Table.Th ta="right">TR index</Table.Th></Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            <Table.Tr><Table.Td>t−252</Table.Td><Table.Td>{s.session_t252 ?? DASH}</Table.Td><Table.Td ta="right">{fmtPx(s.close_t252_raw)}</Table.Td><Table.Td ta="right">{s.tr_t252?.toFixed(4) ?? DASH}</Table.Td></Table.Tr>
            <Table.Tr><Table.Td>t−21</Table.Td><Table.Td>{s.session_t21 ?? DASH}</Table.Td><Table.Td ta="right">{fmtPx(s.close_t21_raw)}</Table.Td><Table.Td ta="right">{s.tr_t21?.toFixed(4) ?? DASH}</Table.Td></Table.Tr>
            <Table.Tr><Table.Td>t (signal)</Table.Td><Table.Td>{s.signal_session}</Table.Td><Table.Td ta="right">{fmtPx(s.close_t)}</Table.Td><Table.Td ta="right">{DASH}</Table.Td></Table.Tr>
          </Table.Tbody>
        </Table>
        <KV rows={[
          ["12–1 momentum", <Signed v={s.momentum}>{fmtPct(s.momentum, 2, true)}</Signed>],
          ["ADV (20 sessions)", fmtMillions(s.adv20)],
          ["Valid bars before t", fmtNum(s.valid_history)],
          ["Rank", s.rank ?? DASH],
          ["Eligibility", s.eligible ? <span className="up">✓ eligible</span> : s.reason_text],
          ["Held (shares)", fmtNum(d.position_shares)],
        ]} />
      </Panel>
      <Panel title="Total-return index" source="Causal TR index from raw closes, splits and cash dividends, rebased to 100 at the first stored bar. Dashed lines mark the lookback anchors. Data through the signal session only.">
        <EChart option={option} height={220} />
      </Panel>
      <Panel title="Raw close (USD)" source="Unadjusted daily closes as delivered by the provider. Splits appear as jumps here but not in the TR index.">
        <EChart option={rawOption} height={160} />
      </Panel>
      <Panel title="Corporate actions" source="Splits (new shares per old share) and cash dividends (USD per share) on their ex-dates, up to the signal session.">
        {d.corporate_actions.length === 0 ? <Empty title="None recorded" /> : (
          <div style={{ maxHeight: 180, overflow: "auto" }}>
            <Table fz="xs" className="mono" verticalSpacing={2}>
              <Table.Tbody>
                {[...d.corporate_actions].reverse().map((a, i) => (
                  <Table.Tr key={i}>
                    <Table.Td>{a.ex_date}</Table.Td>
                    <Table.Td>{a.action_type === "split" ? "split" : "cash dividend"}</Table.Td>
                    <Table.Td ta="right">{a.action_type === "split" ? `${a.ratio}:1` : `$${a.amount?.toFixed(4)}`}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
        )}
      </Panel>
      <Group gap={4}><Badge size="xs" variant="dot" color="gray">No live quotes or news are shown.</Badge></Group>
    </div>
  );
}
