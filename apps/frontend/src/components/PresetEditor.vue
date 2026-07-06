<template>
  <section class="preset-editor">
    <header class="preset-editor-header">
      <div>
        <h2 class="preset-editor-title">{{ document.meta.name || "预设参数" }}</h2>
        <p class="preset-editor-subtitle">{{ relativePath || "未选择预设" }}</p>
      </div>
      <div class="preset-editor-actions">
        <button class="preset-editor-btn" type="button" :disabled="!presetStore.dirty || presetStore.saving" @click="handleSave">
          {{ presetStore.saving ? "保存中…" : "保存" }}
        </button>
      </div>
    </header>

    <p v-if="presetStore.errorMessage" class="preset-editor-error">{{ presetStore.errorMessage }}</p>
    <p v-if="presetStore.warnings.length" class="preset-editor-warning">{{ presetStore.warnings.join("；") }}</p>
    <p v-if="presetStore.compileError" class="preset-editor-error">{{ presetStore.compileError }}</p>

    <nav class="preset-editor-tabs" aria-label="预设编辑模式">
      <button
        v-for="mode in editorModes"
        :key="mode.id"
        class="preset-editor-tab"
        type="button"
        :class="{ active: activeMode === mode.id }"
        @click="selectMode(mode.id)"
      >
        {{ mode.label }}
      </button>
    </nav>

    <div v-if="activeMode === 'structured'" class="preset-editor-mode-panel">
    <details class="preset-editor-section" open>
      <summary>采样参数（default）</summary>
      <div class="preset-editor-grid">
        <label class="preset-editor-field">
          <span>temperature ({{ formatNumber(document.sampling.default.temperature) }})</span>
          <input
            type="range"
            min="0"
            max="2"
            step="0.05"
            :value="document.sampling.default.temperature ?? 1"
            @input="onSamplingChange('temperature', Number(($event.target as HTMLInputElement).value))"
          />
        </label>
        <label class="preset-editor-field">
          <span>top_p ({{ formatNumber(document.sampling.default.topP) }})</span>
          <input
            type="range"
            min="0"
            max="1"
            step="0.01"
            :value="document.sampling.default.topP ?? 0.98"
            @input="onSamplingChange('topP', Number(($event.target as HTMLInputElement).value))"
          />
        </label>
        <label class="preset-editor-field">
          <span>top_k ({{ document.sampling.default.topK ?? '—' }}, 仅 Anthropic)</span>
          <input
            type="number"
            min="1"
            max="1024"
            :value="document.sampling.default.topK ?? ''"
            placeholder="留空=不传"
            @input="onSamplingChange('topK', parseOptionalInt(($event.target as HTMLInputElement).value))"
          />
        </label>
        <label class="preset-editor-field">
          <span>frequency_penalty ({{ formatNumber(document.sampling.default.frequencyPenalty) }}, OpenAI 兼容)</span>
          <input
            type="range"
            min="-2"
            max="2"
            step="0.05"
            :value="document.sampling.default.frequencyPenalty ?? 0"
            @input="onSamplingChange('frequencyPenalty', Number(($event.target as HTMLInputElement).value))"
          />
        </label>
        <label class="preset-editor-field">
          <span>presence_penalty ({{ formatNumber(document.sampling.default.presencePenalty) }}, OpenAI 兼容)</span>
          <input
            type="range"
            min="-2"
            max="2"
            step="0.05"
            :value="document.sampling.default.presencePenalty ?? 0"
            @input="onSamplingChange('presencePenalty', Number(($event.target as HTMLInputElement).value))"
          />
        </label>
        <label class="preset-editor-field">
          <span>seed (留空=随机)</span>
          <input
            type="number"
            :value="document.sampling.default.seed ?? ''"
            placeholder="OpenAI 兼容"
            @input="onSamplingChange('seed', parseOptionalInt(($event.target as HTMLInputElement).value))"
          />
        </label>
      </div>
    </details>

    <details class="preset-editor-section">
      <summary>长度合同</summary>
      <div class="preset-editor-grid">
        <label class="preset-editor-field">
          <span>正文最少字数</span>
          <input type="number" v-model.number="document.lengthContract.bodyMinChars" @change="markDirty" />
        </label>
        <label class="preset-editor-field">
          <span>正文目标字数</span>
          <input type="number" v-model.number="document.lengthContract.bodyTargetChars" @change="markDirty" />
        </label>
        <label class="preset-editor-field">
          <span>正文最多字数</span>
          <input type="number" v-model.number="document.lengthContract.bodyMaxChars" @change="markDirty" />
        </label>
        <label class="preset-editor-field">
          <span>段落最少</span>
          <input type="number" v-model.number="document.lengthContract.paragraphMin" @change="markDirty" />
        </label>
        <label class="preset-editor-field">
          <span>段落最多</span>
          <input type="number" v-model.number="document.lengthContract.paragraphMax" @change="markDirty" />
        </label>
      </div>
    </details>

    <details class="preset-editor-section">
      <summary>思维链（阶段化清单）</summary>
      <label class="preset-editor-field-inline">
        <input type="checkbox" v-model="document.thinking.enabled" @change="markDirty" />
        <span>启用阶段化清单（system_suffix）</span>
      </label>
      <textarea
        class="preset-editor-textarea"
        rows="6"
        :value="document.thinking.stages.join('\n')"
        placeholder="每行一个阶段，如：场景定位：本节发生地点、视角、目标"
        @input="onStagesChange(($event.target as HTMLTextAreaElement).value)"
      />
    </details>

    <details class="preset-editor-section">
      <summary>文风与禁词</summary>
      <label class="preset-editor-field">
        <span>POV（视角说明）</span>
        <input type="text" v-model="document.style.pov" @change="markDirty" placeholder="如：第三人称限知" />
      </label>
      <label class="preset-editor-field">
        <span>视角主角 ID</span>
        <input type="text" v-model="document.style.narrator" @change="markDirty" placeholder="如：chen_siqi" />
      </label>
      <label class="preset-editor-field">
        <span>禁词（逗号分隔）</span>
        <textarea
          class="preset-editor-textarea"
          rows="3"
          :value="document.style.forbiddenWords.join('，')"
          @input="onWordsChange('forbiddenWords', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>禁式正则（每行一条）</span>
        <textarea
          class="preset-editor-textarea"
          rows="3"
          :value="document.style.forbiddenPatterns.join('\n')"
          @input="onLinesChange('forbiddenPatterns', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>风格规则（每行一条）</span>
        <textarea
          class="preset-editor-textarea"
          rows="5"
          :value="document.style.styleRules.join('\n')"
          @input="onLinesChange('styleRules', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
    </details>

    <details class="preset-editor-section">
      <summary>术语硬替换 + 名归一化</summary>
      <p class="preset-editor-hint">每行格式 旧→新 或 旧=新；空行忽略。</p>
      <label class="preset-editor-field">
        <span>术语替换</span>
        <textarea
          class="preset-editor-textarea"
          rows="4"
          :value="kvToText(document.terms.termReplaceMap)"
          @input="onKvChange('termReplaceMap', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>名字归一化</span>
        <textarea
          class="preset-editor-textarea"
          rows="3"
          :value="kvToText(document.terms.nameAliasMap)"
          @input="onKvChange('nameAliasMap', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
    </details>

    <details class="preset-editor-section">
      <summary>整体定位 / prose_register</summary>
      <p class="preset-editor-hint">一句话定位文风方向，会渲染到 stage1 prompt 的【整体定位】段。</p>
      <textarea
        class="preset-editor-textarea"
        rows="4"
        :value="document.style.proseRegister || ''"
        placeholder="例：参考江南《龙族》节奏，耐心铺陈，明喻不忌讳，道具有质感"
        @input="onProseRegisterChange(($event.target as HTMLTextAreaElement).value)"
      />
    </details>

    <details class="preset-editor-section">
      <summary>参考作家 / author_reference</summary>
      <p class="preset-editor-hint">告诉模型借鉴谁、剔除谁。会渲染到 stage1 prompt 的【参考作家】段。</p>
      <label class="preset-editor-field">
        <span>主参考</span>
        <input
          type="text"
          :value="authorRef.primary"
          placeholder="例：江南 — 《龙族》系列"
          @input="onAuthorRefChange('primary', ($event.target as HTMLInputElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>借鉴（每行一条）</span>
        <textarea
          class="preset-editor-textarea"
          rows="4"
          :value="(authorRef.borrow || []).join('\n')"
          @input="onAuthorRefListChange('borrow', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>剔除（每行一条）</span>
        <textarea
          class="preset-editor-textarea"
          rows="3"
          :value="(authorRef.doNotBorrow || []).join('\n')"
          @input="onAuthorRefListChange('doNotBorrow', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>辅参考（每行一条）</span>
        <textarea
          class="preset-editor-textarea"
          rows="2"
          :value="(authorRef.secondary || []).join('\n')"
          @input="onAuthorRefListChange('secondary', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
    </details>

    <details class="preset-editor-section">
      <summary>自由文本插槽 · 顶/底 (free_text_slot)</summary>
      <p class="preset-editor-hint">不走结构化字段的自由文本，会直接插入硬约束块的最顶/最底。适合临时加一条规则或一段引言。</p>
      <label class="preset-editor-field">
        <span>顶置槽 (free_text_slot_pre)</span>
        <textarea
          class="preset-editor-textarea"
          rows="3"
          :value="document.style.freeTextSlotPre || ''"
          placeholder="放在硬约束块最顶，权重最高"
          @input="onFreeSlotChange('freeTextSlotPre', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
      <label class="preset-editor-field">
        <span>底置槽 (free_text_slot_post)</span>
        <textarea
          class="preset-editor-textarea"
          rows="3"
          :value="document.style.freeTextSlotPost || ''"
          placeholder="放在硬约束块最底"
          @input="onFreeSlotChange('freeTextSlotPost', ($event.target as HTMLTextAreaElement).value)"
        />
      </label>
    </details>

    <details v-if="hasImportMetadata" class="preset-editor-section preset-editor-import-meta">
      <summary>导入元数据</summary>
      <div class="preset-import-meta-grid">
        <p v-if="importSourceFormat" class="preset-import-meta-row">
          <span class="preset-import-meta-label">来源格式:</span>
          <span class="preset-import-meta-value">{{ importSourceFormat }}</span>
        </p>
        <details v-if="importWarnings.length" class="preset-import-meta-subdetails">
          <summary>导入提示 ({{ importWarnings.length }})</summary>
          <ul class="preset-import-meta-warning-list">
            <li v-for="(warning, index) in importWarnings.slice(0, 30)" :key="index" class="preset-import-meta-warning-item">
              ⚠ {{ warning }}
            </li>
            <li v-if="importWarnings.length > 30" class="preset-import-meta-warning-more">
              …还有 {{ importWarnings.length - 30 }} 条
            </li>
          </ul>
        </details>
        <details v-if="displayRegexes.length" class="preset-import-meta-subdetails">
          <summary>展示正则 ({{ displayRegexes.length }})</summary>
          <ul class="preset-import-meta-regex-list">
            <li v-for="(regex, index) in displayRegexes" :key="index" class="preset-import-meta-regex-item">
              <span class="preset-import-meta-regex-name">{{ regex.scriptName || `regex_${index + 1}` }}</span>
              <code class="preset-import-meta-regex-find">{{ String(regex.findRegex || "").slice(0, 80) }}</code>
            </li>
          </ul>
        </details>
        <details v-if="hasChatSquashMeta" class="preset-import-meta-subdetails">
          <summary>SPreset ChatSquash 元数据</summary>
          <div class="preset-import-meta-chatsquash">
            <p
              v-for="(value, key) in chatsquashDisplayFields"
              :key="key"
              class="preset-import-meta-chatsquash-row"
            >
              <span class="preset-import-meta-chatsquash-key">{{ key }}:</span>
              <span class="preset-import-meta-chatsquash-value">{{ truncateMeta(String(value), 100) }}</span>
            </p>
          </div>
        </details>
      </div>
    </details>

    <details v-if="presetModules.length" class="preset-editor-section" open>
      <summary>模块开关</summary>
      <div class="preset-module-toggle-list">
        <details
          v-for="(module, index) in presetModules"
          :key="module.id || index"
          class="preset-module-toggle-row"
        >
          <summary class="preset-module-toggle-summary">
            <span class="preset-module-toggle-chevron material-symbols-rounded">chevron_right</span>
            <span class="preset-module-toggle-main">
              <input
                type="checkbox"
                :checked="module.enabledByDefault !== false"
                @click.stop
                @change.stop="onModuleEnabledChange(index, ($event.target as HTMLInputElement).checked)"
              />
              <span>{{ module.title || module.id || `module_${index + 1}` }}</span>
            </span>
            <small>{{ moduleMetaLabel(module) }}</small>
          </summary>
          <label class="preset-module-content">
            <span>内容</span>
            <textarea
              class="preset-editor-textarea preset-module-content-textarea"
              rows="8"
              :value="module.content || ''"
              placeholder="此模块暂无内容。"
              @input="onModuleContentChange(index, ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
        </details>
      </div>
    </details>

    <details class="preset-editor-section">
      <summary>高级 · 原始 JSON（任意扩展字段）</summary>
      <p class="preset-editor-hint">直接编辑底层 JSON，可用于添加 schema 没暴露的自定义字段（schema 默认 extra="allow"）。保存时校验失败会回滚。</p>
      <textarea
        class="preset-editor-textarea preset-editor-json"
        rows="14"
        :value="rawJsonText"
        @input="onRawJsonChange(($event.target as HTMLTextAreaElement).value)"
      />
      <p v-if="rawJsonError" class="preset-editor-error">{{ rawJsonError }}</p>
    </details>
    </div>

    <div v-else-if="activeMode === 'workbench'" class="preset-editor-mode-panel">
      <section class="preset-editor-workbench-hero">
        <div>
          <h3>写作工作台</h3>
          <p>从同一个 compiler 读取模块顺序，重点检查哪些规则会进入本轮写作。</p>
        </div>
        <button class="preset-editor-btn" type="button" :disabled="presetStore.compiling" @click="compileForWorkbench">
          {{ presetStore.compiling ? "生成中…" : "刷新模块" }}
        </button>
      </section>

      <section class="preset-editor-override">
        <label class="preset-editor-field">
          <span>本轮临时规则（不保存到预设，每行一条）</span>
          <textarea
            class="preset-editor-textarea"
            rows="3"
            :value="temporaryRulesText"
            placeholder="例：本轮结尾落在具体动作，不新增悬念。"
            @input="onTemporaryRulesChange(($event.target as HTMLTextAreaElement).value)"
          />
        </label>
        <div v-if="disabledModuleIds.length" class="preset-editor-chip-row">
          <span class="preset-editor-chip-label">本轮禁用</span>
          <button
            v-for="moduleId in disabledModuleIds"
            :key="moduleId"
            class="preset-editor-chip"
            type="button"
            @click="toggleTurnDisabled(moduleId)"
          >
            {{ moduleId }} ×
          </button>
        </div>
      </section>

      <div v-if="!compiledSections.length" class="preset-editor-empty">
        暂无编译结果。点击“刷新模块”查看当前预设会注入哪些模块。
      </div>
      <div v-else class="preset-module-list">
        <article v-for="section in compiledSections" :key="section.id" class="preset-module-card">
          <header class="preset-module-card-header">
            <div>
              <h4>{{ section.title || section.sourceModuleId }}</h4>
              <p>{{ slotLabel(section.slot) }} · priority {{ section.priority }} · {{ scopeLabel(section.scope) }}</p>
            </div>
            <span class="preset-risk-pill" :class="riskClassForModule(section.sourceModuleId)">
              {{ riskTextForModule(section.sourceModuleId) }}
            </span>
          </header>
          <p class="preset-module-summary">{{ summarizeText(section.text) }}</p>
          <footer class="preset-module-card-footer">
            <span>{{ textLengthLabel(section.text) }}</span>
            <button class="preset-editor-ghost-btn" type="button" @click="toggleTurnDisabled(section.sourceModuleId)">
              本轮禁用
            </button>
          </footer>
        </article>
      </div>
    </div>

    <div v-else-if="activeMode === 'preview'" class="preset-editor-mode-panel">
      <section class="preset-editor-workbench-hero">
        <div>
          <h3>注入预览</h3>
          <p>这里展示的 compiled prompt 与 Stage-1 实际注入走同一个后端 compiler。</p>
        </div>
        <button class="preset-editor-btn" type="button" :disabled="presetStore.compiling" @click="presetStore.compileCurrentPreset">
          {{ presetStore.compiling ? "编译中…" : "刷新预览" }}
        </button>
      </section>
      <div v-if="compiledSections.length" class="preset-section-order">
        <span v-for="section in compiledSections" :key="section.id" class="preset-section-token">
          {{ slotLabel(section.slot) }}/{{ section.sourceModuleId }}
        </span>
      </div>
      <pre class="preset-compiled-preview">{{ compilePreviewText }}</pre>
    </div>

    <div v-else class="preset-editor-mode-panel">
      <section class="preset-editor-workbench-hero">
        <div>
          <h3>风险体检</h3>
          <p>静态检查可见思维链、破限、强制钩子、自动暗线和“伞里有东西”这类偏题风险。</p>
        </div>
        <button class="preset-editor-btn" type="button" :disabled="presetStore.compiling" @click="presetStore.riskCheckCurrentPreset">
          {{ presetStore.compiling ? "检查中…" : "重新体检" }}
        </button>
      </section>
      <div v-if="!allRisks.length" class="preset-editor-empty">
        {{ presetStore.compileResult ? "未发现静态风险。" : "点击“重新体检”查看当前预设风险。" }}
      </div>
      <div v-else class="preset-risk-groups">
        <section v-for="group in riskGroups" :key="group.level" class="preset-risk-group">
          <h4>{{ riskLevelLabel(group.level) }} · {{ group.items.length }}</h4>
          <article v-for="risk in group.items" :key="`${risk.sourceModuleId}-${risk.code}-${risk.line || 0}`" class="preset-risk-item">
            <span class="preset-risk-pill" :class="`risk-${risk.level}`">{{ risk.code }}</span>
            <p>{{ risk.message }}</p>
            <small>{{ risk.sourceModuleId || "document" }}{{ risk.line ? ` · line ${risk.line}` : "" }}</small>
          </article>
        </section>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { usePresetStore } from "@/stores/preset";
import type { PresetModulePayload, PresetRisk, SamplingParams } from "@/api/presets";

interface AuthorReferenceEditorValue {
  primary: string;
  borrow: string[];
  doNotBorrow: string[];
  secondary: string[];
}

const presetStore = usePresetStore();
const document = computed(() => presetStore.document);
const relativePath = computed(() => presetStore.activeMainPreset || presetStore.currentName);
const activeMode = ref<"structured" | "workbench" | "preview" | "risk">("structured");
const editorModes = [
  { id: "structured", label: "结构化编辑" },
  { id: "workbench", label: "写作工作台" },
  { id: "preview", label: "注入预览" },
  { id: "risk", label: "风险体检" }
] as const;
const compiledSections = computed(() => presetStore.compileResult?.sections || []);
const allRisks = computed(() => presetStore.compileResult?.risks || []);
const presetModules = computed<PresetModulePayload[]>(() => (Array.isArray(document.value.modules) ? document.value.modules : []));
const disabledModuleIds = computed(() => presetStore.runtimeOverrides.disabledModuleIds || []);
const temporaryRulesText = computed(() => (presetStore.runtimeOverrides.temporaryRules || []).join("\n"));
const compilePreviewText = computed(() => presetStore.compileResult?.compiledText || "暂无编译结果。点击“刷新预览”查看实际注入文本。");
const riskGroups = computed(() => {
  const order: Array<PresetRisk["level"]> = ["error", "warning", "info"];
  return order
    .map((level) => ({ level, items: allRisks.value.filter((risk) => risk.level === level) }))
    .filter((group) => group.items.length > 0);
});

onMounted(async () => {
  if (!presetStore.currentName) {
    await presetStore.loadActiveDocument();
  }
});

function markDirty(): void {
  presetStore.dirty = true;
  presetStore.compileResult = null;
  presetStore.compileError = "";
}

function onSamplingChange<K extends keyof SamplingParams>(key: K, value: SamplingParams[K]): void {
  const next = { ...document.value, sampling: { ...document.value.sampling, default: { ...document.value.sampling.default, [key]: value } } };
  presetStore.markDirty(next);
}

function onStagesChange(text: string): void {
  const stages = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const next = { ...document.value, thinking: { ...document.value.thinking, stages } };
  presetStore.markDirty(next);
}

function onWordsChange(field: "forbiddenWords", text: string): void {
  const items = text.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
  const next = { ...document.value, style: { ...document.value.style, [field]: items } };
  presetStore.markDirty(next);
}

function onLinesChange(field: "forbiddenPatterns" | "styleRules", text: string): void {
  const items = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const next = { ...document.value, style: { ...document.value.style, [field]: items } };
  presetStore.markDirty(next);
}

function onKvChange(field: "termReplaceMap" | "nameAliasMap", text: string): void {
  const map: Record<string, string> = {};
  for (const line of text.split(/\n+/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const match = trimmed.match(/^(.+?)\s*(?:→|=>|=)\s*(.+)$/u);
    if (match) {
      map[match[1].trim()] = match[2].trim();
    }
  }
  const next = { ...document.value, terms: { ...document.value.terms, [field]: map } };
  presetStore.markDirty(next);
}

function kvToText(map: Record<string, string>): string {
  return Object.entries(map).map(([k, v]) => `${k}→${v}`).join("\n");
}

function formatNumber(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(2);
}

function parseOptionalInt(text: string): number | null {
  const trimmed = String(text || "").trim();
  if (!trimmed) return null;
  const num = Number(trimmed);
  return Number.isFinite(num) ? Math.round(num) : null;
}

async function handleSave(): Promise<void> {
  await presetStore.save();
}

async function selectMode(mode: typeof editorModes[number]["id"]): Promise<void> {
  activeMode.value = mode;
  if (mode === "preview" && !presetStore.compileResult) {
    await presetStore.compileCurrentPreset();
  }
  if ((mode === "workbench" || mode === "risk") && !presetStore.compileResult) {
    await presetStore.riskCheckCurrentPreset();
  }
}

async function compileForWorkbench(): Promise<void> {
  await presetStore.riskCheckCurrentPreset();
}

function onTemporaryRulesChange(text: string): void {
  presetStore.setRuntimeOverrides({
    ...presetStore.runtimeOverrides,
    temporaryRules: text.split(/\n+/).map((line) => line.trim()).filter(Boolean)
  });
}

async function toggleTurnDisabled(moduleId: string): Promise<void> {
  const current = new Set(presetStore.runtimeOverrides.disabledModuleIds || []);
  if (current.has(moduleId)) {
    current.delete(moduleId);
  } else {
    current.add(moduleId);
  }
  presetStore.setRuntimeOverrides({
    ...presetStore.runtimeOverrides,
    disabledModuleIds: Array.from(current)
  });
  await presetStore.riskCheckCurrentPreset();
}

function slotLabel(slot: string): string {
  const labels: Record<string, string> = {
    boundary: "硬边界",
    author_reference: "参考作家",
    language_mechanics: "语言机制",
    scene_module: "场景模块",
    negative_rules: "禁用规则",
    self_check: "落笔前检查",
    advanced: "高级模块"
  };
  return labels[slot] || slot;
}

function moduleMetaLabel(module: PresetModulePayload): string {
  const parts: string[] = [slotLabel(module.slot || "advanced")];
  if (typeof module.priority === "number") {
    parts.push(`priority ${module.priority}`);
  }
  if (module.scope) {
    parts.push(scopeLabel(module.scope));
  }
  return parts.join(" · ");
}

function scopeLabel(scope: string): string {
  return scope === "turn" ? "本轮" : "全局";
}

function summarizeText(text: string): string {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  return normalized.length > 120 ? `${normalized.slice(0, 120)}…` : normalized;
}

function textLengthLabel(text: string): string {
  const length = String(text || "").trim().length;
  return length > 0 ? `${length} 字` : "无内容";
}

function riskTextForModule(moduleId: string): string {
  const risks = allRisks.value.filter((risk) => risk.sourceModuleId === moduleId);
  if (risks.some((risk) => risk.level === "error")) return "高风险";
  if (risks.some((risk) => risk.level === "warning")) return "有警告";
  if (risks.some((risk) => risk.level === "info")) return "提示";
  return "通过";
}

function riskClassForModule(moduleId: string): string {
  const risks = allRisks.value.filter((risk) => risk.sourceModuleId === moduleId);
  if (risks.some((risk) => risk.level === "error")) return "risk-error";
  if (risks.some((risk) => risk.level === "warning")) return "risk-warning";
  if (risks.some((risk) => risk.level === "info")) return "risk-info";
  return "risk-ok";
}

function riskLevelLabel(level: PresetRisk["level"]): string {
  if (level === "error") return "高风险";
  if (level === "warning") return "警告";
  return "提示";
}

// v1.3: 新槽位（author_reference / prose_register / free_text_slot_pre/post / 原始 JSON）

const authorRef = computed<AuthorReferenceEditorValue>(() => {
  const raw = (document.value.style as Record<string, unknown>).authorReference;
  if (raw && typeof raw === "object") {
    const record = raw as Record<string, unknown>;
    return {
      primary: typeof record.primary === "string" ? record.primary : "",
      borrow: Array.isArray(record.borrow) ? record.borrow.filter((item): item is string => typeof item === "string") : [],
      doNotBorrow: Array.isArray(record.doNotBorrow) ? record.doNotBorrow.filter((item): item is string => typeof item === "string") : [],
      secondary: Array.isArray(record.secondary) ? record.secondary.filter((item): item is string => typeof item === "string") : []
    };
  }
  return {
    primary: "",
    borrow: [],
    doNotBorrow: [],
    secondary: []
  };
});

// 导入元数据（SillyTavern 适配）
const importSourceFormat = computed(() => document.value.meta?.sourceFormat || "");
const importWarnings = computed<string[]>(() => document.value.meta?.importWarnings || []);
const displayRegexes = computed<Array<Record<string, unknown>>>(() => document.value.meta?.displayRegexes || []);
const chatSquashMeta = computed<Record<string, unknown>>(() => document.value.meta?.chatSquashMeta || {});
const hasChatSquashMeta = computed(() => Object.keys(chatSquashMeta.value).length > 0);
const hasImportMetadata = computed(
  () => Boolean(importSourceFormat.value) || importWarnings.value.length > 0 || displayRegexes.value.length > 0 || hasChatSquashMeta.value
);

const chatsquashDisplayFields = computed<Record<string, unknown>>(() => {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(chatSquashMeta.value)) {
    if (key === "squashed_post_script") {
      result[key] = `[JavaScript ${String(value).length} 字符，未执行]`;
    } else if (value !== null && value !== undefined && value !== "") {
      result[key] = value;
    }
  }
  return result;
});

function truncateMeta(text: string, max: number): string {
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function onProseRegisterChange(text: string): void {
  const next = {
    ...document.value,
    style: { ...document.value.style, proseRegister: text } as typeof document.value.style
  };
  presetStore.markDirty(next);
}

function onAuthorRefChange(field: string, value: string | string[]): void {
  const existing = (document.value.style as Record<string, unknown>).authorReference as Record<string, unknown> | undefined;
  const merged: Record<string, unknown> = { ...(existing || {}), [field]: value };
  const next = {
    ...document.value,
    style: { ...document.value.style, authorReference: merged } as typeof document.value.style
  };
  presetStore.markDirty(next);
}

function onAuthorRefListChange(field: "borrow" | "doNotBorrow" | "secondary", text: string): void {
  const items = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  onAuthorRefChange(field, items);
}

function onFreeSlotChange(field: "freeTextSlotPre" | "freeTextSlotPost", text: string): void {
  const next = {
    ...document.value,
    style: { ...document.value.style, [field]: text } as typeof document.value.style
  };
  presetStore.markDirty(next);
}

function onModuleEnabledChange(index: number, enabled: boolean): void {
  const modules = [...presetModules.value];
  const existing = modules[index];
  if (!existing) {
    return;
  }
  modules[index] = { ...existing, enabledByDefault: enabled };
  presetStore.markDirty({ ...document.value, modules });
}

function onModuleContentChange(index: number, content: string): void {
  const modules = [...presetModules.value];
  const existing = modules[index];
  if (!existing) {
    return;
  }
  modules[index] = { ...existing, content };
  presetStore.markDirty({ ...document.value, modules });
}

// 原始 JSON 编辑器
const rawJsonText = computed(() => JSON.stringify(document.value, null, 2));
const rawJsonError = ref("");

function onRawJsonChange(text: string): void {
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      rawJsonError.value = "";
      presetStore.markDirty(parsed);
    } else {
      rawJsonError.value = "顶层必须是 JSON 对象";
    }
  } catch (err) {
    rawJsonError.value = `JSON 解析错误: ${err instanceof Error ? err.message : String(err)}`;
  }
}
</script>

<style scoped>
.preset-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: var(--bg-sidebar);
  color: var(--text-main);
  height: 100%;
  overflow-y: auto;
}

.preset-editor-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-right: 40px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-ghost);
}

.preset-editor-title {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
}

.preset-editor-subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}

.preset-editor-actions {
  display: inline-flex;
  gap: 8px;
}

.preset-editor-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  padding: 4px;
  background: var(--bg-elevated, #fff);
  border: 1px solid var(--border-ghost);
  border-radius: 8px;
}

.preset-editor-tab {
  min-width: 0;
  padding: 7px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.preset-editor-tab:hover,
.preset-editor-tab:focus-visible {
  background: var(--bg-hover);
  color: var(--text-main);
  outline: none;
}

.preset-editor-tab.active {
  background: var(--accent-strong);
  color: var(--text-on-accent, #fff);
}

.preset-editor-mode-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preset-editor-btn {
  padding: 6px 16px;
  background: var(--accent-strong);
  color: var(--text-on-accent, #fff);
  border: 0;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.preset-editor-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.preset-editor-ghost-btn {
  padding: 4px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
}

.preset-editor-ghost-btn:hover {
  background: var(--bg-hover);
  color: var(--accent-strong);
}

.preset-editor-error {
  margin: 0;
  padding: 8px 12px;
  background: var(--bg-elevated, #fef2f2);
  color: var(--state-danger, #b91c1c);
  border-radius: 6px;
  font-size: 12px;
}

.preset-editor-warning {
  margin: 0;
  padding: 8px 12px;
  background: var(--bg-elevated, #fffbeb);
  color: var(--state-warning, #b45309);
  border-radius: 6px;
  font-size: 12px;
}

.preset-editor-section {
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 8px 12px;
}

.preset-editor-section summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-main);
}

.preset-editor-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 8px;
}

.preset-editor-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-muted);
}

.preset-editor-field input[type="number"],
.preset-editor-field input[type="text"] {
  padding: 4px 8px;
  background: var(--bg-elevated, #fff);
  color: var(--text-main);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  font-size: 12px;
}

.preset-editor-field input[type="range"] {
  accent-color: var(--accent, #2563eb);
}

.preset-editor-field-inline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 12px;
}

.preset-editor-textarea {
  width: 100%;
  padding: 6px 8px;
  background: var(--bg-elevated, #fff);
  color: var(--text-main);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  font-size: 12px;
  resize: vertical;
}

.preset-editor-hint {
  margin: 4px 0 8px;
  font-size: 11px;
  color: var(--text-faint);
}

.preset-module-toggle-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}

.preset-module-toggle-row {
  min-width: 0;
  padding: 7px 8px;
  border: 1px solid var(--border-ghost);
  border-radius: 6px;
  background: var(--bg-elevated, #fff);
}

.preset-module-toggle-row[open] {
  border-color: var(--border-subtle);
}

.preset-module-toggle-summary {
  min-width: 0;
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) auto;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  list-style: none;
}

.preset-module-toggle-summary::-webkit-details-marker {
  display: none;
}

.preset-module-toggle-chevron {
  color: var(--text-faint);
  font-size: 16px;
  transition: transform 0.12s ease;
}

.preset-module-toggle-row[open] .preset-module-toggle-chevron {
  transform: rotate(90deg);
}

.preset-module-toggle-main {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text-main);
  font-size: 12px;
  font-weight: 600;
}

.preset-module-toggle-main span,
.preset-module-toggle-row small {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-module-toggle-row small {
  color: var(--text-muted);
  font-size: 11px;
}

.preset-module-content {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-ghost);
  color: var(--text-muted);
  font-size: 11px;
}

.preset-module-content-textarea {
  min-height: 112px;
  max-height: 280px;
}

.preset-editor-workbench-hero,
.preset-editor-override,
.preset-module-card,
.preset-risk-group {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--bg-elevated, #fff);
}

.preset-editor-workbench-hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
}

.preset-editor-workbench-hero h3 {
  margin: 0;
  font-size: 13px;
}

.preset-editor-workbench-hero p {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.preset-editor-override {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
}

.preset-editor-chip-row,
.preset-section-order {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.preset-editor-chip-label {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
}

.preset-editor-chip,
.preset-section-token {
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--bg-sidebar);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
}

.preset-editor-chip {
  padding: 3px 8px;
  cursor: pointer;
}

.preset-section-token {
  padding: 4px 8px;
}

.preset-editor-empty {
  padding: 16px 12px;
  border: 1px dashed var(--border-subtle);
  border-radius: 8px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.preset-module-list,
.preset-risk-groups {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.preset-module-card {
  padding: 12px;
}

.preset-module-card-header,
.preset-module-card-footer {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.preset-module-card-header h4,
.preset-risk-group h4 {
  margin: 0;
  color: var(--text-main);
  font-size: 13px;
}

.preset-module-card-header p,
.preset-module-summary,
.preset-module-card-footer,
.preset-risk-item small {
  color: var(--text-muted);
  font-size: 11px;
}

.preset-module-card-header p,
.preset-module-summary {
  margin: 4px 0 0;
  line-height: 1.6;
}

.preset-module-card-footer {
  align-items: center;
  margin-top: 10px;
}

.preset-risk-pill {
  flex: 0 0 auto;
  padding: 3px 7px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.risk-ok {
  background: rgba(22, 163, 74, 0.12);
  color: var(--state-success, #15803d);
}

.risk-info {
  background: rgba(37, 99, 235, 0.12);
  color: var(--accent-strong, #1d4ed8);
}

.risk-warning {
  background: rgba(217, 119, 6, 0.14);
  color: var(--state-warning, #b45309);
}

.risk-error {
  background: rgba(220, 38, 38, 0.14);
  color: var(--state-danger, #b91c1c);
}

.preset-compiled-preview {
  min-height: 360px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--bg-elevated, #fff);
  color: var(--text-main);
  font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  line-height: 1.7;
  white-space: pre-wrap;
}

.preset-risk-group {
  padding: 12px;
}

.preset-risk-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 8px;
  padding: 10px 0;
  border-top: 1px solid var(--border-ghost);
}

.preset-risk-item:first-of-type {
  margin-top: 8px;
}

.preset-risk-item p {
  margin: 0;
  color: var(--text-main);
  font-size: 12px;
  line-height: 1.6;
}

.preset-risk-item small {
  grid-column: 2;
}

.preset-editor-json {
  font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  white-space: pre;
  overflow-x: auto;
}

@media (max-width: 720px) {
  .preset-editor-tabs {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .preset-editor-workbench-hero,
  .preset-module-card-header,
  .preset-module-card-footer {
    flex-direction: column;
    align-items: stretch;
  }
}

/* 导入元数据区 */
.preset-editor-import-meta {
  border-color: var(--border-ghost);
}

.preset-import-meta-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.preset-import-meta-row {
  display: flex;
  gap: 8px;
  margin: 0;
  font-size: 12px;
}

.preset-import-meta-label {
  flex: 0 0 auto;
  color: var(--text-muted);
}

.preset-import-meta-value {
  color: var(--text-soft);
}

.preset-import-meta-subdetails {
  border-top: 1px solid var(--border-ghost);
  padding-top: 6px;
}

.preset-import-meta-subdetails summary {
  cursor: pointer;
  font-size: 11px;
  color: var(--text-soft);
  user-select: none;
}

.preset-import-meta-subdetails summary:hover {
  color: var(--text-main);
}

.preset-import-meta-warning-list,
.preset-import-meta-regex-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.preset-import-meta-warning-item {
  font-size: 11px;
  color: var(--state-warning);
  font-family: var(--font-mono);
  word-break: break-all;
}

.preset-import-meta-warning-more {
  font-size: 11px;
  color: var(--text-muted);
  font-style: italic;
}

.preset-import-meta-regex-item {
  display: flex;
  gap: 8px;
  font-size: 11px;
  align-items: center;
}

.preset-import-meta-regex-name {
  flex: 0 0 auto;
  color: var(--text-main);
}

.preset-import-meta-regex-find {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-import-meta-chatsquash {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-top: 6px;
}

.preset-import-meta-chatsquash-row {
  display: flex;
  gap: 8px;
  font-size: 11px;
  margin: 0;
}

.preset-import-meta-chatsquash-key {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.preset-import-meta-chatsquash-value {
  color: var(--text-soft);
  word-break: break-all;
}
</style>
