const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const manifestPath = path.join(desktopRoot, "agent-runtime", "Cargo.toml");
const release = process.argv.includes("--release");
const profile = release ? "release" : "debug";
const executableSuffix = process.platform === "win32" ? ".exe" : "";
const cargoArgs = ["build", "--locked", "--manifest-path", manifestPath, "-p", "storydex-agentd", "-p", "storydex-coomi-bridge"];
if (release) cargoArgs.splice(1, 0, "--release");

const result = spawnSync(process.env.CARGO || "cargo", cargoArgs, {
  cwd: repoRoot,
  stdio: "inherit",
  windowsHide: true
});
if (result.error) throw new Error(`Failed to start Cargo: ${result.error.message}`);
if (result.status !== 0) throw new Error(`Rust Beta runtime build failed with exit ${result.status}`);

for (const binaryName of ["storydex-agentd", "storydex-coomi-bridge"]) {
  const binaryPath = path.join(desktopRoot, "agent-runtime", "target", profile, `${binaryName}${executableSuffix}`);
  if (!fs.existsSync(binaryPath)) throw new Error(`Rust Beta runtime binary is missing: ${binaryPath}`);
  const version = spawnSync(binaryPath, ["--version"], { encoding: "utf8", windowsHide: true });
  if (version.status !== 0 || !String(version.stdout || version.stderr || "").includes(binaryName)) {
    throw new Error(`Rust Beta runtime identity check failed: ${binaryPath}`);
  }
  console.log(`[Storydex Rust Beta] Built ${binaryPath}`);
}
