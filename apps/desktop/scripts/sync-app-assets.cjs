const fs = require("fs");
const path = require("path");

const desktopRoot = path.resolve(__dirname, "..");
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
  if (normalized.includes("/__pycache__/")) return false;
  if (normalized.includes("/.pytest_cache/")) return false;
  if (normalized.includes("/.mypy_cache/")) return false;
  if (normalized.includes("/tests/")) return false;
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
  if (normalized.includes("/__pycache__/")) return false;
  if (normalized.includes("/.pytest_cache/")) return false;
  if (normalized.includes("/.mypy_cache/")) return false;
  if (normalized.includes("/.ruff_cache/")) return false;
  if (normalized.endsWith(".pyc")) return false;
  return true;
}

function copyEmbeddedPythonEnv() {
  ensureSource(embeddedPythonSource, "embedded python env");
  resetDirectory(embeddedPythonTarget);
  fs.cpSync(embeddedPythonSource, embeddedPythonTarget, {
    recursive: true,
    filter: shouldCopyPythonEnv
  });
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
  copyHelpGuide();
  copyMinGit();
  copyEmbeddedPythonEnv();
  copyDesktopIcon();
  console.log("[Storydex Desktop] Synced app assets (frontend, backend, docs, MinGit, embedded python, icon) to apps/desktop/app.");
}

run();
