const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");
const argumentsMap = new Map(process.argv.slice(2).map((value) => {
  const split = value.indexOf("=");
  return split < 0 ? [value, ""] : [value.slice(0, split), value.slice(split + 1)];
}));
const releaseDir = path.resolve(argumentsMap.get("--release-dir") || path.join(repoRoot, "release-assets"));
const desktop = JSON.parse(fs.readFileSync(path.join(repoRoot, "apps", "desktop", "package.json"), "utf8"));
const version = argumentsMap.get("--version") || desktop.version;
const testSummary = argumentsMap.get("--test-summary") || "quality gate passed";

function command(file, args) {
  try { return execFileSync(file, args, { cwd: repoRoot, encoding: "utf8" }).trim(); }
  catch { return "unknown"; }
}
function digest(filePath, algorithm, encoding = "hex") {
  return crypto.createHash(algorithm).update(fs.readFileSync(filePath)).digest(encoding);
}
function listArtifacts() {
  return fs.readdirSync(releaseDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && !["BUILD_MANIFEST.json", "SHA256SUMS.txt"].includes(entry.name))
    .map((entry) => {
      const target = path.join(releaseDir, entry.name);
      return { name: entry.name, size: fs.statSync(target).size, sha256: digest(target, "sha256") };
    })
    .sort((left, right) => left.name.localeCompare(right.name));
}
function dependencyInventory() {
  const packages = [];
  for (const app of ["frontend", "desktop"]) {
    const lock = JSON.parse(fs.readFileSync(path.join(repoRoot, "apps", app, "package-lock.json"), "utf8"));
    for (const [location, metadata] of Object.entries(lock.packages || {})) {
      if (!location || !metadata.version) continue;
      packages.push({ ecosystem: "npm", application: app, name: location.replace(/^node_modules\//, ""), version: metadata.version });
    }
  }
  const python = fs.readFileSync(path.join(repoRoot, "requirements.txt"), "utf8")
    .split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#") && !line.startsWith("-r"))
    .map((requirement) => ({ ecosystem: "pypi", requirement }));
  return { format: "Storydex dependency inventory v1", version, generatedAt: new Date().toISOString(), packages, python };
}

if (!fs.existsSync(releaseDir)) throw new Error(`Release directory does not exist: ${releaseDir}`);
const setupName = `StorydexSetup-x64-${version}.exe`;
const setupPath = path.join(releaseDir, setupName);
const latestPath = path.join(releaseDir, "latest.yml");
if (!fs.existsSync(setupPath) || !fs.existsSync(latestPath)) throw new Error("installer/latest.yml missing");
const latest = fs.readFileSync(latestPath, "utf8");
const declaredVersion = (latest.match(/^version:\s*(\S+)\s*$/m) || [])[1];
const declaredPath = (latest.match(/^path:\s*(\S+)\s*$/m) || [])[1];
const declaredSize = Number((latest.match(/^\s*size:\s*(\d+)\s*$/m) || [])[1]);
const declaredSha512 = (latest.match(/^\s*sha512:\s*(\S+)\s*$/m) || [])[1];
if (declaredVersion !== version || declaredPath !== setupName) throw new Error("latest.yml version/path mismatch");
if (declaredSize !== fs.statSync(setupPath).size) throw new Error("latest.yml size mismatch");
if (declaredSha512 !== digest(setupPath, "sha512", "base64")) throw new Error("latest.yml sha512 mismatch");

fs.writeFileSync(path.join(releaseDir, "DEPENDENCIES.json"), JSON.stringify(dependencyInventory(), null, 2) + "\n");
const manifest = {
  version,
  gitCommit: command("git", ["rev-parse", "HEAD"]),
  buildTime: new Date().toISOString(),
  nodeVersion: process.version,
  npmVersion: command(process.platform === "win32" ? "npm.cmd" : "npm", ["--version"]),
  pythonVersion: command("python", ["--version"]),
  electronVersion: desktop.devDependencies?.electron || "unknown",
  operatingSystem: `${os.type()} ${os.release()} ${os.arch()}`,
  testSummary,
  artifacts: listArtifacts()
};
fs.writeFileSync(path.join(releaseDir, "BUILD_MANIFEST.json"), JSON.stringify(manifest, null, 2) + "\n");
console.log(`Release metadata generated in ${releaseDir}`);
