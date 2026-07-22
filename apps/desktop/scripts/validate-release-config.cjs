const fs = require("fs");
const path = require("path");

const desktopRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(desktopRoot, "..", "..");
const packageJsonPath = path.join(desktopRoot, "package.json");
const workflowPath = path.join(projectRoot, ".github", "workflows", "release-windows.yml");
const mainPath = path.join(desktopRoot, "electron", "main.cjs");
const staleReleaseConfigPath = path.join(desktopRoot, "storydex-release.json");
const appUpdateConfigScriptPath = path.join(desktopRoot, "scripts", "write-app-update-config.cjs");
const afterPackScriptPath = path.join(desktopRoot, "scripts", "after-pack.cjs");
const windowsSignScriptPath = path.join(desktopRoot, "scripts", "windows-sign.cjs");

const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
const workflow = fs.readFileSync(workflowPath, "utf8");
const mainSource = fs.readFileSync(mainPath, "utf8");
const failures = [];

function fail(message) {
  failures.push(message);
}

function assert(condition, message) {
  if (!condition) {
    fail(message);
  }
}

const packageVersion = String(packageJson.version || "").trim();
const metadataVersion = String(packageJson.build?.extraMetadata?.version || "").trim();
const metadataUpdateFeedUrl = String(packageJson.build?.extraMetadata?.storydexUpdateFeedUrl || "").trim();
const appId = String(packageJson.build?.appId || "").trim();
const artifactName = String(packageJson.build?.win?.artifactName || "").trim();
const buildDesktopScript = String(packageJson.scripts?.["build:desktop"] || "");
const packageWinScript = String(packageJson.scripts?.["package:win"] || "");
const publish = packageJson.build?.publish;
const publishEntries = Array.isArray(publish) ? publish : publish ? [publish] : [];
const genericFeed = publishEntries.find((entry) => String(entry?.provider || "").trim() === "generic" && entry?.url);
const genericFeedUrl = String(genericFeed?.url || "").trim();

assert(packageVersion, "apps/desktop/package.json must define version.");
assert(metadataVersion === packageVersion, "build.extraMetadata.version must match package.json version.");
assert(artifactName.includes("${version}"), "Windows artifactName must use ${version} instead of a literal version.");
assert(Boolean(packageJson.scripts?.["check:embedded-python"]), "package.json must define check:embedded-python.");
assert(buildDesktopScript.includes("check:embedded-python"), "build:desktop must validate the embedded Python runtime before electron-builder runs.");
assert(Boolean(packageJson.scripts?.["write:update-config"]), "package.json must define write:update-config.");
assert(buildDesktopScript.includes("write:update-config"), "build:desktop must write resources/app-update.yml before NSIS packaging.");
assert(fs.existsSync(appUpdateConfigScriptPath), "write:update-config script must exist.");
assert(fs.existsSync(afterPackScriptPath), "after-pack script must exist for signed Windows builds.");
assert(fs.existsSync(windowsSignScriptPath), "scoped Windows signing script must exist.");
assert(packageJson.build?.win?.signAndEditExecutable === false, "Global executable signing must stay disabled to avoid re-signing embedded Python and MinGit binaries.");
assert(packageJson.build?.win?.signtoolOptions?.sign === "scripts/windows-sign.cjs", "Windows builds must use the scoped Storydex signing hook.");
assert(JSON.stringify(packageJson.build?.win?.signtoolOptions?.signingHashAlgorithms) === '["sha256"]', "Windows releases must use SHA-256 Authenticode signatures.");
assert(packageWinScript.includes("electron-builder --win nsis"), "package:win must run a complete NSIS build.");
assert(!packageWinScript.includes("--prepackaged"), "package:win must not use --prepackaged because it skips app signing.");
assert(!packageWinScript.includes("signAndEditExecutable=false"), "package:win must not disable executable signing.");
assert(genericFeedUrl === "https://updates.septemc.com/storydex/windows/", "build.publish must point to the Storydex generic update feed.");
assert(metadataUpdateFeedUrl === genericFeedUrl, "build.extraMetadata.storydexUpdateFeedUrl must match build.publish URL.");
assert(!fs.existsSync(staleReleaseConfigPath), "Remove stale apps/desktop/storydex-release.json; it conflicts with package.json build config.");
assert(!workflow.includes(`StorydexSetup-x64-${packageVersion}`), "release workflow must not hardcode the current package version in artifact names.");
assert(!workflow.includes(`release-notes-v${packageVersion}.md`), "release workflow must not hardcode a versioned release notes path.");
assert(workflow.includes("package.json") && workflow.includes("GITHUB_OUTPUT"), "release workflow must derive release metadata from package.json/tag outputs.");
assert(workflow.includes("steps.release.outputs"), "release workflow must publish files selected by the release preparation step.");
assert(workflow.includes("test:update-feed"), "release workflow must run the desktop update feed regression test.");
assert(workflow.includes("WINDOWS_CSC_LINK") && workflow.includes("WINDOWS_CSC_KEY_PASSWORD"), "production release must require Windows signing secrets.");
assert(workflow.includes("Get-AuthenticodeSignature") && workflow.includes('Status -ne "Valid"'), "production release must verify app and installer signatures.");
assert(workflow.includes("publisherName"), "production release must require publisherName in app-update.yml for update signature verification.");
assert(!mainSource.includes('setAppUserModelId("'), "Electron AppUserModelId must be derived from package build.appId.");
assert(appId && mainSource.includes("DESKTOP_APP_ID"), "Electron main process must expose a DESKTOP_APP_ID derived from package metadata.");
assert(mainSource.includes("resolveUpdateFeedUrl") && mainSource.includes("autoUpdater.setFeedURL"), "Electron updater must set a default generic feed URL at runtime.");

if (failures.length) {
  console.error("[Storydex Desktop] Release configuration validation failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log(`[Storydex Desktop] Release configuration is valid for version ${packageVersion} (${appId}).`);
