import { useMemo, useState } from "react";
import { Alert, Badge, Button, Group, Modal, SegmentedControl, Stack, Switch, Text, Tooltip } from "@mantine/core";
import { IconPlayerPlay, IconRobot, IconTestPipe } from "@tabler/icons-react";
import { useAutomationMutations, useAutomationRuns, useBrokerOrders, useBrokerOverview } from "../api/hooks";
import type { S } from "../api/client";
import { DataTable } from "../components/DataTable";
import { EChart, baseOption, lineSeries, timeAxis, valueAxis, zoom } from "../components/EChart";
import { Empty, ErrorView, KV, Kpi, Loading, Panel, Signed } from "../components/ui";
import { C } from "../lib/colors";
import { fmtNum, fmtPct, fmtPx, fmtTs, fmtUsd } from "../lib/format";

const STATUS_COLOR: Record<string, string> = { ok: "teal", warning: "yellow", error: "red", running: "blue", skipped: "gray" };
const ORDER_COLOR: Record<string, string> = {
  filled: "teal", accepted: "blue", new: "blue", submitted: "blue", partially_filled: "blue", planned: "gray",
  skipped: "gray", expired_unsent: "gray", superseded: "gray", declined: "gray", rejected: "red", canceled: "yellow", expired: "yellow",
};

export function Trading() {
  const { data, isLoading, error, refetch } = useBrokerOverview();
  const runs = useAutomationRuns();
  const m = useAutomationMutations();
  const [confirmLive, setConfirmLive] = useState(false);
  const [confirmApprove, setConfirmApprove] = useState(false);
  if (isLoading) return <div className="page"><Loading what="Alpaca paper account" /></div>;
  if (error || !data) return <div className="page"><ErrorView error={error} retry={refetch} /></div>;
  const a = data.account;
  const equity = a?.equity as number | undefined;
  const trading = data.trading_enabled_env && data.automation_enabled;
  const plan = data.awaiting_plan ?? null;
  const planOrders = plan ? data.awaiting_approval.filter((o) => o.plan_id === plan.plan_id) : [];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Alpaca Paper Trading</h1>
          <div className="page-sub">
            Source of truth for the traded portfolio: your Alpaca PAPER account (no real money). Last sync {fmtTs(data.synced_at)}.
          </div>
        </div>
        <Group gap={8}>
          <Tooltip label="Runs the full daily cycle but never sends orders; shows what it would do.">
            <Button variant="default" leftSection={<IconTestPipe size={14} />} loading={m.run.isPending} onClick={() => m.run.mutate(true)}>
              Dry run now
            </Button>
          </Tooltip>
          <Button color="teal" leftSection={<IconPlayerPlay size={14} />} disabled={!trading} onClick={() => setConfirmLive(true)}>
            Run cycle now
          </Button>
        </Group>
      </div>

      {!data.connected && <Alert color="red" title="Alpaca paper broker not connected">{data.error}</Alert>}

      <div className="grid-12">
        <Panel className="span-4" title="Automation" source={data.schedule_note}>
          <Stack gap={8}>
            <Group justify="space-between">
              <Text size="sm" fw={600}><IconRobot size={14} style={{ verticalAlign: -2 }} /> Automatic trading</Text>
              <Switch checked={data.automation_enabled} onChange={(e) => m.toggle.mutate(e.currentTarget.checked)}
                label={data.automation_enabled ? "ON" : "OFF"} color="teal" />
            </Group>
            <Group justify="space-between">
              <Tooltip multiline w={300} label="Approve (default): each month-end rebalance waits here until you approve the whole plan. Auto: it is sent without asking. Stop-loss covers are automatic in both modes.">
                <Text size="xs">Rebalance mode</Text>
              </Tooltip>
              <SegmentedControl size="xs" value={data.rebalance_mode} onChange={(v) => m.rebalanceMode.mutate(v as "approve" | "auto")}
                data={[{ value: "approve", label: "Approve" }, { value: "auto", label: "Auto" }]} />
            </Group>
            <KV rows={[
              ["BROKER_TRADING_ENABLED (.env)", data.trading_enabled_env ? <span className="up">true</span> : <span className="warn">false</span>],
              ["Orders will be sent", !trading ? <span className="warn">no (planned only)</span>
                : data.rebalance_mode === "approve" ? <span className="up">stops automatically; rebalances after your approval</span>
                : <span className="up">yes, automatically</span>],
              ["Working orders at Alpaca", fmtNum(data.open_orders)],
              ["Last run", runs.data?.[0] ? `${fmtTs(runs.data[0].started_at)} · ${runs.data[0].status}` : "never"],
            ]} />
            <Text size="10px" c="dimmed">
              The switch is a kill switch: turning it off stops new orders immediately (running orders at Alpaca are not cancelled).
            </Text>
          </Stack>
        </Panel>
        <div className="span-8">
          <div className="kpi-strip">
            <Kpi label="Equity" value={fmtUsd(equity, true)} sub="Alpaca paper account" />
            <Kpi label="Cash" value={fmtUsd(a?.cash as number, true)} sub="incl. short proceeds" />
            <Kpi label="Long / short value" value={`${fmtUsd(data.long_value)} / ${fmtUsd(data.short_value)}`}
              sub={equity ? `gross ${fmtPct((data.long_value - data.short_value) / equity, 0)} · net ${fmtPct((data.long_value + data.short_value) / equity, 1, true)}` : "—"} />
            <Kpi label="Positions" value={fmtNum(data.positions.length)} sub={`${data.positions.filter((p) => p.qty < 0).length} short`} />
            <Kpi label="Buying power" value={fmtUsd(a?.buying_power as number)} sub={`Reg T ${fmtUsd(a?.regt_buying_power as number)} · ${a?.multiplier ?? "—"}x`} />
            <Kpi label="Shorting" value={a?.shorting_enabled ? "enabled" : "disabled"} sub={String(a?.status ?? "")} />
          </div>
        </div>

        {plan && (
          <Panel className="span-12" title={`Plan #${plan.plan_id} awaiting your approval`} label="Alpaca Paper"
            source="Approve mode: this month-end rebalance was computed from the frozen signal and your account equity and is held until you approve the whole plan. Stop-loss covers do not wait."
            right={<Button size="compact-sm" color="teal" onClick={() => setConfirmApprove(true)}>Review &amp; approve plan…</Button>}>
            <KV rows={[
              ["Signal (month-end close)", plan.signal_session],
              ["Orders for session", plan.intended_session],
              ["Orders", `${fmtNum(plan.orders)} (${fmtNum(planOrders.filter((o) => o.side === "buy").length)} buy / ${fmtNum(planOrders.filter((o) => o.side === "sell").length)} sell)`],
              ["≈ Buy / sell value", `${fmtUsd(plan.buy_notional)} / ${fmtUsd(plan.sell_notional)}`],
            ]} />
          </Panel>
        )}

        <Panel className="span-12" title="Equity: Alpaca paper vs model vs SPY" label="Alpaca Paper"
          source="Alpaca's daily account equity (portfolio history), the internal model ledger's NAV (theoretical fills at the open ± slippage), and SPY's total-return index rebased to the starting equity.">
          <EquityCompare points={data.equity_history} />
        </Panel>

        <Panel className="span-7" title={`Positions vs model (${data.positions.length})`} label="Alpaca Paper" pad={false}
          source="Actual paper positions from Alpaca (qty signed; shorts negative), weights of account equity, the frozen model target weight from the latest applied plan, and drift = actual − model.">
          <DataTable<S["BrokerPosition"]> data={data.positions} maxHeight={420} empty="No positions yet"
            initialSort={[{ id: "mv", desc: true }]}
            cols={[
              { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
              { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "long" ? "blue" : "orange"}>{r.side}</Badge> },
              { id: "sec", header: "Sector", value: (r) => r.sector, cell: (r) => <Text size="xs" truncate maw={140}>{r.sector}</Text> },
              { id: "qty", header: "Qty", align: "right", value: (r) => r.qty, cell: (r) => fmtNum(r.qty) },
              { id: "entry", header: "Avg entry", align: "right", value: (r) => r.avg_entry_price, cell: (r) => fmtPx(r.avg_entry_price) },
              { id: "px", header: "Price", align: "right", value: (r) => r.current_price, cell: (r) => fmtPx(r.current_price) },
              { id: "mv", header: "Mkt value", align: "right", value: (r) => Math.abs(r.market_value ?? 0), cell: (r) => fmtUsd(r.market_value, true) },
              { id: "w", header: "Weight", align: "right", value: (r) => r.weight, cell: (r) => fmtPct(r.weight, 2, true) },
              { id: "mw", header: "Model", align: "right", value: (r) => r.model_weight, cell: (r) => fmtPct(r.model_weight, 2, true) },
              { id: "d", header: "Drift", align: "right", value: (r) => r.drift, cell: (r) => <Signed v={r.drift}>{fmtPct(r.drift, 2, true)}</Signed> },
              { id: "pl", header: "Unreal. P&L", align: "right", value: (r) => r.unrealized_pl, cell: (r) => <Signed v={r.unrealized_pl}>{fmtUsd(r.unrealized_pl, true)}</Signed> },
            ]} />
        </Panel>
        <Panel className="span-5" title="Automation runs" pad={false}
          source="Every daily cycle: data refresh, account sync, model ledger, stop-losses, rebalance reconciliation and order submission, with the outcome of each step.">
          {runs.isLoading ? <Loading /> : !runs.data?.length ? <Empty title="No runs yet">Use “Dry run now” to see what the cycle would do.</Empty> : (
            <div style={{ maxHeight: 420, overflow: "auto" }}>
              {runs.data.map((r) => (
                <div key={r.id} style={{ padding: "6px 10px", borderBottom: "1px solid var(--grid)" }}>
                  <Group justify="space-between" wrap="nowrap">
                    <Text size="xs" fw={600}>#{r.id} · {r.trigger}{r.dry_run ? " · dry run" : ""}</Text>
                    <Badge size="xs" variant="light" color={STATUS_COLOR[r.status] ?? "gray"}>{r.status}</Badge>
                  </Group>
                  <Text size="10px" c="dimmed">{fmtTs(r.started_at)}</Text>
                  {r.steps.map((st, i) => (
                    <Text key={i} size="10px" c={st.status === "error" ? "red.4" : st.status === "warning" ? "yellow.4" : "dimmed"}>
                      {String(st.name)}: {String(st.detail)}
                    </Text>
                  ))}
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel className="span-12" title="Orders sent to Alpaca paper" label="Alpaca Paper" pad={false}
          source="Deterministic client order ids prevent duplicates. Status comes from Alpaca (synced each run). 'planned' = computed but not sent (dry run, kill switch, or outside the 19:00-09:28 ET opening-auction window); 'skipped' = not easy-to-borrow.">
          <OrdersTable />
        </Panel>
      </div>

      <Modal opened={confirmApprove && !!plan} onClose={() => setConfirmApprove(false)} size="80rem" centered
        title={plan ? `Approve plan #${plan.plan_id}: ${plan.orders} orders for ${plan.intended_session}` : ""}>
        {plan && (
          <Stack gap={8}>
            <DataTable<S["BrokerOrder"]> data={planOrders} maxHeight={420}
              cols={[
                { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
                { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "buy" ? "blue" : "orange"}>{r.side}</Badge> },
                { id: "eff", header: "Effect", value: (r) => r.position_effect, cell: (r) => (r.position_effect ?? "").replace("_", " ") },
                { id: "qty", header: "Qty", align: "right", value: (r) => r.qty, cell: (r) => fmtNum(r.qty) },
                { id: "ref", header: "Signal close", align: "right", value: (r) => r.ref_price, cell: (r) => fmtPx(r.ref_price) },
                { id: "val", header: "≈ Value", align: "right", value: (r) => (r.ref_price ?? 0) * r.qty, cell: (r) => fmtUsd((r.ref_price ?? 0) * r.qty) },
                { id: "why", header: "Reason", value: (r) => r.status_reason, cell: (r) => <Text size="10px" c="dimmed" truncate maw={460}>{r.status_reason}</Text> },
              ]} />
            <Alert color="teal" variant="light">
              All {plan.orders} orders (≈ {fmtUsd(plan.buy_notional)} buys, {fmtUsd(plan.sell_notional)} sells) go to your Alpaca
              PAPER account (no real money): as market-on-open orders if approved between 19:00 and 09:28 ET, as market orders
              if approved during the {plan.intended_session} session, otherwise at the next scheduled run. Quantities are
              re-sized from account equity at send time; catch-ups for this plan need no second approval.
            </Alert>
            <Group justify="space-between">
              <Button variant="subtle" color="gray" loading={m.decline.isPending}
                onClick={() => m.decline.mutate({ plan_id: plan.plan_id, expected_orders: plan.orders }, { onSuccess: () => setConfirmApprove(false) })}>
                Decline plan (skip this rebalance)
              </Button>
              <Group gap={8}>
                <Button variant="default" onClick={() => setConfirmApprove(false)}>Cancel</Button>
                <Button color="teal" loading={m.approve.isPending}
                  onClick={() => m.approve.mutate({ plan_id: plan.plan_id, expected_orders: plan.orders }, { onSuccess: () => setConfirmApprove(false) })}>
                  Approve all {plan.orders} orders
                </Button>
              </Group>
            </Group>
          </Stack>
        )}
      </Modal>

      <Modal opened={confirmLive} onClose={() => setConfirmLive(false)} title="Run the daily cycle now" centered>
        <Stack gap={8}>
          <Alert color="teal" variant="light">Orders go to your Alpaca PAPER account only (no real money). Outside 19:00–09:28 ET they are computed but kept as planned.</Alert>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmLive(false)}>Cancel</Button>
            <Button color="teal" loading={m.run.isPending} onClick={() => m.run.mutate(false, { onSuccess: () => setConfirmLive(false) })}>Run now</Button>
          </Group>
        </Stack>
      </Modal>
    </div>
  );
}

function EquityCompare({ points }: { points: S["EquityPoint"][] }) {
  const option = useMemo(() => {
    const base = baseOption();
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => fmtUsd(v, true) },
      xAxis: timeAxis(points.map((p) => p.session)),
      yAxis: valueAxis((v: number) => `$${(v / 1000).toFixed(0)}k`),
      dataZoom: zoom,
      series: [
        lineSeries("Alpaca paper equity", points.map((p) => p.equity ?? null), C.strategy),
        lineSeries("Model ledger NAV", points.map((p) => p.model_nav ?? null), C.gross, { lineStyle: { width: 1.5, color: C.gross, type: [4, 3] } }),
        lineSeries("SPY (total return, rebased)", points.map((p) => p.benchmark ?? null), C.benchmark, { lineStyle: { width: 1.5, color: C.benchmark } }),
      ],
    };
  }, [points]);
  if (points.length < 2) return <Empty title="Not enough history yet">The curve fills in as the account trades and the model advances.</Empty>;
  return <EChart option={option} height={300} />;
}

function OrdersTable() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useBrokerOrders(offset);
  if (isLoading || !data) return <Loading />;
  return (
    <>
      <DataTable<S["BrokerOrder"]> data={data.rows} maxHeight={420} empty="No orders yet"
        cols={[
          { id: "sess", header: "For session", value: (r) => r.intended_session },
          { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
          { id: "orig", header: "Origin", value: (r) => r.origin, cell: (r) => <Badge size="xs" variant="outline" color={r.origin === "stop_loss" ? "red" : "gray"}>{r.origin.replace("_", " ")}</Badge> },
          { id: "side", header: "Side", value: (r) => r.side },
          { id: "eff", header: "Effect", value: (r) => r.position_effect, cell: (r) => (r.position_effect ?? "").replace("_", " ") },
          { id: "qty", header: "Qty", align: "right", value: (r) => r.qty },
          { id: "tif", header: "TIF", value: (r) => r.time_in_force },
          { id: "st", header: "Status", value: (r) => r.status, cell: (r) => <Badge size="xs" variant="light" color={ORDER_COLOR[r.status] ?? "gray"}>{r.status}</Badge> },
          { id: "fill", header: "Filled", align: "right", value: (r) => r.filled_qty, cell: (r) => (r.filled_qty ? `${r.filled_qty} @ ${fmtPx(r.filled_avg_price)}` : "") },
          { id: "why", header: "Reason", value: (r) => r.status_reason, cell: (r) => <Text size="10px" c="dimmed" truncate maw={380} title={r.status_reason ?? ""}>{r.status_reason}</Text> },
          { id: "cid", header: "Client id", value: (r) => r.client_order_id, cell: (r) => <Text size="10px" className="mono">{r.client_order_id}</Text> },
        ]} />
      <Group justify="space-between" p={6}>
        <Text size="10px" c="dimmed">{data.total} orders</Text>
        <Group gap={4}>
          <Button size="compact-xs" variant="default" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Newer</Button>
          <Button size="compact-xs" variant="default" disabled={offset + 100 >= data.total} onClick={() => setOffset(offset + 100)}>Older</Button>
        </Group>
      </Group>
    </>
  );
}
