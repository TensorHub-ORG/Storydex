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
const { resolveFrontendResource } = require("../scripts/wait-for-dev-frontend.cjs");

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

test("embedded Python packaging excludes test-only distributions from reused environments", () => {
  const rejected = [
    "C:/runtime/Lib/site-packages/pytest/__init__.py",
    "C:/runtime/Lib/site-packages/_pytest/config/__init__.py",
    "C:/runtime/Lib/site-packages/pytest_cov/plugin.py",
    "C:/runtime/Lib/site-packages/pytest-cov.pth",
    "C:/runtime/Lib/site-packages/pytest_timeout.py",
    "C:/runtime/Lib/site-packages/coverage/__init__.py",
    "C:/runtime/Lib/site-packages/hypothesis/__init__.py",
    "C:/runtime/Lib/site-packages/iniconfig/__init__.py",
    "C:/runtime/Lib/site-packages/pluggy/__init__.py"
  ];
  for (const candidate of rejected) {
    assert.equal(shouldCopyPythonEnv(candidate), false, candidate);
  }
  assert.equal(shouldCopyPythonEnv("C:/runtime/Lib/site-packages/fastapi/__init__.py"), true);
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

test("desktop development uses one dynamically selected frontend port", () => {
  const packageJson = require("../package.json");
  const viteConfig = fs.readFileSync(path.resolve(__dirname, "../../frontend/vite.config.ts"), "utf8");
  const launcher = fs.readFileSync(path.resolve(__dirname, "../../../scripts/run_desktop_dev.bat"), "utf8");

  assert.equal(resolveFrontendResource({}), "tcp:127.0.0.1:5173");
  assert.equal(
    resolveFrontendResource({ STORYDEX_DESKTOP_URL: "http://127.0.0.1:5174" }),
    "tcp:127.0.0.1:5174"
  );
  assert.match(packageJson.scripts["dev:electron"], /wait-for-dev-frontend\.cjs/);
  assert.match(viteConfig, /process\.env\.STORYDEX_FRONTEND_PORT/);
  assert.match(viteConfig, /strictPort:\s*true/);
  assert.match(launcher, /select_available_port\.ps1/);
  assert.match(launcher, /STORYDEX_DESKTOP_URL=http:\/\/127\.0\.0\.1:%STORYDEX_FRONTEND_PORT%/);
});

test("Python bootstrap verifies every locked runtime dependency version", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../../../scripts/bootstrap_python39.ps1"), "utf8");
  const functionStart = source.indexOf("function Test-RequirementsInstalled");
  const functionEnd = source.indexOf("function Install-RequirementsWithRetry", functionStart);
  const verificationSource = source.slice(functionStart, functionEnd);

  assert.match(verificationSource, /pip list[^\r\n]*--format=json/);
  assert.match(verificationSource, /Get-Content \$requirementsLockFile/);
  assert.match(verificationSource, /installedVersions\[\$lockedName\] -ne \$lockedVersion/);
});

test("embedded runtime validates the Rust bridge without a vendored Python Coomi wheel", () => {
  const source = fs.readFileSync(
    path.resolve(__dirname, "../scripts/validate-embedded-python.cjs"),
    "utf8"
  );
  assert.match(source, /storydex-coomi-bridge\.exe/);
  assert.match(source, /spawnSync\(bridgeExecutable, \["--version"\]/);
  assert.doesNotMatch(source, /vendor[\\/]python|--find-links/i);
});

test("packaged asset validation inspects the Electron asar archive", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../scripts/validate-packaged-assets.cjs"), "utf8");
  assert.match(source, /require\("@electron\/asar"\)/);
  assert.match(source, /asar\.listPackage\(archivePath\)/);
  assert.match(source, /asar\.extractFile\(archivePath/);
  assert.match(source, /function requireArchiveDirectoryMatchesSource/);
});
