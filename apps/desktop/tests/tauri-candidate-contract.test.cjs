const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const tauriRoot = path.join(desktopRoot, "tauri-preview");

function read(relativePath) {
  return fs.readFileSync(path.join(tauriRoot, relativePath), "utf8");
}

test("Tauri preview has an isolated build descriptor and minimum capability", () => {
  const config = JSON.parse(read("tauri.conf.json"));
  const capability = JSON.parse(read("capabilities/default.json"));
  const desktopPackage = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  assert.equal(config.identifier, "cn.tensorhub.storydex.preview");
  assert.equal(config.build.frontendDist, "../../frontend/dist");
  assert.equal(config.app.withGlobalTauri, false);
  assert.match(config.app.security.csp, /connect-src[^;]*ipc:/);
  assert.match(config.app.security.csp, /script-src 'self'(?:;|$)/);
  assert.doesNotMatch(config.app.security.csp, /script-src[^;]*unsafe-inline/);
  assert.equal(Object.prototype.hasOwnProperty.call(config.bundle, "externalBin"), false);
  assert.deepEqual(capability.permissions, ["core:default"]);
  assert.deepEqual(capability.windows, ["main"]);
  assert.doesNotMatch(JSON.stringify(capability.permissions), /shell|fs|updater|process/i);
  assert.match(desktopPackage.scripts["check:tauri-preview"], /tauri-preview\/Cargo\.toml --locked/);
  assert.match(desktopPackage.scripts["build:tauri-preview"], /tauri-preview\/scripts\/package-preview\.ps1/);
});

test("Tauri preview build input never points at Stable Electron or embedded Python", () => {
  const config = read("tauri.conf.json");
  const prepare = read("scripts/prepare-preview.ps1");
  const source = read("src/main.rs");
  assert.doesNotMatch(config, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.doesNotMatch(prepare, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.doesNotMatch(source, /electron|python|fastapi|uvicorn|node_modules/i);
  assert.match(prepare, /Join-Path \$previewRoot "\.\.\\\.\.\\\.\."/);
  assert.doesNotMatch(prepare, /Join-Path \$previewRoot "\.\.\\\.\.\\\.\.\\\.\."/);
  assert.match(prepare, /storydex-agentd\.exe/);
  assert.match(prepare, /--locked/);
  const packageScript = read("scripts/package-preview.ps1");
  assert.match(packageScript, /externalBin/);
  assert.match(packageScript, /tauri\.generated\.conf\.json/);
  assert.match(packageScript, /WriteAllText/);
  assert.match(packageScript, /UTF8Encoding\(\$false\)/);
  assert.match(packageScript, /Push-Location \$previewRoot/);
  assert.match(packageScript, /finally\s*\{\s*Pop-Location/s);
  assert.match(source, /runtime_info/);
});
