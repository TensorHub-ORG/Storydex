const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  inspectCandidateRoot,
  loadPolicy,
  normalizePolicy,
  parseArgs
} = require("../scripts/validate-rust-candidate-assets.cjs");

function withCandidateFixture(callback) {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-rust-candidate-policy-"));
  const candidateRoot = path.join(projectRoot, "tauri-preview", "staging");
  fs.mkdirSync(candidateRoot, { recursive: true });
  try {
    return callback({ projectRoot, candidateRoot });
  } finally {
    fs.rmSync(projectRoot, { recursive: true, force: true });
  }
}

function policyForTests() {
  return loadPolicy().policy;
}

test("candidate policy accepts Rust sidecar and static frontend assets", () => {
  withCandidateFixture(({ projectRoot, candidateRoot }) => {
    fs.mkdirSync(path.join(candidateRoot, "binaries"));
    fs.writeFileSync(path.join(candidateRoot, "binaries", "storydex-agentd-x86_64-pc-windows-msvc.exe"), "rust sidecar");
    fs.mkdirSync(path.join(candidateRoot, "dist"));
    fs.writeFileSync(path.join(candidateRoot, "dist", "index.html"), "<!doctype html>");
    fs.writeFileSync(path.join(candidateRoot, "dist", "app.js"), "console.log('tauri');");

    const report = inspectCandidateRoot(candidateRoot, { projectRoot, policy: policyForTests() });
    assert.equal(report.ok, true);
    assert.equal(report.filesScanned, 3);
    assert.deepEqual(report.violations, []);
  });
});

test("candidate policy rejects Python and Electron/Node runtime assets", () => {
  withCandidateFixture(({ projectRoot, candidateRoot }) => {
    fs.mkdirSync(path.join(candidateRoot, "python-env", "site-packages", "fastapi"), { recursive: true });
    fs.writeFileSync(path.join(candidateRoot, "python-env", "python.exe"), "python");
    fs.writeFileSync(path.join(candidateRoot, "python-env", "site-packages", "fastapi", "__init__.py"), "# fastapi");
    fs.mkdirSync(path.join(candidateRoot, "node_modules", "electron"), { recursive: true });
    fs.writeFileSync(path.join(candidateRoot, "node_modules", "electron", "electron.exe"), "electron");

    const report = inspectCandidateRoot(candidateRoot, { projectRoot, policy: policyForTests() });
    assert.equal(report.ok, false);
    assert.ok(report.violations.some((item) => item.code === "forbidden-path-token" && item.path.includes("python-env")));
    assert.ok(report.violations.some((item) => item.code === "forbidden-file-name" && item.path.endsWith("python.exe")));
    assert.ok(report.violations.some((item) => item.code === "forbidden-file-extension" && item.path.endsWith("__init__.py")));
    assert.ok(report.violations.some((item) => item.code === "forbidden-path-token" && item.path.includes("node_modules")));
  });
});

test("candidate policy rejects overlap with Stable roots and repository escape", () => {
  withCandidateFixture(({ projectRoot, candidateRoot }) => {
    const stableRoot = path.join(projectRoot, "stable-electron");
    fs.mkdirSync(stableRoot, { recursive: true });
    const policy = normalizePolicy({
      schemaVersion: 1,
      candidate: "test",
      forbiddenPathTokens: [],
      forbiddenFileNames: [],
      forbiddenExtensions: [],
      stableRoots: ["stable-electron"]
    });
    const overlap = inspectCandidateRoot(stableRoot, { projectRoot, policy });
    assert.equal(overlap.ok, false);
    assert.ok(overlap.violations.some((item) => item.code === "stable-root-overlap"));

    const outside = inspectCandidateRoot(path.join(os.tmpdir(), "storydex-user-project"), {
      projectRoot,
      policy
    });
    assert.equal(outside.ok, false);
    assert.ok(outside.violations.some((item) => item.code === "root-outside-repository"));
    void candidateRoot;
  });
});

test("CLI parsing accepts explicit candidate root and JSON output flag", () => {
  assert.deepEqual(parseArgs(["--root", "apps/desktop/candidate/staging", "--manifest", "policy.json", "--json"]), {
    root: "apps/desktop/candidate/staging",
    policy: "policy.json",
    json: true
  });
  assert.throws(() => parseArgs(["--unexpected"]), /unknown argument/);
});

test("candidate policy fails closed for a missing root argument", () => {
  const report = inspectCandidateRoot(undefined, { projectRoot: process.cwd(), policy: policyForTests() });
  assert.equal(report.ok, false);
  assert.ok(report.violations.some((item) => item.code === "invalid-root"));
});
