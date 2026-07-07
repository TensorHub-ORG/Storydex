import assert from "node:assert/strict";
import { resolveMarkdownWorkspaceHref } from "../src/utils/workspaceLinks";

assert.equal(resolveMarkdownWorkspaceHref("001.md", "chapters/index.md"), "chapters/001.md");
assert.equal(resolveMarkdownWorkspaceHref("./002.md#part-2", "chapters/index.md"), "chapters/002.md");
assert.equal(resolveMarkdownWorkspaceHref("../meta/story.md?preview=1", "chapters/draft/index.md"), "chapters/meta/story.md");
assert.equal(resolveMarkdownWorkspaceHref("/root.md", "chapters/index.md"), "root.md");
assert.equal(resolveMarkdownWorkspaceHref("https://example.com/001.md", "chapters/index.md"), null);
assert.equal(resolveMarkdownWorkspaceHref("#local-heading", "chapters/index.md"), null);
assert.equal(resolveMarkdownWorkspaceHref("../../escape.md", "chapters/index.md"), null);

console.log("workspace markdown link resolver regression passed");
