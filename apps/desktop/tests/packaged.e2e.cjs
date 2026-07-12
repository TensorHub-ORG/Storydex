const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { spawn, spawnSync } = require("node:child_process");
const test = require("node:test");
process.env.PW_TEST_SCREENSHOT_NO_FONTS_READY = "1";
const { chromium } = require("playwright");

const executable = process.env.STORYDEX_PACKAGED_EXE || path.resolve(__dirname, "..", "release", "win-unpacked", "Storydex.exe");

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForDebugPort(port, child, logs) {
  for (let index = 0; index < 180; index += 1) {
    if (child.exitCode !== null) throw new Error(`Storydex exited before CDP was ready: ${child.exitCode}\n${logs.join("").slice(-4000)}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return;
    } catch {}
    await delay(500);
  }
  throw new Error(`Storydex CDP endpoint timed out\n${logs.join("").slice(-4000)}`);
}

async function waitForBackendUnavailable(backendBaseUrl, timeoutMs = 10_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      await fetch(`${backendBaseUrl}/sys/health`, { signal: AbortSignal.timeout(500) });
    } catch {
      return true;
    }
    await delay(200);
  }
  return false;
}

function createFakeOpenAiServer() {
  const requests = [];
  const server = http.createServer(async (request, response) => {
    if (request.method === "GET" && request.url === "/v1/models") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "storydex-e2e" }] }));
      return;
    }
    if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
      response.writeHead(404).end();
      return;
    }
    let raw = "";
    for await (const chunk of request) raw += chunk.toString("utf8");
    const payload = JSON.parse(raw || "{}");
    requests.push(payload);
    const messages = Array.isArray(payload.messages) ? payload.messages : [];
    const promptText = messages.map((item) => String(item?.content || "")).join("\n");

    if (!payload.stream) {
      await delay(1_250);
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        id: "intent-e2e",
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model: "storydex-e2e",
        choices: [{
          index: 0,
          finish_reason: "stop",
          message: {
            role: "assistant",
            content: JSON.stringify({ primary: "general", confidence: "high", signals: ["e2e_fake_provider"], reason: "deterministic test provider" })
          }
        }],
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 }
      }));
      return;
    }

    const content = promptText.includes("执行")
      ? "已承接上一轮变量整理操作。"
      : "变量更新已完成，是否需要执行变量整理？";
    response.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
      connection: "keep-alive"
    });
    const chunk = (data) => response.write(`data: ${JSON.stringify(data)}\n\n`);
    chunk({ id: "agent-e2e", object: "chat.completion.chunk", created: 1, model: "storydex-e2e", choices: [{ index: 0, delta: { role: "assistant", content }, finish_reason: null }] });
    chunk({ id: "agent-e2e", object: "chat.completion.chunk", created: 1, model: "storydex-e2e", choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 20, completion_tokens: 10, total_tokens: 30 } });
    response.end("data: [DONE]\n\n");
  });
  return {
    requests,
    async listen() {
      const port = await reservePort();
      await new Promise((resolve, reject) => server.listen(port, "127.0.0.1", resolve).once("error", reject));
      return `http://127.0.0.1:${port}/v1`;
    },
    async close() {
      await new Promise((resolve) => server.close(resolve));
    }
  };
}

function writeFakeProviderConfig(globalRoot, baseUrl) {
  const target = path.join(globalRoot, ".coomi", "config", "providers.json");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, JSON.stringify({
    version: 1,
    active: "e2e",
    providers: {
      e2e: {
        type: "generic",
        display: "Storydex E2E fake provider",
        api_key: "e2e-local-only",
        base_url: baseUrl,
        model: "storydex-e2e",
        fast_model: "storydex-e2e",
        tool_protocol: "disabled"
      }
    }
  }, null, 2));
}

async function launchPackaged({ profile, workspace, globalRoot, logs }) {
  const debugPort = await reservePort();
  const backendPort = await reservePort();
  const backendBaseUrl = `http://127.0.0.1:${backendPort}/api/v1`;
  const child = spawn(executable, [`--remote-debugging-port=${debugPort}`, `--user-data-dir=${path.join(profile, "chromium")}`], {
    env: {
      ...process.env,
      HOME: profile,
      USERPROFILE: profile,
      STORYDEX_GLOBAL_ROOT: globalRoot,
      STORYDEX_WORKSPACE_ROOT: workspace,
      STORYDEX_BACKEND_PORT: String(backendPort),
      STORYDEX_DISABLE_NETWORK: "1",
      STORYDEX_TESTING: "1"
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true
  });
  child.stdout.on("data", (chunk) => logs.push(chunk.toString("utf8")));
  child.stderr.on("data", (chunk) => logs.push(chunk.toString("utf8")));
  await waitForDebugPort(debugPort, child, logs);
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
  const context = browser.contexts()[0];
  const page = context.pages()[0] || await context.waitForEvent("page", { timeout: 30_000 });
  await page.waitForLoadState("domcontentloaded");
  return { child, browser, page, backendBaseUrl };
}

async function closePackaged(instance, { force = false } = {}) {
  const { child, browser, page } = instance;
  if (!force && child.exitCode === null) {
    await page.evaluate(() => window.close()).catch(() => undefined);
    await Promise.race([
      new Promise((resolve) => child.once("exit", resolve)),
      delay(10_000)
    ]);
  }
  await browser.close().catch(() => undefined);
  if (child.exitCode === null) {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore" });
    } else if (!child.killed) {
      child.kill("SIGTERM");
    }
    await Promise.race([new Promise((resolve) => child.once("exit", resolve)), delay(5_000)]);
  }
}

function parseSseFrame(frame) {
  const event = (frame.match(/^event:\s*(.+)$/m) || [])[1] || "message";
  const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
  if (!data || data === "[DONE]") return null;
  return { event, data: JSON.parse(data) };
}

test("packaged updater recovers when its entrypoint appears after a transient install window", { timeout: 90_000 }, async (t) => {
  if (!fs.existsSync(executable)) return t.skip(`packaged executable not found: ${executable}`);
  const appRoot = path.join(path.dirname(executable), "resources", "app");
  const updaterEntry = path.join(appRoot, "node_modules", "electron-updater", "out", "main.js");
  if (!fs.existsSync(updaterEntry)) return t.skip(`packaged updater entrypoint not found: ${updaterEntry}`);

  const stagedEntry = `${updaterEntry}.installing`;
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-updater-retry-"));
  const workspace = path.join(profile, "workspace");
  const globalRoot = path.join(profile, ".storydex");
  const logs = [];
  let app = null;
  let restored = false;
  const restoreUpdater = () => {
    if (!restored && fs.existsSync(stagedEntry)) {
      fs.renameSync(stagedEntry, updaterEntry);
      restored = true;
    }
  };

  fs.mkdirSync(workspace, { recursive: true });
  fs.renameSync(updaterEntry, stagedEntry);
  const restoreWatcher = setInterval(() => {
    if (logs.join("").includes("electron-updater unavailable")) restoreUpdater();
  }, 50);

  try {
    app = await launchPackaged({ profile, workspace, globalRoot, logs });
    restoreUpdater();
    await app.page.waitForFunction(async () => {
      const state = await window.storydexDesktop.updater.getState();
      return state.supported && state.status !== "initializing";
    }, null, { timeout: 15_000 });
    const state = await app.page.evaluate(() => window.storydexDesktop.updater.getState());
    assert.equal(state.supported, true, state.error || "updater retry must recover");
  } finally {
    clearInterval(restoreWatcher);
    restoreUpdater();
    if (app) await closePackaged(app, { force: false });
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 250 });
  }
});

async function streamAgent({ backendBaseUrl, prompt, sessionId, workspaceRoot, stopWhen, timeoutMs = 30_000, busyRetries = 8 }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("SSE timeout")), timeoutMs);
  const started = performance.now();
  const response = await fetch(`${backendBaseUrl}/agent/chat/stream?sessionId=${encodeURIComponent(sessionId)}`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream", "x-trace-id": `e2e-${Date.now()}` },
    body: JSON.stringify({ prompt, workspaceRoot, activeFile: "chapters/001.md" }),
    signal: controller.signal
  });
  if (response.status === 409 && busyRetries > 0) {
    clearTimeout(timer);
    controller.abort();
    await delay(250);
    return streamAgent({ backendBaseUrl, prompt, sessionId, workspaceRoot, stopWhen, timeoutMs, busyRetries: busyRetries - 1 });
  }
  assert.equal(response.status, 200);
  const events = [];
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const parsed = parseSseFrame(frame);
        if (!parsed) continue;
        events.push({ ...parsed, receivedMs: performance.now() - started });
        if (stopWhen(parsed, events)) {
          await reader.cancel();
          return events;
        }
      }
    }
    return events;
  } finally {
    clearTimeout(timer);
    controller.abort();
  }
}

test("packaged Electron validates icons, streaming responsiveness, session recovery, project isolation, and shutdown", { timeout: 240_000 }, async (t) => {
  if (!fs.existsSync(executable)) return t.skip(`packaged executable not found: ${executable}`);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-e2e-"));
  const workspace = path.join(profile, "workspace");
  const secondWorkspace = path.join(profile, "workspace-two");
  const globalRoot = path.join(profile, ".storydex");
  const resultsDir = path.resolve(__dirname, "..", "test-results");
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(secondWorkspace, { recursive: true });
  fs.mkdirSync(resultsDir, { recursive: true });
  const logs = [];
  const fakeProvider = createFakeOpenAiServer();
  const fakeBaseUrl = await fakeProvider.listen();
  writeFakeProviderConfig(globalRoot, fakeBaseUrl);
  let app = null;
  const metrics = {};
  try {
    app = await launchPackaged({ profile, workspace, globalRoot, logs });
    await app.page.waitForFunction(() => {
      const icons = [...document.querySelectorAll(".material-symbols-rounded")];
      return icons.length > 0 && icons.every((node) => {
        const style = getComputedStyle(node);
        return style.visibility !== "hidden" && style.display !== "none" && style.fontSize !== "0px";
      });
    }, null, { timeout: 30_000 });
    const visual = await app.page.evaluate(async () => {
      const icons = [...document.querySelectorAll(".material-symbols-rounded")];
      const hidden = icons.filter((node) => {
        const style = getComputedStyle(node);
        return style.visibility === "hidden" || style.display === "none" || style.fontSize === "0px";
      }).length;
      const health = await fetch(window.storydexDesktop.backendBaseUrl + "/sys/health").then((response) => response.ok).catch(() => false);
      const updater = await window.storydexDesktop.updater.getState();
      return {
        count: icons.length,
        hidden,
        fontReady: document.fonts.check('400 16px "Material Symbols Rounded"'),
        fallbackActive: document.documentElement.classList.contains("icon-font-failed"),
        health,
        updaterSupported: updater.supported,
        updaterError: updater.error
      };
    });
    assert.ok(visual.count > 0);
    assert.equal(visual.hidden, 0);
    assert.equal(visual.fontReady || visual.fallbackActive, true);
    assert.equal(visual.health, true);
    assert.equal(visual.updaterSupported, true, visual.updaterError || "packaged updater must be available");
    await app.page.screenshot({ path: path.join(resultsDir, "packaged-cold-start.png"), fullPage: true });

    const created = await fetch(`${app.backendBaseUrl}/workspace/project/create`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ projectPath: workspace })
    });
    assert.equal(created.status, 200);
    const opened = await fetch(`${app.backendBaseUrl}/workspace/project/open`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ projectPath: workspace })
    });
    assert.equal(opened.status, 200);

    const responsiveness = await streamAgent({
      backendBaseUrl: app.backendBaseUrl,
      prompt: "请检查当前项目状态",
      sessionId: "responsiveness-session",
      workspaceRoot: workspace,
      stopWhen: (packet) => packet.data?._type === "done",
      timeoutMs: 60_000
    });
    const accepted = responsiveness.find((item) => item.data?._type === "RunAccepted");
    const heartbeat = responsiveness.find((item) => item.data?.heartbeat === true);
    assert.ok(accepted, "RunAccepted must be emitted");
    assert.ok(accepted.receivedMs < 200, `first SSE event took ${accepted.receivedMs.toFixed(1)}ms`);
    assert.ok(Number(heartbeat.data.elapsedMs) >= 500 && Number(heartbeat.data.elapsedMs) < 1_000, `heartbeat phase elapsed time was ${heartbeat.data.elapsedMs}ms`);
    assert.ok(heartbeat.receivedMs < 1_500, `heartbeat arrived at ${heartbeat.receivedMs.toFixed(1)}ms`);
    metrics.firstSseMs = Number(accepted.receivedMs.toFixed(2));
    metrics.firstHeartbeatMs = Number(heartbeat.receivedMs.toFixed(2));
    metrics.firstHeartbeatPhaseElapsedMs = Number(heartbeat.data.elapsedMs);

    const firstTurn = await streamAgent({
      backendBaseUrl: app.backendBaseUrl,
      prompt: "请完成变量更新",
      sessionId: "resume-session",
      workspaceRoot: workspace,
      stopWhen: (packet) => packet.data?._type === "done"
    });
    const firstTurnText = firstTurn.map((item) => item.data?.content || "").join("");
    assert.match(
      firstTurnText,
      /是否需要执行变量整理/,
      `events=${JSON.stringify(firstTurn.map((item) => ({ event: item.event, type: item.data?._type, message: item.data?.message })))}`
    );

    const gitInit = await fetch(`${app.backendBaseUrl}/workspace/git/init`, { method: "POST" });
    assert.equal(gitInit.status, 200);
    const baselineFile = await fetch(`${app.backendBaseUrl}/workspace/file/create`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ relativePath: "notes/e2e-baseline.md", content: "baseline\n" })
    });
    assert.equal(baselineFile.status, 200);
    const baseline = await fetch(`${app.backendBaseUrl}/workspace/git/commit`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: "test: establish packaged e2e baseline" })
    });
    assert.equal(baseline.status, 200);
    const baselinePayload = await baseline.json();
    assert.equal(baselinePayload.data?.created, true, `baseline commit failed: ${JSON.stringify(baselinePayload)}`);
    await app.page.reload({ waitUntil: "domcontentloaded" });
    await app.page.getByTitle("新建会话").click();
    const input = app.page.locator(".coomi-input");
    await input.waitFor({ state: "visible", timeout: 30_000 });
    await input.fill("请检查版本状态");
    let uiSessionId = "";
    app.page.on("request", (request) => {
      const match = request.url().match(/\/agent\/chat\/stream\?sessionId=([^&]+)/);
      if (match) uiSessionId = decodeURIComponent(match[1]);
    });
    await app.page.waitForFunction(() => {
      const button = document.querySelector(".coomi-send");
      return button && !button.disabled;
    }, null, { timeout: 30_000 });
    await app.page.locator(".coomi-send").click();
    await delay(750);
    const changed = await fetch(`${app.backendBaseUrl}/workspace/file/create`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ relativePath: "notes/e2e-change.md", content: "packaged e2e change\n" })
    });
    assert.equal(changed.status, 200);
    let latestUiRun = {};
    const historyDeadline = Date.now() + 30_000;
    while (Date.now() < historyDeadline) {
      if (!uiSessionId) {
        await delay(100);
        continue;
      }
      const historyResponse = await fetch(`${app.backendBaseUrl}/agent/history?sessionId=${encodeURIComponent(uiSessionId)}`);
      const historyPayload = await historyResponse.json();
      latestUiRun = (historyPayload.data?.items || []).find((item) => item.prompt === "请检查版本状态") || {};
      if ((latestUiRun.events || []).some((item) => item.event === "GitCommitPrompt")) break;
      await delay(200);
    }
    assert.ok(uiSessionId, "packaged UI must issue the chat request with a Storydex session id");
    const latestUiEvents = (latestUiRun.events || []).map((item) => item.event);
    metrics.uiGitEvents = latestUiEvents.filter((event) => String(event).includes("Git"));
    assert.ok(latestUiEvents.includes("GitCommitPrompt"), `latest packaged UI run did not request a Git decision: ${JSON.stringify(latestUiRun)}`);
    const commitMenu = app.page.locator(".coomi-commit-menu");
    try {
      await commitMenu.waitFor({ state: "visible", timeout: 5_000 });
    } catch (error) {
      const uiState = await app.page.evaluate(() => ({
        inputDisabled: document.querySelector(".coomi-input")?.disabled,
        commitMenuCount: document.querySelectorAll(".coomi-commit-menu").length,
        collapsedHandles: [...document.querySelectorAll(".coomi-collapsed-handle")].map((node) => node.textContent?.trim()),
        composerText: document.querySelector(".coomi-composer")?.textContent?.trim(),
        store: (() => {
          let component = document.querySelector(".coomi-dock")?.__vueParentComponent;
          while (component) {
            const store = component.setupState?.agentStore;
            if (store) {
              return {
                pendingCommitPrompt: store.pendingCommitPrompt,
                currentSessionId: store.currentSessionId,
                latestEvents: (store.executionHistory?.[0]?.events || []).map((item) => item.event)
              };
            }
            component = component.parent;
          }
          return null;
        })()
      }));
      throw new Error(`Git prompt exists in the stream but is not visible: ${JSON.stringify(uiState)}`, { cause: error });
    }
    const skipStarted = performance.now();
    await commitMenu.locator(".coomi-command-option").last().click();
    await commitMenu.waitFor({ state: "hidden", timeout: 5_000 });
    const skipResponseMs = performance.now() - skipStarted;
    assert.ok(skipResponseMs < 100, `Git skip panel took ${skipResponseMs.toFixed(1)}ms to close`);
    metrics.gitSkipResponseMs = Number(skipResponseMs.toFixed(2));

    const openSecond = await fetch(`${app.backendBaseUrl}/workspace/project/open`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ projectPath: secondWorkspace })
    });
    assert.equal(openSecond.status, 200);
    const isolated = await fetch(`${app.backendBaseUrl}/agent/history?sessionId=${encodeURIComponent(uiSessionId)}`);
    assert.equal(isolated.status, 200);
    const isolatedPayload = await isolated.json();
    assert.equal((isolatedPayload.data?.items || []).length, 0);
    const reopenFirst = await fetch(`${app.backendBaseUrl}/workspace/project/open`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ projectPath: workspace })
    });
    assert.equal(reopenFirst.status, 200);

    const firstBackendBaseUrl = app.backendBaseUrl;
    await closePackaged(app);
    app = null;
    assert.equal(await waitForBackendUnavailable(firstBackendBaseUrl), true, "backend must exit after the Electron window closes");

    app = await launchPackaged({ profile, workspace, globalRoot, logs });
    const resumed = await streamAgent({
      backendBaseUrl: app.backendBaseUrl,
      prompt: "执行",
      sessionId: "resume-session",
      workspaceRoot: workspace,
      stopWhen: (packet) => packet.data?._type === "done"
    });
    const contract = resumed.find((item) => item.data?._type === "TurnContract");
    assert.equal(contract?.data?.intentFrame?.method, "deterministic_context");
    assert.match(resumed.map((item) => item.data?.content || "").join(""), /承接上一轮变量整理/);
    assert.notEqual(contract?.data?.intentFrame?.primary, "story_generation");
    metrics.fakeProviderRequests = fakeProvider.requests.length;
  } finally {
    if (app) await closePackaged(app, { force: false });
    await fakeProvider.close().catch(() => undefined);
    fs.writeFileSync(path.join(resultsDir, "packaged-e2e-metrics.json"), JSON.stringify(metrics, null, 2));
    fs.writeFileSync(path.join(resultsDir, "packaged-e2e.log"), logs.join(""));
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 250 });
  }
});
