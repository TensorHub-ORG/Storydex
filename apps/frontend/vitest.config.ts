import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import path from "node:path";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") }
  },
  test: {
    environment: "happy-dom",
    setupFiles: ["tests/setup.ts"],
    include: ["tests/**/*.test.ts"],
    testTimeout: 10_000,
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html", "lcov"],
      reportsDirectory: "test-results/coverage",
      include: ["src/**/*.{ts,vue,mjs}"],
      // Type-only modules are erased by TypeScript and contain no runtime behavior.
      exclude: ["src/**/*.d.ts", "src/types/**", "src/main.ts"],
      thresholds: {
        statements: 80,
        lines: 80,
        functions: 80,
        branches: 70,
        "src/api/sseParser.mjs": { statements: 90, lines: 90, functions: 90, branches: 90 },
        "src/stores/agent.ts": { statements: 90, lines: 90, functions: 90, branches: 90 },
        "src/components/AgentPanel.vue": { statements: 90, lines: 90, functions: 90, branches: 90 }
      }
    }
  }
});
