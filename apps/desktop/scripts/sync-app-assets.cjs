const fs = require("fs");
const path = require("path");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const appRoot = path.join(desktopRoot, "app");
const frontendDistSource = path.resolve(desktopRoot, "..", "frontend", "dist");
const backendSource = path.resolve(desktopRoot, "..", "backend");
const helpGuideSource = path.resolve(desktopRoot, "..", "..", "docs", "使用指南");
const minGitSource =
  process.env.STORYDEX_MINGIT_SOURCE || path.resolve(desktopRoot, "vendor", "mingit");
const embeddedPythonSource =
  process.env.STORYDEX_EMBED_PYTHON || path.resolve(desktopRoot, "..", "..", ".python39");
const desktopIconSource = path.resolve(
  desktopRoot,
  "..",
  "..",
  "assets",
  "Storydex_icon",
  "storydex_icon_01.png"
);
const frontendDistTarget = path.join(appRoot, "frontend-dist");
const backendTarget = path.join(appRoot, "backend");
const helpGuideTarget = path.join(appRoot, "docs", "使用指南");
const minGitTarget = path.join(appRoot, "mingit");
const embeddedPythonTarget = path.join(appRoot, "python-env");
const desktopIconTarget = path.join(appRoot, "assets", "Storydex_icon", "storydex_icon_01.png");
const requirementsSource = path.join(repoRoot, "requirements.txt");
const requirementsLockSource = path.join(repoRoot, "requirements.lock");
const pythonWheelSource = path.join(repoRoot, "vendor", "python");

function ensureSource(pathValue, label) {
  if (!fs.existsSync(pathValue)) {
    throw new Error(`[Storydex Desktop] Missing source for ${label}: ${pathValue}`);
  }
}

function resetDirectory(pathValue) {
  fs.rmSync(pathValue, { recursive: true, force: true });
  fs.mkdirSync(pathValue, { recursive: true });
}

function copyDirectoryContents(sourceDir, targetDir, filter = () => true) {
  for (const entry of fs.readdirSync(sourceDir, { withFileTypes: true })) {
    const sourcePath = path.join(sourceDir, entry.name);
    if (!filter(sourcePath)) {
      continue;
    }
    const targetPath = path.join(targetDir, entry.name);
    if (entry.isDirectory()) {
      fs.mkdirSync(targetPath, { recursive: true });
      copyDirectoryContents(sourcePath, targetPath, filter);
      continue;
    }
    if (entry.isFile()) {
      fs.mkdirSync(path.dirname(targetPath), { recursive: true });
      fs.copyFileSync(sourcePath, targetPath);
    }
  }
}

function copyFrontendDist() {
  ensureSource(frontendDistSource, "frontend dist");
  resetDirectory(frontendDistTarget);
  fs.cpSync(frontendDistSource, frontendDistTarget, { recursive: true });
}

function shouldCopyBackend(sourcePath) {
  const normalized = String(sourcePath).replace(/\\/g, "/");
  const fileName = path.basename(normalized);
  if (normalized.includes("/__pycache__/")) return false;
  if (normalized.includes("/.pytest_cache/")) return false;
  if (normalized.includes("/.mypy_cache/")) return false;
  if (normalized.includes("/.ruff_cache/")) return false;
  if (normalized.includes("/test-results/")) return false;
  if (normalized.includes("/htmlcov/")) return false;
  if (normalized.includes("/coverage-html/")) return false;
  if (normalized.includes("/tests/")) return false;
  if (normalized.includes("/test_support/")) return false;
  if (normalized.includes("/-p/")) return false;
  if (fileName === ".env" || fileName.startsWith(".env.")) return false;
  if (fileName === ".coverage" || fileName.startsWith(".coverage.")) return false;
  if (/^(coverage|pytest)(\.json|\.xml)$/i.test(fileName)) return false;
  if (/\.(log|err|out|tmp|temp|pyc)$/i.test(fileName)) return false;
  if (/\/\.codex-[^/]+\.log$/i.test(normalized)) return false;
  if (/\/\.uvicorn-[^/]+\.log$/i.test(normalized)) return false;
  if (normalized.endsWith("/.DS_Store")) return false;
  return true;
}

function copyBackendSource() {
  ensureSource(backendSource, "backend source");
  resetDirectory(backendTarget);
  fs.cpSync(backendSource, backendTarget, {
    recursive: true,
    filter: shouldCopyBackend
  });
}

function copyRuntimeDependencyManifests() {
  ensureSource(requirementsSource, "root Python requirements");
  ensureSource(requirementsLockSource, "hashed Python requirements lock");
  ensureSource(pythonWheelSource, "vendored Python wheels");
  fs.copyFileSync(requirementsSource, path.join(backendTarget, "requirements-runtime.txt"));
  fs.copyFileSync(requirementsLockSource, path.join(backendTarget, "requirements-runtime.lock"));
  fs.cpSync(pythonWheelSource, path.join(backendTarget, "vendor", "python"), { recursive: true });
}

function copyHelpGuide() {
  ensureSource(helpGuideSource, "help guide");
  resetDirectory(helpGuideTarget);
  copyDirectoryContents(helpGuideSource, helpGuideTarget);
}

function shouldCopyMinGit(sourcePath) {
  const normalized = String(sourcePath).replace(/\\/g, "/");
  if (normalized.endsWith("/.DS_Store")) return false;
  return true;
}

function copyMinGit() {
  ensureSource(minGitSource, "MinGit");
  resetDirectory(minGitTarget);
  fs.cpSync(minGitSource, minGitTarget, {
    recursive: true,
    filter: shouldCopyMinGit
  });
}

function shouldCopyPythonEnv(sourcePath) {
  const normalized = String(sourcePath).replace(/\\/g, "/");
  if (/(^|\/)(test|tests)(\/|$)/i.test(normalized)) return false;
  if (normalized.includes("/__pycache__/")) return false;
  if (normalized.includes("/.pytest_cache/")) return false;
  if (normalized.includes("/.mypy_cache/")) return false;
  if (normalized.includes("/.ruff_cache/")) return false;
  if (normalized.endsWith(".pyc")) return false;
  return true;
}

function readPyvenvHome(sourceRoot) {
  const configPath = path.join(sourceRoot, "pyvenv.cfg");
  if (!fs.existsSync(configPath)) {
    return "";
  }

  const content = fs.readFileSync(configPath, "utf8");
  for (const line of content.split(/\r?\n/)) {
    const match = line.match(/^\s*home\s*=\s*(.+?)\s*$/i);
    if (match?.[1]) {
      return match[1].trim();
    }
  }
  return "";
}

function normalizePathForMatch(pathValue) {
  return String(path.resolve(pathValue)).replace(/\\/g, "/").toLowerCase();
}

function isInsidePath(candidatePath, rootPath) {
  const candidate = normalizePathForMatch(candidatePath);
  const root = normalizePathForMatch(rootPath).replace(/\/+$/, "");
  return candidate === root || candidate.startsWith(`${root}/`);
}

function shouldCopyPythonRuntime(sourcePath) {
  if (!shouldCopyPythonEnv(sourcePath)) return false;
  const normalized = String(sourcePath).replace(/\\/g, "/").toLowerCase();
  if (normalized.endsWith("/pyvenv.cfg")) return false;
  if (normalized.includes("/lib/site-packages/")) return false;
  return true;
}

function resolveSitePackagesDirectory(rootPath) {
  const windowsPath = path.join(rootPath, "Lib", "site-packages");
  if (fs.existsSync(windowsPath)) {
    return windowsPath;
  }

  const libRoot = path.join(rootPath, "lib");
  if (!fs.existsSync(libRoot)) {
    return "";
  }

  for (const entry of fs.readdirSync(libRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^python\d+\.\d+$/i.test(entry.name)) {
      continue;
    }
    const candidate = path.join(libRoot, entry.name, "site-packages");
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return "";
}

function hasRootPythonExecutable(rootPath) {
  const candidates =
    process.platform === "win32"
      ? [path.join(rootPath, "python.exe")]
      : [path.join(rootPath, "bin", "python"), path.join(rootPath, "python")];
  return candidates.some((candidate) => fs.existsSync(candidate));
}

function copyPortablePythonFromVenv(sourceRoot, targetRoot) {
  const baseRoot = readPyvenvHome(sourceRoot);
  if (!baseRoot) {
    throw new Error(`[Storydex Desktop] ${sourceRoot} is a venv, but pyvenv.cfg does not define home.`);
  }
  if (!fs.existsSync(baseRoot) || !hasRootPythonExecutable(baseRoot)) {
    throw new Error(
      [
        `[Storydex Desktop] Cannot package non-relocatable Python venv: ${sourceRoot}`,
        `pyvenv.cfg points to missing base runtime: ${baseRoot}`,
        "Build a fresh runtime on this machine or set STORYDEX_EMBED_PYTHON to a full Python runtime directory."
      ].join("\n")
    );
  }
  if (isInsidePath(baseRoot, sourceRoot)) {
    throw new Error(`[Storydex Desktop] Refusing to copy venv base runtime from inside the venv itself: ${baseRoot}`);
  }

  resetDirectory(targetRoot);
  copyDirectoryContents(baseRoot, targetRoot, shouldCopyPythonRuntime);

  const sourceSitePackages = resolveSitePackagesDirectory(sourceRoot);
  const targetSitePackages = resolveSitePackagesDirectory(targetRoot);
  if (!sourceSitePackages || !targetSitePackages) {
    throw new Error(
      `[Storydex Desktop] Failed to resolve site-packages while packaging embedded Python. source=${sourceSitePackages || "(missing)"} target=${targetSitePackages || "(missing)"}`
    );
  }
  copyDirectoryContents(sourceSitePackages, targetSitePackages, shouldCopyPythonEnv);

  if (fs.existsSync(path.join(targetRoot, "pyvenv.cfg"))) {
    throw new Error("[Storydex Desktop] Embedded Python target still contains pyvenv.cfg after portable runtime sync.");
  }
}

function copyEmbeddedPythonEnv() {
  ensureSource(embeddedPythonSource, "embedded python env");
  if (fs.existsSync(path.join(embeddedPythonSource, "pyvenv.cfg"))) {
    copyPortablePythonFromVenv(embeddedPythonSource, embeddedPythonTarget);
    return;
  }

  resetDirectory(embeddedPythonTarget);
  copyDirectoryContents(embeddedPythonSource, embeddedPythonTarget, shouldCopyPythonRuntime);
}

function copyDesktopIcon() {
  ensureSource(desktopIconSource, "desktop icon");
  fs.mkdirSync(path.dirname(desktopIconTarget), { recursive: true });
  fs.copyFileSync(desktopIconSource, desktopIconTarget);
}

function run() {
  fs.mkdirSync(appRoot, { recursive: true });
  copyFrontendDist();
  copyBackendSource();
  copyRuntimeDependencyManifests();
  copyHelpGuide();
  copyMinGit();
  copyEmbeddedPythonEnv();
  copyDesktopIcon();
  console.log("[Storydex Desktop] Synced app assets (frontend, backend, dependency manifests, docs, MinGit, embedded python, icon) to apps/desktop/app.");
}

if (require.main === module) {
  run();
}

module.exports = {
  shouldCopyBackend,
  shouldCopyPythonEnv,
  shouldCopyPythonRuntime
};
