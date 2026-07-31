const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { TextDecoder } = require("util");

const projectRoot = path.resolve(__dirname, "..");
const decoder = new TextDecoder("utf-8", { fatal: true });

const includedExtensions = new Set([
  ".bat",
  ".cjs",
  ".css",
  ".html",
  ".js",
  ".json",
  ".md",
  ".ps1",
  ".py",
  ".ts",
  ".vue",
  ".yml",
  ".yaml"
]);

const excludedSegments = new Set([
  ".git",
  ".python39",
  "__pycache__",
  "coverage",
  "dist",
  "node_modules",
  "output",
  "release",
  "releases",
  "test-results",
  "vendor"
]);

const excludedRelativePrefixes = [
  "apps/desktop/app/",
  "apps/frontend/package-lock.json",
  "apps/desktop/package-lock.json",
  "docs/adr/",
  "docs/local/",
  "docs/plans/",
  "evals/",
  "local/",
  "CONTEXT.md"
];

const mojibakePattern =
  /(?:\uFFFD|鍚|璇|杩|瀹|鏉|銆|锛|傦|鈹|攢|椤圭洰|浣跨敤|鎻愪氦|鏂囦欢|鐩綍|鍏佽|杈撳叆|鍥炲|瀛楄妭|瀛楃)/;

const failures = [];

function toPosix(relativePath) {
  return relativePath.replace(/\\/g, "/");
}

function shouldSkip(relativePath) {
  const normalized = toPosix(relativePath);
  if (excludedRelativePrefixes.some((prefix) => normalized.startsWith(prefix))) {
    return true;
  }
  return normalized.split("/").some((segment) => excludedSegments.has(segment));
}

function candidateFiles() {
  const output = execFileSync(
    "git",
    ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    { cwd: projectRoot, encoding: "utf8" }
  );
  return output.split("\0").filter(Boolean);
}

function validateFile(fullPath, relativePath) {
  const data = fs.readFileSync(fullPath);
  let text = "";
  try {
    text = decoder.decode(data);
  } catch (error) {
    failures.push(`${relativePath}: not valid UTF-8 (${error.message})`);
    return;
  }

  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (
      relativePath === "scripts/validate_text_encoding.cjs" &&
      (line.includes("mojibakePattern") || lines[index - 1]?.includes("mojibakePattern"))
    ) {
      continue;
    }
    if (line.includes("_ENCODING_SELFTEST") || line.includes("mojibakePattern")) {
      continue;
    }
    if (mojibakePattern.test(line)) {
      failures.push(`${relativePath}:${index + 1}: possible mojibake: ${line.trim().slice(0, 180)}`);
    }
  }
}

for (const relativePath of candidateFiles()) {
  const normalized = toPosix(relativePath);
  if (shouldSkip(normalized) || !includedExtensions.has(path.extname(normalized).toLowerCase())) {
    continue;
  }
  const fullPath = path.join(projectRoot, relativePath);
  if (fs.existsSync(fullPath) && fs.statSync(fullPath).isFile()) {
    validateFile(fullPath, normalized);
  }
}

if (failures.length) {
  console.error("[Storydex] Text encoding validation failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log("[Storydex] Text files are valid UTF-8 with no known mojibake markers.");
