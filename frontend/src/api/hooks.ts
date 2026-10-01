import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { notifications } from "@mantine/notifications";
import { api, ApiError, type S, type StrategyConfig } from "./client";

export const useStatus = () =>
  useQuery({ queryKey: ["status"], queryFn: () => api.get<S["SystemStatus"]>("/status"), refetchInterval: 30_000 });

export const useQuality = () =>
  useQuery({ queryKey: ["quality"], queryFn: () => api.get<S["DataQuality"]>("/data/quality"), refetchInterval: 60_000 });

export const useImports = () =>
  useQuery({ queryKey: ["imports"], queryFn: () => api.get<S["DataImport"][]>("/data/imports"), refetchInterval: 10_000 });

export const useSummary = () =>
  useQuery({ queryKey: ["summary"], queryFn: () => api.get<S["CommandCenter"]>("/portfolio/summary") });

export const usePositions = (enabled = true) =>
  useQuery({ queryKey: ["positions"], queryFn: () => api.get<S["PositionsResponse"]>("/portfolio/positions"), enabled });

export const useUniverse = (session?: string | null) =>
  useQuery({
    queryKey: ["universe", session ?? "latest"],
    queryFn: () => api.get<S["UniverseResponse"]>(`/universe${session ? `?session=${session}` : ""}`),
  });

export const useStock = (symbol: string | null, session?: string | null) =>
  useQuery({
    queryKey: ["stock", symbol, session ?? "latest"],
    queryFn: () => api.get<S["StockDetail"]>(`/universe/${symbol}${session ? `?session=${session}` : ""}`),
    enabled: !!symbol,
  });

export const usePlans = () =>
  useQuery({ queryKey: ["plans"], queryFn: () => api.get<S["PlanSummary"][]>("/rebalance/plans") });

export const usePlan = (id: number | null) =>
  useQuery({
    queryKey: ["plan", id ?? "current"],
    queryFn: () =>
      id ? api.get<S["PlanDetail"]>(`/rebalance/plans/${id}`) : api.get<S["PlanDetail"] | null>("/rebalance/current"),
  });

export const usePaperConfig = (enabled = true) =>
  useQuery({ queryKey: ["paperConfig"], queryFn: () => api.get<S["PaperConfigResponse"]>("/paper/config"), enabled });

export const useResearchDefaults = () =>
  useQuery({ queryKey: ["researchDefaults"], queryFn: () => api.get<S["ResearchDefaults"]>("/research/defaults") });

export const useRuns = () =>
  useQuery({
    queryKey: ["runs"],
    queryFn: () => api.get<S["RunSummary"][]>("/research/runs"),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "queued" || r.status === "running") ? 800 : false),
  });

export const useRun = (id: number | null) =>
  useQuery({
    queryKey: ["run", id],
    queryFn: () => api.get<S["RunDetail"]>(`/research/runs/${id}`),
    enabled: id != null,
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 800 : false),
  });

export const useRunSeries = (id: number | null, ready: boolean) =>
  useQuery({
    queryKey: ["runSeries", id],
    queryFn: () => api.get<S["RunSeriesPoint"][]>(`/research/runs/${id}/series`),
    enabled: id != null && ready,
    staleTime: Infinity,
  });

export const useRunMonthly = (id: number | null, ready: boolean) =>
  useQuery({
    queryKey: ["runMonthly", id],
    queryFn: () => api.get<S["MonthlyReturn"][]>(`/research/runs/${id}/monthly`),
    enabled: id != null && ready,
    staleTime: Infinity,
  });

export const useRunTrades = (id: number | null, ready: boolean, offset: number, symbol: string) =>
  useQuery({
    queryKey: ["runTrades", id, offset, symbol],
    queryFn: () =>
      api.get<S["FillsPage"]>(`/research/runs/${id}/trades?limit=100&offset=${offset}${symbol ? `&symbol=${symbol}` : ""}`),
    enabled: id != null && ready,
  });

export const useRunRebalances = (id: number | null, ready: boolean) =>
  useQuery({
    queryKey: ["runRebalances", id],
    queryFn: () => api.get<S["RunRebalance"][]>(`/research/runs/${id}/rebalances`),
    enabled: id != null && ready,
    staleTime: Infinity,
  });

export function useLedgerPage<T>(kind: "orders" | "fills" | "cash" | "events", offset: number, extra = "") {
  return useQuery({
    queryKey: ["ledger", kind, offset, extra],
    queryFn: () => api.get<T>(`/ledger/${kind}?limit=100&offset=${offset}${extra}`),
  });
}

export const useRepro = () =>
  useQuery({ queryKey: ["repro"], queryFn: () => api.get<S["Reproducibility"]>("/ledger/reproducibility") });

// ------------------------------------------------------------------ mutations
function onError(e: unknown) {
  const msg = e instanceof ApiError ? e.message : String(e);
  notifications.show({ color: "red", title: "Request failed", message: msg, autoClose: 8000 });
}

export function usePaperMutations() {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries();
  const opts = { onError, onSuccess: refresh };
  return {
    advance: useMutation({
      mutationFn: (body: S["PaperAdvanceRequest"]) => api.post<S["AdvanceResponse"]>("/paper/advance", body),
      onError,
      onSuccess: (r: S["AdvanceResponse"]) => {
        refresh();
        notifications.show({
          color: r.stopped_reason === "decision_required" ? "yellow" : "blue",
          title: `Processed ${r.processed_count} session(s) - as of ${r.as_of}`,
          message: r.stopped_explanation,
          autoClose: 7000,
        });
      },
    }),
    init: useMutation({ mutationFn: (body: S["PaperInitRequest"]) => api.post<S["PortfolioMeta"]>("/paper/init", body), ...opts }),
    reset: useMutation({ mutationFn: () => api.post<{ message: string }>("/paper/reset"), ...opts }),
    apply: useMutation({ mutationFn: (id: number) => api.post<S["PlanDetail"]>(`/rebalance/plans/${id}/apply`), ...opts }),
    skip: useMutation({
      mutationFn: (v: { id: number; note?: string }) => api.post<S["PlanDetail"]>(`/rebalance/plans/${v.id}/skip`, { note: v.note }),
      ...opts,
    }),
    updateConfig: useMutation({
      mutationFn: (cfg: StrategyConfig) => api.put<S["PaperConfigResponse"]>("/paper/config", cfg),
      ...opts,
    }),
    refreshData: useMutation({
      mutationFn: () => api.post<{ started: boolean; message: string }>("/data/refresh"),
      onError,
      onSuccess: (r: { message: string }) => {
        notifications.show({ color: "blue", title: "Data refresh", message: r.message });
        setTimeout(refresh, 1500);
      },
    }),
  };
}

export function useLaunchRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: S["RunRequest"]) => api.post<S["RunSummary"]>("/research/runs", body),
    onError,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}

// ------------------------------------------------------------------ Alpaca paper account + automation
export const useBrokerOverview = () =>
  useQuery({ queryKey: ["broker"], queryFn: () => api.get<S["BrokerOverview"]>("/broker/overview"), refetchInterval: 30_000 });

export const useAutomationRuns = () =>
  useQuery({
    queryKey: ["automationRuns"],
    queryFn: () => api.get<S["AutomationRun"][]>("/automation/runs?limit=30"),
    refetchInterval: (q) => (q.state.data?.[0]?.status === "running" ? 1500 : 30_000),
  });

export const useBrokerOrders = (offset: number) =>
  useQuery({ queryKey: ["brokerOrders", offset], queryFn: () => api.get<S["BrokerOrdersPage"]>(`/broker/orders?limit=100&offset=${offset}`) });

export const useRunAlpha = (id: number | null, ready: boolean) =>
  useQuery({
    queryKey: ["runAlpha", id],
    queryFn: () => api.get<AlphaResult>(`/research/runs/${id}/alpha`),
    enabled: id != null && ready,
    staleTime: Infinity,
    retry: 0,
  });

export type AlphaFit = {
  months: number; start: string; end: string; alpha_monthly: number; alpha_annual: number; t_alpha: number;
  betas: Record<string, number>; t_betas: Record<string, number>; r2: number | null; nw_lags: number;
};
export type AlphaResult = { model: string; source: string; full: AlphaFit | null; first_half: AlphaFit | null;
  second_half: AlphaFit | null; notes: string[]; excess_returns: boolean };

export type PlanDecision = { plan_id: number; expected_orders: number };

export function useAutomationMutations() {
  const qc = useQueryClient();
  return {
    toggle: useMutation({
      mutationFn: (enabled: boolean) => api.put<{ automation_enabled: boolean }>("/automation/enabled", { enabled }),
      onError,
      onSuccess: () => qc.invalidateQueries({ queryKey: ["broker"] }),
    }),
    rebalanceMode: useMutation({
      mutationFn: (mode: "approve" | "auto") => api.put<{ rebalance_mode: string }>("/automation/rebalance-mode", { mode }),
      onError,
      onSuccess: () => qc.invalidateQueries({ queryKey: ["broker"] }),
    }),
    approve: useMutation({
      mutationFn: (d: PlanDecision) =>
        api.post<{ approved: number; message: string }>(`/broker/plans/${d.plan_id}/approve`, { expected_orders: d.expected_orders }),
      onError,
      onSuccess: (r: { message: string }) => {
        notifications.show({ color: "teal", title: "Rebalance approved", message: r.message });
        setTimeout(() => qc.invalidateQueries(), 1500);
      },
    }),
    decline: useMutation({
      mutationFn: (d: PlanDecision) =>
        api.post<{ declined: number }>(`/broker/plans/${d.plan_id}/decline`, { expected_orders: d.expected_orders }),
      onError,
      onSuccess: (r: { declined: number }) => {
        notifications.show({ color: "yellow", title: "Rebalance declined", message: `${r.declined} order(s) declined` });
        qc.invalidateQueries();
      },
    }),
    run: useMutation({
      mutationFn: (dry_run: boolean) => api.post<{ started: boolean; message: string }>("/automation/run", { dry_run }),
      onError,
      onSuccess: (r: { message: string }) => {
        notifications.show({ color: "blue", title: "Daily cycle", message: r.message });
        setTimeout(() => qc.invalidateQueries(), 1500);
      },
    }),
  };
}

// ---------------------------------------------------------------- strategy variants
export const useVariants = () =>
  useQuery({
    queryKey: ["variants"],
    queryFn: () => api.get<S["VariantsOverview"]>("/research/variants"),
    refetchInterval: (q) => (q.state.data?.studies.some((s) => !s.complete) ? 1500 : false),
  });

export const useVariantStudy = (id: number | null) =>
  useQuery({
    queryKey: ["variantStudy", id],
    queryFn: () => api.get<S["StudyResult"]>(`/research/variants/studies/${id}`),
    enabled: id != null,
    refetchInterval: (q) => (q.state.data && !q.state.data.study.complete ? 1500 : false),
  });

export function useLaunchVariantStudy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { start_date?: string | null; end_date?: string | null }) =>
      api.post<S["VariantStudy"]>("/research/variants/studies", body),
    onError,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["variants"] }),
  });
}

// ---------------------------------------------------------------- paper strategy switch
export const usePaperStrategy = () =>
  useQuery({ queryKey: ["paperStrategy"], queryFn: () => api.get<S["ActiveVariant"]>("/paper/strategy") });

export const useSwitchPreview = () =>
  useMutation({
    mutationFn: (body: { variant: string; replan_now: boolean }) => api.post<S["SwitchPreview"]>("/paper/strategy/preview", body),
    onError,
  });

export function useSwitchApply() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { variant: string; replan_now: boolean; expected_orders: number }) =>
      api.post<S["SwitchResult"]>("/paper/strategy/apply", body),
    onError,
    onSuccess: (r) => {
      notifications.show({ color: "teal", title: "Paper strategy", message: r.message, autoClose: 12_000 });
      qc.invalidateQueries();
    },
  });
}
