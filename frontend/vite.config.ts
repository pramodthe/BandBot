import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { shikiGrammarAssets } from "./scripts/shiki-grammar-assets.ts";

export default defineConfig({
  plugins: [react(), tailwindcss(), shikiGrammarAssets()],
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [{ name: "react-vendor", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ }],
        },
      },
    },
  },
  worker: { format: "es" },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      // Also covers react-shiki's internal loader, avoiding an embedded base64 WASM bundle.
      "shiki/wasm": fileURLToPath(new URL("./src/lib/shiki-wasm.ts", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": `http://127.0.0.1:${process.env.BAND_E2E_API_PORT || "8000"}`,
      "/ws": { target: `ws://127.0.0.1:${process.env.BAND_E2E_API_PORT || "8000"}`, ws: true },
    },
  },
});
