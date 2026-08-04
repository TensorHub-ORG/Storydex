import { computed, ref } from "vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";

/**
 * 平行时空线的节点动作。
 *
 * 侧栏的紧凑树和主区的放大视图共用同一套动作，逻辑放在这里而不是各写一份，
 * 避免两个入口在"未提交改动怎么办""删除要不要拦"这类判断上出现分歧。
 */
export type WorldlineDialogMode = "" | "fork" | "rename" | "delete" | "jump-dirty";

export interface WorldlineDialogState {
  mode: WorldlineDialogMode;
  /** fork / jump-dirty 的目标节点。 */
  commitId: string;
  /** rename / delete 的目标世界线。 */
  worldlineName: string;
  /** 文本输入框的当前值（新世界线名、新名字、提交说明）。 */
  input: string;
  /** 删除时会被永久丢弃的独有版本数。 */
  exclusiveCommits: number;
  /** 节点的简要描述，用在对话框标题里。 */
  nodeLabel: string;
  error: string;
}

function emptyDialog(): WorldlineDialogState {
  return {
    mode: "",
    commitId: "",
    worldlineName: "",
    input: "",
    exclusiveCommits: 0,
    nodeLabel: "",
    error: ""
  };
}

/** 世界线名沿用后端 `_validate_branch_name` 的字符集，提前在前端拦掉。 */
const WORLDLINE_NAME_PATTERN = /^[A-Za-z0-9._/-]+$/;

export function useWorldlineActions() {
  const gitStore = useGitStore();
  const workspaceStore = useWorkspaceStore();

  const dialog = ref<WorldlineDialogState>(emptyDialog());
  const isSubmitting = ref(false);

  const busy = computed(
    () => gitStore.isJumping || gitStore.isWorldlineBusy || gitStore.isCommitting || isSubmitting.value
  );

  function nodeLabelFor(commitId: string): string {
    const node = gitStore.timelineNodes.find((item) => item.id === commitId);
    if (!node) return commitId.slice(0, 8);
    return `${node.subject || "（没有写说明）"}（${node.shortId}）`;
  }

  function close(): void {
    dialog.value = emptyDialog();
  }

  /**
   * 跳转到某个节点。工作区脏的时候不直接报错，而是先问用户要不要把当前改动
   * 提交成一个节点再跳——写作场景里"丢弃改动"这个选项不该存在，未保存的正文
   * 一旦没了就找不回来。
   */
  async function requestJump(commitId: string): Promise<void> {
    if (!commitId || busy.value) return;
    if (gitStore.changedCount > 0) {
      dialog.value = {
        ...emptyDialog(),
        mode: "jump-dirty",
        commitId,
        nodeLabel: nodeLabelFor(commitId),
        input: ""
      };
      return;
    }
    await performJump(commitId);
  }

  async function performJump(commitId: string): Promise<void> {
    const ok = await gitStore.jumpToCommit(commitId);
    if (ok) {
      await workspaceStore.reloadProjectContext();
    }
  }

  function requestFork(commitId: string): void {
    if (!commitId || busy.value) return;
    dialog.value = {
      ...emptyDialog(),
      mode: "fork",
      commitId,
      nodeLabel: nodeLabelFor(commitId),
      input: ""
    };
  }

  function requestRename(name: string): void {
    if (!name || busy.value) return;
    dialog.value = { ...emptyDialog(), mode: "rename", worldlineName: name, input: name };
  }

  function requestDelete(name: string): void {
    if (!name || busy.value) return;
    const branch = gitStore.timelineBranches.find((item) => item.name === name);
    dialog.value = {
      ...emptyDialog(),
      mode: "delete",
      worldlineName: name,
      exclusiveCommits: branch?.commitCount ?? 0
    };
  }

  async function requestInspect(commitId: string): Promise<void> {
    if (!commitId) return;
    await workspaceStore.openCommitDiff({ commitId, label: nodeLabelFor(commitId) });
  }

  function validateName(value: string): string {
    const name = value.trim();
    if (!name) return "请给这条世界线起个名字。";
    if (name.length > 120) return "名字太长了，请控制在 120 个字符以内。";
    if (!WORLDLINE_NAME_PATTERN.test(name)) {
      return "只能使用英文字母、数字和 . _ - / 这几种符号。";
    }
    if (name.startsWith("-") || name.includes("..")) {
      return "名字不能以 - 开头，也不能包含连续的两个点。";
    }
    return "";
  }

  /** 执行对话框里确认的动作。返回是否成功，便于调用方决定要不要关掉对话框。 */
  async function confirm(): Promise<boolean> {
    if (isSubmitting.value) return false;
    const state = dialog.value;
    isSubmitting.value = true;
    try {
      if (state.mode === "fork") {
        const error = validateName(state.input);
        if (error) {
          dialog.value = { ...state, error };
          return false;
        }
        const ok = await gitStore.createWorldline(state.commitId, state.input.trim());
        if (!ok) {
          dialog.value = { ...state, error: gitStore.error };
          return false;
        }
        await workspaceStore.reloadProjectContext();
        close();
        return true;
      }

      if (state.mode === "rename") {
        const error = validateName(state.input);
        if (error) {
          dialog.value = { ...state, error };
          return false;
        }
        const ok = await gitStore.renameWorldline(state.worldlineName, state.input.trim());
        if (!ok) {
          dialog.value = { ...state, error: gitStore.error };
          return false;
        }
        close();
        return true;
      }

      if (state.mode === "delete") {
        const ok = await gitStore.deleteWorldline(state.worldlineName);
        if (!ok) {
          dialog.value = { ...state, error: gitStore.error };
          return false;
        }
        close();
        return true;
      }

      if (state.mode === "jump-dirty") {
        const committed = await gitStore.commitAll(state.input.trim());
        if (!committed) {
          dialog.value = { ...state, error: gitStore.error || "当前没有可提交的改动，请刷新后重试。" };
          return false;
        }
        const target = state.commitId;
        close();
        await performJump(target);
        return true;
      }

      return false;
    } finally {
      isSubmitting.value = false;
    }
  }

  return {
    dialog,
    busy,
    isSubmitting,
    requestJump,
    requestFork,
    requestRename,
    requestDelete,
    requestInspect,
    validateName,
    confirm,
    close
  };
}
