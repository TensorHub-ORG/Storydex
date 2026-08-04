import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  commitWorkspaceGitChanges: vi.fn(),
  createWorkspaceGitBranch: vi.fn(),
  createWorkspaceWorldline: vi.fn(),
  deleteWorkspaceWorldline: vi.fn(),
  fetchWorkspaceGitBranches: vi.fn(),
  fetchWorkspaceGitSummary: vi.fn(),
  fetchWorkspaceGitTimeline: vi.fn(),
  initializeWorkspaceGitRepository: vi.fn(),
  jumpWorkspaceGitCommit: vi.fn(),
  renameWorkspaceWorldline: vi.fn(),
  restoreWorkspaceGitCommit: vi.fn(),
  switchWorkspaceGitBranch: vi.fn()
}));

vi.mock("@/api/workspace", () => api);
vi.mock("@/api/client", () => ({
  ApiResponseError: class ApiResponseError extends Error {},
  describeTransportError: (error: unknown, fallback: string) =>
    error instanceof Error ? error.message : fallback
}));

import { useGitStore } from "@/stores/git";

const result = (data: unknown) => ({ data, trace: null, audit: [] });

const summary = {
  available: true,
  gitInstalled: true,
  initialized: true,
  branch: "develop",
  clean: true,
  changedFiles: [],
  recentCommits: [],
  graphLines: [],
  defaultBranch: "develop",
  head: { id: "c2", shortId: "c2", subject: "c2", authorName: "Storydex", authoredAt: "now" },
  message: ""
};

function timeline(head: string, branch = "develop") {
  return {
    available: true,
    gitInstalled: true,
    initialized: true,
    currentBranch: branch,
    currentHead: { ...summary.head, id: head, shortId: head, subject: head },
    detached: !branch,
    branches: branch
      ? [{ name: branch, head, isCurrent: true, lane: 0, forkColumn: 0, tipColumn: 0, commitCount: 1, totalCount: 1 }]
      : [],
    nodes: [{
      id: head,
      shortId: head,
      authorName: "Storydex",
      authoredAt: "now",
      subject: head,
      refs: "",
      parents: [],
      branches: branch ? [branch] : [],
      headBranches: branch ? [branch] : [],
      isBranchHead: Boolean(branch),
      isCurrent: true,
      column: 0,
      row: 0,
      laneBranch: branch
    }],
    edges: [],
    message: ""
  };
}

const branches = [{ name: "develop", current: true }];

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();

  api.fetchWorkspaceGitSummary.mockResolvedValue(result(summary));
  api.fetchWorkspaceGitBranches.mockResolvedValue(result({ current: "develop", branches, summary }));
  api.fetchWorkspaceGitTimeline.mockResolvedValue(result(timeline("c2")));
  api.initializeWorkspaceGitRepository.mockResolvedValue(result(summary));
  api.commitWorkspaceGitChanges.mockResolvedValue(result({
    created: true,
    commit: { ...summary.head, id: "c3", shortId: "c3", subject: "c3" },
    summary: { ...summary, head: { ...summary.head, id: "c3", shortId: "c3", subject: "c3" } }
  }));
  api.restoreWorkspaceGitCommit.mockResolvedValue(result({ summary, restoredCommit: summary.head, backupRef: "" }));
  api.jumpWorkspaceGitCommit.mockResolvedValue(result({
    detached: false,
    branch: "develop",
    commit: summary.head,
    summary
  }));
  api.createWorkspaceWorldline.mockResolvedValue(result({
    current: "alt/dark",
    branches: [{ name: "alt/dark", current: true }, ...branches],
    summary: { ...summary, branch: "alt/dark" },
    worldline: "alt/dark",
    fromCommit: "c1"
  }));
  api.renameWorkspaceWorldline.mockResolvedValue(result({
    current: "alt/ending",
    branches: [{ name: "alt/ending", current: true }, ...branches],
    summary: { ...summary, branch: "alt/ending" },
    renamedFrom: "alt/dark",
    renamedTo: "alt/ending"
  }));
  api.deleteWorkspaceWorldline.mockResolvedValue(result({
    current: "develop",
    branches,
    summary,
    deleted: "alt/ending",
    exclusiveCommits: 2
  }));

  useGitStore().reset();
});

describe("git store worldline stability", () => {
  it("shares ordinary timeline reads but lets a forced read supersede stale data", async () => {
    const store = useGitStore();
    let releaseStale: (() => void) | undefined;
    api.fetchWorkspaceGitTimeline.mockImplementationOnce(
      () => new Promise((resolve) => {
        releaseStale = () => resolve(result(timeline("stale")));
      })
    );

    const first = store.refreshTimeline();
    const shared = store.refreshTimeline();
    expect(api.fetchWorkspaceGitTimeline).toHaveBeenCalledTimes(1);

    api.fetchWorkspaceGitTimeline.mockResolvedValueOnce(result(timeline("fresh")));
    await store.refreshTimeline({ force: true });
    expect(store.timeline?.currentHead?.id).toBe("fresh");

    releaseStale?.();
    await Promise.all([first, shared]);
    expect(store.timeline?.currentHead?.id).toBe("fresh");
    expect(store.isTimelineLoading).toBe(false);
  });

  it("does not repopulate a reset store with a previous project's late response", async () => {
    const store = useGitStore();
    let release: (() => void) | undefined;
    api.fetchWorkspaceGitTimeline.mockImplementationOnce(
      () => new Promise((resolve) => {
        release = () => resolve(result(timeline("old-project")));
      })
    );

    const pending = store.refreshTimeline();
    store.reset();
    release?.();
    await pending;

    expect(store.timeline).toBeNull();
    expect(store.isTimelineLoading).toBe(false);
  });

  it("forces a post-commit read even when a background timeline poll is pending", async () => {
    const store = useGitStore();
    let releaseStale: (() => void) | undefined;
    api.fetchWorkspaceGitTimeline.mockImplementationOnce(
      () => new Promise((resolve) => {
        releaseStale = () => resolve(result(timeline("c2")));
      })
    );
    const background = store.refreshTimeline();
    api.fetchWorkspaceGitTimeline.mockResolvedValueOnce(result(timeline("c3")));

    expect(await store.commitAll("new node")).toBe(true);
    await vi.waitFor(() => expect(store.timeline?.currentHead?.id).toBe("c3"));
    expect(api.fetchWorkspaceGitTimeline).toHaveBeenCalledTimes(2);

    releaseStale?.();
    await background;
    expect(store.timeline?.currentHead?.id).toBe("c3");
  });

  it("creates, renames and deletes worldlines while keeping store state synchronized", async () => {
    const store = useGitStore();

    expect(await store.createWorldline("c1", "alt/dark")).toBe(true);
    expect(api.createWorkspaceWorldline).toHaveBeenCalledWith("c1", "alt/dark");
    expect(store.summary?.branch).toBe("alt/dark");
    expect(store.successMessage).toContain("alt/dark");

    expect(await store.renameWorldline("alt/dark", "alt/ending")).toBe(true);
    expect(api.renameWorkspaceWorldline).toHaveBeenCalledWith("alt/dark", "alt/ending");
    expect(store.summary?.branch).toBe("alt/ending");

    expect(await store.deleteWorldline("alt/ending")).toBe(true);
    expect(api.deleteWorkspaceWorldline).toHaveBeenCalledWith("alt/ending");
    expect(store.branches).toEqual(branches);
    expect(store.successMessage).toContain("2 个版本");
    expect(store.isWorldlineBusy).toBe(false);
  });

  it("distinguishes switching to a tip from entering detached observation", async () => {
    const store = useGitStore();

    expect(await store.jumpToCommit("c2")).toBe(true);
    expect(store.successMessage).toContain("已切换到世界线 develop");

    api.jumpWorkspaceGitCommit.mockResolvedValueOnce(result({
      detached: true,
      branch: "",
      commit: { ...summary.head, id: "c1", shortId: "c1", subject: "c1" },
      summary: { ...summary, branch: "" }
    }));
    expect(await store.jumpToCommit("c1")).toBe(true);
    expect(store.successMessage).toContain("观测态");
  });

  it("releases the worldline busy guard after an API failure", async () => {
    const store = useGitStore();
    api.createWorkspaceWorldline.mockRejectedValueOnce(new Error("name already exists"));

    expect(await store.createWorldline("c1", "alt/dark")).toBe(false);
    expect(store.error).toContain("name already exists");
    expect(store.isWorldlineBusy).toBe(false);
  });
});
