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
    tauri: false,
    android: false,
    coomi: false,
    pc_runtime: false,
    feedback: false,
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
  assert.equal(result.pc_runtime, false);
  assert.equal(result.feedback, false);
});

test("PC runtime changes use focused runtime checks without backend coverage", () => {
  const bridge = classifyChangedPaths(["apps/backend/services/coomi_bridge_client.py"]);
  assert.equal(bridge.backend, true);
  assert.equal(bridge.coomi, true);
  assert.equal(bridge.pc_runtime, true);

  const rust = classifyChangedPaths(["apps/desktop/agent-runtime/storydex-bridge/src/main.rs"]);
  assert.equal(rust.backend, false);
  assert.equal(rust.desktop, false);
  assert.equal(rust.tauri, false);
  assert.equal(rust.coomi, true);
  assert.equal(rust.pc_runtime, true);
  assert.equal(rust.android, false);

  const android = classifyChangedPaths(["apps/android/agent-runtime/ui/src/web.rs"]);
  assert.equal(android.backend, false);
  assert.equal(android.android, true);
  assert.equal(android.desktop, false);
  assert.equal(android.coomi, true);
  assert.equal(android.pc_runtime, false);
});

test("desktop and documentation changes stay scoped", () => {
  const desktop = classifyChangedPaths(["apps/desktop/tauri-preview/src/main.rs"]);
  assert.equal(desktop.desktop, true);
  assert.equal(desktop.tauri, true);
  assert.equal(desktop.backend, false);
  assert.equal(desktop.frontend, false);

  const docs = classifyChangedPaths(["README.md", "docs/release.md"]);
  assert.equal(docs.docsOnly, true);
  assert.equal(docs.backend, false);
  assert.equal(docs.frontend, false);
  assert.equal(docs.desktop, false);
  assert.equal(docs.android, false);
  assert.equal(docs.pc_runtime, false);
  assert.equal(docs.feedback, false);
});

test("feedback and release helper changes run only their owning checks", () => {
  const feedback = classifyChangedPaths(["deploy/storydex-feedback/server.py"]);
  assert.equal(feedback.feedback, true);
  assert.equal(feedback.backend, false);
  assert.equal(feedback.frontend, false);
  assert.equal(feedback.desktop, false);
  assert.equal(feedback.android, false);
  assert.equal(feedback.pc_runtime, false);

  const release = classifyChangedPaths(["scripts/prepare_tauri_release_bundle.ps1"]);
  assert.equal(release.desktop, true);
  assert.equal(release.tauri, false);
  assert.equal(release.backend, false);
  assert.equal(release.android, false);
  assert.equal(release.feedback, false);
});

test("CI policy, unknown, empty, and forced scopes follow their intended safety profiles", () => {
  const unknown = classifyChangedPaths(["new-component/main.ts"]);
  assert.deepEqual(unknown.unknownPaths, ["new-component/main.ts"]);

  const ciPolicy = classifyChangedPaths([".github/workflows/quality-gate.yml"]);
  assert.equal(ciPolicy.backend, false);
  assert.equal(ciPolicy.frontend, false);
  assert.equal(ciPolicy.desktop, false);
  assert.equal(ciPolicy.android, false);
  assert.equal(ciPolicy.coomi, false);
  assert.equal(ciPolicy.pc_runtime, false);
  assert.equal(ciPolicy.feedback, false);
  assert.equal(ciPolicy.docsOnly, false);
  assert.equal(ciPolicy.reason, "ci-policy-only");

  const agentRules = classifyChangedPaths(["AGENTS.md"]);
  assert.equal(agentRules.docsOnly, true);
  assert.equal(agentRules.reason, "documentation-only");

  for (const result of [
    unknown,
    classifyChangedPaths([]),
    classifyChangedPaths(["README.md"], { forceAll: true }),
  ]) {
    assert.equal(result.backend, true);
    assert.equal(result.frontend, true);
    assert.equal(result.desktop, true);
    assert.equal(result.tauri, true);
    assert.equal(result.android, true);
    assert.equal(result.coomi, true);
    assert.equal(result.pc_runtime, true);
    assert.equal(result.feedback, true);
  }
});

test("packaging scripts and CI evidence changes avoid unrelated Rust test suites", () => {
  const packaging = classifyChangedPaths(["apps/desktop/tauri-preview/scripts/prepare-preview.ps1"]);
  assert.equal(packaging.desktop, true);
  assert.equal(packaging.tauri, false);
  assert.equal(packaging.pc_runtime, false);
  const policy = classifyChangedPaths(["scripts/resolve_ci_evidence.cjs", "scripts/tests/resolve-ci-evidence.test.cjs"]);
  assert.equal(policy.reason, "ci-policy-only");
  assert.equal(policy.android, false);
  const website = classifyChangedPaths(["apps/website-overlay/storydex-android-download-v0.1.4.js"]);
  assert.equal(website.desktop, true);
  assert.equal(website.pc_runtime, false);
  assert.equal(website.tauri, false);
  assert.equal(website.android, false);
  assert.equal(website.backend, false);
  assert.deepEqual(website.unknownPaths, []);
});
