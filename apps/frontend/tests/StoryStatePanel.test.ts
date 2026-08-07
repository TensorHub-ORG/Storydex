import { afterEach, describe, expect, it, vi } from "vitest";
import { flushPromises, shallowMount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick, unref } from "vue";

const transport = vi.hoisted(() => ({
  get: vi.fn().mockResolvedValue({ data: { ok: true, data: {}, trace: null, audit: [] } }),
  post: vi.fn().mockResolvedValue({ data: { ok: true, data: {}, trace: null, audit: [] } }),
  put: vi.fn().mockResolvedValue({ data: { ok: true, data: {}, trace: null, audit: [] } }),
  patch: vi.fn().mockResolvedValue({ data: { ok: true, data: {}, trace: null, audit: [] } }),
  delete: vi.fn().mockResolvedValue({ data: { ok: true, data: {}, trace: null, audit: [] } }),
  defaults: { headers: { common: {} } }, interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } }
}));
vi.mock("axios", () => ({ default: { create: () => transport, isAxiosError: () => false } }));

import StoryStatePanel from "@/components/StoryStatePanel.vue";
import { useAgentStore } from "@/stores/agent";
import { useUiStore } from "@/stores/ui";

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia(); setActivePinia(pinia);
  return shallowMount(StoryStatePanel, { props: { relationshipOnly: false, initialTab: "changes", ...props }, global: { plugins: [pinia] } });
}

function envelope(data: unknown) {
  return { data: { ok: true, data, trace: null, audit: [] } };
}

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("StoryStatePanel deterministic graph and inspector behavior", () => {
  it("fills the Agent composer and expands the panel for WIKI workflows", async () => {
    const wrapper = mountPanel();
    const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    transport.post.mockReset();
    const agentStore = useAgentStore();
    const uiStore = useUiStore();
    uiStore.setAgentCollapsed(true);

    u.runWikiAgentWorkflow("generate");

    expect(agentStore.promptInput).toContain("重新生成完整知识图谱与 WIKI");
    expect(uiStore.agentCollapsed).toBe(false);
    expect(u.wikiAgentTone.value).toBe("idle");
    expect(u.wikiAgentStatus.value).toContain("已填入 Agent 输入框");
    expect(transport.post).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("provides distinct incremental-update and review prompts without backend jobs", async () => {
    const wrapper = mountPanel();
    const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    transport.post.mockReset();
    const agentStore = useAgentStore();

    u.runWikiAgentWorkflow("update");
    expect(agentStore.promptInput).toContain("增量更新知识图谱与 WIKI");
    u.runWikiAgentWorkflow("review");
    expect(agentStore.promptInput).toContain("全面审阅当前 Storydex 项目的 WIKI");
    expect(transport.post).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("covers scalar labels, geometry, selection and formatting helpers", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    for (const degree of [0, 1, 5, 20]) expect(u.wikiNodeRadius({}, degree)).toBeGreaterThan(0);
    expect(u.wikiNodeLabel("short")).toBe("short"); expect(u.wikiNodeLabel("x".repeat(50)).length).toBeLessThan(50);
    expect(u.shortFieldLabel("unknown")).toBeTruthy();
    expect(u.formatTime()).toBe(""); expect(u.formatTime("bad")).toBe("bad"); expect(u.formatTime(new Date().toISOString())).toBeTruthy();
    for (const level of [-10, 0, 10]) { expect(u.levelWidth(level)).toContain("%"); expect(u.levelColor(level)).toBeTruthy(); }
    for (const value of ["trust", "hostility", "custom", null]) expect(u.formatRelationshipDimension(value)).toBeTruthy();
    expect(u.relationshipDimensionLabel({ dimension: "trust" })).toBeTruthy();
    for (const value of ["character", "location", "unknown", null]) expect(u.formatRelationshipNodeKind(value)).toBeTruthy();
    expect(u.normalizeRelationshipGraphText(" a  b ")).toBe("a b");
    expect(u.relationshipNodePayloadId({ id: "x" })).toBe("x");
    expect(u.relationshipNodePayloadLabel({ label: "L" }, "F")).toBe("L"); expect(u.relationshipNodePayloadLabel(undefined, "F")).toBe("F");
    expect(u.shortNodeLabel("x".repeat(40)).length).toBeLessThan(40);
    const a = { id: "a", x: 10, y: 10 }, b = { id: "b", x: 100, y: 100 };
    expect(u.buildRelationshipEdgePath(a, b, 0)).toContain("M"); expect(u.buildRelationshipEdgeLabelPoint(a, b, 1)).toHaveProperty("x"); expect(u.buildRelationshipCurve(a, b, 2)).toHaveProperty("controlAX");
    expect(u.clampNumber(-1, 0, 10)).toBe(0); expect(u.clampNumber(20, 0, 10)).toBe(10);
    u.zoomRelationshipGraph(1); u.zoomRelationshipGraph(-20); u.resetRelationshipGraphView();
    u.selectRelationshipEdge(-1); u.selectRelationshipEdge(0); u.selectRelationshipNode(""); u.selectRelationshipNode("node");
    expect(u.isCharacterAssetPath(".storydex/characters/a.json")).toBe(true); expect(u.isCharacterAssetPath("chapters/a.md")).toBe(false);
    wrapper.unmount();
  });

  it("keeps internal IDs out of labels and renders relationship semantics without a raw zero", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    expect(u.resolveWikiDisplayLabel("")).toBe("未解析实体");
    expect(u.resolveWikiDisplayLabel("entity:char:linche")).toBe("未解析实体");
    expect(u.resolveWikiDisplayLabel("林澈")).toBe("林澈");
    expect(u.wikiRelationshipSemanticsLabel({
      relationType: "professional_collaboration",
      polarity: "neutral",
      strength: 0.65,
    })).toBe("专业合作 · 中立 · 中等强度");

    u.selectedWikiCategory.value = "characters";
    u.wikiGraphQueryData.value = {
      graph: {
        nodes: [
          { id: "char:linche", label: "林澈", type: "character", entryId: "char:linche" },
          { id: "char:suwan", label: "苏晚", type: "character", entryId: "char:suwan" },
        ],
        edges: [{
          source: "char:linche",
          target: "char:suwan",
          label: "职业",
          type: "relationship",
          level: 0,
          dimension: "professional",
          relationType: "professional_collaboration",
          polarity: "neutral",
          strength: 0.65,
          status: "asserted",
        }],
      },
      entries: [],
    };
    u.recomputeWikiLayout();
    await nextTick();
    const edge = unref(u.wikiGraphEdges)[0];
    expect(edge.displayLabel).toBe("专业合作");
    expect(edge.semanticsLabel).toBe("专业合作 · 中立 · 中等强度");
    expect(edge.displayLabel).not.toContain("0");
    wrapper.unmount();
  });

  it("filters planned and observed plot layers and exposes projection freshness", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    u.selectedWikiCategory.value = "plot";
    u.wikiData.value = {
      projectName: "Demo",
      generatedAt: "2026-07-26T00:00:00Z",
      generator: "local-fallback-wiki",
      summary: "Demo",
      schemaVersion: 2,
      knowledgeRevision: 7,
      builtFromRevision: 7,
      lastSuccessfulRevision: 7,
      status: "ready",
      diagnostics: [],
      entries: [
        { id: "planned:outline", category: "plot", title: "故事大纲", summary: "计划调查", knowledgeStatus: "planned" },
        { id: "chapter:001", category: "plot", title: "第一章", summary: "已经抵达", knowledgeStatus: "observed" },
      ],
      graph: { nodes: [], edges: [] },
    };
    u.wikiGraphQueryData.value = {
      schemaVersion: 2,
      knowledgeRevision: 7,
      builtFromRevision: 7,
      lastSuccessfulRevision: 7,
      status: "ready",
      entries: u.wikiData.value.entries,
      graph: {
        nodes: [
          { id: "planned:outline", label: "故事大纲", type: "event", entryId: "planned:outline", knowledgeStatus: "planned" },
          { id: "chapter:001", label: "第一章", type: "chapter", entryId: "chapter:001", knowledgeStatus: "observed" },
          { id: "event:raw-id", label: "event:raw-id", type: "event", entryId: "planned:outline", knowledgeStatus: "planned" },
        ],
        edges: [],
      },
    };
    u.wikiKnowledgeStatusFilter.value = "planned";
    u.recomputeWikiLayout();
    await nextTick();

    expect(unref(u.visibleWikiEntries).map((entry: any) => entry.id)).toEqual(["planned:outline"]);
    expect(unref(u.wikiGraphNodes).map((node: any) => node.label)).toEqual(["故事大纲", "未解析实体"]);
    expect(unref(u.wikiProjectionStatus)).toBe("ready");
    expect(unref(u.wikiRevisionLabel)).toContain("7/7");
    wrapper.unmount();
  });

  it("surfaces automatic sync failures and marks the shown projection stale", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    transport.post.mockReset();
    transport.post.mockRejectedValueOnce(new Error("network down"));
    u.wikiData.value = {
      projectName: "Demo", generatedAt: "now", generator: "local", summary: "Demo",
      knowledgeRevision: 3, builtFromRevision: 3, status: "ready",
      entries: [], graph: { nodes: [], edges: [] },
    };

    await u.syncWiki();

    expect(u.wikiSyncErrorMessage.value).toContain("知识图谱自动同步失败");
    expect(u.wikiData.value.status).toBe("stale");
    expect(u.wikiData.value.lastSuccessfulRevision).toBe(3);
    wrapper.unmount();
  });

  it("evaluates empty and populated relationship/WIKI computed matrices", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils; const read = (name: string) => unref(u[name]);
    u.snapshot.value = {}; u.wikiData.value = null; u.wikiGraphQueryData.value = null;
    const computedNames = ["changeEntries", "outlineChapters", "conflictEntries", "relationshipEdges", "relationshipGraphNodes", "relationshipGraphEdges", "hasRelationshipGraphContent", "selectedRelationshipEdge", "selectedRelationshipNode", "selectedRelationshipNodeInfo", "selectedRelationshipLatestHistory", "foreshadowThreads", "timelineEntries", "tabs", "visibleTabs", "wikiEntries", "wikiCategoryTabs", "selectedWikiCategoryEntries", "visibleWikiEntries", "selectedWikiEntry", "selectedWikiCategoryLabel", "selectedWikiNode", "selectedWikiRelationEdge", "wikiGraphDegrees", "wikiIsolatedRawGraphNodes", "wikiShouldLimitIsolatedNodes", "wikiHiddenIsolatedNodeCount", "wikiVisibleRawGraphNodes", "wikiGraphNodes", "wikiGraphEdges", "wikiGraphFocusCenterId", "wikiGraphFocusIds", "visibleWikiGraphLabelEdges", "wikiGraphLegend", "selectedWikiDetailEntry", "selectedWikiDetailKicker", "wikiInspectorEmptyTitle", "wikiInspectorEmptyHint", "wikiGenerationModeLabel", "wikiUpdatedAtLabel", "wikiNeedsReviewCount", "selectedWikiSourceLabel", "wikiWorkflowLabel"];
    for (const name of computedNames) read(name);

    u.snapshot.value = {
      changes: [{ field: "name", before: "a", after: "b", segmentPath: "chapters/a.md" }],
      outline: { chapters: [{ id: "c", title: "Chapter" }] }, conflicts: [{ field: "x" }],
      relationships: { edges: [{ source: "Alice", target: "Bob", dimension: "trust", current_level: 2, history: [{ summary: "met", updated_at: "now" }] }] },
      foreshadowing: { threads: [{ id: "f", summary: "hint" }] }, timeline: { entries: [{ id: "t", summary: "event" }] }
    };
    u.wikiData.value = {
      projectName: "Demo", generatedAt: "now", generationMode: "agent full", workflow: "generate_wiki",
      entries: [{ id: "e1", category: "characters", title: "Alice", summary: "hero", sourcePaths: ["characters/a.md"], needsReview: true }, { id: "e2", category: "setting", title: "Place", summary: "city", sourcePaths: [] }],
      graph: { nodes: [{ id: "n1", label: "Alice", type: "character", category: "characters", entryId: "e1" }, { id: "n2", label: "Place", type: "location", category: "setting", entryId: "e2" }], edges: [{ source: "n1", target: "n2", label: "visits", type: "relationship" }] }
    };
    u.wikiGraphQueryData.value = { graph: u.wikiData.value.graph, entries: u.wikiData.value.entries };
    u.selectedWikiCategory.value = "characters"; u.selectedWikiEntryId.value = "e1"; u.selectedWikiNodeId.value = "n1";
    u.selectedRelationshipIndex.value = 0; u.selectedRelationshipNodeId.value = "Alice";
    u.recomputeWikiLayout(); await nextTick(); for (const name of computedNames) read(name);
    const nodes = read("wikiGraphNodes"); const edges = read("wikiGraphEdges");
    if (nodes[0]) { expect(typeof u.isWikiNodeDimmed(nodes[0])).toBe("boolean"); expect(u.isWikiNodeSelectable(nodes[0])).toBe(true); u.selectWikiNode(nodes[0]); }
    if (edges[0]) { expect(typeof u.isWikiEdgeDimmed(edges[0])).toBe("boolean"); u.selectWikiEdge(edges[0].id); expect(u.isWikiEdgeActive(edges[0])).toBe(true); }
    wrapper.unmount();
  });

  it("covers category/search/zoom/reset/selection workflows", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    expect(u.normalizeWikiCategory(undefined)).toBe("characters"); expect(u.normalizeWikiCategory("unknown")).toBe("characters"); expect(u.normalizeWikiCategory("plot")).toBe("plot");
    expect(u.normalizeWikiCategory("relationships")).toBe("characters");
    expect(u.wikiCategoryTabs.value.map((tab: { id: string }) => tab.id)).toEqual(["characters", "plot", "setting"]);
    expect(u.preferredWikiCategory()).toBeTruthy(); expect(typeof u.hasWikiRelationshipNetwork()).toBe("boolean");
    u.selectWikiCategory("plot"); expect(u.selectedWikiCategory.value).toBe("plot"); u.selectWikiEntry(""); u.selectWikiEntry("entry");
    u.wikiGraphSearchInput.value = " query "; u.submitWikiGraphSearch(); expect(u.wikiGraphSearchQuery.value).toBe("query");
    u.clearWikiGraphSearch(); expect(u.wikiGraphSearchQuery.value).toBe("");
    u.zoomWikiGraphAt({ x: 100, y: 100 }, 1.2); u.zoomWikiGraphStep(1); u.zoomWikiGraphStep(-1);
    u.fitWikiGraphView(); u.resetWikiGraphView(); u.clearWikiGraphSelection(); u.ensureWikiSelection(); u.ensureWikiGraphSelection();
    expect(u.currentWikiGraphQueryParams()).toBeTruthy(); u.refreshPanel();
    expect(u.entryHasSegmentPath({ written: ["chapters/a.md"] })).toBe(true); expect(u.entryHasSegmentPath({})).toBe(false);
    wrapper.unmount();
  });

  it("merges paged graph results and preserves project-wide statistics", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    transport.get.mockReset();
    const firstNodes = Array.from({ length: 60 }, (_, index) => ({
      id: `setting:${index}`,
      label: `设定 ${index}`,
      type: "setting",
    }));
    u.selectedWikiCategory.value = "setting";
    u.wikiGraphQueryData.value = {
      graph: { nodes: firstNodes, edges: [] },
      entries: [],
      total: {
        entryCount: 62,
        nodeCount: 62,
        edgeCount: 52,
        confirmedEdgeCount: 52,
        reviewRequiredEdgeCount: 0,
        connectedNodeCount: 62,
        isolatedNodeCount: 0,
      },
      offset: 0,
      returnedNodeCount: 60,
      hasMore: true,
      nextOffset: 60,
    };
    u.wikiGraphHasMore.value = true;
    u.wikiGraphNextOffset.value = 60;
    transport.get.mockResolvedValueOnce(envelope({
      graph: {
        nodes: [
          { id: "setting:60", label: "设定 60", type: "setting" },
          { id: "setting:61", label: "设定 61", type: "setting" },
        ],
        edges: [],
      },
      entries: [],
      total: u.wikiGraphQueryData.value.total,
      offset: 60,
      returnedNodeCount: 2,
      hasMore: false,
      nextOffset: null,
    }));

    await u.loadMoreWikiGraph();

    expect(transport.get).toHaveBeenCalledWith("/story/wiki/graph", {
      params: expect.objectContaining({ offset: 60, includeReview: 1, category: "setting" }),
    });
    expect(unref(u.wikiGraphStats)).toMatchObject({
      nodeCount: 62,
      returnedNodeCount: 62,
      confirmedEdgeCount: 52,
      isolatedNodeCount: 0,
      notLoadedNodeCount: 0,
    });
    expect(u.wikiGraphQueryData.value.graph.nodes).toHaveLength(62);
    expect(u.wikiGraphHasMore.value).toBe(false);
    wrapper.unmount();
  });

  it("sends fingerprint-safe confirm and reject requests with reviewer edits", async () => {
    const wrapper = mountPanel(); const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    transport.get.mockReset();
    transport.post.mockReset();
    const first = {
      id: "candidate:first",
      fingerprint: "fingerprint-first",
      subjectId: "entity:beast",
      predicate: "栖息于",
      objectId: "entity:planet",
      targetSourcePath: ".storydex/worldbook/潮汐兽.md",
      sourceRefs: [{ path: "chapters/001.md", quote: "潮汐兽长期栖息在夜港星浅海。" }],
    };
    const second = {
      id: "candidate:second",
      fingerprint: "fingerprint-second",
      subjectId: "entity:whale",
      predicate: "来自",
      objectId: "entity:tide",
      targetSourcePath: ".storydex/characters/雾鲸.relations.md",
      usesSidecar: true,
      sourceRefs: [{ path: "chapters/001.md", quote: "水手传闻雾鲸来自远潮星。" }],
    };
    let reviewRelations = [second];
    transport.post.mockImplementation(async (url: string) => {
      if (url.endsWith("/reject")) reviewRelations = [];
      return envelope({ ok: true });
    });
    transport.get.mockImplementation(async (url: string) => {
      if (url === "/story/wiki") {
        return envelope({ projectName: "Demo", entries: [], graph: { nodes: [], edges: [] } });
      }
      if (url === "/story/wiki/graph") {
        return envelope({
          graph: { nodes: [], edges: [] }, entries: [],
          total: { entryCount: 0, nodeCount: 0, edgeCount: 0, confirmedEdgeCount: 0, reviewRequiredEdgeCount: 0, connectedNodeCount: 0, isolatedNodeCount: 0 },
          returnedNodeCount: 0, hasMore: false, nextOffset: null,
        });
      }
      if (url === "/story/wiki/relations/review") {
        return envelope({ relations: reviewRelations, total: reviewRelations.length, offset: 0, limit: 500, hasMore: false, nextOffset: null });
      }
      throw new Error(`unexpected GET ${url}`);
    });
    u.wikiReviewQueue.value = [first, second];
    u.wikiReviewDrafts.value = {
      [first.id]: {
        subjectId: "entity:beast",
        predicate: "长期栖息于",
        objectId: "entity:planet",
        targetSourcePath: ".storydex/worldbook/潮汐兽.md",
        rejectReason: "incorrect",
        rejectNote: "",
      },
      [second.id]: {
        subjectId: "entity:whale",
        predicate: "来自",
        objectId: "entity:tide",
        targetSourcePath: ".storydex/characters/雾鲸.relations.md",
        rejectReason: "ambiguous",
        rejectNote: "仅为传闻",
      },
    };

    await u.confirmWikiCandidate(first);
    expect(transport.post).toHaveBeenCalledWith(
      "/story/wiki/relations/candidate%3Afirst/confirm",
      {
        expectedFingerprint: "fingerprint-first",
        subjectId: "entity:beast",
        predicate: "长期栖息于",
        objectId: "entity:planet",
        targetSourcePath: ".storydex/worldbook/潮汐兽.md",
      },
    );
    expect(u.wikiReviewQueue.value.map((candidate: { id: string }) => candidate.id)).toEqual(["candidate:second"]);

    await u.rejectWikiCandidate(second);
    expect(transport.post).toHaveBeenCalledWith(
      "/story/wiki/relations/candidate%3Asecond/reject",
      {
        expectedFingerprint: "fingerprint-second",
        reason: "ambiguous",
        note: "仅为传闻",
      },
    );
    expect(u.wikiReviewQueue.value).toEqual([]);
    wrapper.unmount();
  });

  it("renders review-required evidence as a warning edge and shows sidecar guidance", async () => {
    const wrapper = mountPanel({ relationshipOnly: true }); const u = (wrapper.vm as any).__testUtils;
    await flushPromises();
    const candidate = {
      id: "candidate:review",
      fingerprint: "fingerprint-review",
      subjectId: "entity:whale",
      subject: "雾鲸",
      predicate: "来自",
      objectId: "entity:tide",
      object: "远潮星",
      reviewStatus: "review_required",
      knowledgeStatus: "inferred",
      confidence: 0.55,
      provenance: { providerId: "test-provider", model: "test-model" },
      traceId: "trace-review",
      targetSourcePath: ".storydex/characters/雾鲸.relations.md",
      usesSidecar: true,
      sourceRefs: [{ path: "chapters/001.md", quote: "水手传闻雾鲸来自远潮星。" }],
    };
    u.wikiData.value = { projectName: "Demo", entries: [], graph: { nodes: [], edges: [] } };
    u.wikiGraphQueryData.value = {
      graph: {
        nodes: [
          { id: "entity:whale", label: "雾鲸", type: "setting" },
          { id: "entity:tide", label: "远潮星", type: "setting" },
        ],
        edges: [{
          id: candidate.id,
          fingerprint: candidate.fingerprint,
          source: candidate.subjectId,
          target: candidate.objectId,
          label: candidate.predicate,
          type: "relationship",
          reviewStatus: candidate.reviewStatus,
          knowledgeStatus: candidate.knowledgeStatus,
          confidence: candidate.confidence,
          provenance: candidate.provenance,
          traceId: candidate.traceId,
          sourceRefs: candidate.sourceRefs,
          evidence: candidate.sourceRefs[0].quote,
        }],
      },
      entries: [],
      total: { entryCount: 62, nodeCount: 62, edgeCount: 53, confirmedEdgeCount: 52, reviewRequiredEdgeCount: 1, connectedNodeCount: 62, isolatedNodeCount: 0 },
      returnedNodeCount: 60,
      hasMore: true,
      nextOffset: 60,
    };
    u.wikiGraphHasMore.value = true;
    u.wikiGraphNextOffset.value = 60;
    u.wikiReviewQueue.value = [candidate];
    u.wikiReviewTotal.value = 1;
    u.wikiReviewOpen.value = true;
    u.recomputeWikiLayout();
    u.selectWikiEdge(candidate.id);
    await nextTick();

    expect(wrapper.find(".ssp-wiki-edge.edge-review-required").exists()).toBe(true);
    expect(wrapper.text()).toContain("总节点 62");
    expect(wrapper.text()).toContain("当前返回 60");
    expect(wrapper.text()).toContain("待确认边 1");
    expect(wrapper.text()).toContain("水手传闻雾鲸来自远潮星。");
    expect(wrapper.text()).toContain("test-provider / test-model");
    expect(wrapper.text()).toContain("该关系将写入 JSON 角色卡 sidecar。");
    expect(wrapper.find(".ssp-wiki-source-link").exists()).toBe(true);
    wrapper.unmount();
  });
});
