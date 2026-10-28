import { beforeEach, describe, expect, it, vi } from "vitest";
import { IDBObjectStore } from "fake-indexeddb";
import { draftProjectKey, readProjectDrafts, updateProjectDrafts, writeProjectDrafts } from "@/utils/draftStorage";

beforeEach(() => localStorage.clear());

describe("durable project drafts", () => {
  it("migrates legacy drafts only after committing their replacement", async () => {
    const key = "storydex.drafts:c:/story";
    const drafts = { "chapter.md": { content: "旧草稿", dirty: true } };
    localStorage.setItem(key, JSON.stringify(drafts));
    const failure = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementationOnce(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    await expect(readProjectDrafts("C:/Story")).rejects.toThrow("quota");
    expect(localStorage.getItem(key)).toBe(JSON.stringify(drafts));
    failure.mockRestore();
    expect(await readProjectDrafts("C:/Story")).toEqual(drafts);
    expect(localStorage.getItem(key)).toBeNull();
    expect(await readProjectDrafts("c:\\story")).toEqual(drafts);
  });

  it("preserves unopened drafts and commits deletions without clearing other projects", async () => {
    await writeProjectDrafts("C:/Story", { "a.md": "draft a", "b.md": "draft b" });
    await writeProjectDrafts("C:/Other", { "a.md": "other project" });
    await updateProjectDrafts("C:/Story", {});
    await updateProjectDrafts("C:/Story", { "a.md": null, "c.md": "draft c" });
    expect(await readProjectDrafts("C:/Story")).toEqual({ "b.md": "draft b", "c.md": "draft c" });
    expect(await readProjectDrafts("C:/Other")).toEqual({ "a.md": "other project" });
    expect(draftProjectKey("/Story")).not.toBe(draftProjectKey("/story"));
  });

  it("keeps the previous committed backup if a transaction aborts", async () => {
    await writeProjectDrafts("C:/Story", { "a.md": "safe backup" });
    await expect(updateProjectDrafts("C:/Story", { "a.md": () => "not cloneable" })).rejects.toThrow();
    expect(await readProjectDrafts("C:/Story")).toEqual({ "a.md": "safe backup" });
    await updateProjectDrafts("C:/Story", { "a.md": "recovered" });
    expect(await readProjectDrafts("C:/Story")).toEqual({ "a.md": "recovered" });
  });
});
