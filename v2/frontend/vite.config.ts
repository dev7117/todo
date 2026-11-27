import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `python main.py` in v2/ serves the API on 7680; Vite proxies to it.
const target = process.env.API_PROXY_TARGET ?? "http://localhost:7680";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: { "/api": target, "/health": target },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
