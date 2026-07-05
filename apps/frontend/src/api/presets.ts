import { ApiResponseError, apiClient, unwrapEnvelope } from "@/api/client";
import type { ApiEnvelope, ApiResult } from "@/types/api";

export interface PresetListItem {
  name: string;
  path: string;
  hasSidecar: boolean;
}

export interface PresetListResponse {
  active: PresetListItem[];
  library: PresetListItem[];
  activeMainPreset: string;
}

export interface PresetModulePayload {
  id: string;
  title?: string;
  slot?: string;
  enabledByDefault?: boolean;
  priority?: number;
  scope?: string;
  content?: string;
  tags?: string[];
  virtual?: boolean;
  [key: string]: unknown;
}

export interface PresetRuntimeOverridesPayload {
  enabledModuleIds?: string[];
  disabledModuleIds?: string[];
  temporaryRules?: string[];
}

export interface PresetCompiledSection {
  id: string;
  title: string;
  slot: string;
  sourceModuleId: string;
  priority: number;
  enabled: boolean;
  scope: string;
  text: string;
  virtual?: boolean;
}

export interface PresetRisk {
  level: "error" | "warning" | "info";
  code: string;
  message: string;
  sourceModuleId?: string;
  line?: number | null;
}

export interface PresetCompileResult {
  relativePath: string;
  compiledText: string;
  sections: PresetCompiledSection[];
  risks: PresetRisk[];
  warnings: string[];
}

export interface PresetCompileRequest {
  document?: PresetDocumentPayload;
  presetOverrides?: PresetRuntimeOverridesPayload;
}

export interface SillyTavernPresetImportFilePayload {
  name: string;
  contentBase64: string;
}

export interface SillyTavernPresetImportItem {
  name: string;
  title: string;
  relativePath: string;
  sidecarPath: string;
  moduleCount: number;
  filteredCount: number;
  filteredBlocks: Array<{ name: string; identifier: string; reason: string }>;
  warnings: string[];
  importWarnings?: string[];
  displayRegexes?: Array<{
    scriptName: string;
    findRegex: string;
    replaceString: string;
    markdownOnly: boolean;
    id?: string;
  }>;
  chatSquashMeta?: Record<string, unknown>;
  modules?: Array<{
    id: string;
    title: string;
    slot: string;
    priority: number;
    enabledByDefault: boolean;
  }>;
  sampling?: SamplingParams;
}

export interface SillyTavernPresetImportResponse {
  items: SillyTavernPresetImportItem[];
}

export interface SamplingParams {
  temperature?: number | null;
  topP?: number | null;
  topK?: number | null;
  frequencyPenalty?: number | null;
  presencePenalty?: number | null;
  seed?: number | null;
  stop?: string[] | null;
}

export interface AuthorReferencePayload {
  primary?: string;
  borrow?: string[];
  doNotBorrow?: string[];
  secondary?: string[];
  notes?: string;
  [key: string]: unknown;
}

export interface PresetDocumentPayload {
  version: number;
  modules?: PresetModulePayload[];
  moduleProfiles?: Array<Record<string, unknown>>;
  runtimeDefaults?: Record<string, unknown>;
  riskPolicy?: Record<string, unknown>;
  meta: {
    name: string;
    description: string;
    compatibleProviders: string[];
    updatedAt: string;
    sourceFormat?: string;
    displayRegexes?: Array<Record<string, unknown>>;
    chatSquashMeta?: Record<string, unknown>;
    importWarnings?: string[];
  };
  sampling: {
    default: SamplingParams;
    perPurpose: Record<string, SamplingParams>;
  };
  lengthContract: {
    bodyMinChars: number;
    bodyTargetChars: number;
    bodyMaxChars: number;
    paragraphMin: number;
    paragraphMax: number;
    requiredTags: string[];
    forbiddenTags: string[];
  };
  thinking: {
    enabled: boolean;
    mode: string;
    stages: string[];
    injectPosition: string;
    visibleInOutput: boolean;
  };
  style: {
    pov: string;
    narrator: string;
    forbiddenWords: string[];
    forbiddenPatterns: string[];
    styleRules: string[];
    maxConsecutiveRepeat: number;
    proseRegister?: string;
    authorReference?: AuthorReferencePayload;
    freeTextSlotPre?: string;
    freeTextSlotPost?: string;
    [key: string]: unknown;
  };
  memory: {
    summaryFormat: string;
    summaryMinChars: number;
    summaryMaxChars: number;
    bigSummaryTriggerChapters: number;
  };
  terms: {
    nameAliasMap: Record<string, string>;
    termReplaceMap: Record<string, string>;
    enforceAtGeneration: boolean;
  };
  characterVoices: Record<string, { tone: string; signatureActions: string[]; taboo: string[] }>;
}

export interface ActivePresetResponse {
  activeMainPreset: string;
  document: PresetDocumentPayload;
  warnings: string[];
}

export interface PresetDocumentResponse {
  relativePath: string;
  document: PresetDocumentPayload;
  warnings: string[];
}

export class PresetApiError extends ApiResponseError {}

function unwrap<T>(envelope: ApiEnvelope<T>, fallback: string): ApiResult<T> {
  try {
    return unwrapEnvelope(envelope, fallback);
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new PresetApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

function encodePresetPath(name: string): string {
  return String(name || "").split("/").map((part) => encodeURIComponent(part)).join("/");
}

export async function listPresets(): Promise<ApiResult<PresetListResponse>> {
  const response = await apiClient.get<ApiEnvelope<PresetListResponse>>("/presets/list");
  return unwrap(response.data, "Preset list request failed.");
}

export async function fetchPresetSchema(): Promise<ApiResult<{ schema: Record<string, unknown>; version: number }>> {
  const response = await apiClient.get<ApiEnvelope<{ schema: Record<string, unknown>; version: number }>>("/presets/_schema");
  return unwrap(response.data, "Preset schema request failed.");
}

export async function importSillyTavernPresets(
  payload: { files: SillyTavernPresetImportFilePayload[] }
): Promise<ApiResult<SillyTavernPresetImportResponse>> {
  const response = await apiClient.post<ApiEnvelope<SillyTavernPresetImportResponse>>(
    "/presets/import/sillytavern",
    payload
  );
  return unwrap(response.data, "SillyTavern preset import failed.");
}

export async function previewSillyTavernImport(
  payload: { files: SillyTavernPresetImportFilePayload[] }
): Promise<ApiResult<SillyTavernPresetImportResponse>> {
  const response = await apiClient.post<ApiEnvelope<SillyTavernPresetImportResponse>>(
    "/presets/import/preview",
    payload
  );
  return unwrap(response.data, "SillyTavern preset preview failed.");
}

export async function fetchActivePreset(): Promise<ApiResult<ActivePresetResponse>> {
  const response = await apiClient.get<ApiEnvelope<ActivePresetResponse>>("/presets/active");
  return unwrap(response.data, "Active preset request failed.");
}

export async function fetchPresetDocument(name: string): Promise<ApiResult<PresetDocumentResponse>> {
  const response = await apiClient.get<ApiEnvelope<PresetDocumentResponse>>(
    `/presets/${encodePresetPath(name)}/document`
  );
  return unwrap(response.data, `Preset document ${name} request failed.`);
}

export async function compilePreset(
  name: string,
  payload: PresetCompileRequest = {}
): Promise<ApiResult<PresetCompileResult>> {
  const response = await apiClient.post<ApiEnvelope<PresetCompileResult>>(
    `/presets/${encodePresetPath(name)}/compile`,
    payload
  );
  return unwrap(response.data, `Preset compile ${name} failed.`);
}

export async function riskCheckPreset(
  name: string,
  payload: PresetCompileRequest = {}
): Promise<ApiResult<PresetCompileResult>> {
  const response = await apiClient.post<ApiEnvelope<PresetCompileResult>>(
    `/presets/${encodePresetPath(name)}/risk-check`,
    payload
  );
  return unwrap(response.data, `Preset risk check ${name} failed.`);
}

export async function savePresetDocument(
  name: string,
  document: PresetDocumentPayload
): Promise<ApiResult<{ relativePath: string; ok: boolean }>> {
  const response = await apiClient.put<ApiEnvelope<{ relativePath: string; ok: boolean }>>(
    `/presets/${encodePresetPath(name)}/document`,
    document
  );
  return unwrap(response.data, `Preset save ${name} failed.`);
}

export async function patchPresetParams(
  name: string,
  patch: Record<string, unknown>
): Promise<ApiResult<{ relativePath: string; ok: boolean }>> {
  const response = await apiClient.patch<ApiEnvelope<{ relativePath: string; ok: boolean }>>(
    `/presets/${encodePresetPath(name)}/params`,
    patch
  );
  return unwrap(response.data, `Preset patch ${name} failed.`);
}

export async function activatePreset(name: string): Promise<ApiResult<Record<string, unknown>>> {
  const response = await apiClient.post<ApiEnvelope<Record<string, unknown>>>(
    `/presets/${encodePresetPath(name)}/activate`
  );
  return unwrap(response.data, `Preset activate ${name} failed.`);
}

export async function deactivatePreset(name: string): Promise<ApiResult<Record<string, unknown>>> {
  const response = await apiClient.post<ApiEnvelope<Record<string, unknown>>>(
    `/presets/${encodePresetPath(name)}/deactivate`
  );
  return unwrap(response.data, `Preset deactivate ${name} failed.`);
}
