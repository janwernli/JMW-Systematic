import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev server binds to localhost only and proxies /api to the FastAPI backend.
// The backend port is read from the repository-level .env (APP_PORT, default 8765).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "..", "APP_");
  const apiPort = env.APP_PORT || "8765";
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: { "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false } },
    },
    preview: { host: "127.0.0.1", port: 4173 },
    build: {
      chunkSizeWarningLimit: 1500,
      rollupOptions: {
        output: {
          manualChunks: { echarts: ["echarts"], mantine: ["@mantine/core", "@mantine/hooks", "@mantine/notifications"] },
        },
      },
    },
  };
});
