import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "node:path";

const apiProxyTarget = process.env.STORYDEX_API_PROXY_TARGET || "http://127.0.0.1:18081";

export default defineConfig({
  base: "./",
  plugins: [vue()],
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
