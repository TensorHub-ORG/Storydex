const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

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

requireFile("Storydex executable", path.join(unpacked, "Storydex.exe"));
const resources = path.join(unpacked, "resources");
requireDirectory("Electron resources", resources);
const appRoot = fs.existsSync(path.join(resources, "app", "app")) ? path.join(resources, "app", "app") : path.join(resources, "app");
requireFile("frontend index", path.join(appRoot, "frontend-dist", "index.html"));
requireDirectory("backend source", path.join(appRoot, "backend"));
requireDirectory("embedded Python", path.join(appRoot, "python-env"));
requireDirectory("MinGit", path.join(appRoot, "mingit"));
requireFile("updater config", path.join(resources, "app-update.yml"));
requireFile("electron-updater entrypoint", path.join(resources, "app", "node_modules", "electron-updater", "out", "main.js"));
requireFile("persistent update helper", path.join(resources, "app", "electron", "update-helper.ps1"));
const forbiddenPackageEntries = walk(appRoot).filter((file) => {
  const relative = path.relative(appRoot, file).replace(/\\/g, "/");
  const base = path.basename(relative);
  return (
    /(^|\/)(tests?|test-results|htmlcov|coverage-html|\.pytest_cache|\.mypy_cache|\.ruff_cache|__pycache__)(\/|$)/i.test(relative) ||
    /(^|\/)\.coverage(?:\.|$)/i.test(relative) ||
    /(^|\/)\.env(?:\.|$)/i.test(relative) ||
    /\.(pyc|log|tmp|temp)$/i.test(base)
  );
});
if (forbiddenPackageEntries.length) {
  failures.push(
    `packaged application contains test/cache/private files: ${forbiddenPackageEntries
      .slice(0, 20)
      .map((file) => path.relative(appRoot, file).replace(/\\/g, "/"))
      .join(", ")}`
  );
}
const fonts = walk(path.join(appRoot, "frontend-dist")).filter((file) => /\.woff2?$/.test(file));
if (!fonts.some((file) => file.endsWith(".woff")) || !fonts.some((file) => file.endsWith(".woff2"))) {
  failures.push("frontend build must contain both Material Symbols woff and woff2 assets");
}
for (const cssFile of walk(path.join(appRoot, "frontend-dist")).filter((file) => file.endsWith(".css"))) {
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

