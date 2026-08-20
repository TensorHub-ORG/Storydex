const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const projectRoot = path.resolve(__dirname, "..", "..");
const generator = path.join(projectRoot, "scripts", "generate_release_metadata.cjs");
const desktopPackage = JSON.parse(
  fs.readFileSync(path.join(projectRoot, "apps", "desktop", "package.json"), "utf8")
);

function digest(contentOrPath, algorithm, encoding = "hex") {
  const hash = crypto.createHash(algorithm);
  if (Buffer.isBuffer(contentOrPath)) hash.update(contentOrPath);
  else hash.update(fs.readFileSync(contentOrPath));
  return hash.digest(encoding);
}

test("release metadata writes a complete checksum file from the artifact digest pass", () => {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), "storydex-release-metadata-"));
  const version = desktopPackage.version;
  const setupName = `StorydexSetup-x64-${version}.exe`;
  const setup = Buffer.from("test installer payload", "utf8");
  const signature = "dGVzdC1zaWduYXR1cmU=";

  try {
    fs.writeFileSync(path.join(releaseDir, setupName), setup);
    fs.writeFileSync(path.join(releaseDir, `${setupName}.sig`), `${signature}\n`);
    fs.writeFileSync(path.join(releaseDir, "Storydex-win-portable.zip"), "portable\n");
    fs.writeFileSync(path.join(releaseDir, "RELEASE_NOTES.md"), "# Test release\n");
    fs.writeFileSync(
      path.join(releaseDir, "latest.json"),
      JSON.stringify({
        version,
        platforms: {
          "windows-x86_64": {
            signature,
            url: `https://updates.example.test/${setupName}`
          }
        }
      })
    );

    const result = spawnSync(
      process.execPath,
      [generator, `--release-dir=${releaseDir}`, `--version=${version}`, "--test-summary=unit test"],
      { cwd: projectRoot, encoding: "utf8", windowsHide: true }
    );
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

    const checksumPath = path.join(releaseDir, "SHA256SUMS.txt");
    const checksumEntries = new Map(
      fs.readFileSync(checksumPath, "ascii")
        .trim()
        .split(/\r?\n/)
        .map((line) => {
          const match = line.match(/^([A-F0-9]{64})  (.+)$/);
          assert.ok(match, `invalid checksum line: ${line}`);
          return [match[2], match[1]];
        })
    );
    const checksumFiles = fs.readdirSync(releaseDir)
      .filter((name) => name !== "SHA256SUMS.txt")
      .sort();
    assert.deepEqual([...checksumEntries.keys()].sort(), checksumFiles);
    for (const name of checksumFiles) {
      assert.equal(checksumEntries.get(name), digest(path.join(releaseDir, name), "sha256").toUpperCase());
    }

    const manifest = JSON.parse(fs.readFileSync(path.join(releaseDir, "BUILD_MANIFEST.json"), "utf8"));
    assert.equal(manifest.version, version);
    const manifestFiles = checksumFiles.filter((name) => name !== "BUILD_MANIFEST.json" && name !== "SHA256SUMS.txt");
    assert.deepEqual(
      manifest.artifacts.map((artifact) => artifact.name).sort(),
      manifestFiles.sort()
    );
  } finally {
    fs.rmSync(releaseDir, { recursive: true, force: true });
  }
});
