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

test("Current website overlay points Windows downloads at the desktop release version", () => {
  const pkg = JSON.parse(read("package.json"));
  const overlay = fs.readFileSync(
    path.resolve(desktopRoot, "..", "website-overlay", "storydex-android-download-v0.1.4.js"),
    "utf8"
  );
  assert.match(
    overlay,
    new RegExp(`https://updates\\.septemc\\.com/storydex/windows/StorydexSetup-x64-${pkg.version.replaceAll(".", "\\.")}\\.exe`)
  );
});

test("Windows ICO puts the high-resolution image first for Tauri window icons", () => {
  const iconPaths = [
    path.resolve(desktopRoot, "..", "..", "assets", "Storydex_icon", "storydex_icon_01.ico"),
    path.join(previewRoot, "icons", "icon.ico")
  ];
  const expectedSizes = [256, 128, 96, 64, 48, 40, 32, 24, 20, 16];

  for (const iconPath of iconPaths) {
    const bytes = fs.readFileSync(iconPath);
    assert.equal(bytes.readUInt16LE(0), 0, `${iconPath} must be an ICO file`);
    assert.equal(bytes.readUInt16LE(2), 1, `${iconPath} must contain images`);
    const count = bytes.readUInt16LE(4);
    assert.equal(count, expectedSizes.length, `${iconPath} entry count changed`);
    const sizes = [];
    for (let index = 0; index < count; index += 1) {
      const offset = 6 + index * 16;
      sizes.push(bytes[offset] || 256);
    }
    assert.deepEqual(sizes, expectedSizes, `${iconPath} must keep the 256px entry first`);
    const firstDataOffset = bytes.readUInt32LE(6 + 12);
    assert.deepEqual(
      bytes.subarray(firstDataOffset, firstDataOffset + 8),
      Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
      `${iconPath} high-resolution entry must be a PNG image`
    );
  }

  assert.deepEqual(fs.readFileSync(iconPaths[0]), fs.readFileSync(iconPaths[1]));
});

test("NSIS installer license is UTF-8 text without a duplicate BOM", () => {
  const licensePath = path.join(desktopRoot, "build", "installer-license.zh-CN.txt");
  const bytes = fs.readFileSync(licensePath);
  assert.notDeepEqual(bytes.subarray(0, 2), Buffer.from([0xff, 0xfe]), "license must not be UTF-16LE");
  assert.notDeepEqual(bytes.subarray(0, 2), Buffer.from([0xfe, 0xff]), "license must not be UTF-16BE");
  assert.notDeepEqual(bytes.subarray(0, 3), Buffer.from([0xef, 0xbb, 0xbf]), "bundler adds the UTF-8 BOM");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  assert.match(text, /Storydex 软件许可与使用协议/);
  assert.doesNotMatch(text, /\u0000/);
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
