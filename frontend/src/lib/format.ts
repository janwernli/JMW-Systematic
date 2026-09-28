const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const num2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const DASH = "—";
const nil = (v: unknown): v is null | undefined => v === null || v === undefined || (typeof v === "number" && !isFinite(v));

export const fmtUsd = (v?: number | null, cents = false) => (nil(v) ? DASH : (cents ? usd2 : usd0).format(v));
export const fmtNum = (v?: number | null, digits = 0) =>
  nil(v) ? DASH : digits === 2 ? num2.format(v) : digits === 0 ? num0.format(v) : v.toFixed(digits);
export const fmtPx = (v?: number | null) => (nil(v) ? DASH : v >= 1000 ? num2.format(v) : v.toFixed(2));

/** Percent from a fraction (0.0123 -> "1.23%"). `signed` adds an explicit + for positives. */
export const fmtPct = (v?: number | null, digits = 2, signed = false) =>
  nil(v) ? DASH : `${signed && v > 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;

export const fmtMillions = (v?: number | null) => (nil(v) ? DASH : `$${(v / 1e6).toFixed(1)}M`);
export const fmtBps = (v?: number | null) => (nil(v) ? DASH : `${v.toFixed(0)} bps`);

export const fmtTs = (iso?: string | null) => {
  if (!iso) return DASH;
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
};

export const signClass = (v?: number | null) => (nil(v) || v === 0 ? "" : v > 0 ? "up" : "down");
export const arrow = (v?: number | null) => (nil(v) || v === 0 ? "" : v > 0 ? "▲ " : "▼ ");

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
