import { useMemo, useState } from "react";
import { Alert, Badge, Button, Checkbox, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { IconCheck, IconLock, IconPlayerSkipForward, IconSettings, IconX } from "@tabler/icons-react";
import { usePaperConfig, usePaperMutations, usePlan, usePlans, useStatus } from "../api/hooks";
import type { S, StrategyConfig } from "../api/client";
import { DataTable } from "../components/DataTable";
import { EChart, baseOption, valueAxis } from "../components/EChart";
import { AdvanceControls, InitPortfolio } from "../components/PaperControls";
import { Empty, ErrorView, KV, Kpi, Loading, Panel } from "../components/ui";
import { C } from "../lib/colors";
import { fmtMillions, fmtNum, fmtPct, fmtPx, fmtTs, fmtUsd } from "../lib/format";

const STATUS_COLOR: Record<string, string> = { proposed: "yellow", applied: "blue", executed: "teal", skipped: "gray", blocked: "red" };

type Estimate = {
  nav: number; cash_before: number; est_buy_value: number; est_sell_value: number; est_slippage: number;
  est_commission: number; est_turnover: number; est_cash_after: number; est_positions_after: number; price_basis: string;
  universe_count: number; eligible_count: number; selected_count: number; coverage: number;
  unfilled_estimate: { symbol: string; reason: string; detail: string }[];
};
type Execution = {
  nav_at_open: number; buy_value: number; sell_value: number; turnover: number; slippage_cost: number; commission: number;
  cash_before: number; cash_after: number; fills: number; unfilled: { symbol: string; side: string; reason: string; detail: string }[];
};

export function RebalanceDesk() {
  const { data: status } = useStatus();
  const [planId, setPlanId] = useState<number | null>(null);
  const plans = usePlans();
  const { data: plan, isLoading, error, refetch } = usePlan(planId);
  if (status && !status.has_portfolio) return <div className="page"><Panel title="Rebalance Desk"><InitPortfolio /></Panel></div>;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Rebalance Desk</h1>
          <div className="page-sub">Frozen month-end signals → proposed orders → apply to the INTERNAL virtual portfolio only</div>
        </div>
        <Group gap={8}>
          <Select placeholder="Current plan" clearable value={planId ? String(planId) : null} w={280}
            onChange={(v) => setPlanId(v ? Number(v) : null)}
            data={(plans.data ?? []).map((p) => ({ value: String(p.id), label: `#${p.id} · signal ${p.signal_session} · ${p.status}` }))} />
          <PaperConfigButton />
        </Group>
      </div>
      {isLoading ? <Loading what="plan" /> : error ? <ErrorView error={error} retry={refetch} /> : !plan ? (
        <Panel title="Plans"><Empty title="No rebalance plan yet">A plan is formed after the close of the last NYSE session of each month. Advance the portfolio to a month-end.</Empty><AdvanceControls /></Panel>
      ) : <PlanView plan={plan} />}
    </div>
  );
}

function PlanView({ plan }: { plan: S["PlanDetail"] }) {
  const m = usePaperMutations();
  const [confirm, setConfirm] = useState(false);
  const [ack, setAck] = useState(false);
  const [skipOpen, setSkipOpen] = useState(false);
  const [note, setNote] = useState("");
  const est = plan.estimate as unknown as Estimate;
  const ex = plan.execution as unknown as Execution | null;
  const sells = plan.orders.filter((o) => o.side === "sell");
  const buys = plan.orders.filter((o) => o.side === "buy");

  return (
    <>
      <div className="kpi-strip">
        <Kpi label="Plan" value={<span>#{plan.id} <Badge color={STATUS_COLOR[plan.status]} variant="light" size="sm">{plan.status}</Badge></span>} sub={`created ${fmtTs(plan.created_at)}`} />
        <Kpi label="Signal frozen at" value={plan.signal_session} sub="after the close (data ≤ this session)" />
        <Kpi label="Fill session" value={plan.fill_session} sub="simulated at the OPEN ± slippage" />
        <Kpi label="Targets" value={`${plan.selected_count}`} sub={`of ${est.eligible_count} eligible / ${est.universe_count} universe`} />
        <Kpi label="Est. turnover" value={fmtPct(plan.est_turnover, 1)} sub={plan.realized_turnover != null ? `realized ${fmtPct(plan.realized_turnover, 1)}` : "one-way, of NAV"} />
        <Kpi label="Est. costs" value={fmtUsd(est.est_slippage + est.est_commission, true)} sub={`${plan.config.slippage_bps} bps slippage (assumed)`} />
      </div>

      {plan.status === "blocked" && <Alert color="red" title="Rebalance disabled">{plan.block_reason}</Alert>}

      <div className="grid-12">
        <Panel className="span-8" title="Decision" label="Paper Simulation"
          source="Applying records the plan and creates pending internal orders. Fills are simulated when the portfolio is advanced into the fill session, at that session's open. A plan can be applied once; the database rejects duplicates.">
          <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
            <Stack gap={4} maw={520}>
              <Text size="xs">
                Portfolio as of <b>{plan.portfolio_as_of}</b>. {plan.can_apply
                  ? `Ready: apply before advancing into ${plan.fill_session}.`
                  : plan.apply_disabled_reason}
              </Text>
              <Text size="10px" c="dimmed">Data version {plan.data_version} · top {plan.config.top_n} · min price ${plan.config.min_price} · min ADV {fmtMillions(plan.config.min_adv_usd)}</Text>
            </Stack>
            <Group gap={8}>
              <Button color="teal" leftSection={<IconLock size={14} />} disabled={!plan.can_apply} onClick={() => { setAck(false); setConfirm(true); }}>
                Apply to internal virtual portfolio only
              </Button>
              <Button variant="default" leftSection={<IconX size={14} />} disabled={plan.status !== "proposed"} onClick={() => setSkipOpen(true)}>
                Skip
              </Button>
            </Group>
          </Group>
          {(plan.status === "applied" || plan.status === "executed" || plan.status === "skipped") && (
            <Group mt={8}><AdvanceControls compact /></Group>
          )}
        </Panel>
        <Panel className="span-4" title="Cash reconciliation (estimate)" source={est.price_basis}>
          <table className="recon">
            <tbody>
              <tr><td>Cash before</td><td>{fmtUsd(est.cash_before, true)}</td></tr>
              <tr><td>+ Sells (est.)</td><td>{fmtUsd(est.est_sell_value, true)}</td></tr>
              <tr><td>− Buys (est.)</td><td>{fmtUsd(est.est_buy_value, true)}</td></tr>
              <tr><td>− Commissions (est.)</td><td>{fmtUsd(est.est_commission, true)}</td></tr>
              <tr className="total"><td>= Residual cash (est.)</td><td>{fmtUsd(est.est_cash_after, true)}</td></tr>
            </tbody>
          </table>
          <Text size="10px" c="dimmed" mt={4}>Slippage is inside buy/sell prices (est. {fmtUsd(est.est_slippage, true)}). NAV basis {fmtUsd(est.nav, true)}.</Text>
          {ex && (
            <>
              <Text size="xs" fw={600} mt={8}>Executed at open of {plan.fill_session}</Text>
              <KV rows={[
                ["NAV at open", fmtUsd(ex.nav_at_open, true)],
                ["Sells / Buys", `${fmtUsd(ex.sell_value)} / ${fmtUsd(ex.buy_value)}`],
                ["Slippage + commission", fmtUsd(ex.slippage_cost + ex.commission, true)],
                ["Residual cash", fmtUsd(ex.cash_after, true)],
                ["Fills / unfilled", `${ex.fills} / ${ex.unfilled.length}`],
              ]} />
            </>
          )}
        </Panel>

        <Panel className="span-5" title="Rule checks" source="Automated checks evaluated when the plan was frozen (plus a data-revision notice if data changed since).">
          {plan.checks.map((c, i) => (
            <div key={i} className="check-row">
              {c.ok ? <IconCheck size={14} color="#3fb950" /> : <IconX size={14} color="#f0605a" />}
              <div><Text size="xs" fw={600}>{c.rule} <span className={c.ok ? "up" : "down"}>{c.ok ? "pass" : "FAIL"}</span></Text><Text size="10px" c="dimmed">{c.detail}</Text></div>
            </div>
          ))}
          {est.unfilled_estimate.length > 0 && (
            <Alert color="yellow" variant="light" mt={6} p={6}><Text size="10px">{est.unfilled_estimate.map((u) => `${u.symbol}: ${u.detail}`).join(" · ")}</Text></Alert>
          )}
        </Panel>
        <Panel className="span-7" title="Current vs target weight" label="Paper Simulation"
          source="Per-name weight of NAV before the rebalance (current, at the signal close) and the equal target weight. Sorted by target rank; names being exited appear at the right with target 0.">
          <WeightsChart plan={plan} />
        </Panel>

        <Panel className="span-6" title={`Proposed sells (${sells.length})`} pad={false} source="Estimated at the signal-session close with sells priced at close × (1 − slippage). Actual sizes are recomputed at the fill-session open.">
          <OrdersTable rows={sells} />
        </Panel>
        <Panel className="span-6" title={`Proposed buys (${buys.length})`} pad={false} source="Estimated at the signal-session close with buys priced at close × (1 + slippage), whole shares, rank order. Actual sizes recomputed at the open.">
          <OrdersTable rows={buys} />
        </Panel>

        <Panel className="span-12" title="Target portfolio (frozen signal)" pad={false}
          source="Top-ranked eligible stocks at the signal session with their 12–1 momentum, raw close (USD) and 20-session ADV (USD millions). Equal target weights.">
          <DataTable<S["PlanTarget"]> data={plan.targets} maxHeight={320} initialSort={[{ id: "rank", desc: false }]}
            cols={[
              { id: "rank", header: "Rank", align: "right", value: (r) => r.rank },
              { id: "symbol", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
              { id: "mom", header: "12–1 mom", align: "right", value: (r) => r.momentum, cell: (r) => fmtPct(r.momentum, 1, true) },
              { id: "close", header: "Close $", align: "right", value: (r) => r.close_raw, cell: (r) => fmtPx(r.close_raw) },
              { id: "adv", header: "ADV20", align: "right", value: (r) => r.adv20, cell: (r) => fmtMillions(r.adv20) },
              { id: "cw", header: "Current wt", align: "right", value: (r) => r.current_weight, cell: (r) => fmtPct(r.current_weight, 2) },
              { id: "tw", header: "Target wt", align: "right", value: (r) => r.target_weight, cell: (r) => fmtPct(r.target_weight, 2) },
            ]} />
        </Panel>

        {plan.fills.length > 0 && (
          <Panel className="span-12" title={`Simulated fills (${plan.fills.length})`} label="Paper Simulation" pad={false} source="Immutable fill records created at the fill-session open.">
            <DataTable<S["FillModel"]> data={plan.fills} maxHeight={320}
              cols={[
                { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
                { id: "side", header: "Side", value: (r) => r.side },
                { id: "q", header: "Shares", align: "right", value: (r) => r.shares },
                { id: "ref", header: "Open", align: "right", value: (r) => r.ref_price, cell: (r) => fmtPx(r.ref_price) },
                { id: "fp", header: "Fill", align: "right", value: (r) => r.fill_price, cell: (r) => fmtPx(r.fill_price) },
                { id: "v", header: "Value", align: "right", value: (r) => r.gross_value, cell: (r) => fmtUsd(r.gross_value, true) },
                { id: "sl", header: "Slippage", align: "right", value: (r) => r.slippage_cost, cell: (r) => fmtUsd(r.slippage_cost, true) },
                { id: "cm", header: "Comm.", align: "right", value: (r) => r.commission, cell: (r) => fmtUsd(r.commission, true) },
              ]} />
          </Panel>
        )}
      </div>

      <Modal opened={confirm} onClose={() => setConfirm(false)} title={<Text fw={600}>Apply plan #{plan.id} to the internal virtual portfolio</Text>} centered>
        <Stack gap={8}>
          <Alert color="teal" variant="light" icon={<IconLock size={16} />}>
            This only updates the local simulated ledger. No order is sent to any broker or exchange, and no real money is involved.
          </Alert>
          <Text size="xs">
            Pending internal orders will fill at the <b>{plan.fill_session}</b> open (± {plan.config.slippage_bps} bps assumed slippage) when you advance the
            portfolio. Estimated turnover {fmtPct(plan.est_turnover, 1)}, estimated residual cash {fmtUsd(est.est_cash_after, true)}.
          </Text>
          <Checkbox size="xs" checked={ack} onChange={(e) => setAck(e.currentTarget.checked)} label="I understand this is a paper simulation only." />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirm(false)}>Cancel</Button>
            <Button color="teal" disabled={!ack} loading={m.apply.isPending}
              onClick={() => m.apply.mutate(plan.id, { onSuccess: () => setConfirm(false) })}>
              Apply (internal only)
            </Button>
          </Group>
        </Stack>
      </Modal>
      <Modal opened={skipOpen} onClose={() => setSkipOpen(false)} title="Skip this rebalance" centered>
        <Stack gap={8}>
          <Text size="xs">Holdings stay unchanged until the next month-end signal. The decision is recorded in the audit log.</Text>
          <TextInput label="Reason (optional)" value={note} onChange={(e) => setNote(e.currentTarget.value)} />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setSkipOpen(false)}>Cancel</Button>
            <Button color="gray" leftSection={<IconPlayerSkipForward size={14} />} loading={m.skip.isPending}
              onClick={() => m.skip.mutate({ id: plan.id, note: note || undefined }, { onSuccess: () => setSkipOpen(false) })}>Skip plan</Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}

function OrdersTable({ rows }: { rows: S["PlanOrderModel"][] }) {
  return (
    <DataTable<S["PlanOrderModel"]> data={rows} maxHeight={300} empty="None"
      cols={[
        { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
        { id: "cur", header: "Cur sh", align: "right", value: (r) => r.current_shares, cell: (r) => fmtNum(r.current_shares) },
        { id: "tgt", header: "Tgt sh", align: "right", value: (r) => r.target_shares, cell: (r) => fmtNum(r.target_shares) },
        { id: "q", header: "Est qty", align: "right", value: (r) => r.est_shares, cell: (r) => fmtNum(r.est_shares) },
        { id: "px", header: "Est px", align: "right", value: (r) => r.est_price, cell: (r) => fmtPx(r.est_price) },
        { id: "v", header: "Est value", align: "right", value: (r) => r.est_value, cell: (r) => fmtUsd(r.est_value) },
        { id: "w", header: "Wt → tgt", align: "right", value: (r) => r.target_weight, cell: (r) => `${fmtPct(r.current_weight, 1)} → ${fmtPct(r.target_weight, 1)}` },
        { id: "st", header: "Order", value: (r) => r.order_status, cell: (r) => r.order_status
            ? <Badge size="xs" variant="light" color={r.order_status === "filled" ? "teal" : r.order_status === "pending" ? "blue" : "yellow"} title={r.order_reason ?? ""}>{r.order_status}</Badge>
            : <Text size="10px" c="dimmed">not applied</Text> },
      ]} />
  );
}

function WeightsChart({ plan }: { plan: S["PlanDetail"] }) {
  const option = useMemo(() => {
    const t = plan.targets.map((x) => ({ sym: x.symbol, cur: x.current_weight, tgt: x.target_weight }));
    const exits = plan.orders.filter((o) => o.target_weight === 0).map((o) => ({ sym: o.symbol, cur: o.current_weight, tgt: 0 }));
    const all = [...t, ...exits];
    const base = baseOption();
    return {
      ...base,
      grid: { left: 44, right: 10, top: 26, bottom: 54 },
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => `${v.toFixed(2)}%` },
      xAxis: { type: "category", data: all.map((a) => a.sym), axisLabel: { rotate: 90, fontSize: 9, color: C.muted, interval: 0 }, axisTick: { show: false }, axisLine: { lineStyle: { color: C.axis } } },
      yAxis: valueAxis((v: number) => `${v}%`, { scale: false }),
      series: [
        { name: "Current weight", type: "bar", barGap: "10%", barMaxWidth: 6, data: all.map((a) => +(a.cur * 100).toFixed(3)), itemStyle: { color: "#6b737c", borderRadius: [2, 2, 0, 0] } },
        { name: "Target weight", type: "bar", barMaxWidth: 6, data: all.map((a) => +(a.tgt * 100).toFixed(3)), itemStyle: { color: C.strategy, borderRadius: [2, 2, 0, 0] } },
      ],
    };
  }, [plan]);
  return <EChart option={option} height={250} />;
}

function PaperConfigButton() {
  const [open, setOpen] = useState(false);
  const { data } = usePaperConfig(open);
  const m = usePaperMutations();
  const [draft, setDraft] = useState<StrategyConfig | null>(null);
  const cfg = draft ?? data?.config ?? null;
  const set = (k: keyof StrategyConfig) => (v: string | number) => cfg && setDraft({ ...cfg, [k]: Number(v) });
  return (
    <>
      <Button variant="default" leftSection={<IconSettings size={14} />} onClick={() => { setDraft(null); setOpen(true); }}>Paper config</Button>
      <Modal opened={open} onClose={() => setOpen(false)} title="Paper portfolio strategy config" size="lg" centered>
        {!cfg ? <Loading /> : (
          <Stack gap={8}>
            <Alert color="blue" variant="light"><Text size="xs">Changes create a new config version that applies to <b>future</b> rebalance plans only. Past signals, plans and fills are never rewritten.</Text></Alert>
            <SimpleGrid cols={3} spacing={8}>
              <NumberInput label="Top N" value={cfg.top_n} onChange={set("top_n")} min={1} max={1000} />
              <NumberInput label="Min price (USD)" value={cfg.min_price} onChange={set("min_price")} min={0} decimalScale={2} />
              <NumberInput label="Min ADV (USD)" value={cfg.min_adv_usd} onChange={set("min_adv_usd")} min={0} step={1e6} thousandSeparator="," />
              <NumberInput label="Slippage (bps)" value={cfg.slippage_bps} onChange={set("slippage_bps")} min={0} max={500} />
              <NumberInput label="Commission / fill (USD)" value={cfg.commission_per_order} onChange={set("commission_per_order")} min={0} decimalScale={2} />
              <NumberInput label="Commission (bps)" value={cfg.commission_bps} onChange={set("commission_bps")} min={0} />
            </SimpleGrid>
            <Text size="10px" c="dimmed">Config versions used so far: {data?.history.length ?? 0} change(s). Current config id #{data?.config_id}.</Text>
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setOpen(false)}>Cancel</Button>
              <Button disabled={!draft} loading={m.updateConfig.isPending} onClick={() => draft && m.updateConfig.mutate(draft, { onSuccess: () => setOpen(false) })}>Save new version</Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </>
  );
}
