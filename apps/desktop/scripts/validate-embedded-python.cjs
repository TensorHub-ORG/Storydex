const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const desktopRoot = path.resolve(__dirname, "..");
const pythonRoot = path.join(desktopRoot, "app", "python-env");
const pythonExecutable =
  process.platform === "win32"
    ? path.join(pythonRoot, "python.exe")
    : path.join(pythonRoot, "bin", "python");
const pyvenvConfig = path.join(pythonRoot, "pyvenv.cfg");

const failures = [];

function fail(message) {
  failures.push(message);
}

function exists(filePath) {
  return fs.existsSync(filePath);
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

if (!failures.length) {
  const preflightCode = [
    "import sys",
    "modules = ('fastapi', 'uvicorn', 'anthropic', 'pydantic_settings', 'dotenv')",
    "for name in modules: __import__(name)",
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
      PYTHONNOUSERSITE: "1"
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
