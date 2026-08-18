const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const READY_RUNTIME = "storydex-agentd";
const DEFAULT_STARTUP_TIMEOUT_MS = 15_000;
const DEFAULT_HEALTH_ATTEMPTS = 80;
const DEFAULT_HEALTH_INTERVAL_MS = 100;
const DEFAULT_GRACEFUL_SHUTDOWN_MS = 5_000;
const MAX_LOG_TAIL_CHARS = 12_000;

function normalizePathForComparison(value) {
  const normalized = path.resolve(String(value || ""));
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

function isPathInside(candidatePath, rootPath) {
  const candidate = normalizePathForComparison(candidatePath);
  const root = normalizePathForComparison(rootPath);
  const relative = path.relative(root, candidate);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function resolveRealPath(targetPath) {
  return fs.realpathSync.native ? fs.realpathSync.native(targetPath) : fs.realpathSync(targetPath);
}

function resolveThroughExistingAncestor(targetPath) {
  let ancestor = path.resolve(targetPath);
  const missingSegments = [];
  while (!fs.existsSync(ancestor)) {
    const parent = path.dirname(ancestor);
    if (parent === ancestor) {
      throw new Error(`Unable to resolve an existing ancestor for ${targetPath}`);
    }
    missingSegments.unshift(path.basename(ancestor));
    ancestor = parent;
  }
  return path.join(resolveRealPath(ancestor), ...missingSegments);
}

function ensureTemporaryDirectory(targetPath, temporaryRoot, label) {
  const realTemporaryRoot = resolveRealPath(temporaryRoot);
  const resolved = resolveThroughExistingAncestor(targetPath);
  if (!isPathInside(resolved, realTemporaryRoot)) {
    throw new Error(`${label} must stay inside the operating-system temporary directory: ${temporaryRoot}`);
  }
  fs.mkdirSync(resolved, { recursive: true });
  const realTarget = resolveRealPath(resolved);
  if (!isPathInside(realTarget, realTemporaryRoot)) {
    throw new Error(`${label} resolves outside the operating-system temporary directory`);
  }
  return realTarget;
}

function createIsolatedBetaPaths(options = {}) {
  const temporaryRoot = resolveRealPath(options.temporaryRoot || os.tmpdir());
  const configuredProfile = String(options.profileRoot || "").trim();
  const profileRoot = configuredProfile
    ? ensureTemporaryDirectory(configuredProfile, temporaryRoot, "Rust Beta profile")
    : fs.mkdtempSync(path.join(temporaryRoot, "storydex-rust-beta-"));
  const configuredWorkspace = String(options.workspaceRoot || "").trim();
  const workspaceRoot = ensureTemporaryDirectory(
    configuredWorkspace || path.join(profileRoot, "workspace"),
    temporaryRoot,
    "Rust Beta workspace"
  );
  const coomiHome = ensureTemporaryDirectory(path.join(profileRoot, "coomi-home"), temporaryRoot, "Rust Beta Coomi home");
  const logsDir = ensureTemporaryDirectory(path.join(profileRoot, "logs"), temporaryRoot, "Rust Beta logs");
  const appDataRoot = ensureTemporaryDirectory(
    path.join(profileRoot, "app-data", "roaming"),
    temporaryRoot,
    "Rust Beta roaming application data"
  );
  const localAppDataRoot = ensureTemporaryDirectory(
    path.join(profileRoot, "app-data", "local"),
    temporaryRoot,
    "Rust Beta local application data"
  );
  const userDataRoot = ensureTemporaryDirectory(
    path.join(profileRoot, "electron-user-data"),
    temporaryRoot,
    "Rust Beta Electron profile"
  );
  return {
    profileRoot,
    workspaceRoot,
    coomiHome,
    logsDir,
    appDataRoot,
    localAppDataRoot,
    userDataRoot,
    temporaryRoot
  };
}

function resolveAgentdBinary(desktopRoot, environment = process.env, options = {}) {
  const configured = String(environment.STORYDEX_RUST_BETA_AGENTD || "").trim();
  const profile = String(environment.STORYDEX_RUST_BETA_BUILD_PROFILE || "debug").trim().toLowerCase();
  if (profile !== "debug" && profile !== "release") {
    throw new Error(`Invalid STORYDEX_RUST_BETA_BUILD_PROFILE: ${profile}`);
  }
  const executableName = process.platform === "win32" ? "storydex-agentd.exe" : "storydex-agentd";
  const candidates = configured
    ? [path.resolve(configured)]
    : [
      ...(options.resourcesRoot ? [path.join(options.resourcesRoot, "agent-runtime", executableName)] : []),
      path.join(desktopRoot, "agent-runtime", "target", profile, executableName)
    ];
  for (const candidate of candidates) {
    const resolved = path.resolve(candidate);
    if (path.basename(resolved).toLowerCase() !== executableName.toLowerCase()) {
      continue;
    }
    if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
      return resolved;
    }
  }
  const attempted = candidates.map((candidate) => path.resolve(candidate)).join(", ");
  throw new Error(`Rust Beta storydex-agentd binary is missing (attempted: ${attempted})`);
}

function buildAgentdEnvironment(baseEnvironment, runtimePaths) {
  const environment = { ...baseEnvironment };
  for (const variable of [
    "CONDA_PREFIX",
    "PYTHONHOME",
    "PYTHONPATH",
    "STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK",
    "STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE",
    "STORYDEX_COOMI_BRIDGE",
    "STORYDEX_EMBED_PYTHON",
    "STORYDEX_GLOBAL_ROOT",
    "STORYDEX_PYTHON",
    "STORYDEX_WORKSPACE_ROOT",
    "VIRTUAL_ENV"
  ]) {
    delete environment[variable];
  }
  environment.HOME = runtimePaths.profileRoot;
  environment.USERPROFILE = runtimePaths.profileRoot;
  environment.APPDATA = runtimePaths.appDataRoot || path.join(runtimePaths.profileRoot, "app-data", "roaming");
  environment.LOCALAPPDATA = runtimePaths.localAppDataRoot || path.join(runtimePaths.profileRoot, "app-data", "local");
  environment.XDG_CACHE_HOME = path.join(runtimePaths.profileRoot, "xdg", "cache");
  environment.XDG_CONFIG_HOME = path.join(runtimePaths.profileRoot, "xdg", "config");
  environment.XDG_DATA_HOME = path.join(runtimePaths.profileRoot, "xdg", "data");
  environment.STORYDEX_AGENTD_REFACTOR_ROOT = runtimePaths.workspaceRoot;
  environment.STORYDEX_COOMI_HOME = runtimePaths.coomiHome;
  environment.STORYDEX_DISABLE_NETWORK = "1";
  environment.STORYDEX_TESTING = "1";
  return environment;
}

function parseReadyPacket(line) {
  let payload;
  try {
    payload = JSON.parse(String(line || ""));
  } catch (error) {
    throw new Error(`storydex-agentd ready packet is not valid JSON: ${error.message}`);
  }
  if (payload?.event !== "ready" || payload?.runtime !== READY_RUNTIME) {
    throw new Error("storydex-agentd first packet must be the ready event");
  }
  const port = Number(payload.port);
  const token = String(payload.token || "").trim();
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`storydex-agentd ready packet has an invalid loopback port: ${payload.port}`);
  }
  if (!/^[0-9a-f]{32}$/i.test(token)) {
    throw new Error("storydex-agentd ready packet has an invalid random token");
  }
  return {
    event: "ready",
    runtime: READY_RUNTIME,
    port,
    token,
    version: String(payload.version || "").trim()
  };
}

function redactRuntimeSecrets(value, token = "") {
  let text = String(value || "");
  text = text.replace(/("token"\s*:\s*")[^"]+(")/gi, "$1[redacted]$2");
  if (token) {
    text = text.split(token).join("[redacted]");
  }
  return text;
}

function appendLogTail(chunks, chunk) {
  const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk || "");
  if (!text) return;
  chunks.push(text);
  let total = chunks.reduce((sum, item) => sum + item.length, 0);
  while (total > MAX_LOG_TAIL_CHARS && chunks.length > 1) {
    total -= chunks.shift().length;
  }
}

function createRuntimeLogger(logsDir) {
  fs.mkdirSync(logsDir, { recursive: true });
  const logFilePath = path.join(logsDir, "rust-beta-agentd.log");
  const previousPath = path.join(logsDir, "rust-beta-agentd.prev.log");
  if (fs.existsSync(logFilePath)) {
    fs.rmSync(previousPath, { force: true });
    fs.renameSync(logFilePath, previousPath);
  }
  const stream = fs.createWriteStream(logFilePath, { flags: "a", encoding: "utf8" });
  stream.on("error", (error) => {
    process.stderr.write(`[Storydex Rust Beta] Runtime log write failed: ${error.message || String(error)}\n`);
  });
  return {
    logFilePath,
    write(message) {
      const line = String(message || "");
      if (line) stream.write(line.endsWith("\n") ? line : `${line}\n`);
    },
    close() {
      stream.end();
    }
  };
}

function requestJson({ port, route, method = "GET", token = "", timeoutMs = 1500 }) {
  return new Promise((resolve, reject) => {
    const headers = { accept: "application/json" };
    if (token) headers.authorization = `Bearer ${token}`;
    const request = http.request(
      {
        host: "127.0.0.1",
        port,
        path: route,
        method,
        headers,
        agent: false,
        timeout: timeoutMs
      },
      (response) => {
        let body = "";
        response.on("data", (chunk) => {
          body = `${body}${chunk.toString("utf8")}`.slice(-1024 * 1024);
        });
        response.on("end", () => {
          let json = null;
          try {
            json = body ? JSON.parse(body) : null;
          } catch (error) {
            reject(new Error(`Invalid JSON from storydex-agentd ${route}: ${error.message}`));
            return;
          }
          resolve({ statusCode: response.statusCode || 0, json });
        });
      }
    );
    request.on("timeout", () => request.destroy(new Error(`storydex-agentd ${route} timed out`)));
    request.on("error", reject);
    request.end();
  });
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForAgentdHealth({ ready, request = requestJson, attempts, intervalMs }) {
  let lastDetail = "health endpoint was not reached";
  for (let index = 0; index < attempts; index += 1) {
    try {
      const health = await request({ port: ready.port, route: "/api/v1/sys/health" });
      const runtime = String(health.json?.data?.runtime || "");
      if (health.statusCode >= 200 && health.statusCode < 300 && health.json?.ok === true && runtime === READY_RUNTIME) {
        const version = await request({
          port: ready.port,
          route: "/api/v1/sys/version",
          token: ready.token
        });
        if (
          version.statusCode >= 200 &&
          version.statusCode < 300 &&
          version.json?.ok === true &&
          version.json?.data?.runtime === READY_RUNTIME
        ) {
          return;
        }
        throw new Error(`authenticated version probe failed with HTTP ${version.statusCode}`);
      }
      lastDetail = `health returned HTTP ${health.statusCode} runtime=${runtime || "missing"}`;
    } catch (error) {
      lastDetail = error.message || String(error);
    }
    await delay(intervalMs);
  }
  throw new Error(`storydex-agentd did not become healthy: ${lastDetail}`);
}

function waitForProcessExit(processRef, timeoutMs, isExited = () => processRef.exitCode !== null || !!processRef.signalCode) {
  if (!processRef || isExited()) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (exited) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      processRef.removeListener("exit", onExit);
      processRef.removeListener("close", onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    timer = setTimeout(() => finish(isExited()), timeoutMs);
    processRef.once("exit", onExit);
    processRef.once("close", onExit);
  });
}

async function killProcessTree(processRef) {
  if (!processRef?.pid || processRef.exitCode !== null || processRef.signalCode) return;
  if (process.platform === "win32") {
    await new Promise((resolve) => {
      const killer = spawn("taskkill", ["/PID", String(processRef.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore"
      });
      killer.once("close", resolve);
      killer.once("error", resolve);
    });
    return;
  }
  try {
    processRef.kill("SIGKILL");
  } catch {
    // Process already exited.
  }
}

class RustBetaRuntimeSupervisor {
  constructor(options) {
    this.binaryPath = options.binaryPath;
    this.runtimePaths = options.runtimePaths;
    this.environment = options.environment || process.env;
    this.spawnProcess = options.spawnProcess || spawn;
    this.request = options.request || requestJson;
    this.killTree = options.killTree || killProcessTree;
    this.logger = options.logger || createRuntimeLogger(this.runtimePaths.logsDir);
    this.onUnexpectedExit = options.onUnexpectedExit || (() => undefined);
    this.startupTimeoutMs = options.startupTimeoutMs || DEFAULT_STARTUP_TIMEOUT_MS;
    this.healthAttempts = options.healthAttempts || DEFAULT_HEALTH_ATTEMPTS;
    this.healthIntervalMs = options.healthIntervalMs || DEFAULT_HEALTH_INTERVAL_MS;
    this.gracefulShutdownMs = options.gracefulShutdownMs || DEFAULT_GRACEFUL_SHUTDOWN_MS;
    this.processRef = null;
    this.ready = null;
    this.startedHealthy = false;
    this.exited = false;
    this.stopping = false;
    this.stopPromise = null;
    this.stdoutTail = [];
    this.stderrTail = [];
    this.stdoutBuffer = "";
  }

  log(message) {
    this.logger.write(`[${new Date().toISOString()}] ${redactRuntimeSecrets(message, this.ready?.token)}`);
  }

  logTail() {
    return [this.stdoutTail.join(""), this.stderrTail.join("")]
      .filter(Boolean)
      .join("\n")
      .slice(-4000);
  }

  async start() {
    if (this.processRef) throw new Error("Rust Beta runtime has already been started");
    this.log(`Launching ${this.binaryPath} with --port 0 (Python fallback disabled)`);
    const args = [
      "--port",
      "0",
      "--shutdown-timeout-ms",
      String(this.gracefulShutdownMs),
      "--coomi-home",
      this.runtimePaths.coomiHome
    ];
    const processRef = this.spawnProcess(this.binaryPath, args, {
      cwd: this.runtimePaths.workspaceRoot,
      env: buildAgentdEnvironment(this.environment, this.runtimePaths),
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true
    });
    this.processRef = processRef;

    let resolveReady;
    let rejectReady;
    let rejectStartupExit;
    let startupSettled = false;
    const readyPromise = new Promise((resolve, reject) => {
      resolveReady = resolve;
      rejectReady = reject;
    });
    const startupExitPromise = new Promise((_, reject) => {
      rejectStartupExit = reject;
    });

    const rejectStartup = (error) => {
      if (!this.ready) rejectReady(error instanceof Error ? error : new Error(String(error)));
    };

    processRef.stdout.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      appendLogTail(this.stdoutTail, redactRuntimeSecrets(text, this.ready?.token));
      this.stdoutBuffer += text;
      const lines = this.stdoutBuffer.split(/\r?\n/);
      this.stdoutBuffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        if (!this.ready) {
          try {
            this.ready = parseReadyPacket(line);
            resolveReady(this.ready);
          } catch (error) {
            rejectStartup(error);
          }
        }
        this.log(`[agentd:stdout] ${line}`);
      }
    });

    processRef.stderr.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      appendLogTail(this.stderrTail, redactRuntimeSecrets(text, this.ready?.token));
      this.log(`[agentd:stderr] ${text}`);
    });

    processRef.once("error", (error) => {
      rejectStartup(new Error(`storydex-agentd spawn failed: ${error.message || String(error)}`));
    });
    processRef.once("exit", (code, signal) => {
      this.exited = true;
      const info = {
        code,
        signal,
        logFilePath: this.logger.logFilePath || "",
        detail: this.logTail()
      };
      this.log(`storydex-agentd exited code=${code} signal=${signal}`);
      if (!this.ready) {
        rejectStartup(new Error(`storydex-agentd exited before ready: code=${code} signal=${signal}`));
      } else if (!startupSettled) {
        rejectStartupExit(new Error(`storydex-agentd exited during startup: code=${code} signal=${signal}`));
      } else if (this.startedHealthy && !this.stopping) {
        Promise.resolve(this.onUnexpectedExit(info)).catch((error) => {
          this.log(`unexpected-exit callback failed: ${error.message || String(error)}`);
        });
      }
    });

    let startupTimer;
    try {
      const ready = await Promise.race([
        readyPromise,
        new Promise((_, reject) => {
          startupTimer = setTimeout(
            () => reject(new Error(`storydex-agentd did not emit ready within ${this.startupTimeoutMs}ms`)),
            this.startupTimeoutMs
          );
        })
      ]);
      clearTimeout(startupTimer);
      await Promise.race([
        waitForAgentdHealth({
          ready,
          request: this.request,
          attempts: this.healthAttempts,
          intervalMs: this.healthIntervalMs
        }),
        startupExitPromise
      ]);
      startupSettled = true;
      this.startedHealthy = true;
      this.log(`storydex-agentd healthy on 127.0.0.1:${ready.port}; authenticated version probe passed`);
      return {
        port: ready.port,
        token: ready.token,
        version: ready.version,
        baseUrl: `http://127.0.0.1:${ready.port}/api/v1`,
        workspaceRoot: this.runtimePaths.workspaceRoot,
        logFilePath: this.logger.logFilePath || ""
      };
    } catch (error) {
      startupSettled = true;
      if (startupTimer) clearTimeout(startupTimer);
      this.stopping = true;
      await this.killTree(processRef);
      await waitForProcessExit(processRef, 2000, () => this.exited);
      this.logger.close();
      throw new Error(
        [error.message, this.logger.logFilePath ? `Log: ${this.logger.logFilePath}` : "", this.logTail()]
          .filter(Boolean)
          .join("\n")
          .trim()
      );
    }
  }

  async stop() {
    if (this.stopPromise) return this.stopPromise;
    this.stopPromise = this.stopRuntime();
    return this.stopPromise;
  }

  async stopRuntime() {
    this.stopping = true;
    const processRef = this.processRef;
    if (!processRef || this.exited) {
      this.logger.close();
      return;
    }

    if (this.ready?.token) {
      try {
        const response = await this.request({
          port: this.ready.port,
          route: "/api/v1/sys/shutdown",
          method: "POST",
          token: this.ready.token,
          timeoutMs: 1500
        });
        if (response.statusCode < 200 || response.statusCode >= 300 || response.json?.ok !== true) {
          throw new Error(`shutdown returned HTTP ${response.statusCode}`);
        }
        this.log("Graceful storydex-agentd shutdown requested");
      } catch (error) {
        this.log(`Graceful shutdown request failed: ${error.message || String(error)}`);
      }
    }

    const exitedGracefully = await waitForProcessExit(processRef, this.gracefulShutdownMs, () => this.exited);
    if (!exitedGracefully && !this.exited) {
      this.log(`Graceful shutdown exceeded ${this.gracefulShutdownMs}ms; cleaning the process tree`);
      await this.killTree(processRef);
      await waitForProcessExit(processRef, 2000, () => this.exited);
    }
    this.logger.close();
  }
}

module.exports = {
  READY_RUNTIME,
  RustBetaRuntimeSupervisor,
  buildAgentdEnvironment,
  createIsolatedBetaPaths,
  createRuntimeLogger,
  isPathInside,
  killProcessTree,
  parseReadyPacket,
  redactRuntimeSecrets,
  requestJson,
  resolveAgentdBinary,
  waitForAgentdHealth,
  waitForProcessExit
};
