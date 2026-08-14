"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { classifyChangedPaths } = require("../resolve_ci_scope.cjs");

test("frontend-only changes run only frontend quality checks", () => {
  assert.deepEqual(classifyChangedPaths([
    "apps/frontend/src/components/StoryStatePanel.vue",
    "apps/frontend/tests/StoryStatePanel.test.ts",
  ]), {
    backend: false,
    frontend: true,
    desktop: false,
    android: false,
    coomi: false,
    docsOnly: false,
    changedCount: 2,
    unknownPaths: [],
    reason: "path-classified",
  });
});

test("ordinary backend changes keep full Python checks without unrelated Rust work", () => {
  const result = classifyChangedPaths(["apps/backend/services/story_wiki_service.py"]);
  assert.equal(result.backend, true);
  assert.equal(result.frontend, false);
  assert.equal(result.desktop, false);
  assert.equal(result.android, false);
  assert.equal(result.coomi, false);
});

test("Coomi bridge and Rust changes restore runtime and packaging checks", () => {
  const bridge = classifyChangedPaths(["apps/backend/services/coomi_bridge_client.py"]);
  assert.equal(bridge.backend, true);
  assert.equal(bridge.coomi, true);

  const rust = classifyChangedPaths(["apps/desktop/agent-runtime/storydex-bridge/src/main.rs"]);
  assert.equal(rust.backend, true);
  assert.equal(rust.desktop, true);
  assert.equal(rust.coomi, true);

  const android = classifyChangedPaths(["apps/android/agent-runtime/ui/src/web.rs"]);
  assert.equal(android.backend, false);
  assert.equal(android.android, true);
  assert.equal(android.desktop, false);
  assert.equal(android.coomi, true);
});

test("desktop and documentation changes stay scoped", () => {
  const desktop = classifyChangedPaths(["apps/desktop/electron/main.cjs"]);
  assert.equal(desktop.desktop, true);
  assert.equal(desktop.backend, false);
  assert.equal(desktop.frontend, false);

  const docs = classifyChangedPaths(["README.md", "docs/release.md"]);
  assert.equal(docs.docsOnly, true);
  assert.equal(docs.backend, false);
  assert.equal(docs.frontend, false);
  assert.equal(docs.desktop, false);
  assert.equal(docs.android, false);
});

test("workflow, classifier, unknown, empty, and forced scopes fail safe", () => {
  const unknown = classifyChangedPaths(["new-component/main.ts"]);
  assert.deepEqual(unknown.unknownPaths, ["new-component/main.ts"]);

  for (const result of [
    classifyChangedPaths([".github/workflows/quality-gate.yml"]),
    classifyChangedPaths(["scripts/resolve_ci_scope.cjs"]),
    unknown,
    classifyChangedPaths([]),
    classifyChangedPaths(["README.md"], { forceAll: true }),
  ]) {
    assert.equal(result.backend, true);
    assert.equal(result.frontend, true);
    assert.equal(result.desktop, true);
    assert.equal(result.android, true);
    assert.equal(result.coomi, true);
  }
});
