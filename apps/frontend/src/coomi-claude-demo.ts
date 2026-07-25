// 临时演示入口：预览 Claude Desktop 风格的 Coomi 回复布局。
// 通过脚本模拟一次真实运行（工具调用运行中展开 -> 结束折叠 -> 可再点开），
// 底部展示处理过程 / 时间 / token / 状态。不触发任何真实请求。
import { computed, createApp, h, onBeforeUnmount, onMounted, ref } from "vue";
import CoomiClaudeTurn from "@/components/demo/CoomiClaudeTurn.vue";
import { initializeIconFontState } from "@/utils/iconFont";
import type { AgentExecutionRun, CoomiWaterfallItem, CoomiWaterfallItemStatus } from "@/types/agent";
import "@fontsource/material-symbols-rounded/400.css";
import "./assets/theme.css";

initializeIconFontState();

type ThemeKey = "default" | "snow" | "book" | "dark";

function makeItem(partial: Partial<CoomiWaterfallItem> & { type: CoomiWaterfallItem["type"] }): CoomiWaterfallItem {
  return {
    id: partial.id || `item-${Math.random().toString(36).slice(2, 9)}`,
    type: partial.type,
    status: partial.status || "success",
    title: partial.title || "",
    content: partial.content || "",
    timestamp: partial.timestamp || new Date().toISOString(),
    toolName: partial.toolName,
    toolCallId: partial.toolCallId,
    arguments: partial.arguments,
    resultPreview: partial.resultPreview,
    usage: partial.usage,
    compression: partial.compression,
    raw: partial.raw
  };
}

function baseRun(): AgentExecutionRun {
  return {
    traceId: `demo-${Date.now()}`,
    sessionId: "demo-session",
    prompt: "帮我在 apps/frontend 里找到负责主题切换的代码，并说明它是怎么工作的。",
    route: "coomi",
    agentMode: "chat",
    llmModel: "claude-opus-4-8",
    llmProvider: "anthropic",
    status: "running",
    noRestorePoint: false,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    lastAction: "chat",
    reply: "",
    trace: null,
    audit: [],
    events: [],
    tasks: [],
    changeLedger: {} as AgentExecutionRun["changeLedger"],
    items: [],
    errorMessage: "",
    errorCode: null,
    turnTokens: null,
    turnDurationMs: null
  };
}

// 一步步“播放”的脚本：模拟推理、工具运行->完成、正文。
type ScriptStep =
  | { at: number; kind: "reasoning"; content: string }
  | { at: number; kind: "tool-start"; id: string; toolName: string; args: Record<string, unknown> }
  | { at: number; kind: "tool-done"; id: string; result: string; status?: CoomiWaterfallItemStatus }
  | { at: number; kind: "assistant"; content: string }
  | { at: number; kind: "done" };

const SCRIPT: ScriptStep[] = [
  { at: 400, kind: "reasoning", content: "先理解需求：用户想定位主题切换逻辑。\n主题相关代码通常在 theme.css、stores、以及顶部工具栏组件里。\n我先搜索 theme 关键字，再读关键文件确认。" },
  { at: 1400, kind: "tool-start", id: "t1", toolName: "search", args: { query: "data-theme", path: "apps/frontend/src" } },
  { at: 2600, kind: "tool-done", id: "t1", result: "找到 7 处匹配：\n- assets/theme.css: [data-theme=\"snow\"] ...\n- stores/workspace.ts: applyTheme()\n- components/TopHeader.vue: theme-select" },
  { at: 3000, kind: "tool-start", id: "t2", toolName: "read_file", args: { path: "apps/frontend/src/stores/workspace.ts" } },
  { at: 4200, kind: "tool-done", id: "t2", result: "function applyTheme(theme) {\n  document.documentElement.setAttribute('data-theme', theme)\n  localStorage.setItem('storydex-theme', theme)\n}" },
  { at: 4600, kind: "tool-start", id: "t3", toolName: "read_file", args: { path: "apps/frontend/src/components/TopHeader.vue" } },
  { at: 5800, kind: "tool-done", id: "t3", result: "<select class=\"theme-select\" @change=\"applyTheme\">\n  <option value=\"default\">默认</option>\n  <option value=\"dark\">深色</option>\n</select>" },
  { at: 6200, kind: "reasoning", content: "已经确认三处关键代码，可以组织成一个清晰的说明了。\n主题状态存 localStorage，通过 data-theme 属性驱动 CSS 变量。" },
  {
    at: 7400,
    kind: "assistant",
    content:
      "主题切换由三部分协作完成：\n\n1. **`assets/theme.css`** — 用 `[data-theme=\"xxx\"]` 选择器定义每套主题的 CSS 变量（颜色、圆角等）。\n2. **`stores/workspace.ts` 的 `applyTheme()`** — 把选中的主题写到 `<html>` 的 `data-theme` 属性，并存进 `localStorage` 持久化。\n3. **`components/TopHeader.vue`** — 顶部的 `<select>` 触发 `applyTheme`，用户切换即时生效。\n\n整体是**属性驱动**的方案：切换只改一个属性，所有组件通过 CSS 变量自动响应，无需重新渲染。"
  },
  { at: 7600, kind: "done" }
];

const App = {
  setup() {
    const run = ref<AgentExecutionRun>(baseRun());
    const elapsedMs = ref(0);
    const theme = ref<ThemeKey>("default");
    let timers: number[] = [];
    let ticker: number | null = null;
    let startedAt = 0;

    const isRunning = computed(() => run.value.status === "running");

    function applyTheme(next: ThemeKey): void {
      theme.value = next;
      if (next === "default") {
        document.documentElement.removeAttribute("data-theme");
      } else {
        document.documentElement.setAttribute("data-theme", next);
      }
    }

    function clearTimers(): void {
      timers.forEach((t) => window.clearTimeout(t));
      timers = [];
      if (ticker !== null) {
        window.clearInterval(ticker);
        ticker = null;
      }
    }

    function pushItem(item: CoomiWaterfallItem): void {
      run.value = { ...run.value, items: [...run.value.items, item], updatedAt: new Date().toISOString() };
    }

    function patchTool(id: string, patch: Partial<CoomiWaterfallItem>): void {
      run.value = {
        ...run.value,
        items: run.value.items.map((item) => (item.toolCallId === id ? { ...item, ...patch } : item)),
        updatedAt: new Date().toISOString()
      };
    }

    function play(): void {
      clearTimers();
      run.value = baseRun();
      elapsedMs.value = 0;
      startedAt = Date.now();

      // 运行中只本地计时；单轮 token 要等“AgentCompleted”才有真实值
      ticker = window.setInterval(() => {
        if (run.value.status === "running") {
          elapsedMs.value = Date.now() - startedAt;
        }
      }, 200);

      for (const step of SCRIPT) {
        const timer = window.setTimeout(() => {
          if (step.kind === "reasoning") {
            pushItem(makeItem({ type: "reasoning", content: step.content }));
          } else if (step.kind === "tool-start") {
            pushItem(
              makeItem({
                type: "tool",
                status: "running",
                toolName: step.toolName,
                toolCallId: step.id,
                title: step.toolName,
                arguments: step.args
              })
            );
          } else if (step.kind === "tool-done") {
            patchTool(step.id, { status: step.status || "success", resultPreview: step.result });
          } else if (step.kind === "assistant") {
            pushItem(makeItem({ type: "assistant", content: step.content }));
          } else if (step.kind === "done") {
            // 模拟 AgentCompleted：后端在这一刻才给出权威的单轮耗时与 token
            const durationMs = Date.now() - startedAt;
            elapsedMs.value = durationMs;
            run.value = {
              ...run.value,
              status: "completed",
              turnTokens: 1520,
              turnDurationMs: durationMs,
              updatedAt: new Date().toISOString()
            };
            if (ticker !== null) {
              window.clearInterval(ticker);
              ticker = null;
            }
          }
        }, step.at);
        timers.push(timer);
      }
    }

    function showStatic(): void {
      // 直接展示一个已完成的回复（用于查看折叠态默认外观）
      clearTimers();
      const done = baseRun();
      done.status = "completed";
      done.items = [
        makeItem({ type: "reasoning", content: "先理解需求，再搜索并读取关键文件确认实现。" }),
        makeItem({ type: "tool", status: "success", toolName: "search", toolCallId: "s1", arguments: { query: "data-theme" }, resultPreview: "找到 7 处匹配" }),
        makeItem({ type: "tool", status: "success", toolName: "read_file", toolCallId: "s2", arguments: { path: "stores/workspace.ts" }, resultPreview: "applyTheme() { ... }" }),
        makeItem({ type: "tool", status: "success", toolName: "read_file", toolCallId: "s3", arguments: { path: "components/TopHeader.vue" }, resultPreview: "<select class=theme-select>" }),
        makeItem({
          type: "assistant",
          content: "主题切换由 `theme.css`、`applyTheme()` 和顶部 `<select>` 协作完成，属性驱动、即时生效。"
        })
      ];
      done.turnDurationMs = 7200;
      done.turnTokens = 1520;
      run.value = done;
      elapsedMs.value = 7200;
    }

    onMounted(() => play());
    onBeforeUnmount(() => clearTimers());

    const themes: Array<{ key: ThemeKey; label: string }> = [
      { key: "default", label: "默认" },
      { key: "snow", label: "雪色" },
      { key: "book", label: "书卷" },
      { key: "dark", label: "深色" }
    ];

    return () =>
      h("div", { class: "demo-root" }, [
        h("aside", { class: "demo-panel" }, [
          h("div", { class: "demo-panel-title" }, "演示控制"),
          h("div", { class: "demo-btn-row" }, [
            h("button", { class: "demo-btn primary", onClick: play }, isRunning.value ? "重新播放" : "播放运行过程"),
            h("button", { class: "demo-btn", onClick: showStatic }, "查看完成态（折叠）")
          ]),
          h("div", { class: "demo-panel-title" }, "主题"),
          h(
            "div",
            { class: "demo-btn-row" },
            themes.map((t) =>
              h(
                "button",
                {
                  class: ["demo-btn", { active: theme.value === t.key }],
                  onClick: () => applyTheme(t.key)
                },
                t.label
              )
            )
          ),
          h("p", { class: "demo-hint" }, "运行中工具组默认展开，结束后自动折叠；点标题或每行可再次展开查看参数与结果。")
        ]),
        h("main", { class: "demo-stage" }, [
          h("div", { class: "demo-surface" }, [
            h(CoomiClaudeTurn, {
              run: run.value,
              elapsedMs: elapsedMs.value
            })
          ])
        ])
      ]);
  }
};

createApp(App).mount("#app");
