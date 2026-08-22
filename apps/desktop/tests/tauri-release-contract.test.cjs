const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const previewRoot = path.join(desktopRoot, "tauri-preview");
const read = (relativePath) => fs.readFileSync(path.join(desktopRoot, relativePath), "utf8");

test("Stable desktop scripts use Tauri and no longer expose Electron as a package entry", () => {
  const pkg = JSON.parse(read("package.json"));
  assert.equal(pkg.main, undefined);
  assert.match(pkg.scripts.dev, /dev:tauri/);
  assert.match(pkg.scripts["build:desktop"], /package-preview\.ps1/);
  assert.match(pkg.scripts["package:win"], /build:desktop/);
  assert.doesNotMatch(JSON.stringify(pkg.devDependencies), /electron|electron-builder/i);
  assert.doesNotMatch(pkg.scripts["build:desktop"], /electron|python|electron-builder/i);
});

test("Tauri release contract requires signed updater artifacts and a Rust-only candidate", () => {
  const config = JSON.parse(fs.readFileSync(path.join(previewRoot, "tauri.conf.json"), "utf8"));
  const packageScript = read("tauri-preview/scripts/package-preview.ps1");
  const prepareScript = read("tauri-preview/scripts/prepare-preview.ps1");
  const artifactScript = read("scripts/prepare-tauri-artifacts.cjs");
  const validator = read("scripts/validate-tauri-release-assets.cjs");
  assert.equal(config.identifier, "cn.tensorhub.storydex");
  assert.equal(config.bundle.createUpdaterArtifacts, true);
  assert.equal(config.bundle.licenseFile, "../build/installer-license.zh-CN.txt");
  assert.equal(config.plugins.updater.endpoints[0], "https://updates.septemc.com/storydex/windows/latest.json");
  assert.match(packageScript, /TAURI_SIGNING_PRIVATE_KEY/);
  assert.match(packageScript, /STORYDEX_TAURI_UPDATER_PUBKEY/);
  assert.match(packageScript, /npx --no-install tauri build --ci/);
  assert.match(packageScript, /TAURI_SIGNING_PRIVATE_KEY_PATH/);
  assert.match(packageScript, /binaries\/storydex-agentd/);
  assert.match(packageScript, /binaries\/storydex-coomi-bridge/);
  assert.match(prepareScript, /-p storydex-agentd -p storydex-coomi-bridge/);
  assert.match(prepareScript, /storydex-coomi-bridge\.exe/);
  assert.match(artifactScript, /latest\.json/);
  assert.match(artifactScript, /updaterSignature/);
  assert.match(artifactScript, /\.sig/);
  assert.doesNotMatch(artifactScript, /\.nsis\.zip/);
  assert.match(artifactScript, /\.sig/);
  assert.match(validator, /windows-x86_64/);
  assert.match(validator, /inspectCandidateRoot/);
  assert.match(validator, /storydex-coomi-bridge\.exe/);
  assert.match(validator, /spawnSync\(bridgePath, \["--version"\]/);
  assert.match(validator, /Storydex-win-portable\.zip/);
  assert.deepEqual(config.bundle.icon, ["../../../assets/Storydex_icon/storydex_icon_01.ico"]);
  assert.doesNotMatch(artifactScript, /python|electron|node_modules/i);
});

test("Tauri Rust shell exposes the desktop capabilities consumed by Vue", () => {
  const main = fs.readFileSync(path.join(previewRoot, "src/main.rs"), "utf8");
  const sidecar = fs.readFileSync(path.join(previewRoot, "src/sidecar.rs"), "utf8");
  assert.match(main, /open_preview_window/);
  assert.match(main, /get_pending_open_target/);
  assert.match(main, /ack_open_target/);
  assert.match(main, /tauri_plugin_single_instance/);
  assert.match(sidecar, /openPreviewWindow/);
  assert.match(sidecar, /getPendingOpenTarget/);
  assert.match(sidecar, /ackOpenTarget/);
});
