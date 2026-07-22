const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { resolveUpdateFeedUrl } = require("../electron/update-feed.cjs");

const root = path.resolve(__dirname, "..");
const projectRoot = path.resolve(root, "..", "..");
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));

test("package, lockfile, extra metadata, and artifact naming agree", () => {
  assert.equal(pkg.version, pkg.build.extraMetadata.version);
  assert.equal(pkg.version, lock.version);
  assert.equal(pkg.version, lock.packages[""].version);
  assert.equal(pkg.build.win.artifactName, "StorydexSetup-x64-${version}.${ext}");
});

test("release configuration is offline-capable and updater-aware", () => {
  assert.equal(pkg.build.asar, false);
  assert.ok(pkg.build.files.includes("app/**/*"));
  assert.equal(pkg.build.publish[0].provider, "generic");
  assert.equal(resolveUpdateFeedUrl(pkg), pkg.build.extraMetadata.storydexUpdateFeedUrl);
  assert.match(pkg.build.extraMetadata.storydexUpdateFeedUrl, /^https:\/\//);
});

test("desktop packages the guide and prompt repository and exposes their roots to the backend", () => {
  const mainSource = fs.readFileSync(path.join(root, "electron", "main.cjs"), "utf8");
  const syncSource = fs.readFileSync(path.join(root, "scripts", "sync-app-assets.cjs"), "utf8");
  assert.match(mainSource, /STORYDEX_HELP_GUIDE_ROOT:\s*helpGuideRoot/);
  assert.match(mainSource, /STORYDEX_PROMPT_REPOSITORY_ROOT:\s*promptRepositoryRoot/);
  assert.match(mainSource, /STORYDEX_BUILTIN_SKILLS_ROOT:\s*builtinSkillsRoot/);
  assert.match(syncSource, /docs",\s*"guide"/);
  assert.match(syncSource, /docs",\s*"prompts"/);
  assert.match(syncSource, /docs",\s*"skills"/);
  assert.doesNotMatch(syncSource, /docs",\s*"使用指南"/);
});

test("desktop source declares process cleanup and a strict IPC whitelist", () => {
  const source = fs.readFileSync(path.join(root, "electron", "main.cjs"), "utf8");
  assert.match(source, /app\.on\("before-quit"[\s\S]*stopBackendKernel\(\)/);
  assert.match(source, /taskkill[\s\S]*\/t[\s\S]*\/f/i);
  assert.match(source, /PYTHONDONTWRITEBYTECODE:\s*"1"/);
  const channels = [...source.matchAll(/ipcMain\.handle\("([^"]+)"/g)].map((match) => match[1]);
  assert.ok(channels.length >= 8);
  assert.equal(new Set(channels).size, channels.length);
  assert.ok(channels.every((channel) => channel.startsWith("storydex:")));
});

test("local release scripts derive version from package.json, not hardcoded strings", () => {
  const runSuite = fs.readFileSync(path.join(projectRoot, "scripts", "run_full_test_suite.ps1"), "utf8");
  const prepareBundle = fs.readFileSync(path.join(projectRoot, "scripts", "prepare_release_bundle.ps1"), "utf8");

  // These scripts must NOT contain a literal three-segment version number as a standalone string
  // that would drift from apps/desktop/package.json.
  const versionPattern = /(?:^|[^.\d])\d+\.\d+\.\d+(?:[^.\d]|$)/g;
  const suiteMatches = [...runSuite.matchAll(versionPattern)].map((m) => m[0].trim());
  const bundleMatches = [...prepareBundle.matchAll(versionPattern)].map((m) => m[0].trim());

  const allowedSuite = suiteMatches.filter(
    (match) => match === pkg.version || match === "69.8" || match === "89.5" || match === "70.0" || match === "90.0"
  );
  assert.equal(
    suiteMatches.length,
    allowedSuite.length,
    "run_full_test_suite.ps1 contains hardcoded version drift: " +
      suiteMatches.filter((m) => !allowedSuite.includes(m)).join(", ")
  );

  // prepare_release_bundle.ps1 default is a dynamic expression; the only literal version allowed
  // is the current package version appearing in a fallback or comment context.
  const allowedBundle = bundleMatches.filter(
    (match) => match === "1.0" || match === "88.0"
  );
  assert.equal(
    bundleMatches.length,
    allowedBundle.length,
    "prepare_release_bundle.ps1 contains hardcoded version drift: " +
      bundleMatches.filter((m) => !allowedBundle.includes(m)).join(", ")
  );
});

test("desktop updater retries transient module replacement and delegates installation to a persistent helper", () => {
  const source = fs.readFileSync(path.join(root, "electron", "main.cjs"), "utf8");
  assert.match(source, /UPDATER_RETRY_DELAYS_MS\s*=\s*\[[^\]]+\]/);
  assert.match(source, /scheduleAutoUpdaterRetry\(\)/);
  assert.match(source, /update-helper\.ps1/);
  assert.match(source, /installing\.json/);
  assert.match(source, /showUpdateInstallInProgress/);
  assert.match(source, /autoInstallOnAppQuit\s*=\s*false/);
  assert.doesNotMatch(source, /quitAndInstall\(true,\s*true\)/);
  assert.ok(fs.existsSync(path.join(root, "electron", "update-helper.ps1")));
});

test("production Windows releases sign both the app and installer before publishing", () => {
  const workflow = fs.readFileSync(path.join(projectRoot, ".github", "workflows", "release-windows.yml"), "utf8");
  const afterPack = fs.readFileSync(path.join(root, "scripts", "after-pack.cjs"), "utf8");
  const packageScript = String(pkg.scripts?.["package:win"] || "");
  assert.equal(pkg.build.win.signAndEditExecutable, false);
  assert.equal(pkg.build.afterPack, "scripts/after-pack.cjs");
  assert.equal(pkg.build.win.signtoolOptions.sign, "scripts/windows-sign.cjs");
  assert.deepEqual(pkg.build.win.signtoolOptions.signingHashAlgorithms, ["sha256"]);
  assert.match(afterPack, /packager\.sign\(executablePath\)/);
  assert.match(packageScript, /electron-builder --win nsis/);
  assert.doesNotMatch(packageScript, /--prepackaged|signAndEditExecutable=false/);
  assert.match(workflow, /WINDOWS_CSC_LINK/);
  assert.match(workflow, /WINDOWS_CSC_KEY_PASSWORD/);
  assert.match(workflow, /Get-AuthenticodeSignature/);
  assert.match(workflow, /Status\s*-ne\s*\"Valid\"/);
  assert.match(workflow, /publisherName/);
});

