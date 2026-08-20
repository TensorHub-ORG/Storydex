"use strict";

const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
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

test("Rust dependency audit covers the Agent workspace and the Windows Tauri runtime", () => {
  const audit = read("scripts/run_rust_dependency_audit.ps1");
  assert.match(audit, /apps\/desktop\/agent-runtime\/Cargo\.toml/);
  assert.match(audit, /apps\/desktop\/tauri-preview\/Cargo\.toml/);
  assert.match(audit, /--target x86_64-pc-windows-msvc/);
  assert.doesNotMatch(audit, /--exclude|advisories\.ignore/);
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
  assert.match(developmentCi, /Check Tauri Stable shell/);
});

test("Agent runtimes are owned by their platforms without cross-source dependencies", () => {
  const desktopRoot = "apps/desktop/agent-runtime";
  const androidRoot = "apps/android/agent-runtime";
  assert.ok(fs.existsSync(path.join(root, desktopRoot, "Cargo.toml")));
  assert.ok(fs.existsSync(path.join(root, androidRoot, "Cargo.toml")));
  const legacySources = execFileSync(
    "git",
    ["ls-files", "--", "apps/desktop/coomi-rs-desktop", "apps/desktop/coomi-rs-android"],
    { cwd: root, encoding: "utf8" },
  ).trim();
  assert.equal(legacySources, "");

  const desktopSources = readSourceTree(desktopRoot);
  const androidSources = readSourceTree(androidRoot);
  assert.doesNotMatch(desktopSources, /apps[\\/]android[\\/]agent-runtime/);
  assert.doesNotMatch(androidSources, /apps[\\/]desktop[\\/]agent-runtime/);

  const desktopUiMain = read(`${desktopRoot}/ui/src/main.rs`);
  const androidUiMain = read(`${androidRoot}/ui/src/main.rs`);
  assert.equal(fs.existsSync(path.join(root, desktopRoot, "ui/src/web.rs")), false);
  assert.ok(fs.existsSync(path.join(root, androidRoot, "ui/src/web.rs")));
  assert.doesNotMatch(desktopUiMain, /Command::Serve|Android WebView|mod web/);
  assert.match(androidUiMain, /Command::Serve/);
  assert.match(read(`${androidRoot}/ui/src/web.rs`), /Coomi Mobile for Storydex/);

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

test("website overlay deployments invalidate cached Windows download links", () => {
  const workflow = read(".github/workflows/deploy-android.yml");
  assert.match(workflow, /overlay_revision:/);
  assert.match(workflow, /OVERLAY_REVISION: \$\{\{ inputs\.overlay_revision \}\}/);
  assert.match(workflow, /overlay_url="\/assets\/\$overlay_name\?revision=\$OVERLAY_REVISION"/);
  assert.match(workflow, /injector_tmp.*\$overlay_url/);
  assert.match(workflow, /grep -Fq '\$overlay_script_url'/);
});

test("local Fast suite covers CI policy regressions and runtime commit identity", () => {
  const suite = read("scripts/run_full_test_suite.ps1");
  const qualityGate = read(".github/workflows/quality-gate.yml");
  const runtimeVerifier = read("scripts/verify_coomi_runtime.py");
  assert.match(suite, /resolve-ci-scope\.test\.cjs/);
  assert.match(suite, /ci-preflight\.test\.cjs/);
  assert.match(suite, /STORYDEX_COOMI_GIT_SHA/);
  assert.match(suite, /import main; assert main\.app\.title/);
  assert.match(suite, /\$Mode -eq "Release"\) \{ "release" \} else \{ "advisory" \}/);
  assert.match(suite, /Environment preflight/);
  assert.match(suite, /Assert-NpmDependencies/);
  assert.doesNotMatch(suite, /\$runBackend -and -not \$runCoomi|Build current-commit Storydex Coomi desktop runtime/);
  assert.match(suite, /python -m pytest -q -m "not coomi_runtime" --timeout=120/);
  assert.match(suite, /Pinned Coomi runtime and backend contract[\s\S]*?verify_coomi_runtime\.py/);
  assert.match(qualityGate, /pc-runtime-tests:[\s\S]*?Build current-commit Storydex PC Agent runtime[\s\S]*?Verify pinned runtime and backend contract/);
  assert.match(runtimeVerifier, /BRIDGE_PROTOCOL_VERSION/);
  assert.match(runtimeVerifier, /protocolVersion/);
  assert.match(runtimeVerifier, /bridge_command\(\)/);
  const backendJob = qualityGate.split(/\r?\n  backend-tests:/)[1].split(/\r?\n  pc-runtime-tests:/)[0];
  assert.doesNotMatch(backendJob, /rust-toolchain|rust-cache|cargo (?:build|run|test)/);
  assert.match(backendJob, /-m "not coomi_runtime"/);
  assert.doesNotMatch(qualityGate, /without unchanged Coomi runtime/);
  assert.ok(
    suite.indexOf('Invoke-Step "Environment preflight"') < suite.indexOf('Invoke-Step "Backend tests and coverage"'),
    "dependency preflight must run before the expensive backend suite",
  );
});
