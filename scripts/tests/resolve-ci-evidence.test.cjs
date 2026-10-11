"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { collectVerifiedChecks } = require("../resolve_ci_evidence.cjs");
const sha = "a".repeat(40);
const repository = "example/storydex";
const run = { id: 10, run_attempt: 1, head_sha: sha, conclusion: "success", head_branch: "dev/windows",
  head_repository: { full_name: repository }, event: "push" };

function queryFor(runs, jobs) {
  return async (route) => {
    if (route.includes("/jobs?")) return { jobs };
    return { workflow_runs: route.includes("dev-ci.yml") ? runs : [] };
  };
}

test("only successful jobs for the exact commit and trusted branch can be reused", async () => {
  const verified = await collectVerifiedChecks({ sha, repository, getJson: queryFor([run], [
    { name: "windows-quality-gate / pc-runtime-tests", status: "completed", conclusion: "success" },
    { name: "windows-quality-gate / frontend-tests", status: "completed", conclusion: "skipped" },
    { name: "windows-quality-gate / tauri-tests", status: "completed", conclusion: "failure" },
    { name: "windows-quality-gate / quality-gate-summary", status: "completed", conclusion: "success" },
  ]) });
  assert.deepEqual(verified, { pc_runtime: true, frontend: false, desktop: false, tauri: false });
});

test("a stale commit, fork, pull request, failed run, or different branch proves nothing", async () => {
  for (const override of [
    { head_sha: "b".repeat(40) }, { head_repository: { full_name: "fork/storydex" } },
    { event: "pull_request" }, { conclusion: "failure" }, { head_branch: "other" },
  ]) {
    const verified = await collectVerifiedChecks({ sha, repository, getJson: queryFor([{ ...run, ...override }], [
      { name: "pc-runtime-tests", status: "completed", conclusion: "success" },
    ]) });
    assert.ok(Object.values(verified).every((value) => !value));
  }
});

test("missing API evidence falls back to executing checks", async () => {
  const verified = await collectVerifiedChecks({ sha, repository, getJson: async () => { throw new Error("unavailable"); } });
  assert.ok(Object.values(verified).every((value) => !value));
});

test("job evidence is paginated and stays attached to the successful run attempt", async () => {
  const calls = [];
  const verified = await collectVerifiedChecks({ sha, repository, getJson: async (route) => {
    calls.push(route);
    if (!route.includes("/jobs?")) return { workflow_runs: route.includes("dev-ci.yml") ? [run] : [] };
    assert.ok(route.includes("/attempts/1/jobs"));
    return { jobs: route.endsWith("page=1") ? Array(100).fill({ name: "unrelated", conclusion: "success" })
      : [{ name: "windows-quality-gate / desktop-unit-tests", status: "completed", conclusion: "success" }] };
  } });
  assert.equal(verified.desktop, true);
  assert.ok(calls.some((route) => route.endsWith("page=2")));
});
