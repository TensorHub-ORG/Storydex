<template>
  <section
    v-if="visible"
    class="coomi-config-shell"
    @keydown.ctrl.s.prevent="() => saveConfig()"
    @keydown.meta.s.prevent="() => saveConfig()"
  >
    <header class="coomi-config-header">
      <div class="coomi-config-title">LLM配置</div>
      <button class="coomi-config-close" type="button" title="退出" @click="$emit('close')">
        <span class="material-symbols-rounded">close</span>
        <span>退出</span>
      </button>
    </header>

    <div v-if="errorMessage" class="coomi-config-error">{{ errorMessage }}</div>

    <main class="coomi-config-body">
      <section v-if="hasProviders" class="coomi-provider-manager">
        <div class="coomi-provider-picker-row">
          <label class="coomi-config-field coomi-provider-picker">
            <span>提供方</span>
            <select :value="selectedProviderId" :disabled="loading || saving" @change="handleProviderSelect">
              <option v-for="provider in providerOptions" :key="provider.id" :value="provider.id">
                {{ provider.id }} · {{ provider.display }}{{ provider.isActive ? "（当前）" : "" }}
              </option>
            </select>
          </label>
          <div class="coomi-toolbar-actions">
            <button
              class="coomi-config-icon-action"
              type="button"
              title="新建提供方"
              :disabled="loading || saving"
              @click="createProvider"
            >
              <span class="material-symbols-rounded">add</span>
            </button>
            <button
              class="coomi-config-icon-action danger"
              type="button"
              title="删除提供方"
              :disabled="loading || saving || providerOptions.length <= 1"
              @click="deleteProvider"
            >
              <span class="material-symbols-rounded">delete</span>
            </button>
          </div>
        </div>

        <section class="coomi-provider-editor">
          <div class="coomi-editor-head">
            <div class="coomi-section-title">
              <span>编辑提供方</span>
              <small>{{ selectedProviderId }}</small>
            </div>
          </div>

          <div class="coomi-provider-form">
            <label class="coomi-config-field full">
              <span>提供方 ID</span>
              <input
                v-model="form.id"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="deepseek"
                @input="markDirty"
              />
            </label>

            <label class="coomi-config-field">
              <span>类型</span>
              <select v-model="form.type" :disabled="loading || saving" @change="syncProviderFields">
                <option value="generic">generic</option>
                <option value="openai">openai</option>
                <option value="anthropic">anthropic</option>
              </select>
            </label>

            <label class="coomi-config-field">
              <span>工具协议</span>
              <select v-model="form.toolProtocol" :disabled="loading || saving" @change="syncProviderFields">
                <option value="auto">auto</option>
                <option value="native">native</option>
                <option value="structured">structured</option>
                <option value="mimo">mimo</option>
                <option value="disabled">disabled</option>
              </select>
            </label>

            <label class="coomi-config-field full">
              <span>显示名称</span>
              <input
                v-model="form.display"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="DeepSeek V4"
                @input="syncProviderFields"
              />
            </label>

            <label class="coomi-config-field full">
              <span>接口地址</span>
              <input
                v-model="form.baseUrl"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="https://api.example.com/v1"
                @input="handleProviderConnectionInput"
              />
            </label>

            <label class="coomi-config-field full">
              <span>API 密钥</span>
              <input
                v-model="form.apiKey"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="sk-..."
                @input="handleProviderConnectionInput"
              />
            </label>

            <div class="coomi-model-fetch-row">
              <button
                class="coomi-config-action"
                type="button"
                :disabled="modelFetchDisabled"
                @click="fetchModels"
              >
                <span class="material-symbols-rounded">cloud_download</span>
                <span>{{ fetchingModels ? "获取中" : "获取模型" }}</span>
              </button>
              <span v-if="modelFetchMessage" class="coomi-model-fetch-message">{{ modelFetchMessage }}</span>
            </div>

            <label class="coomi-config-field full">
              <span>标准模型</span>
              <input
                v-model="form.model"
                class="coomi-model-input"
                :disabled="loading || saving"
                list="coomi-standard-model-options"
                spellcheck="false"
                placeholder="输入模型名，或从已获取列表中选择"
                @input="syncProviderFields"
              />
              <datalist id="coomi-standard-model-options">
                <option v-for="model in modelOptions" :key="model" :value="model"></option>
              </datalist>
            </label>

            <label class="coomi-config-field full">
              <span>快速模型</span>
              <input
                v-model="form.fastModel"
                class="coomi-fast-model-input"
                :disabled="loading || saving"
                list="coomi-fast-model-options"
                spellcheck="false"
                placeholder="留空跟随标准模型，或输入模型名"
                @input="syncProviderFields"
              />
              <datalist id="coomi-fast-model-options">
                <option v-for="model in modelOptions" :key="model" :value="model"></option>
              </datalist>
            </label>

            <label class="coomi-config-field full">
              <span>上下文窗口（tokens）</span>
              <input
                v-model="form.contextWindow"
                :disabled="loading || saving"
                spellcheck="false"
                inputmode="numeric"
                placeholder="留空使用默认 256000；按模型实际窗口填写，压缩阈值随之生效"
                @input="syncProviderFields"
              />
            </label>
          </div>
        </section>
      </section>

      <section v-else class="coomi-config-empty">
        <span>providers.json 里还没有提供方。</span>
        <button class="coomi-config-action primary" type="button" :disabled="loading || saving" @click="createProvider">
          <span class="material-symbols-rounded">add</span>
          <span>新建提供方</span>
        </button>
      </section>
    </main>

    <footer class="coomi-config-footer">
      <div class="coomi-config-path">
        <span>配置文件</span>
        <code>{{ configPath }}</code>
      </div>
      <div class="coomi-config-footer-row">
        <span class="coomi-config-meta">{{ updatedLabel }}</span>
        <div class="coomi-config-actions">
          <button
            class="coomi-config-action"
            type="button"
            title="只写入配置文件，不切换当前使用的提供方"
            :disabled="loading || saving"
            @click="() => saveConfig()"
          >
            <span class="material-symbols-rounded">save</span>
            <span>保存</span>
          </button>
          <button
            class="coomi-config-action primary"
            type="button"
            title="保存配置，并切换为当前正在编辑的提供方"
            :disabled="loading || saving"
            @click="applyConfig"
          >
            <span class="material-symbols-rounded">task_alt</span>
            <span>应用</span>
          </button>
        </div>
      </div>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { fetchAgentCoomiConfig, fetchAgentCoomiModels, updateAgentCoomiConfig } from "@/api/agent";
import { useAgentStore } from "@/stores/agent";

type ProviderType = "generic" | "openai" | "anthropic";
type ToolProtocol = "auto" | "native" | "structured" | "mimo" | "disabled";

interface ProviderForm {
  id: string;
  type: ProviderType;
  toolProtocol: ToolProtocol;
  display: string;
  apiKey: string;
  baseUrl: string;
  model: string;
  fastModel: string;
  contextWindow: string;
}

interface ProviderOption {
  id: string;
  display: string;
  model: string;
  isActive: boolean;
}

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{
  close: [];
  saved: [];
}>();

const agentStore = useAgentStore();
const loading = ref(false);
const saving = ref(false);
const fetchingModels = ref(false);
const configPath = ref("C:/Users/Septem/.storydex/.coomi/config/providers.json");
const updatedAt = ref("");
const errorMessage = ref("");
const modelFetchMessage = ref("");
const modelOptions = ref<string[]>([]);
const configData = ref<Record<string, unknown>>(emptyConfig());
const selectedProviderId = ref("");
const formProviderId = ref("");
const dirty = ref(false);
const form = reactive<ProviderForm>(emptyForm());

const activeProviderId = computed(() => asString(configData.value.active) || "");

const providerOptions = computed<ProviderOption[]>(() => {
  const providers = getProviders();
  return Object.entries(providers).map(([id, value]) => {
    const provider = asRecord(value) || {};
    const model = asString(provider.model) || "";
    const display = asString(provider.display) || model || id;
    return {
      id,
      display,
      model,
      isActive: id === activeProviderId.value
    };
  });
});

const hasProviders = computed(() => providerOptions.value.length > 0);

const modelFetchDisabled = computed(
  () => loading.value || saving.value || fetchingModels.value || !form.baseUrl.trim() || !form.apiKey.trim()
);

const updatedLabel = computed(() => {
  if (saving.value) return "正在保存";
  if (loading.value) return "正在加载";
  if (dirty.value) return "存在未保存修改";
  return updatedAt.value ? `已更新 ${formatDate(updatedAt.value)}` : "就绪";
});

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      void loadConfig();
    }
  },
  { immediate: true }
);

async function loadConfig(): Promise<void> {
  if (!props.visible) {
    return;
  }
  loading.value = true;
  errorMessage.value = "";
  try {
    const result = await fetchAgentCoomiConfig();
    configPath.value = result.data.configPath || configPath.value;
    updatedAt.value = result.data.updatedAt || "";
    configData.value = normalizeConfig(result.data.content);
    selectInitialProvider();
    dirty.value = false;
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "无法加载 Coomi providers.json。";
  } finally {
    loading.value = false;
  }
}

async function saveConfig(options: { apply?: boolean } = {}): Promise<void> {
  errorMessage.value = "";
  if (!commitFormToConfig()) {
    return;
  }
  const savedProviderId = selectedProviderId.value;
  if (options.apply) {
    configData.value.active = savedProviderId;
  }

  saving.value = true;
  try {
    const content = `${JSON.stringify(configData.value, null, 2)}\n`;
    const result = await updateAgentCoomiConfig({ content });
    configPath.value = result.data.configPath || configPath.value;
    updatedAt.value = result.data.updatedAt || "";
    configData.value = normalizeConfig(result.data.content || content);
    const providers = getProviders();
    selectedProviderId.value =
      (savedProviderId && Object.prototype.hasOwnProperty.call(providers, savedProviderId) && savedProviderId) ||
      activeProviderId.value ||
      Object.keys(providers)[0] ||
      "";
    if (selectedProviderId.value) {
      loadForm(selectedProviderId.value);
    }
    dirty.value = false;
    if (options.apply) {
      await agentStore.refreshCoomiStatus();
      emit("saved");
    }
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "无法保存 Coomi providers.json。";
  } finally {
    saving.value = false;
  }
}

async function applyConfig(): Promise<void> {
  await saveConfig({ apply: true });
}

function selectProvider(providerId: string): void {
  commitProviderFields(formProviderId.value);
  selectedProviderId.value = providerId;
  loadForm(providerId);
}

function handleProviderSelect(event: Event): void {
  const providerId = event.target instanceof HTMLSelectElement ? event.target.value : "";
  if (providerId) {
    selectProvider(providerId);
  }
}

function createProvider(): void {
  const providers = getProviders();
  const id = nextProviderId(providers);
  providers[id] = {
    type: "generic",
    display: "",
    api_key: "",
    base_url: "",
    model: "",
    fast_model: "",
    tool_protocol: "auto"
  };
  configData.value.providers = providers;
  if (!activeProviderId.value) {
    configData.value.active = id;
  }
  selectedProviderId.value = id;
  loadForm(id);
  dirty.value = true;
}

function deleteProvider(): void {
  const providers = getProviders();
  const currentId = selectedProviderId.value;
  if (!currentId || providerOptions.value.length <= 1) {
    return;
  }
  delete providers[currentId];
  configData.value.providers = providers;
  if (activeProviderId.value === currentId) {
    configData.value.active = Object.keys(providers)[0] || "";
  }
  selectedProviderId.value = activeProviderId.value || Object.keys(providers)[0] || "";
  if (selectedProviderId.value) {
    loadForm(selectedProviderId.value);
  } else {
    formProviderId.value = "";
    Object.assign(form, emptyForm());
  }
  dirty.value = true;
}

function syncProviderFields(): void {
  commitProviderFields(formProviderId.value);
  dirty.value = true;
}

function handleProviderConnectionInput(): void {
  resetModelOptions();
  syncProviderFields();
}

async function fetchModels(): Promise<void> {
  const baseUrl = form.baseUrl.trim();
  const apiKey = form.apiKey.trim();
  errorMessage.value = "";
  modelFetchMessage.value = "";
  if (!baseUrl || !apiKey) {
    modelFetchMessage.value = "请先填写接口地址和 API 密钥。";
    return;
  }

  fetchingModels.value = true;
  try {
    const result = await fetchAgentCoomiModels({ baseUrl, apiKey });
    modelOptions.value = normalizeModelOptions(result.data.models);
    if (!modelOptions.value.length) {
      modelFetchMessage.value = "未获取到模型列表，可继续保留当前模型。";
      return;
    }
    modelFetchMessage.value = `已获取 ${modelOptions.value.length} 个模型，可从输入建议中选择。`;
  } catch (error: unknown) {
    modelFetchMessage.value = error instanceof Error ? error.message : "获取模型失败。";
  } finally {
    fetchingModels.value = false;
  }
}

function normalizeModelOptions(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values || []) {
    const model = String(value || "").trim();
    if (!model || seen.has(model)) {
      continue;
    }
    seen.add(model);
    result.push(model);
  }
  return result;
}

function resetModelOptions(): void {
  modelOptions.value = [];
  modelFetchMessage.value = "";
}

function markDirty(): void {
  dirty.value = true;
}

function commitFormToConfig(): boolean {
  const nextId = normalizeProviderId(form.id);
  if (!nextId) {
    errorMessage.value = "Provider ID 不能为空。";
    return false;
  }

  const providers = getProviders();
  const previousId = formProviderId.value || selectedProviderId.value;
  if (nextId !== previousId && Object.prototype.hasOwnProperty.call(providers, nextId)) {
    errorMessage.value = `Provider ID "${nextId}" 已存在。`;
    return false;
  }

  const provider = buildProviderRecord(asRecord(providers[previousId]) || {});
  if (previousId && previousId !== nextId) {
    delete providers[previousId];
  }
  providers[nextId] = provider;
  configData.value.providers = providers;
  if (!activeProviderId.value || activeProviderId.value === previousId) {
    configData.value.active = nextId;
  }
  selectedProviderId.value = nextId;
  formProviderId.value = nextId;
  form.id = nextId;
  return true;
}

function commitProviderFields(providerId: string): void {
  const providers = getProviders();
  if (!providerId || !Object.prototype.hasOwnProperty.call(providers, providerId)) {
    return;
  }
  providers[providerId] = buildProviderRecord(asRecord(providers[providerId]) || {});
  configData.value.providers = providers;
}

function buildProviderRecord(previous: Record<string, unknown>): Record<string, unknown> {
  const next: Record<string, unknown> = { ...previous };
  next.type = normalizeProviderType(form.type);
  next.display = form.display.trim();
  next.api_key = form.apiKey.trim();
  next.model = form.model.trim();
  next.tool_protocol = normalizeToolProtocol(form.toolProtocol);

  const baseUrl = form.baseUrl.trim();
  const fastModel = form.fastModel.trim();
  if (baseUrl) {
    next.base_url = baseUrl;
  } else {
    delete next.base_url;
  }
  if (fastModel) {
    next.fast_model = fastModel;
  } else {
    delete next.fast_model;
  }
  const contextWindow = Number.parseInt(form.contextWindow.trim(), 10);
  if (Number.isFinite(contextWindow) && contextWindow > 0) {
    next.context_window = contextWindow;
  } else {
    delete next.context_window;
  }
  return next;
}

function selectInitialProvider(): void {
  const providers = getProviders();
  const active = activeProviderId.value;
  const first = Object.keys(providers)[0] || "";
  selectedProviderId.value = active && providers[active] ? active : first;
  if (selectedProviderId.value) {
    if (!activeProviderId.value) {
      configData.value.active = selectedProviderId.value;
    }
    loadForm(selectedProviderId.value);
  } else {
    formProviderId.value = "";
    Object.assign(form, emptyForm());
  }
}

function loadForm(providerId: string): void {
  const provider = asRecord(getProviders()[providerId]) || {};
  resetModelOptions();
  formProviderId.value = providerId;
  Object.assign(form, {
    id: providerId,
    type: normalizeProviderType(provider.type),
    toolProtocol: normalizeToolProtocol(provider.tool_protocol),
    display: asString(provider.display) || "",
    apiKey: asString(provider.api_key) || "",
    baseUrl: asString(provider.base_url) || "",
    model: asString(provider.model) || "",
    fastModel: asString(provider.fast_model) || "",
    contextWindow: provider.context_window ? String(provider.context_window) : ""
  });
}

function getProviders(): Record<string, unknown> {
  const providers = asRecord(configData.value.providers);
  if (providers) {
    return providers;
  }
  const next: Record<string, unknown> = {};
  configData.value.providers = next;
  return next;
}

function normalizeConfig(content: string): Record<string, unknown> {
  try {
    const parsed = asRecord(JSON.parse(content || "{}"));
    if (!parsed) {
      return emptyConfig();
    }
    if (!asRecord(parsed.providers)) {
      parsed.providers = {};
    }
    if (typeof parsed.version !== "number") {
      parsed.version = 1;
    }
    if (typeof parsed.active !== "string") {
      parsed.active = "";
    }
    return parsed;
  } catch {
    return emptyConfig();
  }
}

function emptyConfig(): Record<string, unknown> {
  return { version: 1, active: "", providers: {} };
}

function emptyForm(): ProviderForm {
  return {
    id: "",
    type: "generic",
    toolProtocol: "auto",
    display: "",
    apiKey: "",
    baseUrl: "",
    model: "",
    fastModel: "",
    contextWindow: ""
  };
}

function nextProviderId(providers: Record<string, unknown>): string {
  let index = 1;
  let id = "new-provider";
  while (Object.prototype.hasOwnProperty.call(providers, id)) {
    index += 1;
    id = `new-provider-${index}`;
  }
  return id;
}

function normalizeProviderId(value: string): string {
  return value.trim().replace(/\s+/g, "-");
}

function normalizeProviderType(value: unknown): ProviderType {
  const normalized = String(value || "generic").trim().toLowerCase();
  if (normalized === "openai" || normalized === "anthropic") {
    return normalized;
  }
  return "generic";
}

function normalizeToolProtocol(value: unknown): ToolProtocol {
  const normalized = String(value || "auto").trim().toLowerCase().replace(/-/g, "_");
  if (
    normalized === "native" ||
    normalized === "structured" ||
    normalized === "mimo" ||
    normalized === "disabled"
  ) {
    return normalized;
  }
  return "auto";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}
</script>

<style scoped>
.coomi-config-shell {
  position: relative;
  width: 100%;
  min-height: 100%;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  overflow: hidden;
  background: transparent;
}

.coomi-config-header {
  min-height: 42px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 14px 0 20px;
  border-bottom: 1px solid var(--border-subtle);
}

.coomi-config-title {
  color: var(--text-main);
  font-size: 14px;
  font-weight: 700;
}

.coomi-config-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 30px;
  padding: 0 8px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.coomi-config-close:hover,
.coomi-config-action:hover,
.coomi-config-icon-action:hover {
  background: var(--bg-hover);
}

.coomi-config-error {
  padding: 10px 20px;
  border-bottom: 1px solid var(--border-subtle);
  background: rgb(127 29 29 / 0.14);
  color: #fca5a5;
  font-size: 12px;
}

.coomi-config-body {
  min-height: 0;
  overflow: auto;
  padding: 12px 20px 16px;
}

.coomi-config-toolbar,
.coomi-provider-picker-row,
.coomi-editor-head,
.coomi-config-footer-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.coomi-config-toolbar {
  margin-bottom: 12px;
}

.coomi-toolbar-actions {
  display: inline-flex;
  gap: 8px;
}

.coomi-section-title {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.coomi-section-title span {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 700;
}

.coomi-section-title small {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-provider-manager {
  display: grid;
  gap: 16px;
}

.coomi-provider-picker-row {
  align-items: end;
}

.coomi-provider-picker {
  flex: 1 1 auto;
}

.coomi-provider-picker-row .coomi-toolbar-actions {
  flex: 0 0 auto;
  padding-bottom: 1px;
}

.coomi-provider-editor {
  min-width: 0;
}

.coomi-editor-head {
  margin-bottom: 14px;
}

.coomi-editor-actions,
.coomi-config-actions {
  display: inline-flex;
  gap: 8px;
}

.coomi-provider-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 10px;
  row-gap: 12px;
}

.coomi-model-fetch-row {
  grid-column: 1 / -1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}

.coomi-model-fetch-message {
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-config-field {
  display: grid;
  gap: 6px;
  min-width: 0;
  color: var(--text-muted);
  font-size: 12px;
}

.coomi-config-field.full {
  grid-column: 1 / -1;
}

.coomi-config-field input,
.coomi-config-field select {
  width: 100%;
  min-width: 0;
  height: 40px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  padding: 0 11px;
  background: var(--bg-input);
  color: var(--text-main);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.2;
  outline: none;
}

.coomi-config-field input:focus,
.coomi-config-field select:focus {
  border-color: var(--accent);
}

.coomi-config-field input:disabled,
.coomi-config-field select:disabled {
  opacity: 0.62;
}

.coomi-config-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 38px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
  color: var(--text-main);
  cursor: pointer;
}

.coomi-config-action {
  gap: 6px;
  padding: 0 10px;
  white-space: nowrap;
  font-size: 12px;
}

.coomi-config-icon-action {
  width: 38px;
  height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
  color: var(--text-main);
  cursor: pointer;
}

.coomi-config-action.danger {
  color: var(--danger);
}

.coomi-config-icon-action.danger {
  color: var(--danger);
}

.coomi-config-action.primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
}

.coomi-config-action:disabled,
.coomi-config-icon-action:disabled {
  cursor: default;
  opacity: 0.55;
}

.coomi-config-empty {
  min-height: 180px;
  display: grid;
  place-items: center;
  gap: 14px;
  color: var(--text-muted);
  font-size: 13px;
}

.coomi-config-footer {
  display: grid;
  gap: 10px;
  padding: 10px 20px 12px;
  border-top: 1px solid var(--border-subtle);
}

.coomi-config-path {
  display: grid;
  gap: 4px;
  min-width: 0;
  color: var(--text-muted);
  font-size: 11px;
}

.coomi-config-path code {
  min-width: 0;
  overflow: hidden;
  color: var(--text-soft);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-config-meta {
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 620px) {
  .coomi-provider-picker-row {
    align-items: stretch;
    flex-direction: column;
  }

  .coomi-model-fetch-row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
