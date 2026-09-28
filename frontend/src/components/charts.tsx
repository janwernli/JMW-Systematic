import { useMemo, useState } from "react";
import { SegmentedControl, Tooltip } from "@mantine/core";
import { EChart, baseOption, lineSeries, timeAxis, valueAxis, zoom } from "./EChart";
import { C, DIVERGING } from "../lib/colors";
import { MONTHS, fmtPct, fmtUsd } from "../lib/format";

type Pt = { session: string; nav: number; gross_nav?: number | null; benchmark_nav?: number | null };

const usdAxis = (v: number) => (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${(v / 1e3).toFixed(0)}k`);

/** NAV vs benchmark, both in USD on ONE axis (benchmark scaled to the same starting capital). */
export function EquityChart({
  points, benchmarkName, showGross = false, height = 300, netName = "Net NAV",
}: {
  points: Pt[];
  benchmarkName?: string | null;
  showGross?: boolean;
  height?: number;
  netName?: string;
}) {
  const option = useMemo(() => {
    const dates = points.map((p) => p.session);
    const series = [lineSeries(netName, points.map((p) => p.nav), C.strategy)];
    if (showGross)
      series.push(lineSeries("Gross NAV (pre-cost)", points.map((p) => p.gross_nav ?? null), C.gross, { lineStyle: { width: 1.5, color: C.gross, type: [4, 3] } }));
    if (benchmarkName && points.some((p) => p.benchmark_nav != null))
      series.push(lineSeries(`${benchmarkName} (same start capital)`, points.map((p) => p.benchmark_nav ?? null), C.benchmark, { lineStyle: { width: 1.5, color: C.benchmark } }));
    const base = baseOption();
    return {
      ...base,
      tooltip: {
        ...(base.tooltip as object),
        valueFormatter: (v: number) => fmtUsd(v, true),
      },
      xAxis: timeAxis(dates),
      yAxis: valueAxis(usdAxis),
      dataZoom: zoom,
      series,
    };
  }, [points, benchmarkName, showGross, netName]);
  return <EChart option={option} height={height} />;
}

export function DrawdownChart({
  sessions, strategy, benchmark, benchmarkName, height = 180,
}: {
  sessions: string[];
  strategy: number[];
  benchmark?: (number | null)[] | null;
  benchmarkName?: string | null;
  height?: number;
}) {
  const option = useMemo(() => {
    const base = baseOption();
    const series: Record<string, unknown>[] = [
      lineSeries("Strategy drawdown", strategy, C.strategy, {
        areaStyle: { color: "rgba(57,135,229,0.16)" },
        lineStyle: { width: 1.5, color: C.strategy },
      }),
    ];
    if (benchmark && benchmark.some((v) => v != null))
      series.push(lineSeries(`${benchmarkName ?? "Benchmark"} drawdown`, benchmark, C.benchmark, { lineStyle: { width: 1.2, color: C.benchmark } }));
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), valueFormatter: (v: number) => fmtPct(v, 2) },
      xAxis: timeAxis(sessions),
      yAxis: valueAxis((v: number) => `${(v * 100).toFixed(0)}%`, { max: 0, scale: false }),
      dataZoom: zoom,
      series,
    };
  }, [sessions, strategy, benchmark, benchmarkName]);
  return <EChart option={option} height={height} />;
}

export function drawdownOf(values: number[]): number[] {
  let peak = -Infinity;
  return values.map((v) => {
    peak = Math.max(peak, v);
    return v / peak - 1;
  });
}

function heatColor(v: number | null | undefined, scale: number) {
  if (v == null) return "transparent";
  const bins = [-3, -2, -1, 0, 1, 2, 3];
  const x = Math.max(-3, Math.min(3, Math.round((v / scale) * 3)));
  return DIVERGING[bins.indexOf(x)];
}

/** Monthly return heatmap (years × months), with compounded year column. Values shown in every cell. */
export function MonthlyHeatmap({ rows }: { rows: { year: number; month: number; strategy: number; benchmark: number | null }[] }) {
  const [mode, setMode] = useState<"strategy" | "benchmark" | "difference">("strategy");
  const val = (r: (typeof rows)[number]) =>
    mode === "strategy" ? r.strategy : mode === "benchmark" ? r.benchmark : r.benchmark == null ? null : r.strategy - r.benchmark;
  const years = [...new Set(rows.map((r) => r.year))].sort();
  const map = new Map(rows.map((r) => [`${r.year}-${r.month}`, r]));
  const scale = 0.08; // ±8% saturates the scale
  return (
    <div>
      <SegmentedControl
        mb={6}
        value={mode}
        onChange={(v) => setMode(v as typeof mode)}
        data={[
          { value: "strategy", label: "Strategy" },
          { value: "benchmark", label: "Benchmark" },
          { value: "difference", label: "Difference" },
        ]}
      />
      <div style={{ overflowX: "auto" }}>
        <table className="heat" style={{ borderCollapse: "separate", width: "100%", minWidth: 720 }}>
          <thead>
            <tr>
              <th />
              {MONTHS.map((m) => <th key={m}>{m}</th>)}
              <th>Year</th>
            </tr>
          </thead>
          <tbody>
            {years.map((y) => {
              let comp: number | null = 1;
              return (
                <tr key={y}>
                  <th style={{ textAlign: "right", paddingRight: 6 }}>{y}</th>
                  {MONTHS.map((_, i) => {
                    const r = map.get(`${y}-${i + 1}`);
                    const v = r ? val(r) : null;
                    if (r && mode !== "difference") comp = v == null || comp == null ? null : comp * (1 + v);
                    return (
                      <Tooltip key={i} label={r ? `${MONTHS[i]} ${y}: ${mode} ${fmtPct(v, 2, true)}` : "No data"} disabled={!r}>
                        <td style={{ background: heatColor(v, scale), color: v != null && Math.abs(v) > scale * 0.6 ? "#fff" : "#c4c9cf" }}>
                          {r ? fmtPct(v, 1, true) : ""}
                        </td>
                      </Tooltip>
                    );
                  })}
                  <td style={{ fontWeight: 600, color: "#e6e8eb" }}>
                    {mode === "difference" ? "" : comp == null ? "—" : fmtPct(comp - 1, 1, true)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="chart-note">
        Month return = last NAV of the month ÷ last NAV of the prior month − 1 (first month vs. starting capital). Colour: blue
        positive, red negative, brighter = larger (saturates at ±8%). Year = compounded months shown.
      </div>
    </div>
  );
}
