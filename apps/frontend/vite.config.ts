import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "node:path";
import type { Plugin, Rule } from "postcss";
import { isMaterialSymbolSelector, transformPaneRelativePixelValue } from "./src/utils/paneFontScale";

// Browser-only development can opt into a Rust agentd endpoint. Tauri does
// not use this proxy: it injects its dynamic sidecar URL at runtime.
const apiProxyTarget = process.env.STORYDEX_API_PROXY_TARGET || "http://127.0.0.1:18080";
const frontendPort = Number(process.env.STORYDEX_FRONTEND_PORT || 5173);

if (!Number.isInteger(frontendPort) || frontendPort < 1 || frontendPort > 65535) {
  throw new Error(`Invalid STORYDEX_FRONTEND_PORT: ${process.env.STORYDEX_FRONTEND_PORT}`);
}

function paneFontScalePlugin(): Plugin {
  return {
    postcssPlugin: "storydex-pane-font-scale",
    Declaration(declaration) {
      if (declaration.prop !== "font-size" && declaration.prop !== "line-height") {
        return;
      }
      const selector = declaration.parent?.type === "rule" ? (declaration.parent as Rule).selector : "";
      if (isMaterialSymbolSelector(selector)) {
        return;
      }
      declaration.value = transformPaneRelativePixelValue(declaration.value);
    }
  };
}

export default defineConfig({
  base: "./",
  plugins: [vue()],
  css: {
    postcss: {
      plugins: [paneFontScalePlugin()]
    }
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src")
    }
  },
  server: {
    port: frontendPort,
    host: "127.0.0.1",
    strictPort: true,
    proxy: {
      "/api/v1": {
        target: apiProxyTarget,
        changeOrigin: true
      }
    }
  }
});
