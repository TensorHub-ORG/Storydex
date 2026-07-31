const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const pythonRoot = path.join(desktopRoot, "app", "python-env");
const pythonExecutable =
  process.platform === "win32"
    ? path.join(pythonRoot, "python.exe")
    : path.join(pythonRoot, "bin", "python");
const pyvenvConfig = path.join(pythonRoot, "pyvenv.cfg");
const backendDirectory = path.join(desktopRoot, "app", "backend");
const maxEmbeddedPythonBytes = 512 * 1024 * 1024;
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

function directoryStats(directoryPath) {
  const pending = [directoryPath];
  let bytes = 0;
  let files = 0;
  while (pending.length) {
    const current = pending.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) {
        pending.push(target);
      } else if (entry.isFile()) {
        files += 1;
        bytes += fs.statSync(target).size;
      }
    }
  }
  return { bytes, files };
}

function buildPythonEnvironment(runtimeRoot = "") {
  const pathEntries =
    process.platform === "win32"
      ? [
          pythonRoot,
          path.join(pythonRoot, "Scripts"),
          path.join(pythonRoot, "Library", "bin"),
          path.join(pythonRoot, "Library", "usr", "bin"),
          path.join(pythonRoot, "DLLs")
        ]
      : [path.join(pythonRoot, "bin")];
  const environment = {
    ...process.env,
    PATH: [...pathEntries, String(process.env.PATH || "")].join(path.delimiter),
    PYTHONHOME: pythonRoot,
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
    PYTHONUNBUFFERED: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONDONTWRITEBYTECODE: "1"
  };
  if (runtimeRoot) {
    environment.STORYDEX_WORKSPACE_ROOT = path.join(runtimeRoot, "workspace");
    environment.STORYDEX_GLOBAL_ROOT = path.join(runtimeRoot, "global");
    environment.STORYDEX_DISABLE_NETWORK = "1";
  }
  return environment;
}

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0 }, () => {
      const address = server.address();
      const port = address && typeof address === "object" ? address.port : 0;
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

function probeBackendHealth(port) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    const request = http.request(
      {
        host: "127.0.0.1",
        port,
        path: "/api/v1/sys/health",
        method: "GET",
        agent: false,
        timeout: 1000
      },
      (response) => {
        let body = "";
        response.on("data", (chunk) => {
          body = `${body}${chunk}`.slice(-4000);
        });
        response.on("end", () =>
          finish({
            ok: typeof response.statusCode === "number" && response.statusCode >= 200 && response.statusCode < 300,
            statusCode: response.statusCode,
            detail: body
          })
        );
        response.on("error", (error) => finish({ ok: false, detail: error.message }));
      }
    );
    request.on("timeout", () => {
      request.destroy();
      finish({ ok: false, detail: "health request timed out" });
    });
    request.on("error", (error) => finish({ ok: false, detail: error.message }));
    request.end();
  });
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function stopProcess(child) {
  if (!child || child.exitCode !== null) return;
  child.kill();
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    delay(3000)
  ]);
}

async function validateBackendHealth() {
  const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-embedded-python-check-"));
  const port = await reservePort();
  let output = "";
  let spawnError = null;
  let lastProbe = { ok: false, detail: "health endpoint was not reached" };
  const child = spawn(
    pythonExecutable,
    ["-u", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: backendDirectory,
      env: buildPythonEnvironment(runtimeRoot),
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true
    }
  );
  const appendOutput = (chunk) => {
    output = `${output}${chunk.toString("utf8")}`.slice(-16000);
  };
  child.stdout.on("data", appendOutput);
  child.stderr.on("data", appendOutput);
  child.on("error", (error) => {
    spawnError = error;
  });

  try {
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      if (spawnError) {
        throw new Error(`backend spawn failed: ${spawnError.message}`);
      }
      if (child.exitCode !== null) {
        throw new Error(`backend exited with code ${child.exitCode}`);
      }
      lastProbe = await probeBackendHealth(port);
      if (lastProbe.ok) {
        return;
      }
      await delay(200);
    }
    throw new Error(
      `backend did not become healthy on port ${port}; last probe=${lastProbe.statusCode || "none"} ${lastProbe.detail || ""}`
    );
  } catch (error) {
    throw new Error(`${error.message}\n${output.trim() || "backend produced no output"}`);
  } finally {
    await stopProcess(child);
    fs.rmSync(runtimeRoot, { recursive: true, force: true });
  }
}

function reportFailures() {
  console.error("[Storydex Desktop] Embedded Python validation failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
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
  const stats = directoryStats(pythonRoot);
  if (stats.bytes > maxEmbeddedPythonBytes) {
    fail(
      `Embedded Python is unexpectedly large: ${(stats.bytes / 1024 / 1024).toFixed(1)} MB across ${stats.files} files ` +
        `(limit ${(maxEmbeddedPythonBytes / 1024 / 1024).toFixed(0)} MB). Check for copied Conda/CUDA/MKL payloads.`
    );
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
    "modules = ('coomi', 'fastapi', 'uvicorn', 'anthropic', 'pydantic_settings', 'dotenv', 'bcrypt', 'greenlet', 'jiter', 'psycopg', 'pydantic_core', 'ssl', 'sqlite3', 'tiktoken')",
    "for name in modules: __import__(name)",
    "import ssl",
    "import sqlite3",
    "ssl.create_default_context()",
    "with sqlite3.connect(':memory:') as connection:",
    "    if connection.execute('select 1').fetchone() != (1,):",
    "        errors.append('sqlite3 runtime query failed')",
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
    cwd: backendDirectory,
    encoding: "utf8",
    env: buildPythonEnvironment()
  });
  const output = `${result.stdout || ""}${result.stderr || ""}`.trim();
  if (result.status !== 0 || !output.includes("storydex-embedded-python-ok")) {
    fail(`Embedded Python preflight failed with exit=${result.status}:\n${output || result.error?.message || "no output"}`);
  }
}

if (failures.length) {
  reportFailures();
  process.exit(1);
}

validateBackendHealth()
  .then(() => {
    console.log(
      `[Storydex Desktop] Embedded Python is relocatable and serves the backend health endpoint: ${pythonExecutable}`
    );
  })
  .catch((error) => {
    fail(`Embedded Python backend health check failed:\n${error.message}`);
    reportFailures();
    process.exitCode = 1;
  });
