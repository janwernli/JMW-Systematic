import { useEffect, useState } from "react";
import { NavLink as RouterLink, Outlet, useLocation } from "react-router";
import { Alert, AppShell, Badge, Burger, Group, NavLink, Text, Tooltip } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import {
  IconAdjustmentsHorizontal,
  IconArrowsExchange,
  IconBook2,
  IconFlask,
  IconLayoutDashboard,
  IconListNumbers,
  IconWallet,
} from "@tabler/icons-react";
import { usePlan, useStatus } from "../api/hooks";
import { DataLabel } from "./ui";

const NAV = [
  { to: "/", label: "Command Center", icon: IconLayoutDashboard },
  { to: "/universe", label: "Universe & Rankings", icon: IconListNumbers },
  { to: "/portfolio", label: "Portfolio", icon: IconWallet },
  { to: "/rebalance", label: "Rebalance Desk", icon: IconArrowsExchange },
  { to: "/research", label: "Research Lab", icon: IconFlask },
  { to: "/ledger", label: "Ledger & Diagnostics", icon: IconBook2 },
];

const STATUS_TEXT: Record<string, [string, string]> = {
  open: ["NYSE OPEN", "#3fb950"],
  pre_open: ["PRE-OPEN", "#e3a008"],
  after_close: ["CLOSED", "#7c848d"],
  closed: ["CLOSED", "#7c848d"],
};

function useNow() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function Clock() {
  const now = useNow();
  const { data } = useStatus();
  const ny = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(now);
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(now);
  const st = data ? STATUS_TEXT[data.market_clock.status] : undefined;
  return (
    <Tooltip
      multiline
      w={300}
      label={
        <>
          New York time from your computer's clock. Session status from the XNYS exchange calendar (holidays & early
          closes) as of the last status poll — schedule only, no live quotes.
          {data?.market_clock.next_open_utc && <div>Next open: {data.market_clock.next_open_utc}</div>}
        </>
      }
    >
      <Group gap={8} wrap="nowrap" className="clock">
        <span>{day}</span>
        <span>{ny} ET</span>
        {st && (
          <Group gap={4} wrap="nowrap">
            <span className="dot" style={{ background: st[1] }} />
            <span style={{ color: st[1] }}>{st[0]}</span>
          </Group>
        )}
      </Group>
    </Tooltip>
  );
}

export function Layout() {
  const [opened, { toggle, close }] = useDisclosure();
  const { data: status, error } = useStatus();
  const { data: plan } = usePlan(null);
  const loc = useLocation();
  useEffect(close, [loc.pathname, close]);
  const pending = plan?.status === "proposed";

  return (
    <AppShell header={{ height: 44 }} navbar={{ width: 200, breakpoint: "sm", collapsed: { mobile: !opened } }} padding={0}>
      <AppShell.Header style={{ background: "#0f1215", borderColor: "#252a30" }}>
        <Group h="100%" px="sm" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Toggle navigation" />
            <Text fw={700} size="sm" style={{ letterSpacing: "0.14em" }}>
              MOMENTUM<span style={{ color: "#3987e5" }}>·</span>TERMINAL
            </Text>
            <Text size="xs" c="dimmed" visibleFrom="md">US equities · 12–1 cross-sectional momentum · long-short / long-only</Text>
          </Group>
          <Group gap="md" wrap="nowrap">
            <Clock />
            {status && (
              <Group gap={6} wrap="nowrap" visibleFrom="md">
                <DataLabel label={status.data_label} />
                <Tooltip label="Latest stored daily bar (end-of-day data)">
                  <Text size="xs" c="dimmed" className="mono">data {status.data_end ?? "—"}</Text>
                </Tooltip>
                {status.portfolio_as_of && (
                  <Tooltip label="Virtual portfolio valued through this session's close">
                    <Text size="xs" c="dimmed" className="mono">paper {status.portfolio_as_of}</Text>
                  </Tooltip>
                )}
              </Group>
            )}
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p={6} style={{ background: "#0f1215", borderColor: "#252a30" }}>
        <AppShell.Section grow>
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              component={RouterLink}
              to={n.to}
              end={n.to === "/"}
              label={n.label}
              leftSection={<n.icon size={16} stroke={1.6} />}
              rightSection={
                n.to === "/rebalance" && pending ? (
                  <Badge size="xs" color="yellow" variant="filled">1</Badge>
                ) : undefined
              }
              className={loc.pathname === n.to || (n.to !== "/" && loc.pathname.startsWith(n.to)) ? "nav-link-active" : ""}
              styles={{ label: { fontSize: 12 } }}
              py={7}
            />
          ))}
        </AppShell.Section>
        <AppShell.Section>
          <Group gap={6} p={8} wrap="nowrap" align="flex-start">
            <IconAdjustmentsHorizontal size={14} color="#7c848d" style={{ flexShrink: 0, marginTop: 2 }} />
            <Text size="10px" c="dimmed" lh={1.4}>
              Research & internal paper simulation only. No broker connection, no real orders. Not investment advice.
            </Text>
          </Group>
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main>
        {status?.mode === "demo" && (
          <div className="demo-ribbon" role="status">
            DEMO DATA — synthetic prices for fictional tickers (containing digits). Nothing here is real market data or real performance.
          </div>
        )}
        {status?.provider && !status.provider.point_in_time_universe && (
          <div className="survivor-ribbon" role="alert">
            SURVIVORSHIP BIAS: {status.provider.survivorship_note}
          </div>
        )}
        {(error || status?.provider_error) && (
          <Alert color="red" variant="light" m="xs" title="Backend / data provider problem">
            {status?.provider_error ?? (error as Error)?.message}
          </Alert>
        )}
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
