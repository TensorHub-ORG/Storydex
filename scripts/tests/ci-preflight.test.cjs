"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

function readSourceTree(relativeRoot) {
  const sourceRoot = path.join(root, relativeRoot);
  const chunks = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.name === "target") continue;
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(absolute);
      } else if (/\.(?:rs|toml|json|md)$/i.test(entry.name)) {
        chunks.push(fs.readFileSync(absolute, "utf8"));
      }
    }
  };
  visit(sourceRoot);
  return chunks.join("\n");
}

test("versioned pre-push hook fails closed through the PowerShell guard", () => {
  const hook = read(".githooks/pre-push");
  const attributes = read(".gitattributes");
  assert.match(hook, /^#!\/bin\/sh/m);
  assert.match(hook, /set -eu/);
  assert.match(hook, /run_pre_push_ci\.ps1/);
  assert.match(hook, /command -v pwsh/);
  assert.match(hook, /command -v powershell\.exe/);
  assert.match(hook, /exit 1/);
  assert.match(attributes, /^\.githooks\/\* text eol=lf$/m);
});

test("pre-push stays lightweight and leaves component suites to GitHub Actions", () => {
  const guard = read("scripts/run_pre_push_ci.ps1");
  assert.match(guard, /validate_text_encoding\.cjs/);
  assert.match(guard, /validate_version_consistency\.cjs/);
  assert.match(guard, /Conflict markers/);
  assert.match(guard, /merge-base/);
  assert.match(guard, /diff --check/);
  assert.match(guard, /Component test suites run in GitHub Actions/);
  assert.doesNotMatch(guard, /run_full_test_suite|pytest|cargo test|npm test|storydex-ci-preflight/);
});

test("development branches use lightweight CI while main keeps the remote full gate", () => {
  const guard = read("scripts/run_pre_push_ci.ps1");
  const ci = read(".github/workflows/ci.yml");
  const developmentCi = read(".github/workflows/dev-ci.yml");
  assert.match(guard, /full local pre-push gate has been retired/);
  assert.match(ci, /pull_request:\s*\n\s*branches: \[main\]/);
  assert.match(ci, /push:\s*\n\s*branches: \[main\]/);
  assert.match(developmentCi, /dev-flowby/);
  assert.match(developmentCi, /dev\/windows/);
  assert.match(developmentCi, /dev\/android/);
  assert.match(developmentCi, /Run basic repository checks/);
  assert.match(developmentCi, /Test CI policies/);
  assert.match(developmentCi, /windows-tests:[\s\S]*?runs-on: windows-latest/);
  assert.match(developmentCi, /android-tests:[\s\S]*?runs-on: ubuntu-latest/);
  assert.match(developmentCi, /cargo test --manifest-path apps\/desktop\/agent-runtime\/Cargo\.toml/);
  assert.match(developmentCi, /cargo test --manifest-path apps\/android\/agent-runtime\/Cargo\.toml/);
  assert.doesNotMatch(developmentCi, /pytest|coverage|electron-e2e|package:win/);
});

test("Agent runtimes are owned by their platforms without cross-source dependencies", () => {
  const desktopRoot = "apps/desktop/agent-runtime";
  const androidRoot = "apps/android/agent-runtime";
  assert.ok(fs.existsSync(path.join(root, desktopRoot, "Cargo.toml")));
  assert.ok(fs.existsSync(path.join(root, androidRoot, "Cargo.toml")));
  assert.equal(fs.existsSync(path.join(root, "apps/desktop/coomi-rs-desktop")), false);
  assert.equal(fs.existsSync(path.join(root, "apps/desktop/coomi-rs-android")), false);

  const desktopSources = readSourceTree(desktopRoot);
  const androidSources = readSourceTree(androidRoot);
  assert.doesNotMatch(desktopSources, /apps[\\/]android[\\/]agent-runtime/);
  assert.doesNotMatch(androidSources, /apps[\\/]desktop[\\/]agent-runtime/);

  const gradle = read("apps/android/app/build.gradle");
  const bridge = read("apps/backend/services/coomi_bridge_client.py");
  assert.match(gradle, /rootProject\.file\("agent-runtime"\)/);
  assert.match(bridge, /"desktop" \/ "agent-runtime"/);
});

test("hook installer is repository local and agent rules require remote success", () => {
  const installer = read("scripts/install_git_hooks.ps1");
  const rules = read("AGENTS.md");
  assert.match(installer, /config --local core\.hooksPath \.githooks/);
  assert.doesNotMatch(installer, /config --global/);
  assert.match(rules, /run_pre_push_ci\.ps1/);
  assert.match(rules, /install_git_hooks\.ps1/);
  assert.match(rules, /--no-verify/);
  assert.match(rules, /dev-flowby/);
  assert.match(rules, /不运行 Backend/);
  assert.match(rules, /GitHub Actions/);
  assert.match(rules, /success/);
});

test("local Fast suite covers CI policy regressions and runtime commit identity", () => {
  const suite = read("scripts/run_full_test_suite.ps1");
  const qualityGate = read(".github/workflows/quality-gate.yml");
  assert.match(suite, /resolve-ci-scope\.test\.cjs/);
  assert.match(suite, /ci-preflight\.test\.cjs/);
  assert.match(suite, /STORYDEX_COOMI_GIT_SHA/);
  assert.match(suite, /import main; assert main\.app\.title/);
  assert.match(suite, /\$Mode -eq "Release"\) \{ "release" \} else \{ "advisory" \}/);
  assert.match(suite, /Environment preflight/);
  assert.match(suite, /Assert-NpmDependencies/);
  assert.match(suite, /\$runBackend -and -not \$runCoomi/);
  assert.match(suite, /Build current-commit Storydex Coomi desktop runtime/);
  assert.match(suite, /python -m pytest -q --timeout=120/);
  assert.doesNotMatch(suite, /not coomi_runtime/);
  assert.match(qualityGate, /pc-runtime-tests:[\s\S]*?Build current-commit Storydex PC Agent runtime/);
  const backendJob = qualityGate.split(/\r?\n  backend-tests:/)[1].split(/\r?\n  pc-runtime-tests:/)[0];
  assert.doesNotMatch(backendJob, /cargo test --manifest-path apps\/desktop\/agent-runtime\/Cargo\.toml/);
  assert.doesNotMatch(qualityGate, /without unchanged Coomi runtime|not coomi_runtime/);
  assert.ok(
    suite.indexOf('Invoke-Step "Environment preflight"') < suite.indexOf('Invoke-Step "Backend tests and coverage"'),
    "dependency preflight must run before the expensive backend suite",
  );
});
