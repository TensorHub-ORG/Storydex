import { defineStore } from "pinia";
import {
  activatePreset,
  compilePreset,
  deactivatePreset,
  fetchActivePreset,
  fetchPresetDocument,
  listPresets,
  patchPresetParams,
  PresetApiError,
  riskCheckPreset,
  savePresetDocument
} from "@/api/presets";
import type {
  ActivePresetResponse,
  PresetCompileResult,
  PresetDocumentPayload,
  PresetListItem,
  PresetListResponse,
  PresetRuntimeOverridesPayload
} from "@/api/presets";
import { describeTransportError } from "@/api/client";
import { useWorkspaceStore } from "@/stores/workspace";

function emptyDocument(): PresetDocumentPayload {
  return {
    version: 1,
    meta: { name: "", description: "", compatibleProviders: [], updatedAt: "" },
    sampling: { default: {}, perPurpose: {} },
    lengthContract: {
      bodyMinChars: 1200,
      bodyTargetChars: 2400,
      bodyMaxChars: 3600,
      paragraphMin: 6,
      paragraphMax: 24,
      requiredTags: [],
      forbiddenTags: []
    },
    thinking: {
      enabled: false,
      mode: "stage_list",
      stages: [],
      injectPosition: "system_suffix",
      visibleInOutput: false
    },
    style: {
      pov: "",
      narrator: "",
      forbiddenWords: [],
      forbiddenPatterns: [],
      styleRules: [],
      maxConsecutiveRepeat: 2,
      proseRegister: "",
      authorReference: undefined,
      freeTextSlotPre: "",
      freeTextSlotPost: ""
    },
    modules: [],
    moduleProfiles: [],
    runtimeDefaults: {},
    riskPolicy: {},
    memory: {
      summaryFormat: "scene_outline",
      summaryMinChars: 240,
      summaryMaxChars: 600,
      bigSummaryTriggerChapters: 8
    },
    terms: { nameAliasMap: {}, termReplaceMap: {}, enforceAtGeneration: true },
    characterVoices: {}
  };
}

interface PresetStoreState {
  activeMainPreset: string;
  activeList: PresetListItem[];
  libraryList: PresetListItem[];
  currentName: string;
  document: PresetDocumentPayload;
  warnings: string[];
  loading: boolean;
  saving: boolean;
  dirty: boolean;
  errorMessage: string;
  compileResult: PresetCompileResult | null;
  compiling: boolean;
  compileError: string;
  runtimeOverrides: PresetRuntimeOverridesPayload;
}

export const usePresetStore = defineStore("preset", {
  state: (): PresetStoreState => ({
    activeMainPreset: "",
    activeList: [],
    libraryList: [],
    currentName: "",
    document: emptyDocument(),
    warnings: [],
    loading: false,
    saving: false,
    dirty: false,
    errorMessage: "",
    compileResult: null,
    compiling: false,
    compileError: "",
    runtimeOverrides: { enabledModuleIds: [], disabledModuleIds: [], temporaryRules: [] }
  }),
  actions: {
    async refreshList() {
      this.loading = true;
      this.errorMessage = "";
      try {
        const { data } = await listPresets();
        const payload = data as PresetListResponse;
        this.activeList = payload.active;
        this.libraryList = payload.library;
        this.activeMainPreset = payload.activeMainPreset;
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设列表加载失败。");
      } finally {
        this.loading = false;
      }
    },
    async loadActiveDocument() {
      this.loading = true;
      this.errorMessage = "";
      try {
        const { data } = await fetchActivePreset();
        const payload = data as ActivePresetResponse;
        this.activeMainPreset = payload.activeMainPreset;
        this.currentName = payload.activeMainPreset;
        this.document = payload.document;
        this.warnings = payload.warnings;
        this.dirty = false;
        this.compileResult = null;
        this.compileError = "";
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设加载失败。");
      } finally {
        this.loading = false;
      }
    },
    async loadDocument(name: string) {
      this.loading = true;
      this.errorMessage = "";
      this.currentName = name;
      try {
        const { data } = await fetchPresetDocument(name);
        this.document = data.document;
        this.warnings = data.warnings;
        this.dirty = false;
        this.compileResult = null;
        this.compileError = "";
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设加载失败。");
      } finally {
        this.loading = false;
      }
    },
    markDirty(next: PresetDocumentPayload) {
      this.document = next;
      this.dirty = true;
      this.compileResult = null;
      this.compileError = "";
    },
    setRuntimeOverrides(next: PresetRuntimeOverridesPayload) {
      this.runtimeOverrides = {
        enabledModuleIds: [...(next.enabledModuleIds || [])],
        disabledModuleIds: [...(next.disabledModuleIds || [])],
        temporaryRules: [...(next.temporaryRules || [])]
      };
      this.compileResult = null;
      this.compileError = "";
    },
    async compileCurrentPreset() {
      if (!this.currentName) {
        this.compileError = "未选择预设。";
        return;
      }
      this.compiling = true;
      this.compileError = "";
      try {
        const { data } = await compilePreset(this.currentName, {
          document: this.document,
          presetOverrides: this.runtimeOverrides
        });
        this.compileResult = data;
      } catch (error) {
        this.compileError = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设编译失败。");
      } finally {
        this.compiling = false;
      }
    },
    async riskCheckCurrentPreset() {
      if (!this.currentName) {
        this.compileError = "未选择预设。";
        return;
      }
      this.compiling = true;
      this.compileError = "";
      try {
        const { data } = await riskCheckPreset(this.currentName, {
          document: this.document,
          presetOverrides: this.runtimeOverrides
        });
        this.compileResult = data;
      } catch (error) {
        this.compileError = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设风险体检失败。");
      } finally {
        this.compiling = false;
      }
    },
    async save() {
      if (!this.currentName) {
        this.errorMessage = "未选择预设。";
        return;
      }
      this.saving = true;
      this.errorMessage = "";
      try {
        await savePresetDocument(this.currentName, this.document);
        const workspaceStore = useWorkspaceStore();
        if (workspaceStore.activeFileBindingOrPath === this.currentName && !workspaceStore.isDirty) {
          await workspaceStore.openFile(this.currentName, { forceReload: true });
        }
        this.dirty = false;
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设保存失败。");
      } finally {
        this.saving = false;
      }
    },
    async patchParams(patch: Record<string, unknown>) {
      if (!this.currentName) {
        return;
      }
      try {
        await patchPresetParams(this.currentName, patch);
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设部分更新失败。");
      }
    },
    async activate(name: string) {
      try {
        await activatePreset(name);
        await this.refreshList();
        await useWorkspaceStore().refreshTree();
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设激活失败。");
      }
    },
    async deactivate(name: string) {
      try {
        await deactivatePreset(name);
        await this.refreshList();
        await useWorkspaceStore().refreshTree();
      } catch (error) {
        this.errorMessage = error instanceof PresetApiError ? error.message : describeTransportError(error, "预设停用失败。");
      }
    }
  }
});
