const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const helper = path.resolve(__dirname, "..", "electron", "update-helper.ps1");

function runHelper(exitCode, { invalidLogPath = false, parentPid = 999999 } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-update-helper-"));
  const installer = path.join(root, "fake-installer.cmd");
  const lock = path.join(root, "installing.json");
  const log = path.join(root, "install.log");
  fs.writeFileSync(installer, `@exit /b ${exitCode}\r\n`);
  if (invalidLogPath) fs.mkdirSync(log);
  fs.writeFileSync(lock, JSON.stringify({ state: "preparing", updatedAt: new Date().toISOString() }));
  const result = spawnSync("powershell.exe", [
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helper,
    "-InstallerPath", installer,
    "-AppPath", process.execPath,
    "-LockPath", lock,
    "-ParentPid", String(parentPid),
    "-LogPath", log,
    "-TestMode"
  ], { encoding: "utf8", timeout: invalidLogPath || parentPid !== 999999 ? 5_000 : 30_000, windowsHide: true });
  const output = fs.existsSync(log) && fs.statSync(log).isFile()
    ? fs.readFileSync(log, "utf8")
    : `${result.stdout || ""}${result.stderr || ""}`;
  const lockExists = fs.existsSync(lock);
  fs.rmSync(root, { recursive: true, force: true });
  return { result, output, lockExists };
}

test("persistent update helper completes installation and clears the install lock", { skip: process.platform !== "win32" }, () => {
  const { result, output, lockExists } = runHelper(0);
  assert.equal(result.status, 0, output);
  assert.match(output, /completed successfully/i);
  assert.equal(lockExists, false);
});

test("persistent update helper reports installer failure, preserves recovery, and clears the lock", { skip: process.platform !== "win32" }, () => {
  const { result, output, lockExists } = runHelper(7);
  assert.equal(result.status, 1, output);
  assert.match(output, /failed/i);
  assert.equal(lockExists, false);
});

test("persistent update helper clears the lock when initialization fails", { skip: process.platform !== "win32" }, () => {
  const { result, lockExists } = runHelper(0, { invalidLogPath: true });
  assert.equal(result.status, 1);
  assert.equal(lockExists, false);
});

test("persistent update helper does not install while the parent app is still running", { skip: process.platform !== "win32" }, () => {
  const { result, output, lockExists } = runHelper(0, { parentPid: process.pid });
  assert.equal(result.status, 1, output);
  assert.match(output, /did not exit/i);
  assert.equal(lockExists, false);
});
