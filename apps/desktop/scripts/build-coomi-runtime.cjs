const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const manifest = path.join(repoRoot, "vendor", "coomi-rs", "Cargo.toml");
const binaryName = process.platform === "win32" ? "storydex-coomi-bridge.exe" : "storydex-coomi-bridge";
const binary = path.join(repoRoot, "vendor", "coomi-rs", "target", "release", binaryName);

if (!fs.existsSync(manifest)) {
  throw new Error(`[Storydex Desktop] Rust bridge manifest missing: ${manifest}`);
}

const cargo = process.env.CARGO || "cargo";
const result = spawnSync(
  cargo,
  ["build", "--release", "--locked", "--manifest-path", manifest, "-p", "storydex-coomi-bridge"],
  { cwd: repoRoot, stdio: "inherit", windowsHide: true }
);
if (result.error) {
  throw new Error(`[Storydex Desktop] Failed to start Cargo: ${result.error.message}`);
}
if (result.status !== 0) {
  throw new Error(`[Storydex Desktop] Rust Coomi bridge build failed with exit code ${result.status}`);
}
if (!fs.existsSync(binary)) {
  throw new Error(`[Storydex Desktop] Cargo completed but bridge binary is missing: ${binary}`);
}
console.log(`[Storydex Desktop] Built Storydex Coomi Rust bridge: ${binary}`);
