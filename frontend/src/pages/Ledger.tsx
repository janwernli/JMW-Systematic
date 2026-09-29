import { useState, type ReactNode } from "react";
import { Alert, Badge, Button, Code, Group, Modal, Select, Stack, Tabs, Text } from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";
import { useImports, useLedgerPage, usePaperMutations, useQuality, useRepro, useStatus } from "../api/hooks";
import type { S } from "../api/client";
import { DataTable, type Col } from "../components/DataTable";
import { DataStatus } from "./CommandCenter";
import { Empty, ErrorView, KV, Loading, Panel } from "../components/ui";
import { fmtNum, fmtPct, fmtPx, fmtTs, fmtUsd } from "../lib/format";

export function Ledger() {
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Ledger & Diagnostics</h1>
          <div className="page-sub">Append-only audit trail of the internal virtual portfolio, data ingestion and reproducibility metadata</div>
        </div>
      </div>
      <Tabs defaultValue="fills" keepMounted={false} variant="outline" radius="sm">
        <Tabs.List>
          <Tabs.Tab value="fills">Fills</Tabs.Tab>
          <Tabs.Tab value="orders">Orders</Tabs.Tab>
          <Tabs.Tab value="cash">Cash events</Tabs.Tab>
          <Tabs.Tab value="events">Audit trail</Tabs.Tab>
          <Tabs.Tab value="data">Data quality & ingestion</Tabs.Tab>
          <Tabs.Tab value="repro">Reproducibility</Tabs.Tab>
          <Tabs.Tab value="admin">Portfolio admin</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="fills" pt={10}>
          <Paged<S["FillModel"]> kind="fills" title="Simulated fills" source="Immutable (database-enforced). Open reference price, fill price after assumed slippage, whole shares."
            cols={[
              { id: "id", header: "#", align: "right", value: (r) => r.id },
              { id: "s", header: "Session", value: (r) => r.session },
              { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
              { id: "side", header: "Side", value: (r) => r.side, cell: (r) => <Badge size="xs" variant="light" color={r.side === "buy" ? "blue" : "orange"}>{r.side}</Badge> },
              { id: "eff", header: "Effect", value: (r) => r.position_effect, cell: (r) => (r.position_effect ?? "").replace("_", " ") },
              { id: "q", header: "Shares", align: "right", value: (r) => r.shares, cell: (r) => fmtNum(r.shares) },
              { id: "ref", header: "Open", align: "right", value: (r) => r.ref_price, cell: (r) => fmtPx(r.ref_price) },
              { id: "fp", header: "Fill", align: "right", value: (r) => r.fill_price, cell: (r) => fmtPx(r.fill_price) },
              { id: "v", header: "Value", align: "right", value: (r) => r.gross_value, cell: (r) => fmtUsd(r.gross_value, true) },
              { id: "sl", header: "Slippage", align: "right", value: (r) => r.slippage_cost, cell: (r) => fmtUsd(r.slippage_cost, true) },
              { id: "cm", header: "Comm.", align: "right", value: (r) => r.commission, cell: (r) => fmtUsd(r.commission, true) },
              { id: "plan", header: "Plan", align: "right", value: (r) => r.plan_id, cell: (r) => (r.plan_id ? `#${r.plan_id}` : "") },
              { id: "ts", header: "Recorded", value: (r) => r.created_at, cell: (r) => fmtTs(r.created_at) },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="orders" pt={10}>
          <Paged<S["OrderModel"]> kind="orders" title="Internal orders" source="Plan orders are created when a plan is applied; stop-loss covers are created automatically when a short closes at or above its stop and fill at the next open. Status is set once at the fill session (pending → filled / partially filled / unfilled / no trade)."
            cols={[
              { id: "id", header: "#", align: "right", value: (r) => r.id },
              { id: "plan", header: "Origin", value: (r) => r.plan_id ?? r.origin, cell: (r) => (r.origin === "stop_loss"
                  ? <Badge size="xs" color="red" variant="light" title={`Triggered at close of ${r.trigger_session}`}>stop-loss</Badge>
                  : `plan #${r.plan_id}`) },
              { id: "sym", header: "Symbol", value: (r) => r.symbol, cell: (r) => <b>{r.symbol}</b> },
              { id: "side", header: "Intended", value: (r) => r.intended_side },
              { id: "tw", header: "Target wt", align: "right", value: (r) => r.target_weight, cell: (r) => fmtPct(r.target_weight, 2) },
              { id: "est", header: "Est sh", align: "right", value: (r) => r.est_shares },
              { id: "fill", header: "Filled sh", align: "right", value: (r) => r.filled_shares },
              { id: "fs", header: "Fill session", value: (r) => r.fill_session },
              { id: "st", header: "Status", value: (r) => r.status, cell: (r) => <Badge size="xs" variant="light" color={r.status === "filled" ? "teal" : r.status === "pending" ? "blue" : r.status === "no_trade" ? "gray" : "yellow"}>{r.status}</Badge> },
              { id: "why", header: "Reason", value: (r) => r.status_reason, cell: (r) => <Text size="10px" c="dimmed" truncate maw={320}>{r.status_reason}</Text> },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="cash" pt={10}>
          <Paged<S["CashTx"]> kind="cash" title="Cash transactions" source="Every movement of virtual cash with running balance: deposit, buys and covers, sells and short sales, commissions, dividends (received on longs, paid on shorts, ex-date), cash-in-lieu, delisting close-outs and assumed borrow fees."
            cols={[
              { id: "id", header: "#", align: "right", value: (r) => r.id },
              { id: "s", header: "Session", value: (r) => r.session },
              { id: "k", header: "Kind", value: (r) => r.kind, cell: (r) => <Badge size="xs" variant="outline" color="gray">{r.kind.replaceAll("_", " ")}</Badge> },
              { id: "sym", header: "Symbol", value: (r) => r.symbol },
              { id: "a", header: "Amount", align: "right", value: (r) => r.amount, cell: (r) => <span className={r.amount >= 0 ? "up" : ""}>{fmtUsd(r.amount, true)}</span> },
              { id: "b", header: "Balance", align: "right", value: (r) => r.balance_after, cell: (r) => fmtUsd(r.balance_after, true) },
              { id: "n", header: "Note", value: (r) => r.note, cell: (r) => <Text size="10px" c="dimmed" truncate maw={360}>{r.note}</Text> },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="events" pt={10}><Events /></Tabs.Panel>
        <Tabs.Panel value="data" pt={10}><DataTab /></Tabs.Panel>
        <Tabs.Panel value="repro" pt={10}><Repro /></Tabs.Panel>
        <Tabs.Panel value="admin" pt={10}><Admin /></Tabs.Panel>
      </Tabs>
    </div>
  );
}

function Paged<T>({ kind, title, source, cols, extra = "", right }: {
  kind: "fills" | "orders" | "cash" | "events"; title: string; source: string; cols: Col<T>[]; extra?: string; right?: ReactNode;
}) {
  const [offset, setOffset] = useState(0);
  const { data, isLoading, error, refetch } = useLedgerPage<{ total: number; limit: number; offset: number; rows: T[] }>(kind, offset, extra);
  return (
    <Panel title={title} label={kind === "events" ? undefined : "Paper Simulation"} source={source} pad={false} right={right}>
      {isLoading ? <Loading /> : error || !data ? <ErrorView error={error} retry={refetch} /> : (
        <>
          <DataTable<T> data={data.rows} cols={cols} maxHeight="calc(100vh - 270px)" empty="Nothing recorded yet" />
          <Group justify="space-between" p={6}>
            <Text size="10px" c="dimmed">{data.total ? `${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)} of ${fmtNum(data.total)}` : "0 rows"}</Text>
            <Group gap={4}>
              <Button size="compact-xs" variant="default" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Newer</Button>
              <Button size="compact-xs" variant="default" disabled={offset + 100 >= data.total} onClick={() => setOffset(offset + 100)}>Older</Button>
            </Group>
          </Group>
        </>
      )}
    </Panel>
  );
}

function Events() {
  const [level, setLevel] = useState<string | null>(null);
  return (
    <Paged<S["EventModel"]> kind="events" title="Audit trail" extra={level ? `&level=${level}` : ""}
      source="Immutable system events: imports, portfolio initialization, plans, applications, executions, valuation warnings, backtests."
      right={<Select placeholder="All levels" clearable value={level} onChange={setLevel} data={["info", "warning", "error"]} w={130} />}
      cols={[
        { id: "id", header: "#", align: "right", value: (r) => r.id },
        { id: "ts", header: "Time", value: (r) => r.ts, cell: (r) => fmtTs(r.ts) },
        { id: "lvl", header: "Level", value: (r) => r.level, cell: (r) => <Badge size="xs" variant="light" color={r.level === "error" ? "red" : r.level === "warning" ? "yellow" : "gray"}>{r.level}</Badge> },
        { id: "cat", header: "Category", value: (r) => r.category },
        { id: "s", header: "Session", value: (r) => r.session },
        { id: "m", header: "Message", value: (r) => r.message, cell: (r) => <Text size="xs" style={{ whiteSpace: "normal" }} maw={640}>{r.message}</Text> },
      ]} />
  );
}

function DataTab() {
  const q = useQuality();
  const imports = useImports();
  return (
    <div className="grid-12">
      <div className="span-4" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <DataStatus />
        {q.data && (
          <Panel title="Provider & entitlements" label={q.data.provider.data_label} source="Documented by the provider adapter. Keys are held server-side only.">
            <KV rows={[
              ["Provider", q.data.provider.name], ["Feed", q.data.provider.feed],
              ["Benchmark", `${q.data.benchmark_symbol ?? "—"} (${q.data.provider.benchmark_return_basis.replace("_", " ")})`],
              ["Point-in-time universe", q.data.provider.point_in_time_universe ? "yes" : "NO"],
              ["Latest-session coverage", fmtPct(q.data.latest_session_coverage, 1)],
              ["Expected latest session", q.data.expected_latest_session ?? "—"],
              ["Total stored bars", fmtNum(q.data.total_bars)],
            ]} />
            <Text size="10px" c="dimmed" mt={6}>{q.data.provider.coverage_note}</Text>
            <Text size="10px" c="dimmed" mt={4}>{q.data.provider.entitlement_note}</Text>
            <Text size="10px" c="dimmed" mt={4}>{q.data.provider.adjustment_note}</Text>
          </Panel>
        )}
      </div>
      <div className="span-8" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <Panel title="Data warnings" source={`Generated ${fmtTs(q.data?.generated_at)} from the stored data.`}>
          {q.isLoading ? <Loading /> : !q.data ? <ErrorView error={q.error} /> : q.data.warnings.length === 0 ? <Empty title="No warnings" /> :
            q.data.warnings.map((w, i) => (
              <div key={i} className="check-row">
                <Badge size="xs" variant="light" color={w.level === "error" ? "red" : w.level === "warning" ? "yellow" : "gray"} style={{ flexShrink: 0 }}>{w.level}</Badge>
                <Text size="xs">{w.message}</Text>
              </div>
            ))}
          {q.data && q.data.stale_symbols.length > 0 && (
            <Text size="10px" c="dimmed" mt={6}>Stale symbols: {q.data.stale_symbols.map((s) => `${s.symbol} (${s.last_bar})`).join(", ")}</Text>
          )}
        </Panel>
        <Panel title="Ingestion history" pad={false} source="Each import records requested range, coverage, counts, warnings and provenance.">
          <DataTable<S["DataImport"]> data={imports.data ?? []} maxHeight={320}
            cols={[
              { id: "id", header: "#", align: "right", value: (r) => r.id },
              { id: "p", header: "Provider", value: (r) => r.provider, cell: (r) => `${r.provider} · ${r.feed}` },
              { id: "st", header: "Status", value: (r) => r.status, cell: (r) => <Badge size="xs" variant="light" color={r.status === "succeeded" ? "teal" : r.status === "running" ? "blue" : "red"}>{r.status}</Badge> },
              { id: "t", header: "Finished", value: (r) => r.finished_at, cell: (r) => fmtTs(r.finished_at) },
              { id: "cov", header: "Coverage", value: (r) => r.coverage_end, cell: (r) => `${r.coverage_start ?? "—"} → ${r.coverage_end ?? "—"}` },
              { id: "sym", header: "Symbols", align: "right", value: (r) => r.symbols_loaded, cell: (r) => `${r.symbols_loaded}/${r.symbols_requested}` },
              { id: "bars", header: "Bars", align: "right", value: (r) => r.bars_loaded, cell: (r) => fmtNum(r.bars_loaded) },
              { id: "act", header: "Actions", align: "right", value: (r) => r.actions_loaded },
              { id: "w", header: "Warnings / error", value: (r) => r.warnings.length, cell: (r) => <Text size="10px" c={r.error ? "red" : "dimmed"} truncate maw={300} title={[...r.warnings, r.error ?? ""].join("\n")}>{r.error ?? r.warnings.join(" · ")}</Text> },
            ]} />
        </Panel>
      </div>
    </div>
  );
}

function Repro() {
  const { data, isLoading, error } = useRepro();
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorView error={error} />;
  return (
    <div className="grid-12">
      <Panel className="span-5" title="Environment" source="Software and data versions needed to reproduce results.">
        <KV rows={[
          ["App version", data.app_version], ["Git commit", data.git_commit ?? "not a git checkout"], ["Python", data.python],
          ...Object.entries(data.packages).map(([k, v]) => [k, v] as [string, string]),
          ["Provider", data.provider], ["Current data version", <span style={{ fontSize: 10 }}>{data.data_version}</span>],
          ["Database", <span style={{ fontSize: 10 }}>{data.database_path}</span>],
          ["Schema migrations", data.schema_migrations.map((m) => String(m.name)).join(", ")],
        ]} />
      </Panel>
      <div className="span-7" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <Panel title="Plans and the data version each was frozen on" pad={false}>
          <DataTable<Record<string, unknown>> data={data.plan_data_versions} maxHeight={260}
            cols={[
              { id: "p", header: "Plan", value: (r) => r.plan_id as number, cell: (r) => `#${r.plan_id}` },
              { id: "s", header: "Signal", value: (r) => r.signal_session as string },
              { id: "st", header: "Status", value: (r) => r.status as string },
              { id: "c", header: "Config id", value: (r) => r.config_id as number },
              { id: "dv", header: "Data version", value: (r) => r.data_version as string, cell: (r) => <Text size="10px" className="mono">{String(r.data_version)}</Text> },
            ]} />
        </Panel>
        <Panel title="Paper config versions">
          {data.portfolio_configs.map((c) => (
            <div key={String(c.id)} style={{ marginBottom: 6 }}>
              <Text size="xs" fw={600}>Config #{String(c.id)} · hash {String(c.config_hash)} · {fmtTs(String(c.created_at))}</Text>
              <Code block fz="10px">{JSON.stringify(c.config)}</Code>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}

function Admin() {
  const m = usePaperMutations();
  const { data: status } = useStatus();
  const [open, setOpen] = useState(false);
  return (
    <Panel title="Virtual portfolio administration" className="span-12">
      <Stack gap={8} maw={640}>
        <Text size="xs">
          Archiving keeps every fill, cash event, NAV row and plan of the current portfolio in the database (they are immutable). A new portfolio can then be
          initialized from the Command Center.
        </Text>
        <Group gap={8}>
          <Button color="red" variant="light" leftSection={<IconTrash size={14} />} disabled={!status?.has_portfolio} onClick={() => setOpen(true)}>
            Archive current virtual portfolio
          </Button>
        </Group>
      </Stack>
      <Modal opened={open} onClose={() => setOpen(false)} title="Archive the virtual portfolio?" centered>
        <Alert color="red" variant="light" mb={8}>The portfolio stops advancing. Its history stays in the ledger and remains auditable.</Alert>
        <Group justify="flex-end">
          <Button variant="default" onClick={() => setOpen(false)}>Cancel</Button>
          <Button color="red" loading={m.reset.isPending} onClick={() => m.reset.mutate(undefined, { onSuccess: () => setOpen(false) })}>Archive</Button>
        </Group>
      </Modal>
    </Panel>
  );
}
