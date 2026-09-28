// Small shared building blocks: panels with provenance, KPI tiles, state views, labels.
import type { ReactNode } from "react";
import { Alert, Badge, Button, Group, Loader, Text, Tooltip } from "@mantine/core";
import { IconAlertTriangle, IconInfoCircle, IconRefresh } from "@tabler/icons-react";
import { ApiError } from "../api/client";
import { arrow, signClass } from "../lib/format";

const LABEL_COLOR: Record<string, string> = {
  "Demo Data": "yellow",
  "Delayed Market Data": "cyan",
  Backtest: "violet",
  "Paper Simulation": "teal",
};

export function DataLabel({ label }: { label: string }) {
  return (
    <Badge size="xs" radius="sm" variant="light" color={LABEL_COLOR[label] ?? "gray"} className="data-label">
      {label}
    </Badge>
  );
}

export function InfoTip({ children }: { children: ReactNode }) {
  return (
    <Tooltip label={children} multiline w={320} withArrow position="bottom-start" events={{ hover: true, focus: true, touch: true }}>
      <span className="info-tip" tabIndex={0} aria-label="Explanation">
        <IconInfoCircle size={13} />
      </span>
    </Tooltip>
  );
}

/** Panel = dense bordered section. `source` explains where its values come from (units, timestamp). */
export function Panel({
  title, label, source, right, children, className, pad = true,
}: {
  title: ReactNode;
  label?: string;
  source?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  pad?: boolean;
}) {
  return (
    <section className={`panel ${className ?? ""}`}>
      <header className="panel-head">
        <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
          <h3 className="panel-title">{title}</h3>
          {source && <InfoTip>{source}</InfoTip>}
          {label && <DataLabel label={label} />}
        </Group>
        {right && <Group gap={6} wrap="nowrap">{right}</Group>}
      </header>
      <div className={pad ? "panel-body" : ""}>{children}</div>
    </section>
  );
}

export function Kpi({
  label, value, sub, tone, tip,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: number | null;
  tip?: ReactNode;
}) {
  return (
    <div className="kpi">
      <div className="kpi-label">
        {label}
        {tip && <InfoTip>{tip}</InfoTip>}
      </div>
      <div className={`kpi-value ${tone !== undefined ? signClass(tone) : ""}`}>
        {tone !== undefined && arrow(tone)}
        {value}
      </div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Signed({ v, children }: { v?: number | null; children: ReactNode }) {
  return (
    <span className={signClass(v)}>
      {arrow(v)}
      {children}
    </span>
  );
}

export function Loading({ what = "data" }: { what?: string }) {
  return (
    <div className="state-view">
      <Loader size="sm" color="gray" type="dots" />
      <Text size="xs" c="dimmed">Loading {what}…</Text>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state-view">
      <Text size="sm" fw={600} c="gray.4">{title}</Text>
      {children && <Text size="xs" c="dimmed" ta="center" maw={420}>{children}</Text>}
    </div>
  );
}

export function ErrorView({ error, retry }: { error: unknown; retry?: () => void }) {
  const msg = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
  const code = error instanceof ApiError ? `${error.status} ${error.code}` : "error";
  return (
    <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title={`Could not load (${code})`} m="xs">
      <Text size="xs">{msg}</Text>
      {retry && (
        <Button size="compact-xs" mt={6} variant="subtle" leftSection={<IconRefresh size={12} />} onClick={retry}>
          Retry
        </Button>
      )}
    </Alert>
  );
}

export function StaleBanner({ reason }: { reason?: string | null }) {
  if (!reason) return null;
  return (
    <Alert color="yellow" variant="light" icon={<IconAlertTriangle size={16} />} title="Stale data" py={6} mb="xs">
      <Text size="xs">{reason}</Text>
    </Alert>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v], i) => (
        <div key={i} className="kv-row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
