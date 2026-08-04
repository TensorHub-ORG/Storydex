const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const defaultConfigPath = path.join(repoRoot, "coverage-baseline.json");

class CoverageGateError extends Error {}

function escapeWorkflowCommand(value) {
  return String(value).replace(/%/g, "%25").replace(/\r/g, "%0D").replace(/\n/g, "%0A");
}

function readJson(filePath, label) {
  if (!fs.existsSync(filePath)) {
    throw new CoverageGateError(`${label} is missing: ${filePath}`);
  }
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    throw new CoverageGateError(`${label} is not valid JSON: ${filePath} (${error.message})`);
  }
}

function percentage(coveredValue, totalValue, label) {
  const covered = Number(coveredValue);
  const total = Number(totalValue);
  if (!Number.isFinite(covered) || !Number.isFinite(total) || covered < 0 || total < 0 || covered > total) {
    throw new CoverageGateError(`${label} contains invalid covered/total values.`);
  }
  return total === 0 ? 100 : (covered * 100) / total;
}

function parseBackendSummary(summary, label) {
  if (!summary || typeof summary !== "object") {
    throw new CoverageGateError(`${label} summary is missing.`);
  }
  return {
    lines: percentage(summary.covered_lines, summary.num_statements, `${label} lines`),
    branches: percentage(summary.covered_branches, summary.num_branches, `${label} branches`)
  };
}

function parseBackendReport(payload) {
  if (!payload || typeof payload !== "object" || !payload.files || typeof payload.files !== "object") {
    throw new CoverageGateError("Backend coverage report does not contain a files object.");
  }
  const files = {};
  for (const [fileName, value] of Object.entries(payload.files)) {
    files[fileName.replace(/\\/g, "/")] = parseBackendSummary(value?.summary, fileName);
  }
  return { total: parseBackendSummary(payload.totals, "backend total"), files };
}

function parseFrontendMetric(metric, label) {
  if (!metric || typeof metric !== "object") {
    throw new CoverageGateError(`${label} metric is missing.`);
  }
  return percentage(metric.covered, metric.total, label);
}

function parseFrontendSummary(summary, label) {
  if (!summary || typeof summary !== "object") {
    throw new CoverageGateError(`${label} summary is missing.`);
  }
  return Object.fromEntries(
    ["lines", "statements", "functions", "branches"].map((metric) => [
      metric,
      parseFrontendMetric(summary[metric], `${label} ${metric}`)
    ])
  );
}

function parseFrontendReport(payload) {
  if (!payload || typeof payload !== "object" || !payload.total) {
    throw new CoverageGateError("Frontend coverage summary does not contain total metrics.");
  }
  const files = {};
  for (const [fileName, summary] of Object.entries(payload)) {
    if (fileName === "total") continue;
    files[fileName.replace(/\\/g, "/")] = parseFrontendSummary(summary, fileName);
  }
  return { total: parseFrontendSummary(payload.total, "frontend total"), files };
}

function findFileMetrics(files, expectedPath) {
  const normalizedExpected = expectedPath.replace(/\\/g, "/");
  const match = Object.entries(files).find(([fileName]) =>
    fileName === normalizedExpected || fileName.endsWith(`/${normalizedExpected}`)
  );
  if (!match) {
    throw new CoverageGateError(`Coverage report is missing required file: ${expectedPath}`);
  }
  return match[1];
}

function validateThresholds(report, policy, tolerance) {
  if (!policy || typeof policy !== "object" || !policy.metrics || !policy.critical) {
    throw new CoverageGateError("Coverage baseline component policy is incomplete.");
  }
  const results = [];
  for (const [metric, requiredValue] of Object.entries(policy.metrics)) {
    results.push({ scope: "total", metric, actual: report.total[metric], required: Number(requiredValue) });
  }
  for (const [fileName, thresholds] of Object.entries(policy.critical)) {
    const metrics = findFileMetrics(report.files, fileName);
    for (const [metric, requiredValue] of Object.entries(thresholds)) {
      results.push({ scope: fileName, metric, actual: metrics[metric], required: Number(requiredValue) });
    }
  }

  for (const result of results) {
    if (!Number.isFinite(result.actual) || !Number.isFinite(result.required)) {
      throw new CoverageGateError(`Coverage metric is invalid: ${result.scope} ${result.metric}`);
    }
  }
  return {
    results,
    failures: results.filter((item) => item.actual + tolerance < item.required)
  };
}

function parseArgs(argv) {
  const values = new Map();
  for (const argument of argv) {
    const [key, ...rest] = argument.split("=");
    values.set(key, rest.join("="));
  }
  return values;
}

function run(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const component = args.get("--component");
  const mode = args.get("--mode") || "ci";
  const reportPath = path.resolve(args.get("--report") || "");
  const configPath = path.resolve(args.get("--config") || defaultConfigPath);
  const testExitCode = Number(args.get("--test-exit-code") || 0);

  if (!['backend', 'frontend'].includes(component)) {
    throw new CoverageGateError("--component must be backend or frontend.");
  }
  if (!['advisory', 'ci', 'release'].includes(mode)) {
    throw new CoverageGateError("--mode must be advisory, ci, or release.");
  }
  if (!reportPath || reportPath === path.parse(reportPath).root) {
    throw new CoverageGateError("--report must point to a coverage JSON file.");
  }
  if (!Number.isInteger(testExitCode) || testExitCode < 0) {
    throw new CoverageGateError("--test-exit-code must be a non-negative integer.");
  }
  if (testExitCode !== 0) {
    throw new CoverageGateError(
      `Test command failed with exit code ${testExitCode}; a stale or partial coverage report cannot pass the gate.`
    );
  }

  const config = readJson(configPath, "Coverage baseline");
  if (config.schemaVersion !== 1 || !config.components?.[component]) {
    throw new CoverageGateError(`Coverage baseline does not define ${component} with schemaVersion 1.`);
  }
  const payload = readJson(reportPath, "Coverage report");
  const report = component === "backend" ? parseBackendReport(payload) : parseFrontendReport(payload);
  const tolerance = mode === "release" ? 0 : Number(config.ciTolerance || 0);
  if (!Number.isFinite(tolerance) || tolerance < 0 || tolerance > 0.5) {
    throw new CoverageGateError("ciTolerance must be between 0 and 0.5 percentage points.");
  }

  const { results, failures } = validateThresholds(report, config.components[component], tolerance);
  if (failures.length) {
    const lines = failures.map((item) =>
      `- ${item.scope} ${item.metric}: actual ${item.actual.toFixed(2)}%, required ${item.required.toFixed(2)}%` +
      (tolerance ? ` (CI measurement tolerance ${tolerance.toFixed(2)} points)` : "")
    );
    const message = [
      mode === "advisory"
        ? `Coverage advisory for ${component}:`
        : `Coverage gate failed for ${component} (${mode}):`,
      ...lines,
      `To adjust an intentional baseline increase, edit ${path.relative(repoRoot, configPath)} explicitly; never lower it to bypass missing tests.`
    ].join("\n");
    if (mode === "advisory") {
      console.warn(message);
      if (process.env.GITHUB_ACTIONS === "true") {
        console.warn(`::warning title=Coverage advisory::${escapeWorkflowCommand(message)}`);
      }
      return;
    }
    throw new CoverageGateError(message);
  }

  const totals = results
    .filter((item) => item.scope === "total")
    .map((item) => `${item.metric}=${item.actual.toFixed(2)}% (baseline ${item.required.toFixed(2)}%)`)
    .join(", ");
  console.log(`Coverage gate passed for ${component} (${mode}): ${totals}`);
}

if (require.main === module) {
  try {
    run();
  } catch (error) {
    const message = error instanceof CoverageGateError ? error.message : error?.stack || String(error);
    console.error(message);
    if (process.env.GITHUB_ACTIONS === "true") {
      console.error(`::error title=Coverage gate::${escapeWorkflowCommand(message)}`);
    }
    process.exitCode = 1;
  }
}

module.exports = {
  CoverageGateError,
  escapeWorkflowCommand,
  parseBackendReport,
  parseFrontendReport,
  run,
  validateThresholds
};
