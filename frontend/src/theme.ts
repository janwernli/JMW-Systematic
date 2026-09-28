import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Charcoal scale used for Mantine's dark palette (0 = lightest text, 9 = deepest background).
const dark: MantineColorsTuple = [
  "#e6e8eb", "#c4c9cf", "#a9b0b8", "#7c848d", "#4b525a", "#2f353c", "#1b1f24", "#14171b", "#0f1215", "#0b0d10",
];

export const theme = createTheme({
  primaryColor: "blue",
  colors: { dark },
  fontFamily: "'Inter Variable', system-ui, -apple-system, 'Segoe UI', sans-serif",
  fontFamilyMonospace: "'JetBrains Mono', ui-monospace, SFMono-Regular, monospace",
  headings: { fontFamily: "'Inter Variable', system-ui, sans-serif", fontWeight: "600" },
  defaultRadius: "sm",
  fontSizes: { xs: "11px", sm: "12px", md: "13px", lg: "15px", xl: "18px" },
  components: {
    Button: { defaultProps: { size: "xs" } },
    TextInput: { defaultProps: { size: "xs" } },
    NumberInput: { defaultProps: { size: "xs" } },
    Select: { defaultProps: { size: "xs" } },
    SegmentedControl: { defaultProps: { size: "xs" } },
    Tooltip: { defaultProps: { color: "dark.6", fz: "xs" } },
  },
});
