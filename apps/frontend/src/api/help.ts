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
