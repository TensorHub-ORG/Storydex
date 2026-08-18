"use strict";

// Build a deterministic inventory of the public FastAPI routes and the
// frontend API consumers that the Rust backend candidate must replace.  The
// inventory is intentionally descriptive: it never starts a server, reads a
// user project, or silently marks an endpoint as migrated.

const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const backendApiRoot = path.join(repoRoot, "apps", "backend", "api");
const frontendApiRoot = path.join(repoRoot, "apps", "frontend", "src", "api");

const ROUTE_RE = /@router\.(get|post|put|patch|delete)\(\s*["']([^"']+)["']/g;
// TypeScript generics may be nested (ApiEnvelope<Record<...>>), so matching
// up to the call parenthesis is more robust than trying to parse angle
// brackets with a regular expression.
const CONSUMER_RE = /apiClient\.(get|post|put|patch|delete)[^\n]{0,400}?\(\s*["']([^"']+)["']/g;

function readPythonRoutes() {
  const routes = [];
  for (const fileName of fs.readdirSync(backendApiRoot).filter((name) => name.endsWith(".py")).sort()) {
    const filePath = path.join(backendApiRoot, fileName);
    const source = fs.readFileSync(filePath, "utf8");
    for (const match of source.matchAll(ROUTE_RE)) {
      routes.push({
        method: match[1].toUpperCase(),
        path: match[2],
        owner: fileName.replace(/\.py$/, "")
      });
    }
  }
  return dedupeRoutes(routes);
}

function readFrontendConsumers() {
  const consumers = [];
  for (const fileName of fs.readdirSync(frontendApiRoot).filter((name) => name.endsWith(".ts")).sort()) {
    const filePath = path.join(frontendApiRoot, fileName);
    const source = fs.readFileSync(filePath, "utf8");
    for (const match of source.matchAll(CONSUMER_RE)) {
      consumers.push({
        method: match[1].toUpperCase(),
        path: match[2],
        consumer: path.posix.join("apps/frontend/src/api", fileName)
      });
    }
  }
  return dedupeRoutes(consumers, ["method", "path", "consumer"]);
}

function dedupeRoutes(items, keys = ["method", "path", "owner"]) {
  const seen = new Set();
  return items.filter((item) => {
    const key = keys.map((name) => item[name] || "").join("\0");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).sort((left, right) => {
    const a = `${left.path}\0${left.method}\0${left.owner || left.consumer || ""}`;
    const b = `${right.path}\0${right.method}\0${right.owner || right.consumer || ""}`;
    return a.localeCompare(b, "en");
  });
}

function routeKey(method, routePath) {
  return `${method.toUpperCase()} ${routePath}`;
}

function normalizePath(routePath) {
  return String(routePath || "").replace(/\{[^}]+\}/g, "{param}");
}

function targetFor(routePath) {
  const normalized = normalizePath(routePath);
  if (normalized.startsWith("/agent/")) {
    return normalized.includes("/coomi/")
      ? "storydex-agentd + coomi-services"
      : "storydex-agentd";
  }
  if (normalized.startsWith("/story/wiki")) return "coomi-services::storydex_project + wiki projection";
  if (normalized.startsWith("/workspace/git/")) return "coomi-services::storydex_project";
  if (normalized.startsWith("/file/") || normalized.startsWith("/workspace/")) return "coomi-services::workspace boundary";
  if (normalized.startsWith("/story/")) return "coomi-services::story domain";
  if (normalized.startsWith("/presets/")) return "coomi-services::preset domain";
  if (normalized.startsWith("/help/")) return "coomi-services::help domain";
  if (normalized.startsWith("/sys/")) return "storydex-agentd::system boundary";
  if (normalized.startsWith("/auth/")) return "candidate auth boundary";
  return "unclassified candidate route";
}

function buildInventory() {
  const routes = readPythonRoutes();
  const consumers = readFrontendConsumers();
  const routeSet = new Set(routes.map((route) => routeKey(route.method, normalizePath(route.path))));
  const consumerCoverage = consumers.map((consumer) => ({
    ...consumer,
    normalizedPath: normalizePath(consumer.path),
    routePresent: routeSet.has(routeKey(consumer.method, normalizePath(consumer.path))),
    target: targetFor(consumer.path)
  }));
  const groups = {};
  for (const route of routes) {
    const target = targetFor(route.path);
    groups[target] = (groups[target] || 0) + 1;
  }
  return {
    schemaVersion: 1,
    generatedBy: "scripts/generate_rust_backend_interface_inventory.cjs",
    stableBoundary: {
      runtime: "Electron + Python/FastAPI + Rust Coomi bridge",
      candidateRuntime: "Rust backend + Electron Rust Beta/Tauri preview",
      realUserProjects: "never read by inventory or replay checks"
    },
    counts: {
      pythonRoutes: routes.length,
      frontendConsumers: consumers.length,
      frontendConsumersWithoutRoute: consumerCoverage.filter((item) => !item.routePresent).length,
      targetGroups: Object.keys(groups).length
    },
    targetGroups: Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b, "en"))),
    routes,
    frontendConsumers: consumerCoverage
  };
}

function main(argv = process.argv.slice(2)) {
  const inventory = buildInventory();
  const json = `${JSON.stringify(inventory, null, 2)}\n`;
  const outputIndex = argv.indexOf("--output");
  if (outputIndex >= 0) {
    const output = argv[outputIndex + 1];
    if (!output) throw new Error("--output requires a path");
    const outputPath = path.resolve(repoRoot, output);
    if (!outputPath.startsWith(repoRoot + path.sep)) {
      throw new Error("inventory output must stay inside the repository");
    }
    fs.writeFileSync(outputPath, json, "utf8");
  } else {
    process.stdout.write(json);
  }
  return inventory;
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[Storydex] interface inventory failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { buildInventory, normalizePath, targetFor };
