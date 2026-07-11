const assert = require("node:assert/strict");
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
