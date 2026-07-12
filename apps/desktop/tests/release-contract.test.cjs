const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { resolveUpdateFeedUrl } = require("../electron/update-feed.cjs");

const root = path.resolve(__dirname, "..");
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

