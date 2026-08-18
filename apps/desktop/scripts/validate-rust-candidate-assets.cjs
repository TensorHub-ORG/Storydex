const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "../../..");
const defaultPolicyPath = path.join(__dirname, "..", "candidate", "runtime-policy.json");
const REPORT_SCHEMA_VERSION = 1;

function toPosix(value) {
  return String(value || "").replace(/\\/g, "/");
}

function normalizeToken(value) {
  return String(value || "").trim().toLowerCase();
}

function normalizePolicy(policy) {
  if (!policy || typeof policy !== "object" || Array.isArray(policy)) {
    throw new Error("candidate runtime policy must be a JSON object");
  }
  if (Number(policy.schemaVersion) !== REPORT_SCHEMA_VERSION) {
    throw new Error(`unsupported candidate runtime policy schemaVersion: ${policy.schemaVersion}`);
  }

  const asStringArray = (field) => {
    const value = policy[field];
    if (!Array.isArray(value)) {
      throw new Error(`candidate runtime policy field ${field} must be an array`);
    }
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  };

  return {
    schemaVersion: REPORT_SCHEMA_VERSION,
    candidate: String(policy.candidate || "rust-tauri-preview").trim() || "rust-tauri-preview",
    description: String(policy.description || "").trim(),
    forbiddenPathTokens: asStringArray("forbiddenPathTokens").map(normalizeToken),
    forbiddenFileNames: asStringArray("forbiddenFileNames").map(normalizeToken),
    forbiddenExtensions: asStringArray("forbiddenExtensions").map((item) => {
      const extension = normalizeToken(item);
      return extension.startsWith(".") ? extension : `.${extension}`;
    }),
    stableRoots: asStringArray("stableRoots")
  };
}

function loadPolicy(policyPath = defaultPolicyPath) {
  const resolvedPath = path.resolve(policyPath);
  const payload = JSON.parse(fs.readFileSync(resolvedPath, "utf8"));
  return {
    path: resolvedPath,
    policy: normalizePolicy(payload)
  };
}

function isPathInside(candidatePath, rootPath) {
  const relative = path.relative(rootPath, candidatePath);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function resolveRealPath(candidatePath) {
  return fs.realpathSync.native ? fs.realpathSync.native(candidatePath) : fs.realpathSync(candidatePath);
}

function relativePosix(rootPath, candidatePath) {
  const relative = toPosix(path.relative(rootPath, candidatePath));
  return relative || ".";
}

function addViolation(violations, seen, violation) {
  const key = `${violation.code}\0${violation.path}`;
  if (seen.has(key)) return;
  seen.add(key);
  violations.push(violation);
}

function inspectCandidateRoot(candidateRoot, options = {}) {
  const projectRoot = path.resolve(options.projectRoot || repoRoot);
  const policy = options.policy || loadPolicy(options.policyPath || defaultPolicyPath).policy;
  const normalizedPolicy = normalizePolicy(policy);
  const violations = [];
  const seen = new Set();

  let rootPath;
  try {
    rootPath = path.resolve(candidateRoot);
  } catch (error) {
    addViolation(violations, seen, { path: ".", code: "invalid-root", detail: error.message });
    return buildReport(normalizedPolicy, projectRoot, candidateRoot, 0, 0, violations);
  }

  if (!isPathInside(rootPath, projectRoot)) {
    addViolation(violations, seen, {
      path: toPosix(rootPath),
      code: "root-outside-repository",
      detail: "candidate staging root must stay inside the repository; real user project paths are out of scope"
    });
    return buildReport(normalizedPolicy, projectRoot, rootPath, 0, 0, violations);
  }

  if (!fs.existsSync(rootPath)) {
    addViolation(violations, seen, {
      path: relativePosix(projectRoot, rootPath),
      code: "root-missing",
      detail: "candidate staging root does not exist"
    });
    return buildReport(normalizedPolicy, projectRoot, rootPath, 0, 0, violations);
  }

  let realProjectRoot;
  let realRootPath;
  try {
    realProjectRoot = resolveRealPath(projectRoot);
    realRootPath = resolveRealPath(rootPath);
  } catch (error) {
    addViolation(violations, seen, {
      path: relativePosix(projectRoot, rootPath),
      code: "root-unreadable",
      detail: error.message
    });
    return buildReport(normalizedPolicy, projectRoot, rootPath, 0, 0, violations);
  }

  if (!isPathInside(realRootPath, realProjectRoot)) {
    addViolation(violations, seen, {
      path: relativePosix(projectRoot, rootPath),
      code: "root-symlink-escapes-repository",
      detail: "candidate staging root resolves outside the repository"
    });
    return buildReport(normalizedPolicy, projectRoot, rootPath, 0, 0, violations);
  }

  if (!fs.statSync(rootPath).isDirectory()) {
    addViolation(violations, seen, {
      path: relativePosix(projectRoot, rootPath),
      code: "root-not-directory",
      detail: "candidate staging root must be a directory"
    });
    return buildReport(normalizedPolicy, projectRoot, rootPath, 0, 0, violations);
  }

  for (const stableRoot of normalizedPolicy.stableRoots) {
    const stablePath = path.resolve(projectRoot, stableRoot);
    if (isPathInside(rootPath, stablePath) || isPathInside(stablePath, rootPath)) {
      addViolation(violations, seen, {
        path: relativePosix(projectRoot, rootPath),
        code: "stable-root-overlap",
        detail: `candidate root overlaps Stable asset root ${toPosix(stableRoot)}`
      });
    }
  }

  // Never walk a Stable tree (or a parent containing one). The candidate gate
  // is intentionally isolated so a mistaken root cannot inspect or package
  // Stable assets as part of the preview check.
  if (violations.some((violation) => violation.code === "stable-root-overlap")) {
    return buildReport(normalizedPolicy, projectRoot, rootPath, 0, 0, violations);
  }

  const counters = { files: 0, directories: 0 };
  walk(rootPath, rootPath, normalizedPolicy, violations, seen, counters);
  return buildReport(normalizedPolicy, projectRoot, rootPath, counters.files, counters.directories, violations);
}

function buildReport(policy, projectRoot, rootPath, files, directories, violations) {
  let reportRoot = ".";
  try {
    if (typeof rootPath === "string" && rootPath.trim()) {
      reportRoot = relativePosix(projectRoot, path.resolve(rootPath));
    }
  } catch {
    reportRoot = ".";
  }
  return {
    schemaVersion: REPORT_SCHEMA_VERSION,
    candidate: policy.candidate,
    root: reportRoot,
    filesScanned: files,
    directoriesScanned: directories,
    violations,
    ok: violations.length === 0
  };
}

function walk(currentPath, rootPath, policy, violations, seen, counters) {
  let entries;
  try {
    entries = fs.readdirSync(currentPath, { withFileTypes: true }).sort((left, right) =>
      left.name.localeCompare(right.name, "en", { sensitivity: "base" })
    );
  } catch (error) {
    addViolation(violations, seen, {
      path: relativePosix(rootPath, currentPath),
      code: "entry-unreadable",
      detail: error.message
    });
    return;
  }

  for (const entry of entries) {
    const absolutePath = path.join(currentPath, entry.name);
    const relativePath = relativePosix(rootPath, absolutePath);
    const normalizedRelativePath = toPosix(relativePath);

    if (entry.isSymbolicLink()) {
      addViolation(violations, seen, {
        path: normalizedRelativePath,
        code: "symbolic-link",
        detail: "symbolic links are not allowed in candidate runtime assets"
      });
      continue;
    }

    const segments = normalizedRelativePath.split("/").map(normalizeToken);
    const forbiddenToken = segments.find((segment) => policy.forbiddenPathTokens.includes(segment));
    if (forbiddenToken) {
      addViolation(violations, seen, {
        path: normalizedRelativePath,
        code: "forbidden-path-token",
        detail: `forbidden runtime path token: ${forbiddenToken}`
      });
    }

    if (entry.isDirectory()) {
      counters.directories += 1;
      walk(absolutePath, rootPath, policy, violations, seen, counters);
      continue;
    }

    if (!entry.isFile()) {
      addViolation(violations, seen, {
        path: normalizedRelativePath,
        code: "unsupported-entry",
        detail: "candidate assets must be regular files or directories"
      });
      continue;
    }

    counters.files += 1;
    const basename = normalizeToken(entry.name);
    if (policy.forbiddenFileNames.includes(basename)) {
      addViolation(violations, seen, {
        path: normalizedRelativePath,
        code: "forbidden-file-name",
        detail: `forbidden runtime file: ${entry.name}`
      });
    }
    const extension = normalizeToken(path.extname(entry.name));
    if (extension && policy.forbiddenExtensions.includes(extension)) {
      addViolation(violations, seen, {
        path: normalizedRelativePath,
        code: "forbidden-file-extension",
        detail: `forbidden runtime extension: ${extension}`
      });
    }
  }
}

function parseArgs(argv) {
  const args = { root: process.env.STORYDEX_RUST_CANDIDATE_ROOT || "", policy: defaultPolicyPath, json: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--root") {
      args.root = argv[++index] || "";
    } else if (argument === "--policy" || argument === "--manifest") {
      args.policy = argv[++index] || "";
    } else if (argument === "--json") {
      args.json = true;
    } else if (argument === "--help" || argument === "-h") {
      args.help = true;
    } else {
      throw new Error(`unknown argument: ${argument}`);
    }
  }
  return args;
}

function usage() {
  return [
    "Usage: node validate-rust-candidate-assets.cjs --root <candidate-staging-root> [--manifest <policy.json>] [--json]",
    "The root must be an isolated directory inside this repository; Stable Electron assets and real user projects are never scanned."
  ].join("\n");
}

function runCli(argv = process.argv.slice(2)) {
  let args;
  try {
    args = parseArgs(argv);
    if (args.help) {
      console.log(usage());
      return 0;
    }
    if (!args.root) {
      throw new Error("--root is required (or set STORYDEX_RUST_CANDIDATE_ROOT)");
    }
    const { policy } = loadPolicy(args.policy);
    const report = inspectCandidateRoot(path.resolve(repoRoot, args.root), { projectRoot: repoRoot, policy });
    if (args.json) {
      console.log(JSON.stringify(report, null, 2));
    } else if (report.ok) {
      console.log(
        `[Storydex] Rust candidate runtime policy passed: ${report.filesScanned} files, ${report.directoriesScanned} directories (${report.root})`
      );
    } else {
      console.error(`[Storydex] Rust candidate runtime policy failed for ${report.root}:`);
      for (const violation of report.violations) {
        console.error(`- ${violation.path}: ${violation.code} (${violation.detail})`);
      }
    }
    return report.ok ? 0 : 1;
  } catch (error) {
    console.error(`[Storydex] Rust candidate runtime policy usage/configuration error: ${error.message}`);
    console.error(usage());
    return 2;
  }
}

if (require.main === module) {
  process.exitCode = runCli();
}

module.exports = {
  defaultPolicyPath,
  inspectCandidateRoot,
  loadPolicy,
  normalizePolicy,
  parseArgs,
  runCli,
  usage
};
