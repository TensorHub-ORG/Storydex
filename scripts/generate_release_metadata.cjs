"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const repoRoot = path.resolve(__dirname, "..");
const argumentsMap = new Map(process.argv.slice(2).map((value) => {
  const split = value.indexOf("=");
  return split < 0 ? [value, ""] : [value.slice(0, split), value.slice(split + 1)];
}));
const releaseDir = path.resolve(argumentsMap.get("--release-dir") || path.join(repoRoot, "release-assets"));
const desktop = JSON.parse(fs.readFileSync(path.join(repoRoot, "apps", "desktop", "package.json"), "utf8"));
const version = argumentsMap.get("--version") || desktop.version;
const testSummary = argumentsMap.get("--test-summary") || "Tauri quality gate passed";

function command(file, args) {
  try { return execFileSync(file, args, { cwd: repoRoot, encoding: "utf8" }).trim(); }
  catch { return "unknown"; }
}
function digest(filePath, algorithm) {
  return crypto.createHash(algorithm).update(fs.readFileSync(filePath)).digest("hex");
}
function artifactRecord(name) {
  const target = path.join(releaseDir, name);
  return { name, size: fs.statSync(target).size, sha256: digest(target, "sha256") };
}
function listArtifacts() {
  return fs.readdirSync(releaseDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && !["BUILD_MANIFEST.json", "SHA256SUMS.txt"].includes(entry.name))
    .map((entry) => artifactRecord(entry.name))
    .sort((left, right) => left.name.localeCompare(right.name));
}
function npmDependencies(application) {
  const lock = JSON.parse(fs.readFileSync(path.join(repoRoot, "apps", application, "package-lock.json"), "utf8"));
  return Object.entries(lock.packages || [])
    .filter(([location, metadata]) => location && metadata.version)
    .map(([location, metadata]) => ({ ecosystem: "npm", application, name: location.replace(/^node_modules\//, ""), version: metadata.version }));
}
function cargoDependencies(lockPath, application) {
  const source = fs.readFileSync(lockPath, "utf8");
  const records = [];
  for (const block of source.split(/\r?\n\[\[package\]\]\r?\n/).slice(1)) {
    const name = (block.match(/^name\s*=\s*"([^"]+)"/m) || [])[1];
    const crateVersion = (block.match(/^version\s*=\s*"([^"]+)"/m) || [])[1];
    if (name && crateVersion) records.push({ ecosystem: "cargo", application, name, version: crateVersion });
  }
  return records;
}
function dependencyInventory() {
  const packages = [
    ...npmDependencies("frontend"),
    ...npmDependencies("desktop"),
    ...cargoDependencies(path.join(repoRoot, "apps", "desktop", "agent-runtime", "Cargo.lock"), "storydex-agentd"),
    ...cargoDependencies(path.join(repoRoot, "apps", "desktop", "tauri-preview", "Cargo.lock"), "storydex-tauri")
  ];
  return { format: "Storydex dependency inventory v2", runtime: "tauri-rust", version, generatedAt: new Date().toISOString(), packages };
}

if (!fs.existsSync(releaseDir)) throw new Error(`Release directory does not exist: ${releaseDir}`);
const setupName = `StorydexSetup-x64-${version}.exe`;
const updaterName = setupName;
const latestPath = path.join(releaseDir, "latest.json");
for (const name of [setupName, `${updaterName}.sig`, "latest.json", "Storydex-win-portable.zip"]) {
  if (!fs.existsSync(path.join(releaseDir, name))) throw new Error(`Tauri release asset missing: ${name}`);
}
const latest = JSON.parse(fs.readFileSync(latestPath, "utf8"));
const platform = latest.platforms?.["windows-x86_64"];
const signature = fs.readFileSync(path.join(releaseDir, `${updaterName}.sig`), "utf8").trim();
if (latest.version !== version) throw new Error("latest.json version mismatch");
if (!platform || platform.signature !== signature || !String(platform.url || "").endsWith(`/${updaterName}`)) {
  throw new Error("latest.json updater installer/signature mismatch");
}

fs.writeFileSync(path.join(releaseDir, "DEPENDENCIES.json"), `${JSON.stringify(dependencyInventory(), null, 2)}\n`);
const artifacts = listArtifacts();
const manifest = {
  version,
  runtime: "Tauri 2 + storydex-agentd",
  gitCommit: command("git", ["rev-parse", "HEAD"]),
  buildTime: new Date().toISOString(),
  rustVersion: command("rustc", ["--version"]),
  nodeVersion: process.version,
  tauriCliVersion: desktop.devDependencies?.["@tauri-apps/cli"] || "unknown",
  operatingSystem: `${os.type()} ${os.release()} ${os.arch()}`,
  testSummary,
  artifacts
};
const manifestName = "BUILD_MANIFEST.json";
fs.writeFileSync(path.join(releaseDir, manifestName), `${JSON.stringify(manifest, null, 2)}\n`);
const checksumRecords = [...artifacts, artifactRecord(manifestName)].sort((left, right) => left.name.localeCompare(right.name));
fs.writeFileSync(
  path.join(releaseDir, "SHA256SUMS.txt"),
  `${checksumRecords.map((artifact) => `${artifact.sha256.toUpperCase()}  ${artifact.name}`).join("\n")}\n`,
  "ascii"
);
console.log(`Tauri release metadata and checksums generated in ${releaseDir}`);
