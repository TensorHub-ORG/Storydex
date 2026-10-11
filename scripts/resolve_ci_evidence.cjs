"use strict";

const fs = require("node:fs");

const WINDOWS_CHECKS = {
  pc_runtime: "pc-runtime-tests",
  frontend: "frontend-tests",
  desktop: "desktop-unit-tests",
  tauri: "tauri-tests",
};

// Only actual successful component jobs are evidence. A green workflow with
// skipped jobs (including one which reused another run) proves no extra checks.
async function collectVerifiedChecks({ sha, repository, getJson, warn = () => {} }) {
  const verified = Object.fromEntries(Object.keys(WINDOWS_CHECKS).map((key) => [key, false]));
  const workflows = [
    ["dev-ci.yml", "dev/windows"],
    ["ci.yml", "main"],
  ];
  const listings = await Promise.allSettled(workflows.map(([workflow, branch]) =>
    getJson(`/repos/${repository}/actions/workflows/${workflow}/runs?branch=${encodeURIComponent(branch)}&head_sha=${sha}&status=completed&per_page=20`)));
  for (let index = 0; index < listings.length; index += 1) {
    const listing = listings[index];
    if (listing.status !== "fulfilled") {
      warn(`Previous ${workflows[index][0]} results unavailable; affected checks will run.`);
      continue;
    }
    const runs = (listing.value.workflow_runs || []).filter((run) =>
      run.head_sha === sha && run.conclusion === "success"
      && run.head_branch === workflows[index][1]
      && run.head_repository?.full_name === repository
      && ["push", "workflow_dispatch"].includes(run.event)
      && Number.isSafeInteger(run.id) && Number.isSafeInteger(run.run_attempt));
    for (const run of runs) {
      try {
        for (let page = 1; page <= 10; page += 1) {
          const result = await getJson(`/repos/${repository}/actions/runs/${run.id}/attempts/${run.run_attempt}/jobs?per_page=100&page=${page}`);
          const jobs = result.jobs || [];
          for (const [key, name] of Object.entries(WINDOWS_CHECKS)) {
            if (jobs.some((job) => job.name?.split(" / ").at(-1) === name
                && job.status === "completed" && job.conclusion === "success")) {
              verified[key] = true;
            }
          }
          if (jobs.length < 100) break;
        }
      } catch {
        warn(`Component evidence for run ${run.id} unavailable; unverified checks will run.`);
      }
    }
  }
  return verified;
}

async function main() {
  const sha = process.env.CURRENT_SHA || process.env.GITHUB_SHA || "";
  const repository = process.env.GITHUB_REPOSITORY || "";
  if (!/^[a-f0-9]{40}$/.test(sha) || !/^[\w.-]+\/[\w.-]+$/.test(repository)) {
    throw new Error("A valid current commit and GitHub repository are required.");
  }
  const getJson = async (route) => {
    const response = await fetch(`https://api.github.com${route}`, {
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${process.env.GH_TOKEN || process.env.GITHUB_TOKEN || ""}`,
      },
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error(`GitHub returned ${response.status}`);
    return response.json();
  };
  const verified = process.env.FULL === "true"
    ? Object.fromEntries(Object.keys(WINDOWS_CHECKS).map((key) => [key, false]))
    : await collectVerifiedChecks({ sha, repository, getJson, warn: console.warn });
  const outputs = Object.entries(verified).map(([key, value]) => `${key}_verified=${value}`);
  outputs.push(`upstream_verified=${Object.values(verified).some(Boolean)}`);
  if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, `${outputs.join("\n")}\n`);
  console.log(`Verified checks for ${sha}: ${Object.keys(verified).filter((key) => verified[key]).join(", ") || "none; run affected checks"}`);
}

if (require.main === module) main().catch((error) => { console.error(error.message); process.exitCode = 1; });
module.exports = { WINDOWS_CHECKS, collectVerifiedChecks };
