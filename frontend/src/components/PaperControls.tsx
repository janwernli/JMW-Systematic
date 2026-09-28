import { useState } from "react";
import { Button, Group, NumberInput, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPlayerSkipForward, IconPlayerTrackNext, IconPlus, IconRefresh } from "@tabler/icons-react";
import { usePaperMutations, useStatus } from "../api/hooks";
import { Empty } from "./ui";

/** Advance the INTERNAL virtual portfolio through stored sessions. */
export function AdvanceControls({ compact = false }: { compact?: boolean }) {
  const m = usePaperMutations();
  const { data: status } = useStatus();
  const live = status?.mode === "live";
  return (
    <Group gap={6} wrap="wrap">
      <Tooltip label="Process exactly one more stored session (pre-open actions → open fills → close valuation → month-end signal)">
        <Button variant="default" leftSection={<IconPlayerSkipForward size={14} />} loading={m.advance.isPending}
          onClick={() => m.advance.mutate({ max_sessions: 1 })}>
          {compact ? "+1 session" : "Advance 1 session"}
        </Button>
      </Tooltip>
      <Tooltip label="Process all stored sessions; stops automatically when a month-end plan needs your decision">
        <Button variant="default" leftSection={<IconPlayerTrackNext size={14} />} loading={m.advance.isPending}
          onClick={() => m.advance.mutate({})}>
          {compact ? "To latest" : "Advance to latest data"}
        </Button>
      </Tooltip>
      {live && (
        <Tooltip label="Fetch new end-of-day bars from the configured provider (runs in the background)">
          <Button variant="subtle" leftSection={<IconRefresh size={14} />} loading={m.refreshData.isPending}
            onClick={() => m.refreshData.mutate()}>
            Refresh data
          </Button>
        </Tooltip>
      )}
    </Group>
  );
}

export function InitPortfolio() {
  const m = usePaperMutations();
  const { data: status } = useStatus();
  const [capital, setCapital] = useState<number | string>(100000);
  const [inception, setInception] = useState<string>("");
  return (
    <Empty title="No internal virtual portfolio yet">
      <Stack gap={8} align="center" mt={6}>
        <Text size="xs" c="dimmed">
          Funds a simulated portfolio with virtual cash at the close of the chosen session. No broker is involved.
        </Text>
        <Group gap={8} align="flex-end">
          <NumberInput label="Virtual capital (USD)" value={capital} onChange={setCapital} min={1000} step={10000} thousandSeparator="," w={170} />
          <TextInput label="Inception session" placeholder={status?.data_end ?? "latest"} type="date" value={inception}
            onChange={(e) => setInception(e.currentTarget.value)} w={160} />
          <Button leftSection={<IconPlus size={14} />} loading={m.init.isPending}
            onClick={() => m.init.mutate({ inception_session: inception || null, config: { ...DEFAULT_CFG, initial_capital: Number(capital) } })}>
            Initialize
          </Button>
        </Group>
        {status?.mode === "demo" && (
          <Button variant="subtle" size="compact-xs" loading={m.reseed.isPending} onClick={() => m.reseed.mutate()}>
            …or re-create the demo portfolio
          </Button>
        )}
      </Stack>
    </Empty>
  );
}

export const DEFAULT_CFG = {
  initial_capital: 100000, top_n: 50, lookback_sessions: 252, skip_sessions: 21, min_history_sessions: 252, min_price: 5,
  adv_window: 20, min_adv_usd: 5_000_000, slippage_bps: 10, commission_per_order: 0, commission_bps: 0,
  min_session_coverage: 0.9, start_date: null, end_date: null, benchmark_symbol: null,
};
