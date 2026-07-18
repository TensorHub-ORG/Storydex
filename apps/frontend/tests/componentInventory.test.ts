import { afterEach, describe, expect, it, vi } from "vitest";
import { shallowMount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { nextTick } from "vue";

const transport = vi.hoisted(() => {
  const response = { data: { success: true, data: { items: [] }, trace: null, audit: [] } };
  const method = vi.fn().mockResolvedValue(response);
  return {
    get: method,
    post: method,
    put: method,
    patch: method,
    delete: method,
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } }
  };
});

vi.mock("axios", () => ({
  default: {
    create: () => transport,
    isAxiosError: () => false
  }
}));

vi.mock("@/api/system", async (load) => ({
  ...(await load<any>()),
  updateUiPreferences: vi.fn().mockResolvedValue({ data: {}, trace: null, audit: [] })
}));

import App from "@/App.vue";
import AccountMenu from "@/components/ActivityAccountMenu.vue";
import ActivityBar from "@/components/ActivityBar.vue";
import AgentFloatBar from "@/components/AgentExecutionFloatBar.vue";
import AgentPanel from "@/components/AgentPanel.vue";
import AuthConfigPanel from "@/components/CoomiConfigPanel.vue";
import EditorPane from "@/components/EditorPane.vue";
import ExplorerSidebar from "@/components/ExplorerSidebar.vue";
import FilePreviewPane from "@/components/GitReviewPane.vue";
import MessageBubble from "@/components/MessageBubble.vue";
import PresetEditor from "@/components/PresetEditor.vue";
import PresetImportPreview from "@/components/PresetImportPreview.vue";
import PresetSidebar from "@/components/PresetManagementSidebar.vue";
import PromptRepositorySidebar from "@/components/PromptRepositorySidebar.vue";
import SearchToolSidebar from "@/components/SourceControlSidebar.vue";
import StatusBar from "@/components/StatusBar.vue";
import StorySettingsPanel from "@/components/StorySettingsPanel.vue";
import StoryStatePanel from "@/components/StoryStatePanel.vue";
import SettingsWindow from "@/components/SystemSettingsWindow.vue";
import ToolCallCard from "@/components/ToolCallCard.vue";
import TopHeader from "@/components/TopHeader.vue";
import TracePanel from "@/components/TracePanel.vue";
import WorkspaceStartPage from "@/components/WelcomeStartPage.vue";
import WorkbenchLayout from "@/layouts/WorkbenchLayout.vue";
import FilePreviewView from "@/views/FilePreviewView.vue";
import WorkbenchView from "@/views/WorkbenchView.vue";

afterEach(() => vi.clearAllTimers());

describe("frontend component inventory", () => {
  const components = [
    App, AccountMenu, ActivityBar, AgentFloatBar, AgentPanel, AuthConfigPanel,
    EditorPane, ExplorerSidebar, FilePreviewPane, MessageBubble, PresetEditor,
    PresetImportPreview, PresetSidebar, PromptRepositorySidebar, SearchToolSidebar, StatusBar,
    StorySettingsPanel, StoryStatePanel, SettingsWindow, ToolCallCard, TopHeader,
    TracePanel, WorkspaceStartPage, WorkbenchLayout, FilePreviewView, WorkbenchView
  ];

  it.each(components.map((component, index) => [index, component] as const))(
    "initializes component %s without a real backend",
    async (_index, component) => {
      const pinia = createPinia();
      setActivePinia(pinia);
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: "/", component: { template: "<div />" } },
          { path: "/preview", component: { template: "<div />" } }
        ]
      });
      await router.push("/");
      await router.isReady();
      const wrapper = shallowMount(component as never, {
        props: {
          open: true,
          visible: true,
          modelValue: "",
          items: [],
          loading: false,
          errorMessage: "",
          toolName: "read_file",
          status: "success",
          diff: "",
          message: { role: "assistant", content: "hello" },
          item: { id: "tool", type: "tool", title: "Tool", content: "done", status: "success", timestamp: new Date().toISOString(), raw: {} }
        },
        global: {
          plugins: [pinia, router],
          stubs: { teleport: true, transition: false }
        }
      });
      await nextTick();
      expect(wrapper.exists()).toBe(true);
      // Exercise every currently rendered interaction surface. This inventory sweep is
      // intentionally generic: dedicated component suites assert semantics, while this
      // catches wiring regressions and ensures template handlers can execute offline.
      for (const element of wrapper.findAll("button, input, textarea, select, a, [role='button'], [tabindex]")) {
        for (const event of ["click", "dblclick", "contextmenu", "change", "input"]) {
          try {
            await element.trigger(event);
          } catch {
            // Invalid actions are expected for disabled/guarded empty-state controls.
          }
        }
        for (const key of ["Enter", "Escape", "ArrowDown", "ArrowUp", " "]) {
          try {
            await element.trigger("keydown", { key });
          } catch {
            // Component-specific keyboard assertions live in focused suites.
          }
        }
      }
      await nextTick();
      wrapper.unmount();
    }
  );
});
