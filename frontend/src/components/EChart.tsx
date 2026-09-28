import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AxisPointerComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { C } from "../lib/colors";

echarts.use([
  LineChart, BarChart, HeatmapChart, ScatterChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  VisualMapComponent, MarkLineComponent, MarkPointComponent, MarkAreaComponent, AxisPointerComponent, CanvasRenderer,
]);

export function EChart({ option, height = 280 }: { option: EChartsCoreOption; height?: number | string }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    chart.current = echarts.init(ref.current, undefined, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(ref.current);
    return () => {
      ro.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true });
  }, [option]);

  return <div ref={ref} style={{ width: "100%", height }} />;
}

const FONT = "'Inter Variable', system-ui, -apple-system, 'Segoe UI', sans-serif";
const MONO = "'JetBrains Mono', ui-monospace, monospace";

/** Shared recessive chrome: hairline grid, muted axis labels, dark tooltip. */
export function baseOption(): Record<string, unknown> {
  return {
    backgroundColor: "transparent",
    textStyle: { fontFamily: FONT, color: C.text2, fontSize: 11 },
    animation: false,
    grid: { left: 58, right: 16, top: 28, bottom: 28, containLabel: false },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#1b1f24",
      borderColor: "#2f353c",
      borderWidth: 1,
      padding: [6, 9],
      textStyle: { color: C.text, fontSize: 11, fontFamily: MONO },
      axisPointer: { type: "line", lineStyle: { color: "#4b525a", width: 1 } },
    },
    legend: {
      top: 0, right: 8, itemWidth: 14, itemHeight: 2, icon: "rect",
      textStyle: { color: C.text2, fontSize: 11 }, inactiveColor: "#4b525a",
    },
  };
}

export const timeAxis = (data: string[]) => ({
  type: "category",
  data,
  boundaryGap: false,
  axisLine: { lineStyle: { color: C.axis } },
  axisTick: { show: false },
  axisLabel: { color: C.muted, fontSize: 10, hideOverlap: true },
  splitLine: { show: false },
});

export const valueAxis = (fmt: (v: number) => string, extra: Record<string, unknown> = {}) => ({
  type: "value",
  scale: true,
  axisLine: { show: false },
  axisTick: { show: false },
  axisLabel: { color: C.muted, fontSize: 10, formatter: fmt, fontFamily: MONO },
  splitLine: { lineStyle: { color: C.grid, width: 1 } },
  ...extra,
});

export const lineSeries = (name: string, data: (number | null)[], color: string, extra: Record<string, unknown> = {}) => ({
  name,
  type: "line",
  data,
  showSymbol: false,
  symbolSize: 8,
  lineStyle: { width: 2, color },
  itemStyle: { color },
  emphasis: { focus: "none" },
  connectNulls: false,
  ...extra,
});

export const zoom = [{ type: "inside", throttle: 30 }];
