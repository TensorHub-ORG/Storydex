"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
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

test("pre-push certification is clean-tree and commit specific", () => {
  const guard = read("scripts/run_pre_push_ci.ps1");
  assert.match(guard, /diff", "--quiet"/);
  assert.match(guard, /diff", "--cached", "--quiet"/);
  assert.match(guard, /rev-parse HEAD/);
  assert.match(guard, /storydex-ci-preflight\.json/);
  assert.match(guard, /headSha -eq \$headSha/);
  assert.match(guard, /resolve_ci_scope\.cjs/);
  assert.match(guard, /merge-base/);
  assert.match(guard, /baseSha -eq \$baseIdentity/);
  assert.match(guard, /scope -eq \$scopeKey/);
  assert.match(guard, /run_full_test_suite\.ps1/);
  assert.match(guard, /-Mode Fast/);
  assert.match(guard, /-Scope \$scopeNames\.ToArray\(\)/);
});

test("hook installer is repository local and agent rules require remote success", () => {
  const installer = read("scripts/install_git_hooks.ps1");
  const rules = read("AGENTS.md");
  assert.match(installer, /config --local core\.hooksPath \.githooks/);
  assert.doesNotMatch(installer, /config --global/);
  assert.match(rules, /run_pre_push_ci\.ps1/);
  assert.match(rules, /install_git_hooks\.ps1/);
  assert.match(rules, /--no-verify/);
  assert.match(rules, /GitHub Actions/);
  assert.match(rules, /success/);
});

test("local Fast suite covers CI policy regressions and runtime commit identity", () => {
  const suite = read("scripts/run_full_test_suite.ps1");
  assert.match(suite, /resolve-ci-scope\.test\.cjs/);
  assert.match(suite, /ci-preflight\.test\.cjs/);
  assert.match(suite, /STORYDEX_COOMI_GIT_SHA/);
  assert.match(suite, /import main; assert main\.app\.title/);
  assert.match(suite, /\$Mode -eq "Release"\) \{ "release" \} else \{ "advisory" \}/);
  assert.match(suite, /Environment preflight/);
  assert.match(suite, /Assert-NpmDependencies/);
  assert.match(suite, /not coomi_runtime/);
  assert.ok(
    suite.indexOf('Invoke-Step "Environment preflight"') < suite.indexOf('Invoke-Step "Backend tests and coverage"'),
    "dependency preflight must run before the expensive backend suite",
  );
});
