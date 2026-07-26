import { describe, it, expect } from "vitest";

interface WorkspaceTreeNode {
  name: string;
  relativePath: string | null;
  kind: "directory" | "file";
  children: WorkspaceTreeNode[];
}

function flattenWorkspaceFiles(nodes: WorkspaceTreeNode[], collected: WorkspaceTreeNode[] = []): WorkspaceTreeNode[] {
  for (const node of nodes) {
    if (node.kind === "file" && node.relativePath) {
      collected.push(node);
    }
    if (node.children?.length) {
      flattenWorkspaceFiles(node.children, collected);
    }
  }
  return collected;
}

function commandMatches(haystack: string, query: string): boolean {
  const normalized = haystack.toLowerCase();
  return query.toLowerCase().split(/\s+/).every((token) => normalized.includes(token));
}

describe("Command Search Functions", () => {
  describe("flattenWorkspaceFiles", () => {
    it("should extract all files from nested tree structure", () => {
      const tree: WorkspaceTreeNode[] = [
        {
          name: "chapters",
          relativePath: "chapters",
          kind: "directory",
          children: [
            {
              name: "chapter1.md",
              relativePath: "chapters/chapter1.md",
              kind: "file",
              children: [],
            },
            {
              name: "chapter2.md",
              relativePath: "chapters/chapter2.md",
              kind: "file",
              children: [],
            },
          ],
        },
        {
          name: "settings",
          relativePath: "settings",
          kind: "directory",
          children: [
            {
              name: "config.json",
              relativePath: "settings/config.json",
              kind: "file",
              children: [],
            },
          ],
        },
      ];

      const files = flattenWorkspaceFiles(tree);
      expect(files).toHaveLength(3);
      expect(files.map((f) => f.relativePath)).toEqual([
        "chapters/chapter1.md",
        "chapters/chapter2.md",
        "settings/config.json",
      ]);
    });

    it("should ignore directories", () => {
      const tree: WorkspaceTreeNode[] = [
        {
          name: "empty",
          relativePath: "empty",
          kind: "directory",
          children: [],
        },
      ];

      const files = flattenWorkspaceFiles(tree);
      expect(files).toHaveLength(0);
    });

    it("should ignore files without relativePath", () => {
      const tree: WorkspaceTreeNode[] = [
        {
          name: "file.txt",
          relativePath: null,
          kind: "file",
          children: [],
        },
      ];

      const files = flattenWorkspaceFiles(tree);
      expect(files).toHaveLength(0);
    });
  });

  describe("commandMatches", () => {
    it("should match single token", () => {
      expect(commandMatches("打开文件浏览器", "文件")).toBe(true);
      expect(commandMatches("Open File Explorer", "file")).toBe(true);
    });

    it("should match multiple tokens", () => {
      expect(commandMatches("打开文件浏览器", "打开 文件")).toBe(true);
      expect(commandMatches("Open File Explorer", "open explorer")).toBe(true);
    });

    it("should be case insensitive", () => {
      expect(commandMatches("Open File Explorer", "OPEN FILE")).toBe(true);
      expect(commandMatches("Open File Explorer", "open file")).toBe(true);
    });

    it("should not match when token is missing", () => {
      expect(commandMatches("打开文件浏览器", "打开 关闭")).toBe(false);
      expect(commandMatches("Open File Explorer", "open close")).toBe(false);
    });

    it("should handle empty query", () => {
      expect(commandMatches("打开文件浏览器", "")).toBe(true);
    });

    it("should match partial words", () => {
      expect(commandMatches("chapter01.md", "chap")).toBe(true);
      expect(commandMatches("settings/config.json", "sett conf")).toBe(true);
    });
  });
});
