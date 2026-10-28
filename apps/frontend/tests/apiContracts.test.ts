import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiClient,
  ApiResponseError,
  describeTransportError,
  getApiAuthToken,
  getRuntimeAuthToken,
  setApiAuthToken,
  unwrapEnvelope
} from "@/api/client";
import * as agentApi from "@/api/agent";
import * as authApi from "@/api/auth";
import * as helpApi from "@/api/help";
import * as presetApi from "@/api/presets";
import * as systemApi from "@/api/system";
import * as workspaceApi from "@/api/workspace";

const success = { data: { ok: true, data: { items: [] }, trace: null, audit: [] } };

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(apiClient, "get").mockResolvedValue(success as never);
  vi.spyOn(apiClient, "post").mockResolvedValue(success as never);
  vi.spyOn(apiClient, "put").mockResolvedValue(success as never);
  vi.spyOn(apiClient, "patch").mockResolvedValue(success as never);
  vi.spyOn(apiClient, "delete").mockResolvedValue(success as never);
});

async function callExportedApiFunctions(module: Record<string, unknown>, excluded: string[] = []) {
  const calls: Promise<unknown>[] = [];
  for (const [name, value] of Object.entries(module)) {
    if (excluded.includes(name) || typeof value !== "function" || /^[A-Z]/.test(name)) continue;
    calls.push(Promise.resolve((value as (...args: unknown[]) => unknown)(
      { path: "chapters/001.md", name: "demo", files: [], mode: "skip" },
      { path: "chapters/001.md", content: "demo", files: [] },
      "session-test"
    )));
  }
  return Promise.all(calls);
}

describe("API envelope and transport contracts", () => {
  it("sends explicit workspace identity for settings reads and both write endpoints", async () => {
    const workspaceRoot = "C:/project A";
    await workspaceApi.fetchStoryProjectSettings(workspaceRoot);
    expect(apiClient.get).toHaveBeenLastCalledWith("/workspace/story/settings", { params: { workspaceRoot } });
    await workspaceApi.updateStoryProjectSettings({ workspaceRoot, chapterLengthTier: "long" } as any);
    expect(apiClient.put).toHaveBeenLastCalledWith("/workspace/story/settings", { workspaceRoot, chapterLengthTier: "long" });
    await workspaceApi.updateStoryChapterCompletion({ workspaceRoot, chapterPath: "chapters/one", completed: true });
    expect(apiClient.put).toHaveBeenLastCalledWith("/workspace/story/chapters/completion", { workspaceRoot, chapterPath: "chapters/one", completed: true });
  });
  it("distinguishes server errors, timeouts and unreadable responses", () => {
    const error = { isAxiosError: true, message: "Network Error" };
    expect(describeTransportError({ ...error, response: { status: 500, data: "error" } }, "fallback")).toContain("HTTP 500");
    expect(describeTransportError({ ...error, request: { status: 503 } }, "fallback")).toContain("HTTP 503");
    expect(describeTransportError({ ...error, response: { status: 500, data: { error: { message: "specific failure" } } } }, "fallback")).toBe("specific failure");
    expect(describeTransportError({ ...error, code: "ECONNABORTED" }, "fallback")).toContain("超时");
    expect(describeTransportError(error, "fallback")).toContain("未收到可读取的后端响应");
    expect(describeTransportError(error, "fallback")).not.toContain("启动");
  });

  it("rejects oversized UTF-8 drafts before sending without truncating them", async () => {
    const content = "文".repeat(3 * 1024 * 1024);
    await expect(workspaceApi.writeWorkspaceFile({ relativePath: "large.md", content, expectedContentSha256: "v1" })).rejects.toThrow("8 MiB");
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it("covers successful calls for every non-stream API surface", async () => {
    const results = await Promise.all([
      callExportedApiFunctions(agentApi, ["streamAgentPrompt"]),
      callExportedApiFunctions(authApi),
      callExportedApiFunctions(helpApi),
      callExportedApiFunctions(presetApi),
      callExportedApiFunctions(systemApi, ["validateSystemHealthRuntime"]),
      callExportedApiFunctions(workspaceApi)
    ]);
    expect(results.flat().length).toBeGreaterThan(60);
    expect(apiClient.get).toHaveBeenCalled();
    expect(apiClient.post).toHaveBeenCalled();
    expect(apiClient.put).toHaveBeenCalled();
    expect(apiClient.patch).toHaveBeenCalled();
  });

  it("preserves auth tokens and unwraps successful and failed envelopes", () => {
    setApiAuthToken(" token ");
    expect(getApiAuthToken()).toBe("token");
    window.storydexDesktop = {
      platform: "win32",
      backendAuthToken: " runtime-token ",
      versions: { tauri: "2.0.5" }
    };
    expect(getRuntimeAuthToken()).toBe("runtime-token");
    delete window.storydexDesktop;
    expect(unwrapEnvelope({ ok: true, data: { value: 1 }, trace: null, audit: [] }, "fallback").data).toEqual({ value: 1 });
    expect(() => unwrapEnvelope({ ok: false, data: null, error: { message: "bad", code: "bad_code", details: { field: "x" } }, trace: null, audit: [] }, "fallback"))
      .toThrowError(ApiResponseError);
    expect(() => unwrapEnvelope({ ok: true, data: null, trace: null, audit: [] }, "fallback")).toThrow("fallback");
    expect(describeTransportError(new Error("plain"), "fallback")).toBe("plain");
    expect(describeTransportError("bad", "fallback")).toBe("fallback");
  });

  it("maps failed envelopes to each domain error type", async () => {
    vi.spyOn(apiClient, "get").mockResolvedValue({ data: { ok: false, data: null, error: { message: "denied", code: "denied" }, trace: null, audit: [] } } as never);
    await expect(agentApi.fetchAgentSessions()).rejects.toBeInstanceOf(agentApi.AgentApiError);
    await expect(authApi.fetchCurrentAccount()).rejects.toBeInstanceOf(authApi.AuthApiError);
    await expect(helpApi.fetchHelpGuide()).rejects.toBeInstanceOf(helpApi.HelpApiError);
    await expect(presetApi.listPresets()).rejects.toBeInstanceOf(presetApi.PresetApiError);
    await expect(systemApi.fetchSystemHealth()).rejects.toBeInstanceOf(systemApi.SystemApiError);
    await expect(workspaceApi.fetchWorkspaceTree()).rejects.toBeInstanceOf(workspaceApi.WorkspaceApiError);
  });

  it("accepts the Rust sidecar health identity and rejects the legacy Python shape in Tauri", () => {
    const health = {
      status: "ok",
      service: "Storydex Backend",
      time: "2026-09-24T00:00:00Z",
      runtime: "storydex-agentd",
      version: "2.1.0-storydex-desktop.1",
      protocolVersion: 1,
      workspaceRoot: "",
      storydexRoot: "",
      projectName: "",
      hasStorydexConfig: false,
      requiresInitialization: false,
      missingDirectories: [],
      frontendStaticMode: false
    };
    window.storydexDesktop = {
      platform: "win32",
      backendRuntime: "storydex-agentd",
      backendRuntimeVersion: "2.1.0-storydex-desktop.1",
      versions: { tauri: "2.0.9" }
    };

    expect(() => systemApi.validateSystemHealthRuntime(health, true)).not.toThrow();
    expect(() => systemApi.validateSystemHealthRuntime({ ...health, runtime: undefined, version: undefined }, true))
      .toThrowError(expect.objectContaining({ code: "runtime_mismatch" }));
    expect(() => systemApi.validateSystemHealthRuntime({ ...health, runtime: "python-fastapi" }, true))
      .toThrowError(expect.objectContaining({ code: "runtime_mismatch" }));
    delete window.storydexDesktop;
  });
});
