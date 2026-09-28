// Chart palette (validated with the dataviz palette checker against the chart surface #14171b, dark mode:
// all-pairs CVD ΔE ≥ 9.4, normal-vision ΔE ≥ 20.9, contrast ≥ 3:1).
export const C = {
  surface: "#14171b",
  strategy: "#3987e5", // slot 1 - the strategy / net NAV
  benchmark: "#d95926", // slot 2 - benchmark
  gross: "#199e70", // slot 3 - gross (pre-cost) NAV
  series4: "#c98500",
  muted: "#7c848d",
  text: "#e6e8eb",
  text2: "#a9b0b8",
  grid: "#22272d",
  axis: "#383d44",
  // Status (reserved: meaning only, always paired with a sign/arrow/label)
  up: "#3fb950",
  down: "#f0605a",
  warn: "#e3a008",
  neutralMid: "#383835",
};

// Diverging ramp for the monthly heatmap on a dark surface: larger magnitude = brighter; blue positive, red negative.
export const DIVERGING = ["#e66767", "#b24a48", "#6e3634", "#2a2d31", "#1f4a7a", "#2a6fc0", "#5598e7"];
export const SERIES = [C.strategy, C.benchmark, C.gross, C.series4];
