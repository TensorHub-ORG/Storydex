const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { launchUpdateHelper, readActiveInstallLock } = require("../electron/update-installer.cjs");

function createFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-update-launcher-"));
  return {
    root,
    lockPath: path.join(root, "installing.json"),
    logPath: path.join(root, "install.log"),
    helperScript: path.join(root, "update-helper.ps1"),
    installerPath: path.join(root, "installer.exe"),
    appPath: path.join(root, "Storydex.exe")
  };
}

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
