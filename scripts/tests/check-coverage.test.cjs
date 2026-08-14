const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const script = path.resolve(__dirname, "..", "check_coverage.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-coverage-"));
  const config = {
    schemaVersion: 1,
    ciTolerance: 0.05,
    components: {
      backend: {
        metrics: { lines: 80, branches: 70 },
        critical: { "services/core.py": { lines: 85 } }
      },
      frontend: {
        metrics: { lines: 80, statements: 80, functions: 80, branches: 70 },
        critical: { "src/core.ts": { lines: 85, statements: 85, functions: 85, branches: 75 } }
      }
    }
  };
  const configPath = path.join(root, "baseline.json");
  fs.writeFileSync(configPath, JSON.stringify(config));
  return { root, configPath };
}

function run(args, env = {}) {
  return spawnSync(process.execPath, [script, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env }
  });
}

function backendSummary(lines, branches) {
  return {
    covered_lines: lines,
    num_statements: 100,
    covered_branches: branches,
    num_branches: 100
  };
}

function frontendSummary(lines, statements, functions, branches) {
  const metric = (covered) => ({ covered, total: 100, skipped: 0, pct: covered });
  return {
    lines: metric(lines),
    statements: metric(statements),
    functions: metric(functions),
    branches: metric(branches)
  };
}

test("backend gate passes a complete report and reports actual baselines", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "backend.json");
  fs.writeFileSync(reportPath, JSON.stringify({
    totals: backendSummary(80, 70),
    files: { "C:\\repo\\services\\core.py": { summary: backendSummary(85, 75) } }
  }));

  const result = run(["--component=backend", `--report=${reportPath}`, `--config=${configPath}`, "--mode=release"]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /lines=80\.00% \(baseline 80\.00%\)/);
});

test("coverage regression fails with actual, required, and adjustment instructions", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "backend.json");
  fs.writeFileSync(reportPath, JSON.stringify({
    totals: backendSummary(79, 69),
    files: { "services/core.py": { summary: backendSummary(84, 75) } }
  }));

  const result = run(["--component=backend", `--report=${reportPath}`, `--config=${configPath}`]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /actual 79\.00%, required 80\.00%/);
  assert.match(result.stderr, /edit .*baseline\.json explicitly/);
});

test("GitHub Actions failures expose the complete ratchet result as an annotation", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "backend.json");
  fs.writeFileSync(reportPath, JSON.stringify({
    totals: backendSummary(79, 69),
    files: { "services/core.py": { summary: backendSummary(84, 75) } }
  }));

  const result = run(
    ["--component=backend", `--report=${reportPath}`, `--config=${configPath}`],
    { GITHUB_ACTIONS: "true" }
  );
  assert.equal(result.status, 1);
  assert.match(result.stderr, /::error title=Coverage gate::Coverage gate failed/);
  assert.match(result.stderr, /actual 79\.00%25, required 80\.00%25/);
  assert.match(result.stderr, /%0ATo adjust an intentional baseline increase/);
});

test("advisory mode reports regressions without failing standard CI", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "frontend.json");
  fs.writeFileSync(reportPath, JSON.stringify({
    total: frontendSummary(79, 79, 79, 69),
    "src/core.ts": frontendSummary(84, 84, 84, 74)
  }));

  const result = run(
    ["--component=frontend", `--report=${reportPath}`, `--config=${configPath}`, "--mode=advisory"],
    { GITHUB_ACTIONS: "true" }
  );
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /Coverage advisory for frontend/);
  assert.match(result.stderr, /::warning title=Coverage advisory::/);
  assert.doesNotMatch(result.stderr, /::error/);

  const releaseResult = run([
    "--component=frontend",
    `--report=${reportPath}`,
    `--config=${configPath}`,
    "--mode=release"
  ]);
  assert.equal(releaseResult.status, 1);
  assert.match(releaseResult.stderr, /Coverage gate failed for frontend \(release\)/);
});

test("standard CI scopes quality jobs while full CI retains its compatibility matrix", () => {
  const workflow = fs.readFileSync(path.resolve(__dirname, "..", "..", ".github", "workflows", "quality-gate.yml"), "utf8");
  assert.match(workflow, /changes:\s*[\s\S]*?resolve_ci_scope\.cjs/);
  assert.match(workflow, /backend-tests:\s*[\s\S]*?needs:\s*changes\s+[\s\S]*?needs\.changes\.outputs\.backend/);
  assert.match(workflow, /frontend-tests:\s*[\s\S]*?needs:\s*changes\s+[\s\S]*?needs\.changes\.outputs\.frontend/);
  assert.match(workflow, /desktop-unit-tests:\s*[\s\S]*?needs:\s*changes\s+[\s\S]*?needs\.changes\.outputs\.desktop/);
  assert.match(workflow, /inputs\.full\s*&&/);
  assert.equal((workflow.match(/"coverage_ratchet":true/g) || []).length, 2);
  assert.equal((workflow.match(/"coverage_ratchet":false/g) || []).length, 2);
  assert.match(workflow, /if:\s*matrix\.coverage_ratchet/);
  assert.equal((workflow.match(/\|\| 'advisory'/g) || []).length, 2);
  assert.match(workflow, /Swatinem\/rust-cache@v2/);
  assert.match(workflow, /Build current-commit Storydex desktop Agent runtime/);
  assert.match(workflow, /Verify pinned Coomi runtime/);
  assert.doesNotMatch(workflow, /-m "not coomi_runtime"|without unchanged Coomi runtime/);
  assert.match(workflow, /backend-compatibility:\s*[\s\S]*?test_agent_stream_responsiveness\.py[\s\S]*?test_storydex_runtime_fixes\.py/);
  assert.doesNotMatch(workflow, /^  integration-tests:/m);
  assert.match(workflow, /BACKEND:\s*\$\{\{ needs\.changes\.outputs\.backend \}\}/);
  assert.match(workflow, /if backend and not full:\s*required\.add\("backend-compatibility"\)/);
  assert.match(workflow, /desktop-package-smoke:\s*[\s\S]*?if:\s*inputs\.full/);
  assert.match(workflow, /electron-e2e:\s*[\s\S]*?if:\s*inputs\.full/);

  const ci = fs.readFileSync(path.resolve(__dirname, "..", "..", ".github", "workflows", "ci.yml"), "utf8");
  const release = fs.readFileSync(path.resolve(__dirname, "..", "..", ".github", "workflows", "release-windows.yml"), "utf8");
  assert.match(ci, /full:\s*\$\{\{ inputs\.full \|\| false \}\}/);
  assert.match(release, /enforce_coverage:\s*true\s+full:\s*true/);
});

test("missing, malformed, and incomplete reports fail closed", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const missing = run(["--component=backend", `--report=${path.join(root, "missing.json")}`, `--config=${configPath}`]);
  assert.equal(missing.status, 1);
  assert.match(missing.stderr, /is missing/);

  const missingAdvisory = run([
    "--component=backend",
    `--report=${path.join(root, "missing-advisory.json")}`,
    `--config=${configPath}`,
    "--mode=advisory"
  ]);
  assert.equal(missingAdvisory.status, 1);
  assert.match(missingAdvisory.stderr, /is missing/);

  const malformedPath = path.join(root, "malformed.json");
  fs.writeFileSync(malformedPath, "{not-json");
  const malformed = run(["--component=backend", `--report=${malformedPath}`, `--config=${configPath}`]);
  assert.equal(malformed.status, 1);
  assert.match(malformed.stderr, /not valid JSON/);

  const incompletePath = path.join(root, "incomplete.json");
  fs.writeFileSync(incompletePath, JSON.stringify({ totals: {} }));
  const incomplete = run(["--component=backend", `--report=${incompletePath}`, `--config=${configPath}`]);
  assert.equal(incomplete.status, 1);
  assert.match(incomplete.stderr, /files object/);
});

test("a failed test command can never reuse a valid coverage report", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "backend.json");
  fs.writeFileSync(reportPath, JSON.stringify({
    totals: backendSummary(100, 100),
    files: { "services/core.py": { summary: backendSummary(100, 100) } }
  }));

  const result = run([
    "--component=backend",
    `--report=${reportPath}`,
    `--config=${configPath}`,
    "--test-exit-code=2"
  ]);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /stale or partial coverage report cannot pass/);
});

test("frontend json-summary metrics and critical files use the same gate", (t) => {
  const { root, configPath } = fixture();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "frontend.json");
  fs.writeFileSync(reportPath, JSON.stringify({
    total: frontendSummary(80, 80, 80, 70),
    "E:\\repo\\src\\core.ts": frontendSummary(85, 85, 85, 75)
  }));

  const result = run(["--component=frontend", `--report=${reportPath}`, `--config=${configPath}`, "--mode=release"]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /branches=70\.00%/);
});
