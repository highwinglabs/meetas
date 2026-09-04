import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built app is served BY the core on http://127.0.0.1:8765, so in
// production all API calls are same-origin (relative URLs). In `vite dev`
// the listed API prefixes are proxied to the running core, so the same
// relative calls work without CORS.
const CORE = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/health": CORE,
      "/devices": CORE,
      "/consent": CORE,
      "/meetings": CORE,
      "/models": CORE,
      "/search": CORE,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
  },
});
