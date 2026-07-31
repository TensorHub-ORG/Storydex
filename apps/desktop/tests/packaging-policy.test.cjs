const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {
  shouldCopyBackend,
  shouldCopyPythonBaseRuntime,
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

test("portable Python packaging excludes unrelated Conda CUDA and development payloads", () => {
  const root = "C:/conda/envs/pytorch";
  const accepted = [
    `${root}/python.exe`,
    `${root}/python39.dll`,
    `${root}/vcruntime140.dll`,
    `${root}/Lib`,
    `${root}/Lib/os.py`,
    `${root}/DLLs`,
    `${root}/DLLs/_ssl.pyd`,
    `${root}/Library`,
    `${root}/Library/bin`,
    `${root}/Library/bin/libcrypto-3-x64.dll`,
    `${root}/Library/bin/libssl-3-x64.dll`,
    `${root}/Library/bin/sqlite3.dll`,
    `${root}/Library/ssl`,
    `${root}/Library/ssl/cert.pem`
  ];
  const rejected = [
    `${root}/cublas64_11.dll`,
    `${root}/bin/cublasLt64_11.dll`,
    `${root}/Lib/x64/nvrtc_static.lib`,
    `${root}/Lib/nvperf_host.dll`,
    `${root}/Library/bin/mkl_core.2.dll`,
    `${root}/Library/include/openssl/ssl.h`,
    `${root}/Library/lib/libcrypto.lib`,
    `${root}/conda-meta/pytorch.json`,
    `${root}/include/Python.h`,
    `${root}/Scripts/conda.exe`
  ];

  for (const candidate of accepted) {
    assert.equal(shouldCopyPythonBaseRuntime(candidate, root), true, candidate);
  }
  for (const candidate of rejected) {
    assert.equal(shouldCopyPythonBaseRuntime(candidate, root), false, candidate);
  }
});

test("desktop backend startup uses only the resolved runtime port", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../electron/main.cjs"), "utf8");
  assert.doesNotMatch(source, /\bBACKEND_PORT\b/);
  assert.match(source, /PYTHONUNBUFFERED:\s*"1"/);
});

test("packaged E2E runtime roots require explicit test mode", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../electron/main.cjs"), "utf8");
  assert.match(source, /process\.env\.STORYDEX_TESTING\s*===\s*"1"/);
  assert.match(source, /testing\s*&&\s*configuredWorkspaceRoot/);
  assert.match(source, /testing\s*&&\s*configuredGlobalRoot/);
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
