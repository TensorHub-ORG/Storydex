import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, ApiResponseError, describeTransportError, getApiAuthToken, setApiAuthToken, unwrapEnvelope } from "@/api/client";
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
  it("covers successful calls for every non-stream API surface", async () => {
    const results = await Promise.all([
      callExportedApiFunctions(agentApi, ["streamAgentPrompt"]),
      callExportedApiFunctions(authApi),
      callExportedApiFunctions(helpApi),
      callExportedApiFunctions(presetApi),
      callExportedApiFunctions(systemApi),
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
});
