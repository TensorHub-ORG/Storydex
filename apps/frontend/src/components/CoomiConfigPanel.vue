<template>
  <section
    v-if="visible"
    class="llm-cfg"
    @keydown.ctrl.s.prevent="() => saveConfig()"
    @keydown.meta.s.prevent="() => saveConfig()"
  >
    <header class="llm-cfg__topbar">
      <div class="llm-cfg__heading">
        <span class="material-symbols-rounded llm-cfg__heading-icon">tune</span>
        <span class="llm-cfg__title">模型配置</span>
      </div>
      <button class="llm-cfg__close" type="button" title="退出" @click="$emit('close')">
        <span class="material-symbols-rounded">close</span>
      </button>
    </header>

    <div v-if="errorMessage" class="llm-cfg__error">
      <span class="material-symbols-rounded">error</span>
      <span>{{ errorMessage }}</span>
    </div>

    <main class="llm-cfg__body">
      <template v-if="hasProviders">
        <!-- 提供方选择 -->
        <div class="llm-picker">
          <div class="llm-select-wrap llm-picker__select">
            <select
              class="llm-input"
              :value="selectedProviderId"
              :disabled="loading || saving"
              @change="handleProviderSelect"
            >
              <option v-for="provider in providerOptions" :key="provider.id" :value="provider.id">
                {{ provider.id }} · {{ provider.display }}{{ provider.isActive ? "（当前）" : "" }}
              </option>
            </select>
            <span class="material-symbols-rounded llm-select-caret">expand_more</span>
          </div>
          <button
            class="llm-icon-btn"
            type="button"
            title="新建提供方"
            :disabled="loading || saving"
            @click="createProvider"
          >
            <span class="material-symbols-rounded">add</span>
          </button>
          <button
            class="llm-icon-btn llm-icon-btn--danger"
            type="button"
            title="删除提供方"
            :disabled="loading || saving || providerOptions.length <= 1"
            @click="deleteProvider"
          >
            <span class="material-symbols-rounded">delete</span>
          </button>
        </div>

        <!-- 基础信息 -->
        <section class="llm-section">
          <h3 class="llm-section__title">基础信息</h3>
          <div class="llm-fields">
            <div class="llm-row">
              <label class="llm-field">
                <span class="llm-field__label">类型</span>
                <div class="llm-select-wrap">
                  <select class="llm-input" v-model="form.type" :disabled="loading || saving" @change="syncProviderFields">
                    <option value="generic">generic</option>
                    <option value="openai">openai</option>
                    <option value="anthropic">anthropic</option>
                  </select>
                  <span class="material-symbols-rounded llm-select-caret">expand_more</span>
                </div>
              </label>

              <label class="llm-field">
                <span class="llm-field__label">工具协议</span>
                <div class="llm-select-wrap">
                  <select class="llm-input" v-model="form.toolProtocol" :disabled="loading || saving" @change="syncProviderFields">
                    <option value="auto">auto</option>
                    <option value="native">native</option>
                    <option value="structured">structured</option>
                    <option value="mimo">mimo</option>
                    <option value="disabled">disabled</option>
                  </select>
                  <span class="material-symbols-rounded llm-select-caret">expand_more</span>
                </div>
              </label>
            </div>

            <label class="llm-field">
              <span class="llm-field__label">提供方 ID</span>
              <input
                v-model="form.id"
                class="llm-input"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="deepseek"
                @input="markDirty"
              />
            </label>

            <label class="llm-field">
              <span class="llm-field__label">显示名称</span>
              <input
                v-model="form.display"
                class="llm-input"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="DeepSeek V4"
                @input="syncProviderFields"
              />
            </label>
          </div>
        </section>

        <!-- 连接 -->
        <section class="llm-section">
          <h3 class="llm-section__title">连接</h3>
          <div class="llm-fields">
            <label class="llm-field">
              <span class="llm-field__label">接口地址</span>
              <input
                v-model="form.baseUrl"
                class="llm-input"
                :disabled="loading || saving"
                spellcheck="false"
                placeholder="https://api.example.com/v1"
                @input="handleProviderConnectionInput"
              />
            </label>

            <label class="llm-field">
              <span class="llm-field__label">API 密钥</span>
              <input
                v-model="form.apiKey"
                class="llm-input"
                type="password"
                :disabled="loading || saving"
                spellcheck="false"
                autocomplete="off"
                placeholder="sk-..."
                @input="handleProviderConnectionInput"
              />
            </label>

            <div class="llm-fetch">
              <button
                class="llm-btn llm-btn--ghost"
                type="button"
                :disabled="modelFetchDisabled"
                @click="fetchModels"
              >
                <span class="material-symbols-rounded" :class="{ 'is-spin': fetchingModels }">
                  {{ fetchingModels ? "progress_activity" : "cloud_download" }}
                </span>
                <span>{{ fetchingModels ? "获取中" : "获取模型" }}</span>
              </button>
              <span v-if="modelFetchMessage" class="llm-fetch__msg">{{ modelFetchMessage }}</span>
            </div>
          </div>
        </section>

        <!-- 模型 -->
        <section class="llm-section">
          <h3 class="llm-section__title">模型</h3>
          <div class="llm-fields">
            <label class="llm-field">
              <span class="llm-field__label">标准模型</span>
              <input
                v-model="form.model"
                class="llm-input"
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

            <label class="llm-field">
              <span class="llm-field__label">快速模型</span>
              <input
                v-model="form.fastModel"
                class="llm-input"
                :disabled="loading || saving"
                list="coomi-fast-model-options"
                spellcheck="false"
                placeholder="留空跟随标准模型"
                @input="syncProviderFields"
              />
              <datalist id="coomi-fast-model-options">
                <option v-for="model in modelOptions" :key="model" :value="model"></option>
              </datalist>
              <span class="llm-field__hint">用于摘要、命名等轻量任务，留空则复用标准模型。</span>
            </label>

            <label class="llm-field">
              <span class="llm-field__label">上下文窗口（tokens）</span>
              <input
                v-model="form.contextWindow"
                class="llm-input"
                :disabled="loading || saving"
                spellcheck="false"
                inputmode="numeric"
                placeholder="留空使用默认 256000"
                @input="syncProviderFields"
              />
              <span class="llm-field__hint">按模型实际窗口填写，压缩阈值随之生效。</span>
            </label>
          </div>
        </section>
      </template>

      <section v-else class="llm-cfg__empty">
        <span class="material-symbols-rounded llm-cfg__empty-icon">cloud_off</span>
        <p class="llm-cfg__empty-copy">providers.json 里还没有提供方。</p>
        <button class="llm-btn llm-btn--primary" type="button" :disabled="loading || saving" @click="createProvider">
          <span class="material-symbols-rounded">add</span>
          <span>新建提供方</span>
        </button>
      </section>
    </main>

    <footer class="llm-cfg__footer">
      <div class="llm-cfg__path" :title="configPath">
        <span class="material-symbols-rounded">description</span>
        <code>{{ configPath }}</code>
      </div>
      <div class="llm-cfg__footer-row">
        <span class="llm-cfg__status" :class="{ 'is-dirty': dirty, 'is-busy': loading || saving }">
          <span class="llm-cfg__status-dot"></span>
          {{ updatedLabel }}
        </span>
        <div class="llm-cfg__actions">
          <button
            class="llm-btn llm-btn--ghost"
            type="button"
            title="只写入配置文件，不切换当前使用的提供方"
            :disabled="loading || saving"
            @click="() => saveConfig()"
          >
            <span class="material-symbols-rounded">save</span>
            <span>保存</span>
          </button>
          <button
            class="llm-btn llm-btn--primary"
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
.llm-cfg {
  --llm-pad-x: 16px;
  --llm-field-h: 32px;

  position: relative;
  width: 100%;
  min-height: 100%;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  overflow: hidden;
  background: transparent;
  color: var(--text-main);
}

/* ── 顶栏 ─────────────────────────────── */
.llm-cfg__topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 40px;
  padding: 0 8px 0 var(--llm-pad-x);
  border-bottom: 1px solid var(--border-subtle);
}

.llm-cfg__heading {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.llm-cfg__heading-icon {
  font-size: 17px;
  color: var(--text-muted);
}

.llm-cfg__title {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.3;
}

.llm-cfg__close {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: background 120ms ease, color 120ms ease;
}

.llm-cfg__close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.llm-cfg__close .material-symbols-rounded {
  font-size: 18px;
}

/* ── 错误提示 ─────────────────────────── */
.llm-cfg__error {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 10px var(--llm-pad-x) 0;
  padding: 8px 10px;
  border-left: 2px solid var(--danger);
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger);
  font-size: 12px;
  line-height: 1.5;
}

.llm-cfg__error .material-symbols-rounded {
  font-size: 15px;
  margin-top: 1px;
  flex-shrink: 0;
}

/* ── 主体 ─────────────────────────────── */
.llm-cfg__body {
  min-height: 0;
  overflow: auto;
  padding: 4px 0 16px;
}

/* ── 提供方选择 ───────────────────────── */
.llm-picker {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 12px var(--llm-pad-x);
}

.llm-picker__select {
  flex: 1 1 auto;
  min-width: 0;
}

/* ── 扁平分区 ─────────────────────────── */
.llm-section {
  padding: 14px var(--llm-pad-x);
  border-top: 1px solid var(--border-subtle);
}

.llm-section__title {
  margin: 0 0 12px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.llm-fields {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.llm-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

/* ── 字段 ─────────────────────────────── */
.llm-field {
  display: grid;
  gap: 5px;
  min-width: 0;
}

.llm-field__label {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.3;
}

.llm-field__hint {
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.5;
}

.llm-input {
  width: 100%;
  min-width: 0;
  height: var(--llm-field-h);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 0 10px;
  background: var(--bg-input);
  color: var(--text-main);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.2;
  outline: none;
  transition: border-color 120ms ease, box-shadow 120ms ease;
}

.llm-input::placeholder {
  color: var(--text-faint);
}

.llm-input:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.llm-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent) inset;
}

.llm-input:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* select 自定义箭头 */
.llm-select-wrap {
  position: relative;
  display: block;
}

.llm-select-wrap select.llm-input {
  appearance: none;
  -webkit-appearance: none;
  padding-right: 30px;
  cursor: pointer;
}

.llm-select-caret {
  position: absolute;
  top: 50%;
  right: 7px;
  transform: translateY(-50%);
  pointer-events: none;
  font-size: 18px;
  color: var(--text-muted);
}

/* ── 获取模型行 ───────────────────────── */
.llm-fetch {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.llm-fetch__msg {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.4;
}

/* ── 按钮 ─────────────────────────────── */
.llm-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: var(--llm-field-h);
  padding: 0 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  color: var(--text-main);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
}

.llm-btn .material-symbols-rounded {
  font-size: 16px;
}

.llm-btn--ghost:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.llm-btn--primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
}

.llm-btn--primary:hover:not(:disabled) {
  background: var(--accent-strong);
}

.llm-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

/* 图标按钮 */
.llm-icon-btn {
  flex-shrink: 0;
  width: var(--llm-field-h);
  height: var(--llm-field-h);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  color: var(--text-soft);
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
}

.llm-icon-btn .material-symbols-rounded {
  font-size: 18px;
}

.llm-icon-btn:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--bg-hover);
  color: var(--text-main);
}

.llm-icon-btn--danger:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  color: var(--danger);
}

.llm-icon-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.is-spin {
  animation: llm-spin 0.9s linear infinite;
}

@keyframes llm-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ── 空状态 ───────────────────────────── */
.llm-cfg__empty {
  min-height: 220px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 24px;
  color: var(--text-muted);
  text-align: center;
}

.llm-cfg__empty-icon {
  font-size: 38px;
  color: var(--text-faint);
}

.llm-cfg__empty-copy {
  margin: 0;
  font-size: 13px;
}

/* ── 底栏 ─────────────────────────────── */
.llm-cfg__footer {
  display: grid;
  gap: 10px;
  padding: 10px var(--llm-pad-x) 12px;
  border-top: 1px solid var(--border-subtle);
}

.llm-cfg__path {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--text-muted);
  font-size: 11px;
}

.llm-cfg__path .material-symbols-rounded {
  font-size: 14px;
  flex-shrink: 0;
}

.llm-cfg__path code {
  min-width: 0;
  overflow: hidden;
  color: var(--text-soft);
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
  direction: rtl;
  text-align: left;
}

.llm-cfg__footer-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.llm-cfg__status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.llm-cfg__status-dot {
  width: 6px;
  height: 6px;
  flex-shrink: 0;
  border-radius: 999px;
  background: var(--success);
}

.llm-cfg__status.is-dirty {
  color: var(--warning);
}

.llm-cfg__status.is-dirty .llm-cfg__status-dot {
  background: var(--warning);
}

.llm-cfg__status.is-busy .llm-cfg__status-dot {
  background: var(--info);
  animation: llm-pulse 1.1s ease-in-out infinite;
}

@keyframes llm-pulse {
  0%, 100% {
    opacity: 0.4;
  }
  50% {
    opacity: 1;
  }
}

.llm-cfg__actions {
  display: inline-flex;
  gap: 8px;
  flex-shrink: 0;
}

/* ── 窄栏适配 ─────────────────────────── */
@media (max-width: 360px) {
  .llm-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .llm-cfg__footer-row {
    flex-direction: column;
    align-items: stretch;
  }

  .llm-cfg__actions {
    justify-content: flex-end;
  }
}
</style>
