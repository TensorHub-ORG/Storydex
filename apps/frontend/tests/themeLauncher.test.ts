import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useTheme } from "@/composables/useTheme";
import { useProjectLauncher } from "@/composables/useProjectLauncher";
import { useWorkspaceStore } from "@/stores/workspace";

describe("theme and project launcher composables", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
    document.documentElement.style.cssText = "";
  });

  it("applies themes, typography bounds, colors and desktop title bar contrast", async () => {
    const setTitleBarTheme = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: { setTitleBarTheme } });
    const style = vi.spyOn(window, "getComputedStyle");
    style.mockReturnValue({ getPropertyValue: (name: string) => name === "--bg-header" ? "rgba(255, 255, 255, .5)" : "#000" } as CSSStyleDeclaration);
    const theme = useTheme();
    theme.applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(setTitleBarTheme).toHaveBeenCalledWith({ color: "#808080", symbolColor: "#f8fafc" });
    theme.applyTheme("default");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    theme.applyTypography({ fileFontSize: 2, playerFontSize: 100 });
    expect(document.documentElement.style.getPropertyValue("--ui-file-font-size")).toBe("12px");
    expect(document.documentElement.style.getPropertyValue("--ui-player-font-size")).toBe("28px");
    theme.applyTypography({ fileFontSize: Number.NaN });
    expect(document.documentElement.style.getPropertyValue("--ui-file-font-size")).toBe("16px");

    for (const [primary, fallback] of [["", ""], ["bad", "rgb(10,20,30)"], ["#fff", "bad"], ["#ffffff80", "#000"], ["rgb(300,-2,50)", "#000"], ["rgba(0%,100%,0%,2)", "#000"]]) {
      style.mockReturnValue({ getPropertyValue: (name: string) => name === "--bg-header" ? primary : fallback } as CSSStyleDeclaration);
      theme.applyTheme("light");
    }
    expect(setTitleBarTheme).toHaveBeenCalledTimes(8);
  });

  it("validates, browses, opens and creates projects", async () => {
    const store = useWorkspaceStore();
    store.launchScreenVisible = false;
    store.currentProject = { workspaceRoot: "C:\\stories\\current", projectName: "current", openedAt: "" } as never;
    store.recentProjects = [{ workspaceRoot: "D:/recent/demo", projectName: "demo", openedAt: "" }] as never;
    store.openProject = vi.fn().mockResolvedValue(undefined) as never;
    store.createProject = vi.fn().mockResolvedValue(undefined) as never;
    const launcher = useProjectLauncher();

    launcher.openCreateProjectDialog();
    expect(launcher.dialogMode.value).toBe("create");
    expect(launcher.createBaseDirectory.value).toBe("C:\\stories");
    expect(launcher.createValidationMessage.value).toBeTruthy();
    launcher.createProjectName.value = "bad:name";
    expect(launcher.canCreateProject.value).toBe(false);
    launcher.createProjectName.value = "book";
    expect(launcher.newProjectTargetPath.value).toBe("C:\\stories\\book");
    await launcher.handleCreateProjectSubmit();
    expect(store.createProject).toHaveBeenCalledWith("C:\\stories\\book");

    launcher.openProjectDialog();
    expect(launcher.openProjectPathInput.value).toBe("C:\\stories\\current");
    await launcher.openProjectAt("  ");
    launcher.openProjectPathInput.value = "C:/other";
    await launcher.handleOpenProjectSubmit();
    expect(store.openProject).toHaveBeenCalledWith("C:/other");

    const pickDirectory = vi.fn().mockResolvedValueOnce("D:/base").mockResolvedValueOnce("D:/open").mockResolvedValueOnce("E:/direct");
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: { pickDirectory } });
    launcher.openCreateProjectDialog();
    await launcher.browseCreateBaseDirectory();
    expect(launcher.createBaseDirectory.value).toBe("D:/base");
    launcher.openProjectDialog();
    await launcher.browseOpenProjectDirectory();
    expect(launcher.openProjectPathInput.value).toBe("D:/open");
    await launcher.handleOpenProjectRequest();
    expect(store.openProject).toHaveBeenCalledWith("E:/direct");
    launcher.closeDialog();
    expect(launcher.dialogMode.value).toBeNull();
  });
});
