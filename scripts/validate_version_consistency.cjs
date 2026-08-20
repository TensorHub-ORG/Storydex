"use strict";

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const expectedArg = process.argv.find((value) => value.startsWith("--expected="));
const expected = expectedArg ? expectedArg.slice("--expected=".length).replace(/^v/, "") : "";
const desktop = JSON.parse(fs.readFileSync(path.join(root, "apps", "desktop", "package.json"), "utf8"));
const lock = JSON.parse(fs.readFileSync(path.join(root, "apps", "desktop", "package-lock.json"), "utf8"));
const tauri = JSON.parse(fs.readFileSync(path.join(root, "apps", "desktop", "tauri-preview", "tauri.conf.json"), "utf8"));
const cargo = fs.readFileSync(path.join(root, "apps", "desktop", "tauri-preview", "Cargo.toml"), "utf8");
const version = String(desktop.version || "");
const cargoVersion = (cargo.match(/^version\s*=\s*"([^"]+)"/m) || [])[1] || "";
const failures = [];

function equal(label, actual, wanted) {
  if (String(actual || "") !== String(wanted || "")) failures.push(`${label}: ${actual || "<missing>"} != ${wanted}`);
}

if (!/^\d+\.\d+\.\d+$/.test(version)) failures.push(`invalid desktop version: ${version}`);
if (expected) equal("desktop version", version, expected);
equal("package-lock version", lock.version, version);
equal("package-lock root package version", lock.packages?.[""]?.version, version);
equal("Tauri config version", tauri.version, version);
equal("Tauri Cargo version", cargoVersion, version);

const readme = fs.readFileSync(path.join(root, "README.md"), "utf8");
if (!readme.includes(`v${version}`)) failures.push(`README does not identify current release v${version}`);
const notes = path.join(root, "apps", "desktop", "build", `release-notes-v${version}.md`);
if (expected && !fs.existsSync(notes)) failures.push(`missing ${path.relative(root, notes)}`);

if (failures.length) {
  console.error("Version consistency check failed:\n" + failures.map((item) => `- ${item}`).join("\n"));
  process.exit(1);
}
console.log(`Version consistency OK: v${version}`);
