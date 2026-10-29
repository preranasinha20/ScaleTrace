import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In development, /api is proxied to the FastAPI backend, so no CORS setup is needed.
// Set VITE_API_BASE_URL only when the frontend is served from a different origin than the API.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.SCALETRACE_API_PROXY ?? "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    server: { host: "127.0.0.1", port: 5173, proxy: { "/api": { target, changeOrigin: false } } },
    preview: { host: "127.0.0.1", port: 4173, proxy: { "/api": { target, changeOrigin: false } } },
    build: { outDir: "dist", sourcemap: false },
  };
});
