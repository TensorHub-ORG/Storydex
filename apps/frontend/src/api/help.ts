import { ApiResponseError, apiClient, unwrapEnvelope } from "@/api/client";
import type { ApiEnvelope, ApiResult } from "@/types/api";

export interface HelpGuideItem {
  id: string;
  title: string;
  relativePath: string;
  content: string;
  updatedAt: string;
}

export interface HelpGuideResponse {
  root: string;
  items: HelpGuideItem[];
  content: string;
}

export interface PromptRepositoryCategory {
  id: string;
  label: string;
  count: number;
}

export interface PromptRepositoryItem {
  id: string;
  title: string;
  summary: string;
  category: string;
  relativePath: string;
  content: string;
  promptText: string;
  placeholders: string[];
  updatedAt: string;
  isCustom: boolean;
}

export interface PromptRepositoryResponse {
  root: string;
  query: string;
  category: string;
  categories: PromptRepositoryCategory[];
  items: PromptRepositoryItem[];
}

export interface CustomPromptResponse {
  item: PromptRepositoryItem;
}

export class HelpApiError extends ApiResponseError {}

export async function fetchHelpGuide(): Promise<ApiResult<HelpGuideResponse>> {
  const response = await apiClient.get<ApiEnvelope<HelpGuideResponse>>("/help/guide");
  try {
    return unwrapEnvelope(response.data, "Help guide request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new HelpApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchPromptRepository(params: { q?: string; category?: string } = {}): Promise<ApiResult<PromptRepositoryResponse>> {
  const response = await apiClient.get<ApiEnvelope<PromptRepositoryResponse>>("/help/prompts", {
    params: {
      q: params.q?.trim() || undefined,
      category: params.category?.trim() || undefined
    }
  });
  try {
    return unwrapEnvelope(response.data, "Prompt repository request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new HelpApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function createCustomPrompt(payload: { title: string; promptText: string }): Promise<ApiResult<CustomPromptResponse>> {
  const response = await apiClient.post<ApiEnvelope<CustomPromptResponse>>("/help/prompts/custom", payload);
  return unwrapEnvelope(response.data, "Custom prompt creation failed.");
}

export async function updateCustomPrompt(id: string, promptText: string): Promise<ApiResult<CustomPromptResponse>> {
  const promptId = String(id || "").replace(/^custom\//, "");
  const response = await apiClient.put<ApiEnvelope<CustomPromptResponse>>(
    `/help/prompts/custom/${encodeURIComponent(promptId)}`,
    { promptText }
  );
  return unwrapEnvelope(response.data, "Custom prompt update failed.");
}
