const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const pythonRoot = path.join(desktopRoot, "app", "python-env");
const pythonExecutable =
  process.platform === "win32"
    ? path.join(pythonRoot, "python.exe")
    : path.join(pythonRoot, "bin", "python");
const pyvenvConfig = path.join(pythonRoot, "pyvenv.cfg");
const requirementsFile = path.resolve(
  process.env.STORYDEX_REQUIREMENTS_FILE || path.join(repoRoot, "requirements.txt")
);
const requirementsLockFile = path.resolve(
  process.env.STORYDEX_REQUIREMENTS_LOCK || path.join(repoRoot, "requirements.lock")
);

const failures = [];

function fail(message) {
  failures.push(message);
}

function exists(filePath) {
  return fs.existsSync(filePath);
}

function normalizePackageName(value) {
  return String(value || "").toLowerCase().replace(/[_.]+/g, "-");
}

function readExpectedCoomiVersion(filePath) {
  const content = fs.readFileSync(filePath, "utf8");
  const matches = [...content.matchAll(/^\s*coomi-agent\s*==\s*([A-Za-z0-9_.+!-]+)\s*(?:#.*)?$/gim)];
  if (matches.length !== 1) {
    throw new Error(`requirements.txt must pin coomi-agent with == exactly once: ${filePath}`);
  }
  return matches[0][1];
}

function readLockedVersions(filePath) {
  const logicalLines = [];
  let current = "";
  for (const rawLine of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!current && (!line || line.startsWith("#"))) continue;
    current = current ? `${current} ${line}` : line;
    if (current.endsWith("\\")) {
      current = current.slice(0, -1).trimEnd();
      continue;
    }
    logicalLines.push(current);
    current = "";
  }
  if (current) throw new Error(`unterminated requirement in ${filePath}`);

  const versions = {};
  for (const line of logicalLines) {
    if (!line || line.startsWith("#")) continue;
    if (/^--find-links\s+vendor\/python$/i.test(line)) continue;
    const match = line.match(/^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;\\]+)/);
    if (!match) throw new Error(`unrecognized locked requirement: ${line}`);
    const hashes = line.match(/--hash=sha256:[a-f0-9]{64}/gi) || [];
    if (!hashes.length) throw new Error(`locked requirement has no SHA-256 hash: ${match[1]}`);
    const name = normalizePackageName(match[1]);
    if (versions[name] && versions[name] !== match[2]) {
      throw new Error(`conflicting locked versions for ${name}`);
    }
    versions[name] = match[2];
  }
  return versions;
}

if (!exists(pythonRoot)) {
  fail(`Embedded Python directory is missing: ${pythonRoot}`);
} else {
  if (exists(pyvenvConfig)) {
    fail("Embedded Python must not include pyvenv.cfg; Windows venv launchers are not relocatable after packaging.");
  }
  if (!exists(pythonExecutable)) {
    fail(`Embedded Python executable is missing: ${pythonExecutable}`);
  }
}

let expectedCoomiVersion = "";
let lockedVersions = {};
try {
  expectedCoomiVersion = readExpectedCoomiVersion(requirementsFile);
  lockedVersions = readLockedVersions(requirementsLockFile);
  if (lockedVersions["coomi-agent"] !== expectedCoomiVersion) {
    fail(
      `requirements.lock coomi-agent ${lockedVersions["coomi-agent"] || "<missing>"} ` +
        `does not match requirements.txt ${expectedCoomiVersion}`
    );
  }
} catch (error) {
  fail(`Python dependency manifest validation failed: ${error.message}`);
}

if (!failures.length) {
  const lockedVersionsJson = JSON.stringify(lockedVersions);
  const preflightCode = [
    "import json",
    "import sys",
    "import coomi",
    "from importlib.metadata import PackageNotFoundError, version",
    `expected = json.loads(${JSON.stringify(lockedVersionsJson)})`,
    `expected_coomi = ${JSON.stringify(expectedCoomiVersion)}`,
    "errors = []",
    "modules = ('coomi', 'fastapi', 'uvicorn', 'anthropic', 'pydantic_settings', 'dotenv')",
    "for name in modules: __import__(name)",
    "for package_name, expected_version in expected.items():",
    "    try:",
    "        actual_version = version(package_name)",
    "    except PackageNotFoundError:",
    "        errors.append(f'{package_name} is not installed (expected {expected_version})')",
    "        continue",
    "    if actual_version != expected_version:",
    "        errors.append(f'{package_name} {actual_version} != locked {expected_version}')",
    "if version('coomi-agent') != expected_coomi:",
    "    errors.append(f\"coomi-agent metadata {version('coomi-agent')} != expected {expected_coomi}\")",
    "if str(getattr(coomi, '__version__', '') or '') != expected_coomi:",
    "    errors.append(f\"coomi.__version__ {getattr(coomi, '__version__', '')!r} != expected {expected_coomi}\")",
    "if errors:",
    "    print('\\n'.join(errors), file=sys.stderr)",
    "    raise SystemExit(1)",
    "print('storydex-embedded-python-ok')",
    "print(sys.executable)",
    "print(sys.prefix)"
  ].join("\n");
  const result = spawnSync(pythonExecutable, ["-c", preflightCode], {
    cwd: path.join(desktopRoot, "app", "backend"),
    encoding: "utf8",
    env: {
      ...process.env,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONNOUSERSITE: "1",
      PYTHONDONTWRITEBYTECODE: "1"
    }
  });
  const output = `${result.stdout || ""}${result.stderr || ""}`.trim();
  if (result.status !== 0 || !output.includes("storydex-embedded-python-ok")) {
    fail(`Embedded Python preflight failed with exit=${result.status}:\n${output || result.error?.message || "no output"}`);
  }
}

if (failures.length) {
  console.error("[Storydex Desktop] Embedded Python validation failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log(`[Storydex Desktop] Embedded Python is relocatable and passes preflight: ${pythonExecutable}`);
