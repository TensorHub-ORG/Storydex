const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const tauriRoot = path.join(desktopRoot, "tauri-preview");

function read(relativePath) {
  return fs.readFileSync(path.join(tauriRoot, relativePath), "utf8");
}

test("Tauri Stable has an isolated build descriptor and minimum capability", () => {
  const config = JSON.parse(read("tauri.conf.json"));
  const capability = JSON.parse(read("capabilities/default.json"));
  const previewCapability = JSON.parse(read("capabilities/preview.json"));
  const desktopPackage = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  const launcher = fs.readFileSync(
    path.resolve(desktopRoot, "..", "..", "scripts", "run_desktop_dev.bat"),
    "utf8"
  );
  assert.equal(config.identifier, "cn.tensorhub.storydex");
  assert.equal(config.build.frontendDist, "../../frontend/dist");
  assert.equal(config.build.beforeDevCommand, "npm --prefix ../frontend run dev");
  assert.deepEqual(config.app.windows, []);
  assert.equal(config.app.withGlobalTauri, false);
  assert.match(config.app.security.csp, /connect-src[^;]*ipc:/);
  assert.match(config.app.security.csp, /script-src 'self'(?:;|$)/);
  assert.doesNotMatch(config.app.security.csp, /script-src[^;]*unsafe-inline/);
  assert.equal(Object.prototype.hasOwnProperty.call(config.bundle, "externalBin"), false);
  assert.deepEqual(capability.permissions, ["core:default", "updater:default"]);
  assert.deepEqual(capability.windows, ["main"]);
  assert.deepEqual(previewCapability.permissions, ["core:default"]);
  assert.deepEqual(previewCapability.windows, ["preview"]);
  assert.doesNotMatch(JSON.stringify(capability.permissions), /shell|fs|process/i);
  assert.doesNotMatch(JSON.stringify(previewCapability.permissions), /updater|shell|fs|process/i);
  assert.match(desktopPackage.scripts["check:tauri-preview"], /check:tauri/);
  assert.match(desktopPackage.scripts["build:tauri-preview"], /build:desktop/);
  assert.match(desktopPackage.scripts["smoke:tauri-preview"], /smoke:tauri/);
  assert.match(launcher, /cargo build[^\r\n]*storydex-agentd[^\r\n]*storydex-coomi-bridge/);
  assert.match(launcher, /storydex-coomi-bridge\.exe/);
  assert.match(launcher, /call npm run dev/);
  assert.doesNotMatch(
    launcher,
    /bootstrap_python39|build:coomi-runtime|sync:assets|electron\.exe|select_available_port|127\.0\.0\.1:18081/i
  );
});

test("Tauri Stable build input never points at legacy runtime assets", () => {
  const config = read("tauri.conf.json");
  const prepare = read("scripts/prepare-preview.ps1");
  const source = read("src/main.rs");
  const sidecar = read("src/sidecar.rs");
  const desktop = read("src/desktop.rs");
  assert.doesNotMatch(config, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.doesNotMatch(prepare, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.doesNotMatch(source, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.doesNotMatch(sidecar, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.doesNotMatch(desktop, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.match(prepare, /Join-Path \$previewRoot "\.\.\\\.\.\\\.\."/);
  assert.doesNotMatch(prepare, /Join-Path \$previewRoot "\.\.\\\.\.\\\.\.\\\.\."/);
  assert.match(prepare, /storydex-agentd\.exe/);
  assert.match(prepare, /storydex-coomi-bridge\.exe/);
  assert.match(prepare, /-p storydex-agentd -p storydex-coomi-bridge/);
  assert.match(prepare, /--locked/);
  const packageScript = read("scripts/package-preview.ps1");
  assert.match(packageScript, /externalBin/);
  assert.match(packageScript, /binaries\/storydex-coomi-bridge/);
  assert.match(packageScript, /tauri\.generated\.conf\.json/);
  assert.match(packageScript, /prepare-preview\.ps1/);
  assert.match(packageScript, /beforeBuildCommand/);
  assert.match(packageScript, /EncodedCommand/);
  assert.match(packageScript, /STORYDEX_TAURI_CLI/);
  assert.match(packageScript, /npx --no-install tauri build --ci/);
  assert.match(packageScript, /WriteAllText/);
  assert.match(packageScript, /UTF8Encoding\(\$false\)/);
  assert.match(packageScript, /Push-Location \$previewRoot/);
  assert.match(packageScript, /finally\s*\{\s*Pop-Location/s);
  assert.match(packageScript, /Join-Path \$desktopRoot "candidate"/);
  assert.match(packageScript, /Join-Path \$candidateRoot "staging"/);
  assert.match(packageScript, /check:rust-candidate/);
  const smokeScript = read("scripts/smoke-preview.ps1");
  assert.match(smokeScript, /GetTempPath/);
  assert.match(smokeScript, /STORYDEX_AGENTD_REFACTOR_ROOT/);
  assert.match(smokeScript, /STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE/);
  assert.match(smokeScript, /STORYDEX_TAURI_TEST_ROOT/);
  assert.match(smokeScript, /Invoke-RestMethod/);
  assert.match(smokeScript, /storydex-coomi-bridge\.exe/);
  assert.match(smokeScript, /--version/);
  assert.match(smokeScript, /CloseMainWindow/);
  assert.match(smokeScript, /Wait-ForProcessExit/);
  assert.match(smokeScript, /Remove-DirectoryWithRetry/);
  assert.match(smokeScript, /Remove-Item[^\r\n]*-ErrorAction Stop/);
  assert.match(smokeScript, /sidecar stopped cleanly/);
  assert.match(source, /runtime_info/);
  assert.match(source, /WebviewWindowBuilder/);
  assert.match(source, /data_directory/);
  assert.match(source, /WindowEvent::CloseRequested/);
  assert.match(source, /prevent_close/);
  assert.match(source, /app_handle\.exit\(0\)/);
  assert.match(sidecar, /backendAuthToken/);
  assert.match(sidecar, /STORYDEX_COOMI_BRIDGE/);
  assert.match(sidecar, /resolve_bridge_path/);
  assert.match(sidecar, /X-Storydex-Runtime-Token|Authorization: Bearer/);
  assert.match(sidecar, /\/api\/v1\/sys\/health/);
  assert.match(sidecar, /\/api\/v1\/sys\/shutdown/);
  assert.match(sidecar, /JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE/);
  assert.match(sidecar, /STORYDEX_TAURI_TEST_ROOT/);
  assert.match(sidecar, /STORYDEX_TESTING/);
  assert.doesNotMatch(sidecar, /127\.0\.0\.1:18081/);
  assert.match(sidecar, /pickDirectory/);
  assert.match(source, /pick_directory/);
  assert.match(desktop, /FileDialog/);
});
