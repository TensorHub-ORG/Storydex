const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const ACTIVE_INSTALL_STATES = new Set(["waiting-for-app-exit", "installing"]);
const LAUNCH_LOCK_STATES = new Set(["preparing", ...ACTIVE_INSTALL_STATES]);
const DEFAULT_LOCK_MAX_AGE_MS = 30 * 60 * 1000;
const DEFAULT_READY_TIMEOUT_MS = 15_000;
const DEFAULT_POLL_INTERVAL_MS = 50;

function clearInstallLock(lockPath, fsModule = fs) {
  try {
    fsModule.rmSync(lockPath, { force: true });
  } catch {
    // A missing or already-removed lock needs no recovery.
  }
}

function readInstallLockPayload(lockPath, fsModule = fs) {
  try {
    if (!fsModule.existsSync(lockPath)) return null;
    const payload = JSON.parse(fsModule.readFileSync(lockPath, "utf8"));
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  }
}

function readActiveInstallLock(lockPath, options = {}) {
  const fsModule = options.fsModule || fs;
  const now = typeof options.now === "function" ? options.now : Date.now;
  const maxAgeMs = Number(options.maxAgeMs || DEFAULT_LOCK_MAX_AGE_MS);
  const payload = readInstallLockPayload(lockPath, fsModule);
  const updatedAt = Date.parse(String(payload?.updatedAt || ""));
  const active = ACTIVE_INSTALL_STATES.has(String(payload?.state || ""));
  const fresh = Number.isFinite(updatedAt) && now() - updatedAt <= maxAgeMs;
  if (!payload || !active || !fresh) {
    clearInstallLock(lockPath, fsModule);
    return null;
  }
  return payload;
}

function acquirePreliminaryInstallLock(lockPath, fsModule = fs) {
  const payload = JSON.stringify({ state: "preparing", updatedAt: new Date().toISOString() });
  const writeExclusive = () => fsModule.writeFileSync(lockPath, payload, { encoding: "utf8", flag: "wx" });
  try {
    writeExclusive();
    return;
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }

  const existing = readInstallLockPayload(lockPath, fsModule);
  const updatedAt = Date.parse(String(existing?.updatedAt || ""));
  const fresh = Number.isFinite(updatedAt) && Date.now() - updatedAt <= DEFAULT_LOCK_MAX_AGE_MS;
  if (fresh && LAUNCH_LOCK_STATES.has(String(existing?.state || ""))) {
    throw new Error("An update helper launch is already in progress.");
  }

  clearInstallLock(lockPath, fsModule);
  try {
    writeExclusive();
  } catch (error) {
    if (error?.code === "EEXIST") {
      throw new Error("An update helper launch is already in progress.");
    }
    throw error;
  }
}

function helperExitError(code, signal) {
  const detail = signal ? `signal ${signal}` : `exit code ${code ?? "unknown"}`;
  return new Error(`Update helper exited before it was ready (${detail}).`);
}

function launchUpdateHelper(options) {
  const {
    helperScript,
    installerPath,
    appPath,
    lockPath,
    parentPid,
    logPath,
    powershellPath = "powershell.exe",
    spawnProcess = spawn,
    fsModule = fs,
    readyTimeoutMs = DEFAULT_READY_TIMEOUT_MS,
    pollIntervalMs = DEFAULT_POLL_INTERVAL_MS
  } = options || {};

  for (const [name, value] of Object.entries({ helperScript, installerPath, appPath, lockPath, parentPid, logPath })) {
    if (value === undefined || value === null || String(value).trim() === "") {
      throw new TypeError(`Missing update helper option: ${name}`);
    }
  }

  fsModule.mkdirSync(path.dirname(lockPath), { recursive: true });
  acquirePreliminaryInstallLock(lockPath, fsModule);

  let child;
  try {
    child = spawnProcess(
      powershellPath,
      [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helperScript,
        "-InstallerPath", installerPath,
        "-AppPath", appPath,
        "-LockPath", lockPath,
        "-ParentPid", String(parentPid),
        "-LogPath", logPath
      ],
      { detached: true, stdio: "ignore", windowsHide: false }
    );
  } catch (error) {
    clearInstallLock(lockPath, fsModule);
    throw error;
  }

  return new Promise((resolve, reject) => {
    let ready = false;
    let settled = false;
    let pollTimer = null;
    let timeoutTimer = null;

    const clearTimers = () => {
      if (pollTimer) clearTimeout(pollTimer);
      if (timeoutTimer) clearTimeout(timeoutTimer);
      pollTimer = null;
      timeoutTimer = null;
    };

    const rejectLaunch = (error) => {
      if (settled) return;
      settled = true;
      clearTimers();
      clearInstallLock(lockPath, fsModule);
      try {
        child.kill?.();
      } catch {
        // The process has already stopped.
      }
      reject(error);
    };

    const cleanExitedHelperLock = () => {
      const payload = readInstallLockPayload(lockPath, fsModule);
      if (String(payload?.state || "") !== "installing") {
        clearInstallLock(lockPath, fsModule);
      }
    };

    child.once("error", (error) => {
      if (!ready) rejectLaunch(error);
      else cleanExitedHelperLock();
    });
    child.once("exit", (code, signal) => {
      if (!ready) rejectLaunch(helperExitError(code, signal));
      else cleanExitedHelperLock();
    });

    const pollReadyState = () => {
      if (settled) return;
      const state = String(readInstallLockPayload(lockPath, fsModule)?.state || "");
      if (ACTIVE_INSTALL_STATES.has(state)) {
        ready = true;
        settled = true;
        clearTimers();
        resolve(child);
        return;
      }
      pollTimer = setTimeout(pollReadyState, Math.max(1, Number(pollIntervalMs) || DEFAULT_POLL_INTERVAL_MS));
    };

    timeoutTimer = setTimeout(() => {
      rejectLaunch(new Error("Update helper did not become ready in time."));
    }, Math.max(1, Number(readyTimeoutMs) || DEFAULT_READY_TIMEOUT_MS));
    pollReadyState();
  });
}

module.exports = {
  ACTIVE_INSTALL_STATES,
  clearInstallLock,
  launchUpdateHelper,
  readActiveInstallLock
};
