const fs = require("fs");
const path = require("path");

const desktopRoot = path.resolve(__dirname, "..");
const packageJsonPath = path.join(desktopRoot, "package.json");
const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));

const publish = packageJson.build?.publish;
const publishEntries = Array.isArray(publish) ? publish : publish ? [publish] : [];
const genericFeed = publishEntries.find((entry) => String(entry?.provider || "").trim() === "generic" && entry?.url);
const updateUrl = String(genericFeed?.url || "").trim();

if (!updateUrl) {
  console.error("[Storydex Desktop] package.json build.publish must define a generic update URL.");
  process.exit(1);
}

const resourcesDir = path.join(desktopRoot, "release", "win-unpacked", "resources");
if (!fs.existsSync(resourcesDir)) {
  console.error(`[Storydex Desktop] Packaged resources directory not found: ${resourcesDir}`);
  process.exit(1);
}

const appUpdatePath = path.join(resourcesDir, "app-update.yml");
const appUpdateYaml = [
  "provider: generic",
  `url: ${updateUrl}`,
  "updaterCacheDirName: storydex-updater",
  ""
].join("\n");

fs.writeFileSync(appUpdatePath, appUpdateYaml, "utf8");
console.log(`[Storydex Desktop] Wrote update feed config: ${appUpdatePath}`);
