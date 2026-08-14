const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const runtimeRoot = path.join(desktopRoot, "coomi-rs-desktop");
const manifest = path.join(runtimeRoot, "Cargo.toml");
const binaryName = process.platform === "win32" ? "storydex-coomi-bridge.exe" : "storydex-coomi-bridge";
const binary = path.join(runtimeRoot, "target", "release", binaryName);
const buildMetadata = path.join(
  runtimeRoot,
  "target",
  "release",
  "storydex-coomi-build.json"
);

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
const identity = spawnSync(binary, ["--build-info"], {
  cwd: repoRoot,
  encoding: "utf8",
  windowsHide: true
});
if (identity.status !== 0) {
  throw new Error(
    `[Storydex Desktop] Rust Coomi bridge build identity failed: ${String(identity.stderr || identity.error?.message || "unknown error").trim()}`
  );
}
let parsedIdentity;
try {
  parsedIdentity = JSON.parse(String(identity.stdout || ""));
} catch (error) {
  throw new Error(`[Storydex Desktop] Invalid Rust Coomi build identity: ${error.message}`);
}
if (!parsedIdentity.version || !parsedIdentity.gitSha || !parsedIdentity.sourceFingerprint) {
  throw new Error(`[Storydex Desktop] Incomplete Rust Coomi build identity: ${identity.stdout}`);
}
fs.writeFileSync(buildMetadata, `${JSON.stringify(parsedIdentity, null, 2)}\n`, "utf8");
console.log(`[Storydex Desktop] Built Storydex Coomi Rust bridge: ${binary}`);
console.log(`[Storydex Desktop] Recorded Storydex Coomi build identity: ${buildMetadata}`);
