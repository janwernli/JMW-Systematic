import { useState } from "react";
import { Button, Group, NumberInput, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconPlayerSkipForward, IconPlayerTrackNext, IconPlus, IconRefresh } from "@tabler/icons-react";
import { usePaperMutations, useResearchDefaults, useStatus } from "../api/hooks";
import { Empty } from "./ui";

/** Advance the INTERNAL virtual portfolio through stored sessions. */
export function AdvanceControls({ compact = false }: { compact?: boolean }) {
  const m = usePaperMutations();
  const { data: status } = useStatus();
  const live = status?.mode === "live";
  return (
    <Group gap={6} wrap="wrap">
      <Tooltip label="Process exactly one more stored session (pre-open actions → stop covers → open fills → borrow fees & close valuation → stop checks → month-end signal)">
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
  const { data: defaults } = useResearchDefaults();
  const [capital, setCapital] = useState<number | string>(100000);
  const [inception, setInception] = useState<string>("");
  return (
    <Empty title="No model portfolio yet">
      <Stack gap={8} align="center" mt={6}>
        <Text size="xs" c="dimmed">
          The model portfolio is the internal theoretical track (fills at the open ± slippage). The daily automation creates it
          automatically; the traded portfolio is your Alpaca paper account.
        </Text>
        <Group gap={8} align="flex-end">
          <NumberInput label="Virtual capital (USD)" value={capital} onChange={setCapital} min={1000} step={10000} thousandSeparator="," w={170} />
          <TextInput label="Inception session" placeholder={status?.data_end ?? "latest"} type="date" value={inception}
            onChange={(e) => setInception(e.currentTarget.value)} w={160} />
          <Button leftSection={<IconPlus size={14} />} loading={m.init.isPending} disabled={!defaults}
            onClick={() => defaults && m.init.mutate({
              inception_session: inception || null,
              config: { ...defaults.config, initial_capital: Number(capital), start_date: null, end_date: null },
            })}>
            Initialize
          </Button>
        </Group>
        <Text size="10px" c="dimmed" maw={520} ta="center">
          Strategy settings use the documented defaults and can be changed later under Rebalance Desk → Paper config
          (future plans only).
        </Text>
      </Stack>
    </Empty>
  );
}
