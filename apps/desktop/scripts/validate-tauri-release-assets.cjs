"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { inspectCandidateRoot, loadPolicy } = require("./validate-rust-candidate-assets.cjs");

const desktopRoot = path.resolve(__dirname, "..");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
const version = String(packageJson.version || "").trim();
const releaseArg = process.argv.find((value) => value.startsWith("--release="));
const candidateArg = process.argv.find((value) => value.startsWith("--candidate="));
const releaseRoot = path.resolve(releaseArg?.slice("--release=".length) || path.join(desktopRoot, "release"));
const candidateRoot = path.resolve(candidateArg?.slice("--candidate=".length) || path.join(desktopRoot, "candidate", "staging"));
const setupName = `StorydexSetup-x64-${version}.exe`;
const updaterName = setupName;

for (const name of [setupName, `${updaterName}.sig`, "latest.json", "Storydex-win-portable.zip"]) {
  if (!fs.statSync(path.join(releaseRoot, name), { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`missing Tauri release asset: ${path.join(releaseRoot, name)}`);
  }
}

const latest = JSON.parse(fs.readFileSync(path.join(releaseRoot, "latest.json"), "utf8"));
const platform = latest.platforms?.["windows-x86_64"];
const signature = fs.readFileSync(path.join(releaseRoot, `${updaterName}.sig`), "utf8").trim();
if (latest.version !== version) throw new Error(`latest.json version mismatch: ${latest.version}`);
if (!platform || platform.signature !== signature) throw new Error("latest.json signature does not match the Tauri .sig asset");
if (!String(platform.url || "").endsWith(`/${updaterName}`)) throw new Error("latest.json updater URL does not reference the signed NSIS installer");

const report = inspectCandidateRoot(candidateRoot, {
  projectRoot: path.resolve(desktopRoot, "..", ".."),
  policy: loadPolicy().policy
});
if (!report.ok) {
  throw new Error(`Tauri runtime asset policy failed: ${JSON.stringify(report.violations)}`);
}
for (const name of ["Storydex.exe", "storydex-agentd.exe", path.join("mingit", "cmd", "git.exe")]) {
  if (!fs.existsSync(path.join(candidateRoot, name))) throw new Error(`missing Tauri runtime asset: ${name}`);
}

console.log(`[Storydex Desktop] Tauri release assets are valid for v${version}.`);
