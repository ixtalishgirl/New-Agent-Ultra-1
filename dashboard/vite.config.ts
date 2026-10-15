import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built output goes to dashboard/dist — served by the Python backend
// (agent/api/server.py serves dashboard/dist as static files).
// base './' keeps asset URLs relative so it works behind any path/proxy.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
