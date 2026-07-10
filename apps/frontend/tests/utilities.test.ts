import { beforeEach, describe, expect, it, vi } from "vitest";
import { isRunActivelyStreaming, shouldShowLiveTurnPhase } from "@/utils/agentRunDisplay";
import { collapseFragmentedShortLines, formatAgentStreamText } from "@/utils/agentTextLayout";
import { applyCachedThemeSnapshot, applyThemeSnapshot, readCachedThemeCode, THEME_CACHE_KEY, writeCachedThemeCode } from "@/utils/appearance";
import { openFilePreviewWindow } from "@/utils/filePreview";
import { computeForceLayout } from "@/utils/forceLayout";
import { compactText } from "@/utils/format";
import { buildPendingWriteChecklistItem, buildTurnPhaseChecklistItem } from "@/utils/turnPhaseChecklist";
import { findMarkdownLinkAnchor, isExternalMarkdownHref, resolveMarkdownWorkspaceHref } from "@/utils/workspaceLinks";
import router from "@/router";

describe("frontend deterministic utilities", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    vi.restoreAllMocks();
  });

  it("maps live run and phase visibility states", () => {
    expect(isRunActivelyStreaming(" RUNNING ")).toBe(true);
    expect(isRunActivelyStreaming(null)).toBe(false);
    for (const status of ["completed", "committed", "cancelled", "stopped", "error"]) {
      expect(shouldShowLiveTurnPhase({ runStatus: status, phase: "planning" })).toBe(false);
    }
    expect(shouldShowLiveTurnPhase({ runStatus: "preview", phase: "awaiting_approval" })).toBe(true);
    expect(shouldShowLiveTurnPhase({ runStatus: "preview", phaseStatus: "waiting_approval" })).toBe(true);
    expect(shouldShowLiveTurnPhase({ runStatus: "preview", phase: "planning" })).toBe(false);
    expect(shouldShowLiveTurnPhase({ runStatus: "running" })).toBe(true);
  });

  it("repairs fragmented streamed text without damaging structured markdown", () => {
    expect(collapseFragmentedShortLines("")).toBe("");
    expect(collapseFragmentedShortLines("normal\nparagraph")).toBe("normal\nparagraph");
    expect(collapseFragmentedShortLines("你\n好\n世\n界\n，\n这\n是\n我")).toBe("你好世界，这是我");
    expect(collapseFragmentedShortLines("A\nB\nC\nD\nE\nF\nG\nH")).toBe("A\nB\nC\nD\nE\nF\nG\nH");
    expect(collapseFragmentedShortLines("> 你\n> 好\n> 世\n> 界\n> ，\n> 欢\n> 迎\n> 你")).toBe("> 你好世界，欢迎你");
    expect(collapseFragmentedShortLines("- a\n- b\n- c\n- d\n- e\n- f\n- g\n- h")).toContain("\n");
    expect(collapseFragmentedShortLines("```\na\nb\nc\nd\ne\nf\ng\n```")).toContain("```");
    expect(formatAgentStreamText("你\n好\n世\n界\n，\n欢\n迎\n你\n\nplain paragraph")).toContain("你好世界，欢迎你");
  });

  it("caches and applies appearance snapshots including storage failures", () => {
    expect(readCachedThemeCode()).toBeNull();
    writeCachedThemeCode("dark");
    expect(localStorage.getItem(THEME_CACHE_KEY)).toBe("dark");
    expect(applyCachedThemeSnapshot()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    applyThemeSnapshot("white");
    expect(document.documentElement.dataset.theme).toBe("white");
    applyThemeSnapshot("default");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    localStorage.setItem(THEME_CACHE_KEY, "invalid");
    expect(readCachedThemeCode()).toBeNull();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(readCachedThemeCode()).toBeNull();
    vi.restoreAllMocks();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => writeCachedThemeCode("dark")).not.toThrow();
  });

  it("opens previews through desktop integration or browser fallback", async () => {
    expect(await openFilePreviewWindow(" /// ")).toBe(false);
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: { openPreviewWindow: vi.fn().mockResolvedValue(true) } });
    expect(await openFilePreviewWindow("\\chapters\\one.md\\")).toBe(true);
    expect(window.storydexDesktop?.openPreviewWindow).toHaveBeenCalledWith("chapters/one.md");
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
    vi.spyOn(router, "resolve").mockReturnValue({ href: "/preview?relativePath=a.md" } as never);
    const opener = vi.spyOn(window, "open").mockReturnValue({} as Window);
    expect(await openFilePreviewWindow("a.md")).toBe(true);
    expect(opener).toHaveBeenCalled();
    opener.mockReturnValue(null);
    expect(await openFilePreviewWindow("a.md")).toBe(false);
  });

  it("computes bounded deterministic force layouts", () => {
    expect(computeForceLayout([], [], { width: 100, height: 100 })).toEqual({});
    expect(computeForceLayout([{ id: "a" }], [], { width: 100, height: 80 })).toEqual({ a: { x: 50, y: 40 } });
    const layout = computeForceLayout(
      [{ id: "a", x: 50, y: 50, radius: 10 }, { id: "b", x: 50, y: 50 }, { id: "c" }],
      [{ source: "a", target: "b", weight: 9 }, { source: "missing", target: "b" }, { source: "a", target: "a" }],
      { width: 220, height: 180, iterations: 3, padding: 8 }
    );
    expect(Object.keys(layout)).toEqual(["a", "b", "c"]);
    expect(Object.values(layout).every(({ x, y }) => Number.isFinite(x) && Number.isFinite(y))).toBe(true);
    const cramped = computeForceLayout([{ id: "a" }, { id: "b" }], [], { width: 20, height: 20, iterations: 1, padding: 20 });
    expect(cramped.a.x).toBe(10);
  });

  it("builds checklist, compact text and safe workspace link results", () => {
    expect(compactText("short", 10)).toBe("short");
    expect(compactText("123456", 3)).toBe("123...");
    expect(buildTurnPhaseChecklistItem({})).toBeNull();
    expect(buildTurnPhaseChecklistItem({ phase: "planning" })?.status).toBe("in_progress");
    expect(buildTurnPhaseChecklistItem({ phase: "awaiting_approval" })?.id).toContain("awaiting_approval");
    expect(buildTurnPhaseChecklistItem({ phase: "custom", label: "Custom", status: "done" })).toMatchObject({ title: "Custom", status: "done" });
    expect(buildPendingWriteChecklistItem({ hasPendingWrite: false })).toBeNull();
    expect(buildPendingWriteChecklistItem({ hasPendingWrite: true, pendingWriteExpired: true })).toBeNull();
    expect(buildPendingWriteChecklistItem({ hasPendingWrite: true })?.status).toBe("waiting_approval");

    expect(resolveMarkdownWorkspaceHref("../assets/a.png?x=1#y", "chapters/one/a.md")).toBe("chapters/assets/a.png");
    expect(resolveMarkdownWorkspaceHref("/root.md")).toBe("root.md");
    expect(resolveMarkdownWorkspaceHref("a%20b.md")).toBe("a b.md");
    expect(resolveMarkdownWorkspaceHref("%E0%A4%A")).toBe("%E0%A4%A");
    for (const href of ["", "#part", "https://example.com", "//server/a", "C:\\a.md", "\\\\server\\a", "..", "../../escape.md"]) {
      expect(resolveMarkdownWorkspaceHref(href)).toBeNull();
    }
    expect(isExternalMarkdownHref("mailto:a@example.com")).toBe(true);
    expect(isExternalMarkdownHref("notes/a.md")).toBe(false);
    const anchor = document.createElement("a");
    const span = document.createElement("span");
    anchor.append(span); document.body.append(anchor);
    expect(findMarkdownLinkAnchor(span)).toBe(anchor);
    expect(findMarkdownLinkAnchor(span.firstChild)).toBeNull();
    expect(findMarkdownLinkAnchor(document.createTextNode("x"))).toBeNull();
  });
});
