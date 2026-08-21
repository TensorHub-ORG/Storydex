"use strict";

const fs = require("node:fs");
const path = require("node:path");

function normalizePath(value) {
  return String(value || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/^\.\//, "");
}

function isDocumentationOnly(filePath) {
  return (
    filePath === "AGENTS.md"
    || filePath === ".gitattributes"
    || filePath === ".gitignore"
    || filePath === "LICENSE"
    || filePath.startsWith("LICENSE.")
    || /^README(?:\.[^/]+)?$/i.test(filePath)
    || filePath.startsWith("docs/")
  );
}

function isCiPolicyOnly(filePath) {
  return (
    filePath.startsWith(".github/")
    || filePath === "scripts/resolve_ci_scope.cjs"
    || filePath === "scripts/run_pre_push_ci.ps1"
    || filePath === "scripts/tests/ci-preflight.test.cjs"
    || filePath === "scripts/tests/check-coverage.test.cjs"
    || filePath === "scripts/tests/resolve-ci-scope.test.cjs"
  );
}

function classifyChangedPaths(inputPaths, options = {}) {
  const forceAll = options.forceAll === true;
  const paths = [...new Set(inputPaths.map(normalizePath).filter(Boolean))];
  const scope = {
    backend: false,
    frontend: false,
    desktop: false,
    android: false,
    coomi: false,
    pc_runtime: false,
  };
  const unknownPaths = [];
  let ciPolicyOnly = false;

  const enableAll = () => {
    scope.backend = true;
    scope.frontend = true;
    scope.desktop = true;
    scope.android = true;
    scope.coomi = true;
    scope.pc_runtime = true;
  };

  if (forceAll || paths.length === 0) {
    enableAll();
    return {
      ...scope,
      docsOnly: false,
      changedCount: paths.length,
      unknownPaths,
      reason: forceAll ? "forced-full-scope" : "empty-change-set-fail-safe",
    };
  }

  for (const filePath of paths) {
    if (isCiPolicyOnly(filePath)) {
      ciPolicyOnly = true;
      continue;
    }
    if (isDocumentationOnly(filePath)) {
      continue;
    }

    let matched = false;

    if (filePath === "coverage-baseline.json" || filePath === "scripts/check_coverage.cjs" || filePath === "scripts/tests/check-coverage.test.cjs") {
      scope.backend = true;
      scope.frontend = true;
      matched = true;
    }

    if (filePath === "requirements.lock" || filePath === "requirements.txt" || filePath.startsWith("apps/backend/requirements-")) {
      scope.backend = true;
      matched = true;
      if (filePath === "requirements.txt") {
        scope.coomi = true;
      }
    }

    if (filePath.startsWith("apps/backend/")) {
      scope.backend = true;
      matched = true;
    }
    if (filePath.startsWith("apps/frontend/")) {
      scope.frontend = true;
      matched = true;
    }
    if (filePath.startsWith("apps/desktop/")) {
      scope.desktop = true;
      matched = true;
    }
    if (filePath.startsWith("apps/android/") || filePath.startsWith("apps/android-frontend/")) {
      scope.android = true;
      matched = true;
    }
    if (filePath.startsWith("assets/")) {
      scope.frontend = true;
      scope.desktop = true;
      matched = true;
    }

    const desktopRuntimePath = filePath.startsWith("apps/desktop/agent-runtime/");
    const androidRuntimePath = filePath.startsWith("apps/android/agent-runtime/");
    const coomiIntegrationPath = (
      filePath === "scripts/verify_coomi_runtime.py"
      || filePath === "apps/backend/services/coomi_bridge_client.py"
      || filePath === "apps/backend/services/coomi_version_service.py"
      || filePath.startsWith("apps/backend/tests/contract_coomi/")
      || filePath === "apps/desktop/scripts/build-coomi-runtime.cjs"
    );
    if (desktopRuntimePath || coomiIntegrationPath) {
      scope.coomi = true;
      scope.pc_runtime = true;
      matched = true;
      if (desktopRuntimePath || filePath.startsWith("apps/desktop/")) {
        scope.desktop = true;
      }
    }
    if (androidRuntimePath) {
      scope.android = true;
      scope.coomi = true;
      matched = true;
    }

    if (filePath.startsWith("scripts/")) {
      const sourcePolicyOnly = (
        filePath === "scripts/validate_text_encoding.cjs"
        || filePath === "scripts/validate_version_consistency.cjs"
      );
      if (sourcePolicyOnly) {
        matched = true;
      }
    }

    if (!matched) {
      unknownPaths.push(filePath);
    }
  }

  if (unknownPaths.length > 0) {
    enableAll();
  }

  const selected = scope.backend || scope.frontend || scope.desktop || scope.android || scope.coomi || scope.pc_runtime;
  const reason = unknownPaths.length > 0
    ? "unknown-path-fail-safe"
    : (ciPolicyOnly && !selected ? "ci-policy-only" : (!selected ? "documentation-only" : "path-classified"));
  return {
    ...scope,
    docsOnly: !selected && !ciPolicyOnly,
    changedCount: paths.length,
    unknownPaths,
    reason,
  };
}

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    const [name, inlineValue] = argument.split("=", 2);
    if (!["--files-from", "--github-output", "--force-all"].includes(name)) {
      throw new Error(`Unsupported argument: ${argument}`);
    }
    const value = inlineValue !== undefined ? inlineValue : argv[++index];
    if (value === undefined) {
      throw new Error(`Missing value for ${name}`);
    }
    options[name.slice(2)] = value;
  }
  return options;
}

function writeGithubOutputs(filePath, result) {
  const values = {
    backend: result.backend,
    frontend: result.frontend,
    desktop: result.desktop,
    android: result.android,
    coomi: result.coomi,
    pc_runtime: result.pc_runtime,
    docs_only: result.docsOnly,
    changed_count: result.changedCount,
    reason: result.reason,
  };
  const lines = Object.entries(values).map(([name, value]) => `${name}=${String(value)}`);
  fs.appendFileSync(filePath, `${lines.join("\n")}\n`, "utf8");
}

function main(argv) {
  const options = parseArgs(argv);
  if (!options["files-from"]) {
    throw new Error("--files-from is required");
  }
  const fileList = fs.readFileSync(path.resolve(options["files-from"]), "utf8").split(/\r?\n/);
  const result = classifyChangedPaths(fileList, {
    forceAll: ["1", "true", "yes"].includes(String(options["force-all"] || "").toLowerCase()),
  });
  if (options["github-output"]) {
    writeGithubOutputs(path.resolve(options["github-output"]), result);
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (require.main === module) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}

module.exports = {
  classifyChangedPaths,
  isDocumentationOnly,
  normalizePath,
};
