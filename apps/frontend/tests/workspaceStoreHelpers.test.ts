import { describe, expect, it, vi } from "vitest";
import { ApiResponseError } from "@/api/client";
import { __workspaceStoreTestUtils } from "@/stores/workspace";

const u = __workspaceStoreTestUtils!;

describe("workspace store deterministic helpers", () => {
  it("normalizes complete story settings across aliases and boundaries", () => {
    expect(u.defaultStoryProjectSettings("custom.json")).toMatchObject({ settingsPath: "custom.json", source: "default" });
    expect(u.storyProjectConfigRelativePath("")).toContain("project-settings.json");
    expect(u.storyChapterProgressRelativePath("custom")).toBe("custom/memory/chapter-progress.json");
    const payload = u.normalizeStorySettingsPayload({
      segmentExtension: "txt", maxSegmentsPerChapter: 1000, storyFragmentCount: 0, storyFragmentWordCount: 99999,
      storyChapterTemplateId: "single_file_chapter_directory",
      autoUpdateVariables: "true" as never, autoUpdateWiki: "off" as never, agentCommitPromptEnabled: "0" as never,
      autoNameChapterDirectories: "auto" as never, contextConcisionMinCalls: 7, contextConcisionMaxCalls: 2,
      contextConcisionMaxInputTokens: 999999, chapterCompletion: { "chapters/a": { completed: true } as never }
    });
    expect(payload).toMatchObject({ segmentExtension: ".txt", maxSegmentsPerChapter: 99, storyFragmentCount: 1, storyFragmentWordCount: 20000, storyChapterTemplateId: "single_file_chapter_directory", autoUpdateVariables: true, autoUpdateWiki: false, chapterDirectoryNamingMode: "auto", contextConcisionMaxCalls: 7 });
    const fallback = { ...u.defaultStoryProjectSettings(), storyFragmentCount: 4, autoUpdateWiki: true };
    const response = u.normalizeStorySettingsResponse({ storySegmentFormat: "txt", max_segments_per_chapter: 5, story_fragment_word_count: 1200, story_chapter_template_id: "single_file_chapter_directory", auto_name_chapter_title: "manual", updatedAt: " now " } as never, { source: "api", fallbackPath: "fallback.json", fallbackSettings: fallback, currentSettings: null });
    expect(response).toMatchObject({ segmentExtension: ".txt", maxSegmentsPerChapter: 5, storyFragmentCount: 4, storyFragmentWordCount: 1200, storyChapterTemplateId: "single_file_chapter_directory", autoUpdateWiki: true, autoNameChapterDirectories: false, updatedAt: "now" });
    const fromFile = u.normalizeStorySettingsFromProjectFile({ story_settings: JSON.stringify({ storySegmentFormat: "txt", storyFragmentCount: 3, storyChapterTemplateId: "single_file_chapter_directory", auto_update_variables: "enabled", chapterNamingMode: "auto" }) }, "settings.json", { chapters: { "chapters/one": true }, updated_at: "date" });
    expect(fromFile).toMatchObject({ segmentExtension: ".txt", storyFragmentCount: 3, storyChapterTemplateId: "single_file_chapter_directory", autoUpdateVariables: true, autoNameChapterDirectories: true, source: "project_file" });
  });

  it("covers primitive setting and JSON normalization branches", () => {
    for (const value of ["txt", ".txt", "md", null]) expect([".txt", ".md"]).toContain(u.normalizeStorySegmentExtension(value));
    expect(u.normalizeStoryMaxSegmentsPerChapter("bad")).toBe(3); expect(u.normalizeStoryMaxSegmentsPerChapter(-1)).toBe(1);
    expect(u.normalizeStoryFragmentCount("bad")).toBe(1); expect(u.normalizeStoryFragmentCount(99)).toBe(99);
    expect(u.normalizeStoryChapterTemplateId(" ")).toBe("default_chapter_directory");
    expect(u.normalizeStoryFragmentWordCount("bad")).toBe(2000); expect(u.normalizeStoryFragmentWordCount(1)).toBe(100);
    expect(u.normalizeStoryCallCount("bad", 2)).toBe(2); expect(u.normalizeStoryCallCount(99, 2)).toBe(8);
    expect(u.normalizeStoryContextTokens("bad", 5000)).toBe(5000); expect(u.normalizeStoryContextTokens(1, 5000)).toBe(4000);
    expect(u.normalizeStoryAutoNameChapterDirectories("auto")).toBe(true); expect(u.normalizeStoryAutoNameChapterDirectories("manual")).toBe(false);
    for (const [value, expected] of [[true, true], [false, false], ["", true], ["disabled", false], ["enabled", true], ["unknown", true]] as const) expect(u.normalizeBooleanFlag(value, true)).toBe(expected);
    expect(u.normalizeChapterCompletionMap(null)).toEqual({});
    expect(u.normalizeChapterCompletionMap({ "": true, "chapters/a": true, "chapters/b": { completed: false } })).toEqual({ "chapters/a": true, "chapters/b": false });
    expect(u.parseJsonObject('{"a":1}')).toEqual({ a: 1 }); expect(u.parseJsonObject("bad")).toEqual({}); expect(u.parseJsonObject([])).toEqual({});
    expect(u.hasExtendedStorySettingsPayload({} as never)).toBe(false); expect(u.hasExtendedStorySettingsPayload({ story_fragment_count: 1 } as never)).toBe(true); expect(u.hasExtendedStorySettingsPayload({ story_chapter_template_id: "single" } as never)).toBe(true);
  });

  it("walks trees, diagnostics and story segment candidates", () => {
    const tree = [
      { kind: "file", relativePath: "root.py", extension: ".py" },
      { kind: "file", relativePath: "chapters/a.md", extension: ".md" },
      { kind: "file", relativePath: "chapters/a.variables.json", extension: ".json" },
      { kind: "directory", relativePath: "nested", children: [{ kind: "file", relativePath: "nested/b.json", extension: ".json" }] },
      { kind: "directory", relativePath: "empty", children: [] }
    ] as never;
    expect(u.collectDiagnosticCandidatePaths(tree)).toEqual(expect.arrayContaining(["root.py", "chapters/a.md", "nested/b.json"]));
    expect(u.isStoryDiagnosticCandidate("chapters/a.md")).toBe(true); expect(u.isStoryDiagnosticCandidate("chapters/a.variables.json")).toBe(false); expect(u.isStoryDiagnosticCandidate("other/a.md")).toBe(false);
    expect(u.normalizeDiagnostics(undefined)).toEqual([]);
    expect(u.normalizeDiagnostics([null, { relativePath: "chapters/a.md", message: " issue ", source: "", severity: "", line: "2", column: 1 }, { relativePath: "", message: "bad" }] as never)).toHaveLength(1);
    expect(u.isStorySegmentPath("chapters/seg-one.md", ".txt")).toBe(true); expect(u.isStorySegmentPath("chapters/one.txt", ".txt")).toBe(true); expect(u.isStorySegmentPath("chapters/one.variables.json", ".md")).toBe(false);
    expect(u.findFirstFile(tree)).toBe("root.py"); expect(u.findFirstFile([{ kind: "directory", children: [] }] as never)).toBe("");
    expect(u.collectFilePaths(tree).has("nested/b.json")).toBe(true);
    expect(u.treeContainsPath(tree, "nested/b.json")).toBe(true); expect(u.treeContainsPath(tree, "")).toBe(false); expect(u.treeContainsPath(tree, "missing")).toBe(false);
  });

  it("builds preview/diff content and normalizes errors and diff payloads", () => {
    expect(u.buildAgentPreviewId("")).toContain("preview"); expect(u.buildAgentPreviewId("chapters/a.md")).toContain("chapters__a.md");
    expect(u.buildGitReviewId()).toBe("workspace-git-diff"); expect(u.buildGitReviewId(" t ")).toBe("agent-run-diff:t");
    expect(u.buildGitReviewContent(null, "bad")).toContain("bad"); expect(u.buildGitReviewContent(null)).toContain("not loaded");
    expect(u.buildGitReviewContent({ branch: "main", totals: { files: 1, added: 2, removed: 1 }, files: [{ relativePath: "a", added: 2, removed: 1 }] } as never)).toContain("main");
    expect(u.extensionFromPath("")).toBe(".txt"); expect(u.extensionFromPath("README")).toBe(".txt"); expect(u.extensionFromPath("A.JSON")).toBe(".json");
    expect(u.normalizeWorkspaceError(new ApiResponseError("api"))).toBe("api"); expect(u.normalizeWorkspaceError(new Error("plain"))).toBe("plain");
    expect(u.normalizeAgentRunDiffError(new ApiResponseError("missing", "agent_run_not_found"))).toBeTruthy();
    expect(u.normalizeAgentRunDiffError(new ApiResponseError("custom", "other"))).toBe("custom");
    expect(u.normalizeAgentRunDiffError(new Error("404 not found"))).toBeTruthy();
    const diff = u.normalizeGitDiffResponse({ available: true, gitInstalled: true, initialized: true, branch: "main", files: [{ relativePath: "\\a.md", status: "", added: "2", removed: -1, truncated: 1, hunks: [{ header: "h", oldStart: 1, oldLines: 1, newStart: 2, newLines: 1, lines: [{ kind: "added", oldLine: "", newLine: 2, content: null }, { kind: "bad" }] }] }], totals: null, message: null } as never);
    expect(diff.files[0].relativePath).toBe("a.md"); expect(diff.files[0].removed).toBe(0); expect(diff.files[0].hunks[0].lines[1].kind).toBe("context");
    expect(u.normalizeGitDiffResponse({ files: "bad" } as never).files).toEqual([]);
  });

  it("covers count, path, size, restore, containment and rebase boundaries", () => {
    expect(u.normalizeCount("bad")).toBe(0); expect(u.normalizeCount(-2)).toBe(0); expect(u.normalizeCount(2.6)).toBe(3);
    expect(u.normalizeNullableCount("")).toBeNull(); expect(u.normalizeNullableCount("2")).toBe(2);
    expect(u.normalizeGitDiffLineKind("removed")).toBe("removed"); expect(u.normalizeGitDiffLineKind("bad")).toBe("context");
    expect(u.countVisibleCharacters(" a \n b ")).toBe(2); expect(u.fileNameFromPath("a/b.md")).toBe("b.md"); expect(u.estimateUtf8Size("中")).toBeGreaterThan(1);
    expect(u.normalizeRelativePath("/a\\b/")).toBe("a/b"); expect(u.normalizePathList(["a", "a", "", "\\b"])).toEqual(["a", "b"]);
    expect(u.normalizeFilesystemPath(" C:\\story\\ ")).toBe("C:/story");
    expect(u.shouldRestoreProjectFromHealth(null, "x")).toBe(false);
    expect(u.shouldRestoreProjectFromHealth({ workspaceRoot: "C:/story/", hasStorydexConfig: true } as never, "C:\\story")).toBe(true);
    expect(u.shouldRestoreProjectFromHealth({ workspaceRoot: "C:/other", hasStorydexConfig: true } as never, "C:/story")).toBe(false);
    expect(u.resolveRelativeProjectFilePath("", "root")).toBe(""); expect(u.resolveRelativeProjectFilePath("C:/root", "C:/root")).toBe(""); expect(u.resolveRelativeProjectFilePath("C:/other/a", "C:/root")).toBe(""); expect(u.resolveRelativeProjectFilePath("C:/ROOT/a.md", "C:/root")).toBe("a.md");
    expect(u.isSameOrNestedPath("a", "a")).toBe(true); expect(u.isSameOrNestedPath("a/b", "a")).toBe(true); expect(u.isSameOrNestedPath("x", "a")).toBe(false);
    expect(u.rebaseRelativePath("x", "a", "b")).toBe("x"); expect(u.rebaseRelativePath("a", "a", "b")).toBe("b"); expect(u.rebaseRelativePath("a/c", "a", "b")).toBe("b/c");
  });
});
