import { computed, ref } from "vue";
import { useWorkspaceStore } from "@/stores/workspace";

const dialogMode = ref<"create" | "open" | null>(null);
const createBaseDirectory = ref("");
const createProjectName = ref("");
const openProjectPathInput = ref("");

export function useProjectLauncher() {
  const workspaceStore = useWorkspaceStore();

  const newProjectTargetPath = computed(() => joinPath(createBaseDirectory.value, createProjectName.value));
  const createValidationMessage = computed(() => {
    if (!createBaseDirectory.value.trim()) {
      return "请先选择项目存放目录。";
    }
    if (!createProjectName.value.trim()) {
      return "请输入项目名称。";
    }
    if (/[<>:"/\\|?*\u0000-\u001F]/.test(createProjectName.value.trim())) {
      return "项目名称不能包含 Windows 非法字符。";
    }
    return "";
  });
  const canCreateProject = computed(() => !createValidationMessage.value && !!newProjectTargetPath.value);

  function openCreateProjectDialog(): void {
    dialogMode.value = "create";
    createProjectName.value = "";
    createBaseDirectory.value = defaultProjectBaseDirectory(
      workspaceStore.projectRootLabel,
      workspaceStore.recentProjects
    );
  }

  function openProjectDialog(): void {
    dialogMode.value = "open";
    openProjectPathInput.value =
      workspaceStore.projectRootLabel || workspaceStore.recentProjects[0]?.workspaceRoot || "";
  }

  function closeDialog(): void {
    dialogMode.value = null;
    createBaseDirectory.value = "";
    createProjectName.value = "";
    openProjectPathInput.value = "";
  }

  async function browseCreateBaseDirectory(): Promise<void> {
    const selectedPath = await pickDirectory(
      "选择项目存放目录",
      createBaseDirectory.value ||
        defaultProjectBaseDirectory(workspaceStore.projectRootLabel, workspaceStore.recentProjects)
    );
    if (selectedPath) {
      createBaseDirectory.value = selectedPath;
    }
  }

  async function browseOpenProjectDirectory(): Promise<void> {
    const selectedPath = await pickDirectory(
      "选择项目文件夹",
      openProjectPathInput.value ||
        workspaceStore.projectRootLabel ||
        workspaceStore.recentProjects[0]?.workspaceRoot ||
        ""
    );
    if (selectedPath) {
      openProjectPathInput.value = selectedPath;
    }
  }

  async function openProjectAt(projectPath: string): Promise<void> {
    const normalized = projectPath.trim();
    if (!normalized) {
      return;
    }
    await workspaceStore.openProject(normalized);
    closeDialog();
  }

  async function handleOpenProjectRequest(): Promise<void> {
    const selectedPath = await pickDirectory(
      "选择项目文件夹",
      workspaceStore.projectRootLabel || workspaceStore.recentProjects[0]?.workspaceRoot || ""
    );
    if (selectedPath) {
      try {
        await openProjectAt(selectedPath);
      } catch {
        // handled by store
      }
      return;
    }

    if (!window.storydexDesktop?.pickDirectory) {
      openProjectDialog();
    }
  }

  async function handleCreateProjectSubmit(): Promise<void> {
    if (!canCreateProject.value) {
      return;
    }
    try {
      await workspaceStore.createProject(newProjectTargetPath.value);
      closeDialog();
    } catch {
      // handled by store
    }
  }

  async function handleOpenProjectSubmit(): Promise<void> {
    if (!openProjectPathInput.value.trim()) {
      return;
    }
    try {
      await openProjectAt(openProjectPathInput.value);
    } catch {
      // handled by store
    }
  }

  return {
    dialogMode,
    createBaseDirectory,
    createProjectName,
    openProjectPathInput,
    newProjectTargetPath,
    createValidationMessage,
    canCreateProject,
    openCreateProjectDialog,
    openProjectDialog,
    closeDialog,
    browseCreateBaseDirectory,
    browseOpenProjectDirectory,
    openProjectAt,
    handleOpenProjectRequest,
    handleCreateProjectSubmit,
    handleOpenProjectSubmit
  };
}

async function pickDirectory(title: string, defaultPath = ""): Promise<string> {
  if (!window.storydexDesktop?.pickDirectory) {
    return "";
  }
  return window.storydexDesktop.pickDirectory({ title, defaultPath: defaultPath || undefined });
}

function defaultProjectBaseDirectory(
  currentProjectRoot: string,
  recentProjects: Array<{ workspaceRoot: string }>
): string {
  const seedPath = currentProjectRoot || recentProjects[0]?.workspaceRoot || "";
  if (!seedPath) {
    return "";
  }

  const normalized = seedPath.replace(/[\\/]+$/, "");
  const index = Math.max(normalized.lastIndexOf("\\"), normalized.lastIndexOf("/"));
  return index > 0 ? normalized.slice(0, index) : normalized;
}

function joinPath(basePath: string, leaf: string): string {
  const trimmedBase = basePath.trim().replace(/[\\/]+$/, "");
  const trimmedLeaf = leaf.trim().replace(/^[\\/]+/, "");
  if (!trimmedBase || !trimmedLeaf) {
    return "";
  }
  const separator = trimmedBase.includes("\\") ? "\\" : "/";
  return `${trimmedBase}${separator}${trimmedLeaf}`;
}
