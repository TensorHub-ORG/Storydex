import { defineStore } from "pinia";
import { ApiResponseError, describeTransportError } from "@/api/client";
import {
  fetchStoryChapters,
  fetchStoryCurrentState,
  fetchStoryLatestSnapshot
} from "@/api/workspace";
import { useWorkspaceStore } from "@/stores/workspace";
import type { ApiTrace } from "@/types/api";
import type {
  StoryChapterState,
  StoryCurrentStateData,
  StoryCurrentStateModel,
  StoryCurrentStateResponse,
  StoryLatestSnapshotModel,
  StoryLatestSnapshotPayload,
  StoryLatestSnapshotResponse,
  StorySnapshotCharacterUpdate,
  StorySnapshotEventUpdate,
  StorySnapshotMemoryUpdate,
  StorySnapshotOperation
} from "@/types/workspace";

interface StoryState {
  initialized: boolean;
  isRefreshing: boolean;
  isChaptersLoading: boolean;
  isCurrentStateLoading: boolean;
  isLatestSnapshotLoading: boolean;
  chapters: StoryChapterState[];
  currentState: StoryCurrentStateModel | null;
  latestSnapshot: StoryLatestSnapshotModel | null;
  chaptersTrace: ApiTrace | null;
  currentStateTrace: ApiTrace | null;
  latestSnapshotTrace: ApiTrace | null;
  chaptersError: string;
  currentStateError: string;
  latestSnapshotError: string;
  storyError: string;
  lastLoadedAt: string;
}

export const useStoryStore = defineStore("story", {
  state: (): StoryState => createDefaultStoryState(),

  getters: {
    isLoading(state): boolean {
      return (
        state.isRefreshing ||
        state.isChaptersLoading ||
        state.isCurrentStateLoading ||
        state.isLatestSnapshotLoading
      );
    },

    hasData(state): boolean {
      return Boolean(state.chapters.length || state.currentState || state.latestSnapshot);
    },

    error(state): string {
      return state.storyError;
    },

    latestSnapshotPath(state): string {
      return state.latestSnapshot?.relativePath || state.currentState?.latestSnapshotPath || "";
    },

    fullState(state): Record<string, unknown> {
      return state.currentState?.fullState || state.latestSnapshot?.fullState || {};
    },

    focusChapter(state): StoryChapterState | null {
      const chapterRelativePath =
        deriveChapterRelativePath(state.latestSnapshot?.relativePath || "") ||
        deriveChapterRelativePath(state.currentState?.latestSnapshotPath || "");

      if (!chapterRelativePath) {
        return null;
      }

      return (
        state.chapters.find(
          (chapter) => normalizeRelativePath(chapter.relativePath) === chapterRelativePath
        ) || null
      );
    }
  },

  actions: {
    clear(): void {
      Object.assign(this, createDefaultStoryState());
    },

    clearErrors(): void {
      this.chaptersError = "";
      this.currentStateError = "";
      this.latestSnapshotError = "";
      this.storyError = "";
    },

    async refreshAll(): Promise<void> {
      if (!isStoryWorkspaceReady()) {
        this.clear();
        return;
      }

      this.isRefreshing = true;
      this.storyError = "";

      try {
        await Promise.all([
          this.refreshChapters(),
          this.refreshCurrentState(),
          this.refreshLatestSnapshot()
        ]);
      } finally {
        this.storyError = buildStoryErrorMessage([
          this.chaptersError,
          this.currentStateError,
          this.latestSnapshotError
        ]);
        this.isRefreshing = false;
      }
    },

    async refreshChapters(): Promise<StoryChapterState[]> {
      if (!isStoryWorkspaceReady()) {
        this.clear();
        return [];
      }

      this.isChaptersLoading = true;
      this.chaptersError = "";

      try {
        const result = await fetchStoryChapters();
        this.chapters = normalizeStoryChapters(result.data.items);
        this.chaptersTrace = result.trace;
        this.initialized = true;
        this.lastLoadedAt = new Date().toISOString();
        return this.chapters;
      } catch (error: unknown) {
        this.chapters = [];
        this.chaptersTrace = null;
        this.chaptersError = normalizeStoryError(error);
        return [];
      } finally {
        this.storyError = buildStoryErrorMessage([
          this.chaptersError,
          this.currentStateError,
          this.latestSnapshotError
        ]);
        this.isChaptersLoading = false;
      }
    },

    async refreshCurrentState(): Promise<StoryCurrentStateModel | null> {
      if (!isStoryWorkspaceReady()) {
        this.clear();
        return null;
      }

      this.isCurrentStateLoading = true;
      this.currentStateError = "";

      try {
        const result = await fetchStoryCurrentState();
        this.currentState = normalizeStoryCurrentState(result.data);
        this.currentStateTrace = result.trace;
        this.initialized = true;
        this.lastLoadedAt = new Date().toISOString();
        return this.currentState;
      } catch (error: unknown) {
        this.currentState = null;
        this.currentStateTrace = null;
        this.currentStateError = normalizeStoryError(error);
        return null;
      } finally {
        this.storyError = buildStoryErrorMessage([
          this.chaptersError,
          this.currentStateError,
          this.latestSnapshotError
        ]);
        this.isCurrentStateLoading = false;
      }
    },

    async refreshLatestSnapshot(): Promise<StoryLatestSnapshotModel | null> {
      if (!isStoryWorkspaceReady()) {
        this.clear();
        return null;
      }

      this.isLatestSnapshotLoading = true;
      this.latestSnapshotError = "";

      try {
        const result = await fetchStoryLatestSnapshot();
        this.latestSnapshot = normalizeStoryLatestSnapshot(result.data);
        this.latestSnapshotTrace = result.trace;
        this.initialized = true;
        this.lastLoadedAt = new Date().toISOString();
        return this.latestSnapshot;
      } catch (error: unknown) {
        this.latestSnapshot = null;
        this.latestSnapshotTrace = null;
        this.latestSnapshotError = normalizeStoryError(error);
        return null;
      } finally {
        this.storyError = buildStoryErrorMessage([
          this.chaptersError,
          this.currentStateError,
          this.latestSnapshotError
        ]);
        this.isLatestSnapshotLoading = false;
      }
    }
  }
});

function createDefaultStoryState(): StoryState {
  return {
    initialized: false,
    isRefreshing: false,
    isChaptersLoading: false,
    isCurrentStateLoading: false,
    isLatestSnapshotLoading: false,
    chapters: [],
    currentState: null,
    latestSnapshot: null,
    chaptersTrace: null,
    currentStateTrace: null,
    latestSnapshotTrace: null,
    chaptersError: "",
    currentStateError: "",
    latestSnapshotError: "",
    storyError: "",
    lastLoadedAt: ""
  };
}

function isStoryWorkspaceReady(): boolean {
  const workspaceStore = useWorkspaceStore();
  return !workspaceStore.launchScreenVisible && Boolean(workspaceStore.currentProject);
}

function normalizeStoryChapters(items: StoryChapterState[] | undefined): StoryChapterState[] {
  if (!Array.isArray(items)) {
    return [];
  }

  return items
    .filter((item) => isRecord(item))
    .map((item) => ({
      relativePath: normalizeRelativePath(item.relativePath),
      name: normalizeText(item.name),
      displayName: normalizeText(item.displayName || item.name),
      chapterNumber: normalizeNumber(item.chapterNumber, 0),
      completed: Boolean(item.completed),
      updatedAt: normalizeText(item.updatedAt)
    }))
    .filter((item) => Boolean(item.relativePath));
}

function normalizeStoryCurrentState(payload: StoryCurrentStateResponse): StoryCurrentStateModel {
  const raw = normalizeStoryCurrentStateData(payload.data);

  return {
    currentStatePath: normalizeRelativePath(payload.currentStatePath),
    latestSnapshotIndexPath: normalizeRelativePath(payload.latestSnapshotIndexPath),
    data: raw,
    updatedAt: normalizeText(raw.updatedAt),
    latestSnapshotPath: normalizeRelativePath(raw.latestSnapshotPath),
    fullState: normalizeRecord(raw.fullState),
    raw
  };
}

function normalizeStoryCurrentStateData(value: StoryCurrentStateData | undefined): StoryCurrentStateData {
  const record = isRecord(value) ? { ...value } : {};

  return {
    ...record,
    updatedAt: normalizeText(record.updatedAt ?? record.updated_at),
    latestSnapshotPath: normalizeRelativePath(record.latestSnapshotPath ?? record.latest_snapshot_path),
    fullState: normalizeRecord(record.fullState ?? record.full_state)
  };
}

function normalizeStoryLatestSnapshot(
  payload: StoryLatestSnapshotResponse
): StoryLatestSnapshotModel | null {
  const relativePath = normalizeRelativePath(payload.relativePath);
  const raw = normalizeStoryLatestSnapshotPayload(payload.snapshot);

  if (!relativePath && !Object.keys(raw).length) {
    return null;
  }

  return {
    relativePath,
    snapshot: raw,
    chapterId: normalizeText(raw.chapter_id),
    segmentId: normalizeText(raw.segment_id),
    snapshotOrder: normalizeNullableNumber(raw.snapshot_order),
    createdAt: normalizeText(raw.created_at),
    parentSnapshot: normalizeText(raw.parent_snapshot),
    operations: normalizeOperationList(raw.operations),
    fullState: normalizeRecord(raw.full_state),
    memoryUpdates: normalizeMemoryUpdateList(raw.memory_updates),
    characterUpdates: normalizeCharacterUpdateList(raw.character_updates),
    eventUpdates: normalizeEventUpdateList(raw.event_updates),
    snapshotComment: normalizeText(raw.snapshot_comment),
    raw
  };
}

function normalizeStoryLatestSnapshotPayload(
  value: StoryLatestSnapshotPayload | undefined
): StoryLatestSnapshotPayload {
  const record = isRecord(value) ? { ...value } : {};

  return {
    ...record,
    chapter_id: normalizeText(record.chapter_id ?? record.chapterId),
    segment_id: normalizeText(record.segment_id ?? record.segmentId),
    snapshot_order: normalizeNullableNumber(record.snapshot_order ?? record.snapshotOrder) ?? undefined,
    created_at: normalizeText(record.created_at ?? record.createdAt),
    parent_snapshot: normalizeText(record.parent_snapshot ?? record.parentSnapshot),
    operations: normalizeOperationList(record.operations),
    full_state: normalizeRecord(record.full_state ?? record.fullState),
    memory_updates: normalizeMemoryUpdateList(record.memory_updates ?? record.memoryUpdates),
    character_updates: normalizeCharacterUpdateList(record.character_updates ?? record.characterUpdates),
    event_updates: normalizeEventUpdateList(record.event_updates ?? record.eventUpdates),
    snapshot_comment: normalizeText(record.snapshot_comment ?? record.snapshotComment)
  };
}

function normalizeOperationList(value: unknown): StorySnapshotOperation[] {
  return normalizeRecordList(value).map((item) => ({
    ...item,
    op: normalizeText(item.op),
    path: normalizeText(item.path),
    evidence: normalizeText(item.evidence)
  }));
}

function normalizeMemoryUpdateList(value: unknown): StorySnapshotMemoryUpdate[] {
  return normalizeRecordList(value).map((item) => ({
    ...item,
    memory: normalizeText(item.memory),
    evidence: normalizeText(item.evidence)
  }));
}

function normalizeCharacterUpdateList(value: unknown): StorySnapshotCharacterUpdate[] {
  return normalizeRecordList(value).map((item) => ({
    ...item,
    character: normalizeText(item.character),
    changes: Array.isArray(item.changes) ? item.changes : [],
    evidence: normalizeText(item.evidence)
  }));
}

function normalizeEventUpdateList(value: unknown): StorySnapshotEventUpdate[] {
  return normalizeRecordList(value).map((item) => ({
    ...item,
    event: normalizeText(item.event),
    impact: normalizeText(item.impact),
    evidence: normalizeText(item.evidence)
  }));
}

function normalizeRecordList(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Record<string, unknown> => isRecord(item));
}

function normalizeRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function deriveChapterRelativePath(snapshotPath: string): string {
  const normalized = normalizeRelativePath(snapshotPath);
  const match = normalized.match(/^\.storydex\/memory\/chapters\/([^/]+)\/[^/]+\.variables\.json$/i);
  return match ? `chapters/${match[1]}` : "";
}

function buildStoryErrorMessage(messages: string[]): string {
  return messages.filter((message) => Boolean(message)).join(" ");
}

function normalizeStoryError(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return error.message;
  }
  return describeTransportError(error, "Story data request failed.");
}

function normalizeText(value: unknown): string {
  return String(value || "").trim();
}

function normalizeNumber(value: unknown, fallback: number): number {
  const normalized = Number(value);
  return Number.isFinite(normalized) ? normalized : fallback;
}

function normalizeNullableNumber(value: unknown): number | null {
  const normalized = Number(value);
  return Number.isFinite(normalized) ? normalized : null;
}

function normalizeRelativePath(value: unknown): string {
  return normalizeText(value).replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
