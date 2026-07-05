const fs = require("fs");
const path = require("path");

const desktopRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(desktopRoot, "..", "..");
const packageJsonPath = path.join(desktopRoot, "package.json");
const workflowPath = path.join(projectRoot, ".github", "workflows", "release-windows.yml");
const mainPath = path.join(desktopRoot, "electron", "main.cjs");
const staleReleaseConfigPath = path.join(desktopRoot, "storydex-release.json");

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
const appId = String(packageJson.build?.appId || "").trim();
const artifactName = String(packageJson.build?.win?.artifactName || "").trim();
const buildDesktopScript = String(packageJson.scripts?.["build:desktop"] || "");

assert(packageVersion, "apps/desktop/package.json must define version.");
assert(metadataVersion === packageVersion, "build.extraMetadata.version must match package.json version.");
assert(artifactName.includes("${version}"), "Windows artifactName must use ${version} instead of a literal version.");
assert(Boolean(packageJson.scripts?.["check:embedded-python"]), "package.json must define check:embedded-python.");
assert(buildDesktopScript.includes("check:embedded-python"), "build:desktop must validate the embedded Python runtime before electron-builder runs.");
assert(!fs.existsSync(staleReleaseConfigPath), "Remove stale apps/desktop/storydex-release.json; it conflicts with package.json build config.");
assert(!workflow.includes(`StorydexSetup-x64-${packageVersion}`), "release workflow must not hardcode the current package version in artifact names.");
assert(!workflow.includes(`release-notes-v${packageVersion}.md`), "release workflow must not hardcode a versioned release notes path.");
assert(workflow.includes("package.json") && workflow.includes("GITHUB_OUTPUT"), "release workflow must derive release metadata from package.json/tag outputs.");
assert(workflow.includes("steps.release.outputs"), "release workflow must publish files selected by the release preparation step.");
assert(!mainSource.includes('setAppUserModelId("'), "Electron AppUserModelId must be derived from package build.appId.");
assert(appId && mainSource.includes("DESKTOP_APP_ID"), "Electron main process must expose a DESKTOP_APP_ID derived from package metadata.");

if (failures.length) {
  console.error("[Storydex Desktop] Release configuration validation failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log(`[Storydex Desktop] Release configuration is valid for version ${packageVersion} (${appId}).`);
