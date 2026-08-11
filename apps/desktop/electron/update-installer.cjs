const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const ACTIVE_INSTALL_STATES = new Set(["waiting-for-app-exit", "installing"]);
const LAUNCH_LOCK_STATES = new Set(["preparing", ...ACTIVE_INSTALL_STATES]);
const DEFAULT_LOCK_MAX_AGE_MS = 30 * 60 * 1000;
const DEFAULT_READY_TIMEOUT_MS = 15_000;
const DEFAULT_POLL_INTERVAL_MS = 50;

function findCachedInstaller(cacheRoot, fsModule = fs) {
  const pendingRoot = path.join(String(cacheRoot || ""), "pending");
  const metadataPath = path.join(pendingRoot, "update-info.json");
  try {
    const metadata = JSON.parse(fsModule.readFileSync(metadataPath, "utf8"));
    const fileName = path.basename(String(metadata?.fileName || ""));
    if (!fileName || path.extname(fileName).toLowerCase() !== ".exe") return null;
    const installerPath = path.join(pendingRoot, fileName);
    const stat = fsModule.statSync(installerPath);
    if (!stat.isFile() || stat.size <= 0) return null;
    const versionMatch = fileName.match(/(?:^|[-_])v?(\d+\.\d+\.\d+)(?:[-_.]|$)/i);
    return {
      installerPath,
      metadataPath,
      fileName,
      version: versionMatch?.[1] || "",
      sha512: typeof metadata?.sha512 === "string" ? metadata.sha512.trim() : ""
    };
  } catch {
    return null;
  }
}

function verifyCachedInstaller(cached, fsModule = fs) {
  const installerPath = String(cached?.installerPath || "");
  const expected = String(cached?.sha512 || "").trim();
  if (!installerPath || !/^[A-Za-z0-9+/]{80,}={0,2}$/.test(expected)) {
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    const hash = crypto.createHash("sha512");
    const stream = fsModule.createReadStream(installerPath);
    stream.on("error", () => resolve(false));
    hash.on("error", () => resolve(false));
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("base64") === expected));
  });
}

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
    pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
    testMode = false
  } = options || {};

  for (const [name, value] of Object.entries({ helperScript, installerPath, appPath, lockPath, parentPid, logPath })) {
    if (value === undefined || value === null || String(value).trim() === "") {
      throw new TypeError(`Missing update helper option: ${name}`);
    }
  }

  for (const [name, value] of Object.entries({ helperScript, installerPath, appPath })) {
    if (!fsModule.existsSync(value) || !fsModule.statSync(value).isFile()) {
      throw new Error(`Update helper ${name} does not exist: ${value}`);
    }
  }

  fsModule.mkdirSync(path.dirname(lockPath), { recursive: true });
  const launchDiagnosticPath = path.join(path.dirname(lockPath), "launch.json");
  fsModule.writeFileSync(launchDiagnosticPath, JSON.stringify({
    state: "launching",
    helperScript,
    installerPath,
    appPath,
    lockPath,
    logPath,
    parentPid: Number(parentPid),
    updatedAt: new Date().toISOString()
  }, null, 2), "utf8");
  acquirePreliminaryInstallLock(lockPath, fsModule);

  let child;
  try {
    const helperArguments = [
      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helperScript,
      "-InstallerPath", installerPath,
      "-AppPath", appPath,
      "-LockPath", lockPath,
      "-ParentPid", String(parentPid),
      "-LogPath", logPath
    ];
    if (testMode) helperArguments.push("-TestMode");
    child = spawnProcess(
      powershellPath,
      helperArguments,
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
      if (!ready) {
        // A small/test installer can finish before the polling timer observes
        // waiting-for-app-exit. Exit code 0 is the helper's completion contract.
        if (code === 0 && !signal) {
          settled = true;
          clearTimers();
          resolve(child);
          return;
        }
        rejectLaunch(helperExitError(code, signal));
      } else {
        cleanExitedHelperLock();
      }
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
  findCachedInstaller,
  launchUpdateHelper,
  readActiveInstallLock,
  verifyCachedInstaller
};
