"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const repoRoot = path.resolve(__dirname, "..", "..", "..");
const runtimePath = "apps/desktop/agent-runtime";

function git(args, input) {
  return execFileSync("git", args, { cwd: repoRoot, input, encoding: "utf8", windowsHide: true,
    timeout: 30000, maxBuffer: 32 * 1024 * 1024 });
}

function expectedIdentity() {
  const paths = [...new Set(git(["-c", "core.quotepath=false", "ls-files", "--full-name", "-z",
    "--cached", "--others", "--exclude-standard", "--", runtimePath]).split("\0").filter(Boolean))].sort();
  const hashes = git(["hash-object", "--stdin-paths"], `${paths.join("\n")}\n`).trim().split(/\r?\n/);
  if (paths.length !== hashes.length) throw new Error("Runtime source hash list is incomplete.");
  const manifest = paths.map((name, index) => `${name}\0${hashes[index]}\n`).join("");
  const cargo = fs.readFileSync(path.join(repoRoot, runtimePath, "Cargo.toml"), "utf8");
  const bridge = fs.readFileSync(path.join(repoRoot, runtimePath, "storydex-bridge/src/main.rs"), "utf8");
  const version = cargo.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
  const protocolVersion = Number(bridge.match(/const PROTOCOL_VERSION:\s*u\d+\s*=\s*(\d+);/)?.[1]);
  if (!version || !Number.isInteger(protocolVersion)) throw new Error("Runtime version or protocol is missing.");
  return {
    runtime: "storydex-coomi-rs", version, protocolVersion,
    gitSha: git(["rev-parse", "HEAD"]).trim(),
    sourceFingerprint: git(["hash-object", "--stdin"], manifest).trim(),
  };
}

function verifyBuildInfo(actual, expected) {
  for (const [key, value] of Object.entries(expected)) {
    if (actual?.[key] !== value) throw new Error(`Runtime ${key} does not match the current source.`);
  }
}

function main() {
  const binary = path.join(repoRoot, runtimePath, "target", "release",
    process.platform === "win32" ? "storydex-coomi-bridge.exe" : "storydex-coomi-bridge");
  const actual = JSON.parse(execFileSync(binary, ["--build-info"], {
    cwd: repoRoot, encoding: "utf8", windowsHide: true, timeout: 30000,
  }));
  const expected = expectedIdentity();
  verifyBuildInfo(actual, expected);
  console.log(`Runtime identity verified: ${actual.gitSha}, protocol ${actual.protocolVersion}.`);
}

if (require.main === module) main();
module.exports = { expectedIdentity, verifyBuildInfo };
