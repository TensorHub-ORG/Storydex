const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { findCachedInstaller, launchUpdateHelper, readActiveInstallLock, verifyCachedInstaller } = require("../electron/update-installer.cjs");

function createFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-update-launcher-"));
  const fixture = {
    root,
    lockPath: path.join(root, "installing.json"),
    logPath: path.join(root, "install.log"),
    helperScript: path.join(root, "update-helper.ps1"),
    installerPath: path.join(root, "installer.exe"),
    appPath: path.join(root, "Storydex.exe")
  };
  for (const target of [fixture.helperScript, fixture.installerPath, fixture.appPath]) {
    fs.writeFileSync(target, "fixture");
  }
  return fixture;
}

test("cached installer survives an application restart", () => {
  const fixture = createFixture();
  try {
    const pending = path.join(fixture.root, "pending");
    fs.mkdirSync(pending);
    const fileName = "StorydexSetup-x64-2.0.4.exe";
    const installer = Buffer.from("installer");
    const sha512 = crypto.createHash("sha512").update(installer).digest("base64");
    fs.writeFileSync(path.join(pending, fileName), installer);
    fs.writeFileSync(path.join(pending, "update-info.json"), JSON.stringify({ fileName, sha512 }));
    assert.deepEqual(findCachedInstaller(fixture.root), {
      installerPath: path.join(pending, fileName),
      metadataPath: path.join(pending, "update-info.json"),
      fileName,
      version: "2.0.4",
      sha512
    });
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("cached installer checksum is verified before execution", async () => {
  const fixture = createFixture();
  try {
    const installer = Buffer.from("verified installer");
    fs.writeFileSync(fixture.installerPath, installer);
    const cached = {
      installerPath: fixture.installerPath,
      sha512: crypto.createHash("sha512").update(installer).digest("base64")
    };
    assert.equal(await verifyCachedInstaller(cached), true);
    fs.appendFileSync(fixture.installerPath, "tampered");
    assert.equal(await verifyCachedInstaller(cached), false);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("helper launch validates files and records diagnostics before spawning", async () => {
  const fixture = createFixture();
  const child = fakeChild();
  try {
    const launched = launchUpdateHelper({
      ...fixture,
      parentPid: process.pid,
      readyTimeoutMs: 250,
      pollIntervalMs: 5,
      spawnProcess: () => {
        queueMicrotask(() => child.emit("exit", 0, null));
        return child;
      }
    });
    assert.equal(await launched, child);
    const diagnostic = JSON.parse(fs.readFileSync(path.join(fixture.root, "launch.json"), "utf8"));
    assert.equal(diagnostic.installerPath, fixture.installerPath);
    assert.equal(diagnostic.helperScript, fixture.helperScript);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});

function fakeChild() {
  const child = new EventEmitter();
  child.kill = () => true;
  child.unref = () => child;
  return child;
}

test("inactive preliminary install locks never block the next app start", () => {
  const fixture = createFixture();
  try {
    fs.writeFileSync(fixture.lockPath, JSON.stringify({ state: "preparing", updatedAt: new Date().toISOString() }));
    assert.equal(readActiveInstallLock(fixture.lockPath), null);
    assert.equal(fs.existsSync(fixture.lockPath), false);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("helper spawn failure rejects and clears the preliminary install lock", async () => {
  const fixture = createFixture();
  const child = fakeChild();
  try {
    const launched = launchUpdateHelper({
      ...fixture,
      parentPid: process.pid,
      readyTimeoutMs: 250,
      pollIntervalMs: 5,
      spawnProcess: () => {
        queueMicrotask(() => child.emit("error", new Error("powershell unavailable")));
        return child;
      }
    });
    await assert.rejects(launched, /powershell unavailable/);
    assert.equal(fs.existsSync(fixture.lockPath), false);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("a concurrent helper launch cannot overwrite a fresh preliminary lock", async () => {
  const fixture = createFixture();
  const preparing = JSON.stringify({ state: "preparing", updatedAt: new Date().toISOString() });
  try {
    fs.writeFileSync(fixture.lockPath, preparing);
    assert.throws(() => launchUpdateHelper({
      ...fixture,
      parentPid: process.pid,
      spawnProcess: () => {
        throw new Error("spawn must not be reached");
      }
    }), /already in progress/i);
    assert.equal(fs.readFileSync(fixture.lockPath, "utf8"), preparing);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("helper exit before installation clears waiting locks but preserves installing locks", async () => {
  for (const [state, shouldRemain] of [["waiting-for-app-exit", false], ["installing", true]]) {
    const fixture = createFixture();
    const child = fakeChild();
    try {
      const launched = launchUpdateHelper({
        ...fixture,
        parentPid: process.pid,
        readyTimeoutMs: 250,
        pollIntervalMs: 5,
        spawnProcess: () => {
          queueMicrotask(() => {
            fs.writeFileSync(fixture.lockPath, JSON.stringify({ state, updatedAt: new Date().toISOString() }));
          });
          return child;
        }
      });
      assert.equal(await launched, child);
      child.emit("exit", 1, null);
      assert.equal(fs.existsSync(fixture.lockPath), shouldRemain, state);
    } finally {
      fs.rmSync(fixture.root, { recursive: true, force: true });
    }
  }
});

test("successful helper exit before polling is accepted", async () => {
  const fixture = createFixture();
  const child = fakeChild();
  try {
    const launched = launchUpdateHelper({
      ...fixture,
      parentPid: process.pid,
      readyTimeoutMs: 250,
      pollIntervalMs: 50,
      spawnProcess: () => {
        queueMicrotask(() => child.emit("exit", 0, null));
        return child;
      }
    });
    assert.equal(await launched, child);
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true });
  }
});
