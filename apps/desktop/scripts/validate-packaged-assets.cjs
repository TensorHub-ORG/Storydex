const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const packageMetadata = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
const version = String(packageMetadata.version);
const args = new Map(process.argv.slice(2).map((value) => {
  const [key, ...rest] = value.split("=");
  return [key, rest.join("=")];
}));
const unpacked = path.resolve(args.get("--unpacked") || path.join(desktopRoot, "release", "win-unpacked"));
const releaseDir = args.has("--release") ? path.resolve(args.get("--release") || path.join(desktopRoot, "release")) : "";
const failures = [];

function requireFile(label, filePath) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) failures.push(`${label} missing: ${filePath}`);
}
function requireDirectory(label, directoryPath) {
  if (!fs.existsSync(directoryPath) || !fs.statSync(directoryPath).isDirectory()) failures.push(`${label} missing: ${directoryPath}`);
}
function walk(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(target) : [target];
  });
}
function sha256(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex").toUpperCase();
}
function sha256Buffer(content) {
  return crypto.createHash("sha256").update(content).digest("hex").toUpperCase();
}
function containsPemPrivateKey(content) {
  return /-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----/.test(content.toString("utf8"));
}
function archiveApiPath(entryName) {
  return path.join(...String(entryName).replace(/^[/\\]+/, "").split(/[/\\]+/));
}
function requireDirectoryMatchesSource(label, sourceDirectory, packagedDirectory) {
  requireDirectory(label, packagedDirectory);
  if (!fs.existsSync(sourceDirectory) || !fs.statSync(sourceDirectory).isDirectory()) {
    failures.push(`${label} source missing: ${sourceDirectory}`);
    return;
  }

  const sourceFiles = walk(sourceDirectory)
    .map((file) => path.relative(sourceDirectory, file).replace(/\\/g, "/"))
    .sort();
  const packagedFiles = walk(packagedDirectory)
    .map((file) => path.relative(packagedDirectory, file).replace(/\\/g, "/"))
    .sort();

  if (JSON.stringify(sourceFiles) !== JSON.stringify(packagedFiles)) {
    failures.push(
      `${label} file list mismatch: source=[${sourceFiles.join(", ")}] packaged=[${packagedFiles.join(", ")}]`
    );
    return;
  }

  for (const relative of sourceFiles) {
    const sourceFile = path.join(sourceDirectory, ...relative.split("/"));
    const packagedFile = path.join(packagedDirectory, ...relative.split("/"));
    if (sha256(sourceFile) !== sha256(packagedFile)) {
      failures.push(`${label} content mismatch: ${relative}`);
    }
  }
}
requireFile("Storydex executable", path.join(unpacked, "Storydex.exe"));
const resources = path.join(unpacked, "resources");
requireDirectory("Electron resources", resources);
const archivePath = path.join(resources, "app.asar");
requireFile("Electron app.asar", archivePath);
const archiveEntries = fs.existsSync(archivePath)
  ? new Set(asar.listPackage(archivePath).map(normalizeArchiveEntry))
  : new Set();
const appRoot = path.join(resources, "app.asar.unpacked", "app");
requireArchiveFile("frontend index", archiveEntries, "app/frontend-dist/index.html");
requireDirectory("backend source", path.join(appRoot, "backend"));
requireFile("runtime requirements", path.join(appRoot, "backend", "requirements-runtime.txt"));
requireFile("runtime requirements lock", path.join(appRoot, "backend", "requirements-runtime.lock"));
const bridgeBinary = path.join(appRoot, "backend", "runtime", "storydex-coomi-bridge.exe");
requireFile("Storydex Coomi Rust bridge", bridgeBinary);
if (fs.existsSync(bridgeBinary)) {
  const bridgeVersion = spawnSync(bridgeBinary, ["--version"], { cwd: repoRoot, encoding: "utf8", windowsHide: true });
  const output = `${bridgeVersion.stdout || ""}${bridgeVersion.stderr || ""}`;
  if (bridgeVersion.status !== 0 || !/storydex-coomi-bridge\s+\S+/i.test(output)) {
    failures.push(`Storydex Coomi Rust bridge --version failed: ${output.trim() || bridgeVersion.error?.message || "no output"}`);
  }
}
requireDirectory("embedded Python", path.join(appRoot, "python-env"));
requireDirectory("MinGit", path.join(appRoot, "mingit"));
requireFile("updater config", path.join(resources, "app-update.yml"));
requireArchiveFile("electron-updater entrypoint", archiveEntries, "node_modules/electron-updater/out/main.js");
requireFile("persistent update helper", path.join(resources, "app.asar.unpacked", "electron", "update-helper.ps1"));
requireArchiveDirectoryMatchesSource(
  "frontend build",
  archivePath,
  archiveEntries,
  path.join(desktopRoot, "app", "frontend-dist"),
  "app/frontend-dist"
);
for (const [label, directoryName] of [
  ["help guide", "guide"],
  ["prompt repository", "prompts"],
  ["built-in skills", "skills"]
]) {
  requireDirectoryMatchesSource(
    label,
    path.join(repoRoot, "docs", directoryName),
    path.join(appRoot, "docs", directoryName)
  );
}
for (const [sourceName, packagedName] of [
  ["requirements.txt", "requirements-runtime.txt"],
  ["requirements.lock", "requirements-runtime.lock"]
]) {
  const source = path.join(repoRoot, sourceName);
  const packaged = path.join(appRoot, "backend", packagedName);
  if (fs.existsSync(source) && fs.existsSync(packaged) && sha256(source) !== sha256(packaged)) {
    failures.push(`packaged ${packagedName} does not match root ${sourceName}`);
  }
}
const unpackedFiles = walk(appRoot);
const forbiddenPackageEntries = unpackedFiles.filter((file) => {
  const relative = path.relative(appRoot, file).replace(/\\/g, "/");
  const base = path.basename(relative);
  return (
    /(^|\/)(tests?|test-results|htmlcov|coverage-html|\.pytest_cache|\.mypy_cache|\.ruff_cache|__pycache__)(\/|$)/i.test(relative) ||
    /(^|\/)site-packages\/(?:_?pytest(?:\/|[-_.])|coverage(?:\/|[-_.])|_coverage(?:\/|[-_.])|hypothesis(?:\/|[-_.])|iniconfig(?:\/|[-_.])|pluggy(?:\/|[-_.]))/i.test(relative) ||
    /(^|\/)\.coverage(?:\.|$)/i.test(relative) ||
    /(^|\/)\.env(?:\.|$)/i.test(relative) ||
    /\.(pyc|log|tmp|temp)$/i.test(base)
  );
});
const forbiddenArchiveEntries = [...archiveEntries].filter((entry) => {
  const relative = entry.replace(/^\/+/, "");
  const base = path.basename(relative);
  return (
    /(^|\/)(test-results|playwright-report|htmlcov|coverage-html|\.pytest_cache|\.mypy_cache|\.ruff_cache|__pycache__)(\/|$)/i.test(relative) ||
    /(^|\/)\.coverage(?:\.|$)/i.test(relative) ||
    /(^|\/)\.env(?:\.|$)/i.test(relative) ||
    /\.(p12|pfx|key|kdbx|pyc|log|tmp|temp)$/i.test(base)
  );
});
for (const file of unpackedFiles.filter((item) => /\.pem$/i.test(item))) {
  if (containsPemPrivateKey(fs.readFileSync(file))) forbiddenPackageEntries.push(file);
}
for (const entry of [...archiveEntries].filter((item) => /\.pem$/i.test(item))) {
  try {
    if (containsPemPrivateKey(asar.extractFile(archivePath, archiveApiPath(entry)))) {
      forbiddenArchiveEntries.push(entry);
    }
  } catch (error) {
    failures.push(`unable to inspect PEM entry ${entry}: ${error.message}`);
  }
}
if (forbiddenPackageEntries.length || forbiddenArchiveEntries.length) {
  failures.push(
    `packaged application contains test/cache/private files: ${[
      ...forbiddenPackageEntries.map((file) => path.relative(appRoot, file).replace(/\\/g, "/")),
      ...forbiddenArchiveEntries
    ].slice(0, 20).join(", ")}`
  );
}
const frontendBuild = path.join(desktopRoot, "app", "frontend-dist");
const fonts = walk(frontendBuild).filter((file) => /\.woff2?$/.test(file));
if (!fonts.some((file) => file.endsWith(".woff")) || !fonts.some((file) => file.endsWith(".woff2"))) {
  failures.push("frontend build must contain both Material Symbols woff and woff2 assets");
}
for (const cssFile of walk(frontendBuild).filter((file) => file.endsWith(".css"))) {
  const css = fs.readFileSync(cssFile, "utf8");
  if (/https?:\/\//i.test(css) && /font/i.test(css)) failures.push(`external font URL found in ${cssFile}`);
  for (const match of css.matchAll(/url\((['"]?)([^)'"?#]+)\1\)/g)) {
    const target = path.resolve(path.dirname(cssFile), match[2]);
    if (!fs.existsSync(target)) failures.push(`unresolved CSS asset ${match[2]} from ${cssFile}`);
  }
}

if (releaseDir) {
  const setupName = `StorydexSetup-x64-${version}.exe`;
  const setup = path.join(releaseDir, setupName);
  const blockmap = `${setup}.blockmap`;
  const latest = path.join(releaseDir, "latest.yml");
  requireFile("installer", setup);
  requireFile("blockmap", blockmap);
  requireFile("latest.yml", latest);
  if (fs.existsSync(latest)) {
    const metadata = fs.readFileSync(latest, "utf8");
    if (!new RegExp(`^version:\\s*${version.replace(/\./g, "\\.")}\\s*$`, "m").test(metadata)) failures.push("latest.yml version mismatch");
    if (!metadata.includes(`path: ${setupName}`)) failures.push("latest.yml installer path mismatch");
    const size = Number((metadata.match(/^\s*size:\s*(\d+)\s*$/m) || [])[1]);
    if (fs.existsSync(setup) && size !== fs.statSync(setup).size) failures.push("latest.yml installer size mismatch");
  }
  const sums = path.join(releaseDir, "SHA256SUMS.txt");
  if (fs.existsSync(sums)) {
    for (const line of fs.readFileSync(sums, "utf8").split(/\r?\n/).filter(Boolean)) {
      const match = line.match(/^([A-Fa-f0-9]{64})\s+\*?(.+)$/);
      if (!match) { failures.push(`invalid checksum line: ${line}`); continue; }
      const target = path.join(releaseDir, match[2]);
      if (!fs.existsSync(target) || sha256(target) !== match[1].toUpperCase()) failures.push(`SHA256 mismatch: ${match[2]}`);
    }
  }
}

if (failures.length) {
  console.error("Packaged asset validation failed:\n" + failures.map((item) => `- ${item}`).join("\n"));
  process.exit(1);
}
console.log(`Packaged asset validation OK for Storydex ${version}`);

