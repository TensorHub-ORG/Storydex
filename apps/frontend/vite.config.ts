import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "node:path";
import type { Plugin, Rule } from "postcss";
import { isMaterialSymbolSelector, transformPaneRelativePixelValue } from "./src/utils/paneFontScale";

const apiProxyTarget = process.env.STORYDEX_API_PROXY_TARGET || "http://127.0.0.1:18081";

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
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api/v1": {
        target: apiProxyTarget,
        changeOrigin: true
      }
    }
  }
});
