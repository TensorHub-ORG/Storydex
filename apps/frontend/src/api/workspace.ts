import { ApiResponseError, apiClient, unwrapEnvelope } from "@/api/client";
import type { ApiEnvelope, ApiResult } from "@/types/api";
import type {
  StoryChapterListResponse,
  StoryChapterTemplateListResponse,
  WorkspaceCreateDirectoryRequest,
  WorkspaceCreateFileRequest,
  WorkspaceDiagnosticsRequest,
  WorkspaceDiagnosticsResponse,
  WorkspaceDiagnosticFixRequest,
  WorkspaceDiagnosticFixResponse,
  WorkspaceDeleteRequest,
  StoryCurrentStateResponse,
  WorkspaceGitCommitRequest,
  WorkspaceGitCommitResponse,
  WorkspaceGitDiffResponse,
  WorkspaceGitRestoreRequest,
  WorkspaceGitRestoreResponse,
  WorkspaceGitSummaryResponse,
  WorkspaceImportFilesRequest,
  WorkspaceImportFilesResponse,
  StoryLatestSnapshotResponse,
  WorkspaceFileDocument,
  WorkspaceFileReadRequest,
  WorkspaceFileWindowRequest,
  WorkspaceFileWindowResponse,
  WorkspaceFileWriteRequest,
  WorkspacePathInfo,
  WorkspaceProjectInfo,
  WorkspaceProjectPathRequest,
  WorkspaceRenameRequest,
  StoryChapterCompletionRequest,
  StoryProjectSettingsResponse,
  StoryProjectSettingsUpdateRequest,
  WorkspaceTransferRequest,
  WorkspaceTreeResponse
} from "@/types/workspace";

export class WorkspaceApiError extends ApiResponseError {}

export async function fetchWorkspaceTree(): Promise<ApiResult<WorkspaceTreeResponse>> {
  const response = await apiClient.get<ApiEnvelope<WorkspaceTreeResponse>>("/workspace/tree");
  try {
    return unwrapEnvelope(response.data, "Workspace tree request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchCurrentProject(): Promise<ApiResult<WorkspaceProjectInfo>> {
  const response = await apiClient.get<ApiEnvelope<WorkspaceProjectInfo>>("/workspace/project");
  try {
    return unwrapEnvelope(response.data, "Project status request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function openWorkspaceProject(
  payload: WorkspaceProjectPathRequest
): Promise<ApiResult<WorkspaceProjectInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceProjectInfo>>("/workspace/project/open", payload);
  try {
    return unwrapEnvelope(response.data, "Open project request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function createWorkspaceProject(
  payload: WorkspaceProjectPathRequest
): Promise<ApiResult<WorkspaceProjectInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceProjectInfo>>("/workspace/project/create", payload);
  try {
    return unwrapEnvelope(response.data, "Create project request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function initializeWorkspaceProject(
  payload?: WorkspaceProjectPathRequest
): Promise<ApiResult<WorkspaceProjectInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceProjectInfo>>(
    "/workspace/project/initialize",
    payload
  );
  try {
    return unwrapEnvelope(response.data, "Initialize project request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function readWorkspaceFile(
  payload: WorkspaceFileReadRequest
): Promise<ApiResult<WorkspaceFileDocument>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceFileDocument>>("/file/read", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace file read failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function writeWorkspaceFile(
  payload: WorkspaceFileWriteRequest
): Promise<ApiResult<WorkspaceFileDocument>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceFileDocument>>("/file/write", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace file write failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function createWorkspaceFile(
  payload: WorkspaceCreateFileRequest
): Promise<ApiResult<WorkspaceFileDocument>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceFileDocument>>("/workspace/file/create", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace file create failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function createWorkspaceDirectory(
  payload: WorkspaceCreateDirectoryRequest
): Promise<ApiResult<WorkspacePathInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspacePathInfo>>("/workspace/directory/create", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace directory create failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function importWorkspaceFiles(
  payload: WorkspaceImportFilesRequest
): Promise<ApiResult<WorkspaceImportFilesResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceImportFilesResponse>>("/workspace/files/import", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace file import failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function renameWorkspacePath(
  payload: WorkspaceRenameRequest
): Promise<ApiResult<WorkspacePathInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspacePathInfo>>("/workspace/path/rename", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace rename failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function deleteWorkspacePath(
  payload: WorkspaceDeleteRequest
): Promise<ApiResult<WorkspacePathInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspacePathInfo>>("/workspace/path/delete", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace delete failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function copyWorkspacePath(
  payload: WorkspaceTransferRequest
): Promise<ApiResult<WorkspacePathInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspacePathInfo>>("/workspace/path/copy", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace copy failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function moveWorkspacePath(
  payload: WorkspaceTransferRequest
): Promise<ApiResult<WorkspacePathInfo>> {
  const response = await apiClient.post<ApiEnvelope<WorkspacePathInfo>>("/workspace/path/move", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace move failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchWorkspaceDiagnostics(
  payload: WorkspaceDiagnosticsRequest
): Promise<ApiResult<WorkspaceDiagnosticsResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceDiagnosticsResponse>>("/workspace/diagnostics", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace diagnostics request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchWorkspaceGitSummary(): Promise<ApiResult<WorkspaceGitSummaryResponse>> {
  const response = await apiClient.get<ApiEnvelope<WorkspaceGitSummaryResponse>>("/workspace/git/summary");
  try {
    return unwrapEnvelope(response.data, "Workspace Git summary request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function applyWorkspaceDiagnosticFix(
  payload: WorkspaceDiagnosticFixRequest
): Promise<ApiResult<WorkspaceDiagnosticFixResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceDiagnosticFixResponse>>("/workspace/diagnostics/fix", payload);
  return unwrapEnvelope(response.data, "Workspace diagnostic fix failed.");
}

export async function readWorkspaceFileWindow(
  payload: WorkspaceFileWindowRequest,
  signal?: AbortSignal
): Promise<ApiResult<WorkspaceFileWindowResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceFileWindowResponse>>("/file/window", payload, { signal });
  return unwrapEnvelope(response.data, "Workspace file window read failed.");
}

export async function fetchWorkspaceGitDiff(): Promise<ApiResult<WorkspaceGitDiffResponse>> {
  const response = await apiClient.get<ApiEnvelope<WorkspaceGitDiffResponse>>("/workspace/git/diff");
  try {
    return unwrapEnvelope(response.data, "Workspace Git diff request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function initializeWorkspaceGitRepository(): Promise<ApiResult<WorkspaceGitSummaryResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceGitSummaryResponse>>("/workspace/git/init");
  try {
    return unwrapEnvelope(response.data, "Initialize workspace Git repository failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function commitWorkspaceGitChanges(
  payload: WorkspaceGitCommitRequest
): Promise<ApiResult<WorkspaceGitCommitResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceGitCommitResponse>>("/workspace/git/commit", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace Git commit failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function restoreWorkspaceGitCommit(
  payload: WorkspaceGitRestoreRequest
): Promise<ApiResult<WorkspaceGitRestoreResponse>> {
  const response = await apiClient.post<ApiEnvelope<WorkspaceGitRestoreResponse>>("/workspace/git/restore", payload);
  try {
    return unwrapEnvelope(response.data, "Workspace Git restore failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchStoryChapters(): Promise<ApiResult<StoryChapterListResponse>> {
  const response = await apiClient.get<ApiEnvelope<StoryChapterListResponse>>("/story/chapters");
  try {
    return unwrapEnvelope(response.data, "Story chapters request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchStoryChapterTemplates(): Promise<ApiResult<StoryChapterTemplateListResponse>> {
  const paths = ["/workspace/story/templates/chapters", "/story/templates/chapters"];
  for (const path of paths) {
    const result = await requestStoryChapterTemplates(path);
    if (result) {
      return result;
    }
  }
  return emptyStoryChapterTemplatesResult();
}

async function requestStoryChapterTemplates(
  path: string
): Promise<ApiResult<StoryChapterTemplateListResponse> | null> {
  const response = await apiClient.get<ApiEnvelope<StoryChapterTemplateListResponse>>(path, {
    validateStatus: (status) => (status >= 200 && status < 300) || status === 404
  });
  if (response.status === 404) {
    return null;
  }
  try {
    return unwrapEnvelope(response.data, "Story chapter templates request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

function emptyStoryChapterTemplatesResult(): ApiResult<StoryChapterTemplateListResponse> {
  return { data: { items: [] }, trace: null, audit: [] };
}

export async function fetchStoryCurrentState(): Promise<ApiResult<StoryCurrentStateResponse>> {
  const response = await apiClient.get<ApiEnvelope<StoryCurrentStateResponse>>("/story/current-state");
  try {
    return unwrapEnvelope(response.data, "Story current state request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchStoryLatestSnapshot(): Promise<ApiResult<StoryLatestSnapshotResponse>> {
  const response = await apiClient.get<ApiEnvelope<StoryLatestSnapshotResponse>>("/story/snapshots/latest");
  try {
    return unwrapEnvelope(response.data, "Story latest snapshot request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchStoryProjectSettings(): Promise<ApiResult<StoryProjectSettingsResponse>> {
  const response = await apiClient.get<ApiEnvelope<StoryProjectSettingsResponse>>("/workspace/story/settings");
  try {
    return unwrapEnvelope(response.data, "Story settings request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function updateStoryProjectSettings(
  payload: StoryProjectSettingsUpdateRequest
): Promise<ApiResult<StoryProjectSettingsResponse>> {
  const response = await apiClient.put<ApiEnvelope<StoryProjectSettingsResponse>>("/workspace/story/settings", payload);
  try {
    return unwrapEnvelope(response.data, "Story settings update failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function updateStoryChapterCompletion(
  payload: StoryChapterCompletionRequest
): Promise<ApiResult<StoryProjectSettingsResponse>> {
  const response = await apiClient.put<ApiEnvelope<StoryProjectSettingsResponse>>(
    "/workspace/story/chapters/completion",
    payload
  );
  try {
    return unwrapEnvelope(response.data, "Chapter completion update failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new WorkspaceApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

