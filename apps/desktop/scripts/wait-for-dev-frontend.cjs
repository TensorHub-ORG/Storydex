const waitOn = require("wait-on");

function resolveFrontendResource(environment = process.env) {
  const configuredUrl = environment.STORYDEX_DESKTOP_URL || "http://127.0.0.1:5173";
  const parsed = new URL(configuredUrl);
  if (!parsed.hostname || !parsed.port) {
    throw new Error(`Desktop development URL must include a host and port: ${configuredUrl}`);
  }
  return `tcp:${parsed.hostname}:${parsed.port}`;
}

async function main() {
  const resource = resolveFrontendResource();
  console.log(`[Storydex Desktop] Waiting for ${resource}...`);
  await waitOn({
    resources: [resource],
    interval: 250,
    tcpTimeout: 1000,
    timeout: 120000
  });
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`[Storydex Desktop] Frontend startup wait failed: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { resolveFrontendResource };
