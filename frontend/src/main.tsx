import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import "@fontsource-variable/inter";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles/global.css";

import { theme } from "./theme";
import { Layout } from "./components/Layout";
import { CommandCenter } from "./pages/CommandCenter";
import { Universe } from "./pages/Universe";
import { Portfolio } from "./pages/Portfolio";
import { RebalanceDesk } from "./pages/RebalanceDesk";
import { ResearchLab } from "./pages/ResearchLab";
import { Ledger } from "./pages/Ledger";
import { Trading } from "./pages/Trading";

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider theme={theme} forceColorScheme="dark">
      <Notifications position="bottom-right" limit={4} />
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<CommandCenter />} />
              <Route path="trading" element={<Trading />} />
              <Route path="universe" element={<Universe />} />
              <Route path="portfolio" element={<Portfolio />} />
              <Route path="rebalance" element={<RebalanceDesk />} />
              <Route path="research" element={<ResearchLab />} />
              <Route path="ledger" element={<Ledger />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
