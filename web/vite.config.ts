import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Carbon's Sass names the IBM Plex font files as "~@ibm/plex-*"; resolve them from node_modules so they are bundled.
  resolve: { alias: [{ find: /^~(@ibm\/.*)$/, replacement: "$1" }] },
  css: { preprocessorOptions: { scss: { api: "modern-compiler", quietDeps: true, silenceDeprecations: ["mixed-decls"] } } },
  server: { port: 5173, proxy: { "/api": "http://localhost:8080", "/healthz": "http://localhost:8080" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1500 },
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"], css: false },
});
