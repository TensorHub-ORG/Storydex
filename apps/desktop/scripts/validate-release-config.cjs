"use strict";

const fs = require("node:fs");
const path = require("node:path");

const desktopRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(desktopRoot, "..", "..");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
const tauriConfig = JSON.parse(fs.readFileSync(path.join(desktopRoot, "tauri-preview", "tauri.conf.json"), "utf8"));
const capability = JSON.parse(fs.readFileSync(path.join(desktopRoot, "tauri-preview", "capabilities", "default.json"), "utf8"));
const previewCapability = JSON.parse(fs.readFileSync(path.join(desktopRoot, "tauri-preview", "capabilities", "preview.json"), "utf8"));
const workflow = fs.readFileSync(path.join(projectRoot, ".github", "workflows", "release-windows.yml"), "utf8");
const packageScript = fs.readFileSync(path.join(desktopRoot, "tauri-preview", "scripts", "package-preview.ps1"), "utf8");
const failures = [];

function assert(condition, message) {
  if (!condition) failures.push(message);
}

const version = String(packageJson.version || "").trim();
assert(/^\d+\.\d+\.\d+$/.test(version), "apps/desktop/package.json must define a semantic version.");
assert(tauriConfig.version === version, "Tauri config version must match apps/desktop/package.json.");
assert(tauriConfig.identifier === "cn.tensorhub.storydex", "Tauri Stable must use the production application identifier.");
assert(tauriConfig.productName === "Storydex", "Tauri Stable product name must be Storydex.");
assert(packageJson.main === undefined, "Stable desktop must not expose an Electron main entry.");
assert(!packageJson.dependencies?.["electron-updater"], "Stable desktop must not depend on electron-updater.");
assert(!packageJson.devDependencies?.electron, "Stable desktop package must not install Electron.");
assert(!packageJson.devDependencies?.["electron-builder"], "Stable desktop package must not install electron-builder.");
assert(String(packageJson.scripts?.dev || "").includes("dev:tauri"), "Default desktop development must run Tauri.");
assert(String(packageJson.scripts?.["build:desktop"] || "").includes("package-preview.ps1"), "Default desktop build must run the Tauri packaging wrapper.");
assert(String(packageJson.scripts?.["package:win"] || "").includes("build:desktop"), "Windows packaging must use the Tauri build.");
assert(tauriConfig.bundle?.createUpdaterArtifacts === true, "Tauri must create updater artifacts.");
assert(tauriConfig.bundle?.targets?.includes("nsis"), "Tauri Windows release must build NSIS.");
assert(tauriConfig.bundle?.licenseFile === "../build/installer-license.zh-CN.txt", "Tauri NSIS must retain the Stable installer license.");
assert(tauriConfig.plugins?.updater?.pubkey === "__STORYDEX_TAURI_UPDATER_PUBKEY__", "The checked-in Tauri config must not contain a production updater key.");
assert(tauriConfig.plugins?.updater?.endpoints?.[0] === "https://updates.septemc.com/storydex/windows/latest.json", "Tauri updater endpoint must use latest.json.");
assert(capability.permissions?.includes("updater:default"), "The main Tauri window must have updater permission.");
assert(!/shell|fs:|process:/i.test(JSON.stringify(capability.permissions || [])), "The renderer capability must not expose shell, filesystem, or process plugins.");
assert(previewCapability.windows?.includes("preview"), "The dynamically created preview window must have an explicit capability.");
assert(previewCapability.permissions?.includes("core:default"), "The preview window must be allowed to subscribe to Tauri core events.");
assert(!/updater|shell|fs:|process:/i.test(JSON.stringify(previewCapability.permissions || [])), "The preview window must not receive updater, shell, filesystem, or process permissions.");
assert(/npx --no-install tauri build --ci/.test(packageScript), "Tauri packaging must use the pinned npm CLI in non-interactive mode.");
assert(/STORYDEX_TAURI_UPDATER_PUBKEY/.test(packageScript), "Tauri packaging must require the updater public key.");
assert(/TAURI_SIGNING_PRIVATE_KEY/.test(packageScript), "Tauri packaging must require the updater private key.");
assert(/TAURI_SIGNING_PRIVATE_KEY_PATH/.test(packageScript), "Tauri packaging must support CI private key files without treating the path as key content.");
assert(/quality-gate\.yml/.test(workflow), "Windows release must depend on the reusable full quality gate.");
assert(/TAURI_SIGNING_PRIVATE_KEY/.test(workflow), "Windows release must load the updater private key from CI secrets.");
assert(/STORYDEX_TAURI_UPDATER_PUBKEY/.test(workflow), "Windows release must load the updater public key from CI secrets.");
assert(/latest\.json/.test(workflow) && /\.exe\.sig/.test(workflow), "Windows release must publish the Tauri updater manifest, signed NSIS installer, and signature.");
assert(/steps\.release\.outputs\.updater_signature_path/.test(workflow), "GitHub Release must include the signed NSIS updater signature.");
assert(/\$\(\$env:SETUP_NAME\)\.sig/.test(workflow) && /\$env:PORTABLE_NAME/.test(workflow), "VPS publishing must upload the updater signature and portable archive.");
assert(!/\$target:/.test(workflow), "PowerShell remote targets must delimit the target variable before a colon.");
assert(!/setup-python|bootstrap_python39|embedded Python|electron-builder|Electron E2E/i.test(workflow), "Stable Windows release must not build Python or Electron runtime assets.");

if (failures.length) {
  console.error("[Storydex Desktop] Tauri release configuration validation failed:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log(`[Storydex Desktop] Tauri release configuration is valid for version ${version}.`);
