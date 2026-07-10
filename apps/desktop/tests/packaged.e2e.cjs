const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { spawn, spawnSync } = require("node:child_process");
const test = require("node:test");
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

async function waitForDebugPort(port, child, logs) {
  for (let index = 0; index < 180; index += 1) {
    if (child.exitCode !== null) throw new Error(`Storydex exited before CDP was ready: ${child.exitCode}\n${logs.join("").slice(-4000)}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Storydex CDP endpoint timed out\n${logs.join("").slice(-4000)}`);
}

test("packaged Electron cold start has visible icons and a healthy backend", { timeout: 150_000 }, async (t) => {
  if (!fs.existsSync(executable)) return t.skip(`packaged executable not found: ${executable}`);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-e2e-"));
  const debugPort = await reservePort();
  const logs = [];
  const child = spawn(executable, [`--remote-debugging-port=${debugPort}`, `--user-data-dir=${path.join(profile, "chromium")}`], {
    env: {
      ...process.env,
      HOME: profile,
      USERPROFILE: profile,
      STORYDEX_GLOBAL_ROOT: path.join(profile, "global"),
      STORYDEX_WORKSPACE_ROOT: path.join(profile, "workspace"),
      STORYDEX_DISABLE_NETWORK: "1",
      STORYDEX_TESTING: "1"
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true
  });
  child.stdout.on("data", (chunk) => logs.push(chunk.toString("utf8")));
  child.stderr.on("data", (chunk) => logs.push(chunk.toString("utf8")));
  let browser = null;
  try {
    await waitForDebugPort(debugPort, child, logs);
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
    const context = browser.contexts()[0];
    const page = context.pages()[0] || await context.waitForEvent("page", { timeout: 30_000 });
    await page.waitForLoadState("domcontentloaded");
    await page.waitForFunction(() => document.documentElement.classList.contains("icon-font-ready"), null, { timeout: 30_000 });
    const result = await page.evaluate(async () => {
      const icons = [...document.querySelectorAll(".material-symbols-rounded")];
      const hidden = icons.filter((node) => {
        const style = getComputedStyle(node);
        return style.visibility === "hidden" || style.display === "none" || style.fontSize === "0px";
      }).length;
      const health = await fetch("http://127.0.0.1:18081/api/v1/sys/health").then((response) => response.ok).catch(() => false);
      return { count: icons.length, hidden, fontReady: document.fonts.check('400 16px "Material Symbols Rounded"'), health };
    });
    assert.ok(result.count > 0);
    assert.equal(result.hidden, 0);
    assert.equal(result.fontReady, true);
    assert.equal(result.health, true);
  } finally {
    if (browser) await browser.close().catch(() => undefined);
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore" });
    } else if (!child.killed) {
      child.kill("SIGTERM");
    }
    if (child.exitCode === null) {
      await Promise.race([
        new Promise((resolve) => child.once("exit", resolve)),
        new Promise((resolve) => setTimeout(resolve, 5000))
      ]);
    }
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 250 });
  }
});
