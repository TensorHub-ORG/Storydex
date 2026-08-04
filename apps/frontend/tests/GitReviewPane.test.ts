import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import GitReviewPane from "@/components/GitReviewPane.vue";
import type { WorkspaceGitDiffResponse } from "@/types/workspace";

function buildDiff(added = 4): WorkspaceGitDiffResponse {
  return {
    available: true,
    gitInstalled: true,
    initialized: true,
    branch: "develop",
    files: ["chapters/a.md", "chapters/b.md", "chapters/c.md", "chapters/d.md"].map(
      (relativePath, fileIndex) => ({
        relativePath,
        status: fileIndex === 0 ? "??" : "M",
        added,
        removed: fileIndex,
        truncated: false,
        hunks: [{
          header: "@@ -0,0 +1,10 @@",
          oldStart: 0,
          oldLines: 0,
          newStart: 1,
          newLines: 1,
          lines: [{
            kind: "added",
            oldLine: null,
            newLine: 1,
            content: `content ${fileIndex}`
          }]
        }]
      })
    ),
    totals: { files: 4, added: added * 4, removed: 6 },
    message: ""
  };
}

describe("GitReviewPane", () => {
  it("keeps manually expanded files open when refreshed data has the same paths", async () => {
    const wrapper = mount(GitReviewPane, {
      props: { diff: buildDiff(), focusPath: "chapters/a.md" }
    });

    const rows = wrapper.findAll(".git-review-file-row");
    expect(rows[0].attributes("aria-expanded")).toBe("true");
    await rows[1].trigger("click");
    expect(rows[1].attributes("aria-expanded")).toBe("true");

    await wrapper.setProps({ diff: buildDiff(8) });
    const refreshedRows = wrapper.findAll(".git-review-file-row");
    expect(refreshedRows[0].attributes("aria-expanded")).toBe("true");
    expect(refreshedRows[1].attributes("aria-expanded")).toBe("true");

    wrapper.unmount();
  });

  it("does not render raw unified-diff hunk headers", () => {
    const wrapper = mount(GitReviewPane, {
      props: { diff: buildDiff(), focusPath: "chapters/a.md" }
    });

    expect(wrapper.text()).not.toContain("@@ -0,0 +1,10 @@");
    expect(wrapper.find(".git-review-hunk-head").exists()).toBe(false);

    wrapper.unmount();
  });
});
