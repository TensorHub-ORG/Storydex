import { describe, expect, it, vi } from "vitest";
import { shallowMount } from "@vue/test-utils";
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

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia(); setActivePinia(pinia);
  return shallowMount(StoryStatePanel, { props: { relationshipOnly: false, initialTab: "changes", ...props }, global: { plugins: [pinia] } });
}

describe("StoryStatePanel deterministic graph and inspector behavior", () => {
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
    expect(u.normalizeWikiCategory(undefined)).toBe("overview"); expect(u.normalizeWikiCategory("unknown")).toBe("overview"); expect(u.normalizeWikiCategory("plot")).toBe("plot");
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
});
