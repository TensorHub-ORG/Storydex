const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const expectedArg = process.argv.find((value) => value.startsWith("--expected="));
const expected = expectedArg ? expectedArg.slice("--expected=".length).replace(/^v/, "") : "";
const desktopPath = path.join(root, "apps", "desktop", "package.json");
const lockPath = path.join(root, "apps", "desktop", "package-lock.json");
const readmePath = path.join(root, "README.md");
const desktop = JSON.parse(fs.readFileSync(desktopPath, "utf8"));
const lock = JSON.parse(fs.readFileSync(lockPath, "utf8"));
const version = String(desktop.version || "");
const failures = [];

function equal(label, actual, wanted) {
  if (String(actual || "") !== String(wanted || "")) failures.push(`${label}: ${actual || "<missing>"} != ${wanted}`);
}

if (!/^\d+\.\d+\.\d+$/.test(version)) failures.push(`invalid desktop version: ${version}`);
if (expected) equal("desktop version", version, expected);
equal("build.extraMetadata.version", desktop.build?.extraMetadata?.version, version);
equal("package-lock version", lock.version, version);
equal("package-lock root package version", lock.packages?.[""]?.version, version);

const artifactName = desktop.build?.win?.artifactName || "";
if (!artifactName.includes("${version}")) failures.push("desktop artifactName must include ${version}");
const readme = fs.readFileSync(readmePath, "utf8");
if (!readme.includes(`v${version}`)) failures.push(`README does not identify current release v${version}`);
const notes = path.join(root, "apps", "desktop", "build", `release-notes-v${version}.md`);
if (expected && !fs.existsSync(notes)) failures.push(`missing ${path.relative(root, notes)}`);

if (failures.length) {
  console.error("Version consistency check failed:\n" + failures.map((item) => `- ${item}`).join("\n"));
  process.exit(1);
}
console.log(`Version consistency OK: v${version}`);

