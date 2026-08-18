const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PassThrough } = require("node:stream");
const test = require("node:test");
const {
  RustBetaRuntimeSupervisor,
  buildAgentdEnvironment,
  createIsolatedBetaPaths,
  parseReadyPacket,
  redactRuntimeSecrets
} = require("../electron/rust-beta/runtime.cjs");

const token = "0123456789abcdef0123456789abcdef";

function createFakeChild(pid = 43210) {
  const child = new EventEmitter();
  child.pid = pid;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.exitCode = null;
  child.signalCode = null;
  child.killed = false;
  child.kill = (signal = "SIGTERM") => {
    child.killed = true;
    child.finish(null, signal);
  };
  child.finish = (code = 0, signal = null) => {
    if (child.exitCode !== null || child.signalCode) return;
    child.exitCode = code;
    child.signalCode = signal;
    child.emit("exit", code, signal);
    child.emit("close", code, signal);
  };
  return child;
}

function createMemoryLogger() {
  const messages = [];
  let closed = false;
  return {
    messages,
    logFilePath: "C:/temp/storydex-rust-beta.log",
    write(message) {
      messages.push(String(message));
    },
    close() {
      closed = true;
    },
    get closed() {
      return closed;
    }
  };
}

function createRuntimePaths(root) {
  const profileRoot = path.join(root, "profile");
  const workspaceRoot = path.join(profileRoot, "workspace");
  const coomiHome = path.join(profileRoot, "coomi-home");
  const logsDir = path.join(profileRoot, "logs");
  for (const target of [workspaceRoot, coomiHome, logsDir]) fs.mkdirSync(target, { recursive: true });
  return { profileRoot, workspaceRoot, coomiHome, logsDir };
}

function writeReady(child, port = 31415) {
  child.stdout.write(`${JSON.stringify({
    event: "ready",
    runtime: "storydex-agentd",
    port,
    token,
    version: "test"
  })}\n`);
}

test("Rust Beta ready packet requires a dynamic loopback port and random token", () => {
  assert.deepEqual(parseReadyPacket(JSON.stringify({
    event: "ready",
    runtime: "storydex-agentd",
    port: 31415,
    token,
    version: "test"
  })), {
    event: "ready",
    runtime: "storydex-agentd",
    port: 31415,
    token,
    version: "test"
  });
  assert.throws(() => parseReadyPacket('{"event":"ready","runtime":"other","port":1,"token":"bad"}'), /first packet/);
  assert.doesNotMatch(redactRuntimeSecrets(`{"token":"${token}"}`, token), new RegExp(token));
});

test("Rust Beta workspace and profile are restricted to temporary directories", () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-rust-beta-boundary-"));
  try {
    const paths = createIsolatedBetaPaths({
      temporaryRoot,
      profileRoot: path.join(temporaryRoot, "profile"),
      workspaceRoot: path.join(temporaryRoot, "fixture-workspace")
    });
    assert.equal(paths.workspaceRoot, fs.realpathSync.native(path.join(temporaryRoot, "fixture-workspace")));
    assert.throws(
      () => createIsolatedBetaPaths({ temporaryRoot, workspaceRoot: path.resolve(temporaryRoot, "..", "real-user-project") }),
      /must stay inside/
    );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("Rust Beta child environment removes Python and Stable workspace fallbacks", () => {
  const runtimePaths = {
    profileRoot: "C:/temp/profile",
    workspaceRoot: "C:/temp/workspace",
    coomiHome: "C:/temp/coomi"
  };
  const environment = buildAgentdEnvironment({
    PATH: "test-path",
    STORYDEX_PYTHON: "python.exe",
    STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK: "1",
    STORYDEX_WORKSPACE_ROOT: "C:/Users/real/project",
    STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE: "C:/Users/real/replay.json"
  }, runtimePaths);
  assert.equal(environment.PATH, "test-path");
  assert.equal(environment.STORYDEX_PYTHON, undefined);
  assert.equal(environment.STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK, undefined);
  assert.equal(environment.STORYDEX_WORKSPACE_ROOT, undefined);
  assert.equal(environment.STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE, undefined);
  assert.equal(environment.STORYDEX_AGENTD_REFACTOR_ROOT, runtimePaths.workspaceRoot);
  assert.equal(environment.STORYDEX_DISABLE_NETWORK, "1");
});

test("Rust Beta supervisor validates health/auth and shuts down gracefully", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-rust-beta-supervisor-"));
  const child = createFakeChild();
  const logger = createMemoryLogger();
  const calls = [];
  let spawnCall = null;
  let forced = false;
  try {
    const supervisor = new RustBetaRuntimeSupervisor({
      binaryPath: "C:/repo/storydex-agentd.exe",
      runtimePaths: createRuntimePaths(root),
      environment: { PATH: "test-path", STORYDEX_PYTHON: "python.exe" },
      logger,
      spawnProcess(command, args, options) {
        spawnCall = { command, args, options };
        return child;
      },
      async request(request) {
        calls.push(request);
        if (request.route === "/api/v1/sys/health") {
          return { statusCode: 200, json: { ok: true, data: { runtime: "storydex-agentd" } } };
        }
        if (request.route === "/api/v1/sys/version") {
          assert.equal(request.token, token);
          return { statusCode: 200, json: { ok: true, data: { runtime: "storydex-agentd" } } };
        }
        if (request.route === "/api/v1/sys/shutdown") {
          assert.equal(request.method, "POST");
          setImmediate(() => child.finish(0, null));
          return { statusCode: 200, json: { ok: true, data: { status: "stopping" } } };
        }
        throw new Error(`unexpected route ${request.route}`);
      },
      async killTree() {
        forced = true;
        child.finish(null, "SIGKILL");
      },
      startupTimeoutMs: 500,
      healthAttempts: 2,
      healthIntervalMs: 1,
      gracefulShutdownMs: 100
    });
    const started = supervisor.start();
    setImmediate(() => writeReady(child));
    const runtime = await started;
    assert.equal(runtime.baseUrl, "http://127.0.0.1:31415/api/v1");
    assert.equal(runtime.token, token);
    assert.deepEqual(spawnCall.args.slice(0, 2), ["--port", "0"]);
    assert.equal(spawnCall.options.cwd, path.join(root, "profile", "workspace"));
    assert.equal(spawnCall.options.env.STORYDEX_PYTHON, undefined);
    assert.equal(spawnCall.options.env.STORYDEX_AGENTD_REFACTOR_ROOT, spawnCall.options.cwd);
    await supervisor.stop();
    assert.equal(forced, false);
    assert.equal(logger.closed, true);
    assert.ok(calls.some((call) => call.route === "/api/v1/sys/shutdown"));
    assert.doesNotMatch(logger.messages.join("\n"), new RegExp(token));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Rust Beta makes an unexpected agentd crash visible and cleans a hung process tree", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-rust-beta-crash-"));
  const child = createFakeChild();
  const logger = createMemoryLogger();
  let crash = null;
  let forced = false;
  try {
    const supervisor = new RustBetaRuntimeSupervisor({
      binaryPath: "C:/repo/storydex-agentd.exe",
      runtimePaths: createRuntimePaths(root),
      logger,
      spawnProcess: () => child,
      request: async (request) => {
        if (request.route === "/api/v1/sys/health" || request.route === "/api/v1/sys/version") {
          return { statusCode: 200, json: { ok: true, data: { runtime: "storydex-agentd" } } };
        }
        return { statusCode: 200, json: { ok: true, data: { status: "stopping" } } };
      },
      async killTree() {
        forced = true;
        child.finish(null, "SIGKILL");
      },
      onUnexpectedExit(info) {
        crash = info;
      },
      startupTimeoutMs: 500,
      healthAttempts: 2,
      healthIntervalMs: 1,
      gracefulShutdownMs: 100
    });
    const started = supervisor.start();
    setImmediate(() => writeReady(child, 31416));
    await started;
    child.stderr.write(`fatal without token ${token}`);
    child.finish(42, null);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(crash.code, 42);
    assert.doesNotMatch(crash.detail, new RegExp(token));

    const hungChild = createFakeChild(43211);
    const hungSupervisor = new RustBetaRuntimeSupervisor({
      binaryPath: "C:/repo/storydex-agentd.exe",
      runtimePaths: createRuntimePaths(path.join(root, "hung")),
      logger: createMemoryLogger(),
      spawnProcess: () => hungChild,
      request: async (request) => {
        if (request.route === "/api/v1/sys/health" || request.route === "/api/v1/sys/version") {
          return { statusCode: 200, json: { ok: true, data: { runtime: "storydex-agentd" } } };
        }
        return { statusCode: 200, json: { ok: true, data: { status: "stopping" } } };
      },
      async killTree() {
        forced = true;
        hungChild.finish(null, "SIGKILL");
      },
      startupTimeoutMs: 500,
      healthAttempts: 2,
      healthIntervalMs: 1,
      gracefulShutdownMs: 100
    });
    const hungStarted = hungSupervisor.start();
    setImmediate(() => writeReady(hungChild, 31417));
    await hungStarted;
    await hungSupervisor.stop();
    assert.equal(forced, true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Electron Rust Beta remains isolated from Stable entry, package, and update feed", () => {
  const desktopRoot = path.resolve(__dirname, "..");
  const desktopPackage = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  const betaMain = fs.readFileSync(path.join(desktopRoot, "electron", "rust-beta", "main.cjs"), "utf8");
  const betaPreload = fs.readFileSync(path.join(desktopRoot, "electron", "rust-beta", "preload.cjs"), "utf8");
  assert.equal(desktopPackage.main, "electron/main.cjs");
  assert.equal(desktopPackage.scripts["build:desktop"], "npm run prepare:package && npm run build:desktop:prepared");
  assert.equal(desktopPackage.scripts["package:win"], "npm run prepare:package && npm run package:win:prepared");
  assert.equal(desktopPackage.build.publish[0].url, "https://updates.septemc.com/storydex/windows/");
  assert.doesNotMatch(betaMain, /require\(["']\.\.\/main\.cjs["']\)/);
  assert.doesNotMatch(betaMain, /onBeforeSendHeaders|Authorization/);
  assert.match(betaMain, /http:\/\/127\.0\.0\.1:<port>/);
  assert.match(betaMain, /webContents\.on\(["']will-navigate["']/);
  assert.match(betaMain, /event\.sender\.id\s*!==\s*mainWindow\.webContents\.id/);
  assert.match(betaMain, /STORYDEX_RUST_BETA_FRONTEND_DIST must stay inside/);
  assert.match(betaPreload, /backendAuthToken/);
  assert.match(betaPreload, /storydex:rust-beta-runtime-connection/);
});
