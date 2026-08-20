"use strict";

// Build a deterministic inventory of legacy FastAPI routes, Vue API consumers,
// and the Axum routes exposed by the Rust Stable backend. The inventory is
// descriptive only: it never starts a service, reads a user project, or treats
// an unconsumed legacy route as a required migration.

const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const backendApiRoot = path.join(repoRoot, "apps", "backend", "api");
const frontendApiRoot = path.join(repoRoot, "apps", "frontend", "src", "api");
const rustRouterPath = path.join(
  repoRoot,
  "apps",
  "desktop",
  "agent-runtime",
  "storydex-agentd",
  "src",
  "lib.rs"
);

const ROUTE_RE = /@router\.(get|post|put|patch|delete)\(\s*["']([^"']+)["']/g;
const FRONTEND_CALL_RE = /apiClient\.(get|post|put|patch|delete)\b/g;

// This API deliberately passes a route variable to a shared request helper.
// Keep the alternatives explicit so a future edit cannot silently disappear
// from the generated contract inventory.
const EXPLICIT_FRONTEND_ROUTES = [
  {
    method: "GET",
    path: "/workspace/story/templates/chapters",
    consumer: "apps/frontend/src/api/workspace.ts",
    evidence: "fetchStoryChapterTemplates paths[0]"
  },
  {
    method: "GET",
    path: "/story/templates/chapters",
    consumer: "apps/frontend/src/api/workspace.ts",
    evidence: "fetchStoryChapterTemplates paths[1]"
  }
];

// Axum requires catch-all parameters to be final. Preset names may contain
// nested directories, so storydex-agentd registers one bounded catch-all and
// dispatches only these suffix/method combinations.
const EXPLICIT_RUST_DISPATCH_ROUTES = [
  ["GET", "/presets/{name}/document"],
  ["PUT", "/presets/{name}/document"],
  ["POST", "/presets/{name}/compile"],
  ["POST", "/presets/{name}/risk-check"],
  ["PATCH", "/presets/{name}/params"],
  ["POST", "/presets/{name}/activate"],
  ["POST", "/presets/{name}/deactivate"]
].map(([method, path]) => ({
  method,
  path,
  owner: "storydex-agentd",
  source: "apps/desktop/agent-runtime/storydex-agentd/src/presets.rs",
  line: 1,
  evidence: "bounded presets catch-all dispatcher"
}));

function lineNumberAt(source, index) {
  return source.slice(0, index).split("\n").length;
}

function skipQuoted(source, start, quote) {
  for (let index = start + 1; index < source.length; index += 1) {
    if (source[index] === "\\") {
      index += 1;
      continue;
    }
    if (source[index] === quote) return index + 1;
  }
  throw new Error(`unterminated ${quote} string while scanning route inventory`);
}

function skipLineComment(source, start) {
  const end = source.indexOf("\n", start + 2);
  return end < 0 ? source.length : end + 1;
}

function skipBlockComment(source, start) {
  const end = source.indexOf("*/", start + 2);
  if (end < 0) throw new Error("unterminated block comment while scanning route inventory");
  return end + 2;
}

function skipTemplate(source, start) {
  for (let index = start + 1; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\") {
      index += 1;
      continue;
    }
    if (char === "`") return index + 1;
  }
  throw new Error("unterminated template string while scanning route inventory");
}

function skipTrivia(source, start) {
  let index = start;
  while (index < source.length) {
    if (/\s/.test(source[index])) {
      index += 1;
    } else if (source.startsWith("//", index)) {
      index = skipLineComment(source, index);
    } else if (source.startsWith("/*", index)) {
      index = skipBlockComment(source, index);
    } else {
      break;
    }
  }
  return index;
}

function findCallOpen(source, start) {
  let index = skipTrivia(source, start);
  if (source[index] === "<") {
    let depth = 0;
    for (; index < source.length; index += 1) {
      const char = source[index];
      if (char === '"' || char === "'") {
        index = skipQuoted(source, index, char) - 1;
        continue;
      }
      if (char === "`") {
        index = skipTemplate(source, index) - 1;
        continue;
      }
      if (source.startsWith("//", index)) {
        index = skipLineComment(source, index) - 1;
        continue;
      }
      if (source.startsWith("/*", index)) {
        index = skipBlockComment(source, index) - 1;
        continue;
      }
      if (char === "<") depth += 1;
      if (char === ">") {
        depth -= 1;
        if (depth === 0) {
          index = skipTrivia(source, index + 1);
          break;
        }
      }
    }
  }
  return source[index] === "(" ? index : -1;
}

function readCallArguments(source, callOpen) {
  const argumentsList = [];
  let argumentStart = callOpen + 1;
  const stack = [];
  for (let index = callOpen + 1; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"' || char === "'") {
      index = skipQuoted(source, index, char) - 1;
      continue;
    }
    if (char === "`") {
      index = skipTemplate(source, index) - 1;
      continue;
    }
    if (source.startsWith("//", index)) {
      index = skipLineComment(source, index) - 1;
      continue;
    }
    if (source.startsWith("/*", index)) {
      index = skipBlockComment(source, index) - 1;
      continue;
    }
    if (char === "(" || char === "[" || char === "{") {
      stack.push(char);
      continue;
    }
    if (char === ")") {
      if (stack.length === 0) {
        argumentsList.push(source.slice(argumentStart, index).trim());
        return { arguments: argumentsList, close: index };
      }
      stack.pop();
      continue;
    }
    if (char === "]" || char === "}") {
      stack.pop();
      continue;
    }
    if (char === "," && stack.length === 0) {
      argumentsList.push(source.slice(argumentStart, index).trim());
      argumentStart = index + 1;
    }
  }
  throw new Error("unterminated function call while scanning route inventory");
}

function decodeQuotedRoute(expression) {
  const quote = expression[0];
  const body = expression.slice(1, -1);
  if (quote === '"') return JSON.parse(expression);
  return body.replace(/\\([\\'"`])/g, "$1");
}

function normalizeTemplateRoute(expression) {
  let route = "";
  const body = expression.slice(1, -1);
  for (let index = 0; index < body.length; index += 1) {
    if (body[index] === "\\") {
      route += body[index + 1] || "";
      index += 1;
      continue;
    }
    if (body[index] === "$" && body[index + 1] === "{") {
      route += "{param}";
      index += 2;
      let depth = 1;
      for (; index < body.length && depth > 0; index += 1) {
        const char = body[index];
        if (char === '"' || char === "'") {
          index = skipQuoted(body, index, char) - 1;
          continue;
        }
        if (char === "{") depth += 1;
        if (char === "}") depth -= 1;
      }
      index -= 1;
      continue;
    }
    route += body[index];
  }
  return route;
}

function routeFromExpression(expression) {
  const value = String(expression || "").trim();
  if (!value) return null;
  if ((value[0] === '"' || value[0] === "'") && value.at(-1) === value[0]) {
    return decodeQuotedRoute(value);
  }
  if (value[0] === "`" && value.at(-1) === "`") {
    return normalizeTemplateRoute(value);
  }
  return null;
}

function readPythonRoutes() {
  const routes = [];
  for (const fileName of fs.readdirSync(backendApiRoot).filter((name) => name.endsWith(".py")).sort()) {
    const filePath = path.join(backendApiRoot, fileName);
    const source = fs.readFileSync(filePath, "utf8");
    for (const match of source.matchAll(ROUTE_RE)) {
      routes.push({
        method: match[1].toUpperCase(),
        path: match[2],
        owner: fileName.replace(/\.py$/, ""),
        source: path.posix.join("apps/backend/api", fileName),
        line: lineNumberAt(source, match.index)
      });
    }
  }
  return dedupeRoutes(routes, ["method", "path", "owner"]);
}

function readFrontendConsumers() {
  const consumers = [];
  for (const fileName of fs.readdirSync(frontendApiRoot).filter((name) => name.endsWith(".ts")).sort()) {
    const filePath = path.join(frontendApiRoot, fileName);
    const source = fs.readFileSync(filePath, "utf8");
    for (const match of source.matchAll(FRONTEND_CALL_RE)) {
      const callOpen = findCallOpen(source, match.index + match[0].length);
      if (callOpen < 0) continue;
      const call = readCallArguments(source, callOpen);
      const routePath = routeFromExpression(call.arguments[0]);
      if (!routePath || !routePath.startsWith("/")) continue;
      consumers.push({
        method: match[1].toUpperCase(),
        path: routePath,
        consumer: path.posix.join("apps/frontend/src/api", fileName),
        line: lineNumberAt(source, match.index),
        evidence: call.arguments[0]
      });
    }
  }
  consumers.push(...EXPLICIT_FRONTEND_ROUTES);
  return dedupeRoutes(consumers, ["method", "path", "consumer"]);
}

function readRustRoutes() {
  const source = fs.readFileSync(rustRouterPath, "utf8");
  const routes = [];
  const routeCallRe = /\.route\b/g;
  for (const match of source.matchAll(routeCallRe)) {
    const callOpen = findCallOpen(source, match.index + match[0].length);
    if (callOpen < 0) continue;
    const call = readCallArguments(source, callOpen);
    const registeredPath = routeFromExpression(call.arguments[0]);
    if (!registeredPath || !call.arguments[1]) continue;
    const routePath = registeredPath.replace(/^\/api\/v1(?=\/)/, "");
    if (routePath === "/presets/{*path}") continue;
    const methods = [...call.arguments[1].matchAll(/\b(get|post|put|patch|delete)\s*\(/g)];
    for (const method of methods) {
      routes.push({
        method: method[1].toUpperCase(),
        path: routePath,
        owner: "storydex-agentd",
        source: "apps/desktop/agent-runtime/storydex-agentd/src/lib.rs",
        line: lineNumberAt(source, match.index)
      });
    }
  }
  routes.push(...EXPLICIT_RUST_DISPATCH_ROUTES);
  return dedupeRoutes(routes, ["method", "path", "owner"]);
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
  return `${method.toUpperCase()} ${normalizePath(routePath)}`;
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
  const pythonRoutes = readPythonRoutes();
  const frontendConsumers = readFrontendConsumers();
  const rustRoutes = readRustRoutes();
  const pythonByKey = new Map(pythonRoutes.map((route) => [routeKey(route.method, route.path), route]));
  const rustByKey = new Map(rustRoutes.map((route) => [routeKey(route.method, route.path), route]));
  const consumersByKey = new Map();
  for (const consumer of frontendConsumers) {
    const key = routeKey(consumer.method, consumer.path);
    const current = consumersByKey.get(key) || [];
    current.push(consumer);
    consumersByKey.set(key, current);
  }

  const allKeys = new Set([...pythonByKey.keys(), ...consumersByKey.keys(), ...rustByKey.keys()]);
  const contracts = [...allKeys].map((key) => {
    const pythonRoute = pythonByKey.get(key) || null;
    const rustRoute = rustByKey.get(key) || null;
    const consumers = consumersByKey.get(key) || [];
    const [method, ...pathParts] = key.split(" ");
    const normalizedPath = pathParts.join(" ");
    const status = rustRoute ? "implemented" : consumers.length ? "pending" : "excluded";
    return {
      method,
      path: pythonRoute?.path || consumers[0]?.path || rustRoute?.path || normalizedPath,
      normalizedPath,
      target: targetFor(normalizedPath),
      status,
      pythonRoute,
      rustRoute,
      frontendConsumers: consumers,
      exclusionEvidence: status === "excluded"
        ? "No Vue API consumer exists under apps/frontend/src/api and the target candidate has no registered Rust route."
        : null
    };
  }).sort((left, right) => `${left.normalizedPath}\0${left.method}`.localeCompare(`${right.normalizedPath}\0${right.method}`, "en"));

  const frontendCoverage = frontendConsumers.map((consumer) => {
    const key = routeKey(consumer.method, consumer.path);
    return {
      ...consumer,
      normalizedPath: normalizePath(consumer.path),
      pythonRoutePresent: pythonByKey.has(key),
      rustRoutePresent: rustByKey.has(key),
      target: targetFor(consumer.path)
    };
  });
  const groups = {};
  for (const contract of contracts) {
    if (!groups[contract.target]) groups[contract.target] = { implemented: 0, pending: 0, excluded: 0 };
    groups[contract.target][contract.status] += 1;
  }

  return {
    schemaVersion: 2,
    generatedBy: "scripts/generate_rust_backend_interface_inventory.cjs",
    stableBoundary: {
      runtime: "Tauri 2 + storydex-agentd + Rust Coomi",
      legacyRuntime: "Electron + Python/FastAPI retained for compatibility scripts, differential tests, selected full CI jobs, and manual rollback",
      realUserProjects: "never read by inventory or replay checks"
    },
    counts: {
      pythonRoutes: pythonRoutes.length,
      frontendConsumers: frontendConsumers.length,
      rustRoutes: rustRoutes.length,
      frontendConsumersWithoutPythonRoute: frontendCoverage.filter((item) => !item.pythonRoutePresent).length,
      frontendConsumersImplementedInRust: frontendCoverage.filter((item) => item.rustRoutePresent).length,
      frontendConsumersPendingInRust: frontendCoverage.filter((item) => !item.rustRoutePresent).length,
      contractsImplemented: contracts.filter((item) => item.status === "implemented").length,
      contractsPending: contracts.filter((item) => item.status === "pending").length,
      contractsExcluded: contracts.filter((item) => item.status === "excluded").length,
      rustRoutesWithoutPythonRoute: rustRoutes.filter((route) => !pythonByKey.has(routeKey(route.method, route.path))).length,
      targetGroups: Object.keys(groups).length
    },
    targetGroups: Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b, "en"))),
    pythonRoutes,
    frontendConsumers: frontendCoverage,
    rustRoutes,
    contracts
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
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
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

module.exports = {
  buildInventory,
  normalizePath,
  routeFromExpression,
  targetFor
};
