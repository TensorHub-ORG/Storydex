const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {
  shouldCopyBackend,
  shouldCopyPythonEnv,
  shouldCopyPythonRuntime
} = require("../scripts/sync-app-assets.cjs");

test("backend packaging excludes tests, coverage, caches, logs, and environment files", () => {
  const rejected = [
    "C:/repo/apps/backend/tests/test_agent.py",
    "C:/repo/apps/backend/test-results/coverage.json",
    "C:/repo/apps/backend/htmlcov/index.html",
    "C:/repo/apps/backend/.pytest_cache/v/cache/nodeids",
    "C:/repo/apps/backend/.ruff_cache/state",
    "C:/repo/apps/backend/services/__pycache__/agent.pyc",
    "C:/repo/apps/backend/.coverage",
    "C:/repo/apps/backend/.coverage.worker",
    "C:/repo/apps/backend/.env",
    "C:/repo/apps/backend/.env.local",
    "C:/repo/apps/backend/backend.log"
  ];
  for (const candidate of rejected) {
    assert.equal(shouldCopyBackend(candidate), false, candidate);
  }
  assert.equal(shouldCopyBackend("C:/repo/apps/backend/services/agent.py"), true);
});

test("embedded Python packaging excludes caches and non-relocatable venv metadata", () => {
  assert.equal(shouldCopyPythonEnv("C:/runtime/Lib/ctypes/test/test_arrays.py"), false);
  assert.equal(shouldCopyPythonEnv("C:/runtime/Lib/site-packages/pkg/tests/test_api.py"), false);
  assert.equal(shouldCopyPythonEnv("C:/runtime/Lib/site-packages/pkg/__pycache__/x.pyc"), false);
  assert.equal(shouldCopyPythonEnv("C:/runtime/.pytest_cache/state"), false);
  assert.equal(shouldCopyPythonRuntime("C:/runtime/pyvenv.cfg"), false);
  assert.equal(shouldCopyPythonRuntime("C:/runtime/Lib/site-packages/pkg/__init__.py"), false);
  assert.equal(shouldCopyPythonRuntime("C:/runtime/python.exe"), true);
});

test("Python bootstrap prefers standard Python 3.9 before Conda fallback", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../../../scripts/bootstrap_python39.ps1"), "utf8");
  const functionStart = source.indexOf("function Get-PythonCandidate");
  const functionEnd = source.indexOf("function New-InternalPython", functionStart);
  const candidateSource = source.slice(functionStart, functionEnd);
  assert.ok(candidateSource.indexOf('$candidates = @(') < candidateSource.indexOf("Get-CondaPython39Candidate"));
  assert.match(candidateSource, /STORYDEX_PYTHON_SOURCE/);
});

test("embedded Python validation accepts only the vendored wheel source", () => {
  const source = fs.readFileSync(
    path.resolve(__dirname, "../scripts/validate-embedded-python.cjs"),
    "utf8"
  );
  assert.match(source, /\^--find-links\\s\+vendor\\\/python\$/i);
});
