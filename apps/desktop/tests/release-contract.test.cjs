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

test("desktop builder pins the legacy stream modules required by electron-builder", () => {
  const requiredBuilderModules = {
    "core-util-is": "1.0.3",
    isarray: "1.0.0",
    "process-nextick-args": "2.0.1"
  };
  for (const [name, version] of Object.entries(requiredBuilderModules)) {
    assert.equal(pkg.devDependencies[name], version);
    assert.equal(lock.packages[`node_modules/${name}`]?.version, version);
  }
});

test("release configuration is offline-capable and updater-aware", () => {
  assert.equal(pkg.build.asar, true);
  assert.ok(pkg.build.files.includes("app/**/*"));
  assert.deepEqual(pkg.build.asarUnpack, [
    "app/backend/**/*",
    "app/docs/**/*",
    "app/python-env/**/*",
    "app/mingit/**/*",
    "app/assets/**/*",
    "electron/update-helper.ps1"
  ]);
  assert.equal(pkg.build.publish[0].provider, "generic");
  assert.equal(resolveUpdateFeedUrl(pkg), pkg.build.extraMetadata.storydexUpdateFeedUrl);
  assert.match(pkg.build.extraMetadata.storydexUpdateFeedUrl, /^https:\/\//);
});

test("assisted per-user installer does not relaunch elevated and duplicate the license page", () => {
  assert.equal(pkg.build.nsis.oneClick, false);
  assert.equal(pkg.build.nsis.perMachine, false);
  assert.equal(pkg.build.nsis.allowElevation, false);
  assert.equal(pkg.build.nsis.license, "build/installer-license.zh-CN.txt");
});

test("packaged Electron E2E runs serially to avoid competing desktop instances", () => {
  assert.match(pkg.scripts["test:smoke"], /--test-concurrency=1/);
  assert.match(pkg.scripts["test:smoke"], /--test-name-pattern/);
  assert.match(pkg.scripts["test:e2e"], /--test-concurrency=1/);
  assert.doesNotMatch(pkg.scripts["test:e2e"], /--test-name-pattern/);
});

test("desktop packaging exposes reusable prepared stages without weakening release checks", () => {
  assert.match(pkg.scripts["prepare:package"], /build:frontend/);
  assert.match(pkg.scripts["prepare:package"], /build:coomi-runtime/);
  assert.match(pkg.scripts["prepare:package"], /prepare:package:assets/);
  assert.match(pkg.scripts["prepare:package:assets"], /sync:assets/);
  assert.match(pkg.scripts["prepare:package:assets"], /check:embedded-python/);
  assert.match(pkg.scripts["build:desktop"], /prepare:package/);
  assert.match(pkg.scripts["build:desktop"], /build:desktop:prepared/);
  assert.match(pkg.scripts["package:win"], /prepare:package/);
  assert.match(pkg.scripts["package:win"], /package:win:prepared/);
  assert.doesNotMatch(pkg.scripts["build:desktop:prepared"], /build:frontend|build:coomi-runtime|sync:assets/);
  assert.doesNotMatch(pkg.scripts["package:win:prepared"], /build:frontend|build:coomi-runtime|sync:assets/);
});

test("local Full and Release suites reuse completed build outputs", () => {
  const suite = fs.readFileSync(path.join(projectRoot, "scripts", "run_full_test_suite.ps1"), "utf8");
  assert.match(
    suite,
    /Build Storydex Coomi desktop runtime[\s\S]*scripts\/build-coomi-runtime\.cjs/,
    "the reusable package stage must refresh build metadata from the current bridge binary"
  );
  const frontendPackage = JSON.parse(fs.readFileSync(path.join(projectRoot, "apps", "frontend", "package.json"), "utf8"));
  assert.match(frontendPackage.scripts.build, /vue-tsc[\s\S]*vite build/);
  assert.equal(frontendPackage.scripts["build:bundle"], "vite build");
  assert.match(suite, /Frontend type check[\s\S]*Frontend production build[^\r\n]*run build:bundle/);
  assert.match(suite, /\$Mode -eq "Full"[\s\S]*run prepare:package:assets[\s\S]*run build:desktop:prepared[\s\S]*run test:smoke/);
  assert.match(suite, /\$Mode -eq "Release"[\s\S]*run prepare:package:assets[\s\S]*run package:win:prepared[\s\S]*run test:e2e/);
  assert.doesNotMatch(suite, /npm --prefix \$desktop run build:desktop\s*\}/);
  assert.doesNotMatch(suite, /npm --prefix \$desktop run package:win\s*\}/);
  assert.match(suite, /pipeline-timings\.json/);
});

test("local and CI release bundling share the optimized archive pipeline", () => {
  const workflow = fs.readFileSync(path.join(projectRoot, ".github", "workflows", "release-windows.yml"), "utf8");
  const qualityGate = fs.readFileSync(path.join(projectRoot, ".github", "workflows", "quality-gate.yml"), "utf8");
  const bundleScript = fs.readFileSync(path.join(projectRoot, "scripts", "prepare_release_bundle.ps1"), "utf8");
  const metadataScript = fs.readFileSync(path.join(projectRoot, "scripts", "generate_release_metadata.cjs"), "utf8");
  assert.match(workflow, /prepare_release_bundle\.ps1/);
  assert.match(workflow, /run_packaged_checks:\s*false/);
  assert.match(workflow, /Run packaged Electron E2E on release artifact[\s\S]*npm run test:e2e/);
  assert.match(qualityGate, /run_packaged_checks:/);
  assert.match(qualityGate, /inputs\.full && inputs\.run_packaged_checks/);
  assert.doesNotMatch(workflow, /Compress-Archive|Get-FileHash/);
  assert.match(bundleScript, /ZipFile\]::CreateFromDirectory/);
  assert.match(bundleScript, /\[string\]\$CompressionLevel = "Fastest"/);
  assert.match(bundleScript, /Verify portable ZIP index/);
  assert.doesNotMatch(bundleScript, /Compress-Archive|Expand-Archive/);
  assert.match(metadataScript, /SHA256SUMS\.txt/);
  assert.match(metadataScript, /const checksumRecords = \[\.\.\.artifacts, artifactRecord\(manifestName\)\]/);
});

test("desktop packages the guide and prompt repository and exposes their roots to the backend", () => {
  const mainSource = fs.readFileSync(path.join(root, "electron", "main.cjs"), "utf8");
  const syncSource = fs.readFileSync(path.join(root, "scripts", "sync-app-assets.cjs"), "utf8");
  assert.match(mainSource, /STORYDEX_HELP_GUIDE_ROOT:\s*helpGuideRoot/);
  assert.match(mainSource, /STORYDEX_PROMPT_REPOSITORY_ROOT:\s*promptRepositoryRoot/);
  assert.match(mainSource, /STORYDEX_BUILTIN_SKILLS_ROOT:\s*builtinSkillsRoot/);
  assert.match(mainSource, /app\.asar\.unpacked/);
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

test("backend startup failure is recoverable instead of silently quitting", () => {
  const source = fs.readFileSync(path.join(root, "electron", "main.cjs"), "utf8");
  assert.match(source, /DEFAULT_BACKEND_STARTUP_ATTEMPTS\s*=\s*2/);
  assert.match(source, /STORYDEX_BACKEND_STARTUP_ATTEMPTS/);
  assert.match(source, /automatic backend startup retry/);
  assert.match(source, /buttons:\s*\["Retry", "Open log", "Exit"\]/);
  assert.match(source, /shell\.openPath\(backendLogFilePath\)/);
  assert.match(source, /action === "retry" \|\| action === "open-log"/);
  assert.match(source, /waitForBackendProcessExit\(failedProcess\)/);
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
  const helperSource = fs.readFileSync(path.join(root, "electron", "update-helper.ps1"), "utf8");
  const updateConfigSource = fs.readFileSync(path.join(root, "scripts", "write-app-update-config.cjs"), "utf8");
  assert.match(source, /UPDATER_RETRY_DELAYS_MS\s*=\s*\[[^\]]+\]/);
  assert.match(source, /scheduleAutoUpdaterRetry\(\)/);
  assert.match(source, /update-helper\.ps1/);
  assert.match(source, /installing\.json/);
  assert.match(source, /storydex-desktop-updater/);
  assert.match(updateConfigSource, /updaterCacheDirName:\s*storydex-desktop-updater/);
  assert.ok(
    helperSource.indexOf('Set-InstallLock "waiting-for-app-exit"') <
      helperSource.indexOf("New-Object Windows.Forms.Form"),
    "update helper must publish readiness before creating its UI"
  );
  assert.match(source, /showUpdateInstallInProgress/);
  assert.match(source, /autoInstallOnAppQuit\s*=\s*false/);
  assert.doesNotMatch(source, /quitAndInstall\(true,\s*true\)/);
  assert.ok(fs.existsSync(path.join(root, "electron", "update-helper.ps1")));
});

test("Windows releases sign when credentials are configured and otherwise report unsigned artifacts", () => {
  const workflow = fs.readFileSync(path.join(projectRoot, ".github", "workflows", "release-windows.yml"), "utf8");
  const afterPack = fs.readFileSync(path.join(root, "scripts", "after-pack.cjs"), "utf8");
  const packageScript = String(pkg.scripts?.["package:win:prepared"] || "");
  assert.equal(pkg.build.win.signAndEditExecutable, false);
  assert.equal(pkg.build.afterPack, "scripts/after-pack.cjs");
  assert.equal(pkg.build.win.signtoolOptions.sign, "scripts/windows-sign.cjs");
  assert.deepEqual(pkg.build.win.signtoolOptions.signingHashAlgorithms, ["sha256"]);
  assert.match(afterPack, /packager\.sign\(executablePath\)/);
  assert.match(packageScript, /electron-builder --win nsis/);
  assert.doesNotMatch(packageScript, /--prepackaged|signAndEditExecutable=false/);
  assert.match(workflow, /WINDOWS_CSC_LINK/);
  assert.match(workflow, /WINDOWS_CSC_KEY_PASSWORD/);
  assert.match(workflow, /Resolve Windows code-signing mode/);
  assert.match(workflow, /steps\.signing\.outputs\.enabled/);
  assert.match(workflow, /Unsigned Windows release/);
  assert.doesNotMatch(workflow, /Missing required Windows signing secret/);
  assert.match(workflow, /Get-AuthenticodeSignature/);
  assert.match(workflow, /Status\s*-ne\s*\"Valid\"/);
  assert.match(workflow, /publisherName/);
});

