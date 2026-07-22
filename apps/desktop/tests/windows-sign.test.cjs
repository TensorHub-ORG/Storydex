const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const signWindowsArtifact = require("../scripts/windows-sign.cjs");
const { shouldSignWindowsArtifact } = signWindowsArtifact;

test("Windows signing is restricted to Storydex-owned release executables", () => {
  const release = path.join("C:", "build", "release");
  assert.equal(shouldSignWindowsArtifact(path.join(release, "win-unpacked", "Storydex.exe")), true);
  assert.equal(shouldSignWindowsArtifact(path.join(release, "StorydexSetup-x64-1.2.3.exe")), true);
  assert.equal(shouldSignWindowsArtifact(path.join(release, "__uninstaller-nsis-storydex.exe")), true);
  assert.equal(shouldSignWindowsArtifact(path.join(release, "win-unpacked", "resources", "app", "python-env", "python.exe")), false);
  assert.equal(shouldSignWindowsArtifact(path.join(release, "win-unpacked", "resources", "app", "mingit", "cmd", "git.exe")), false);
  assert.equal(shouldSignWindowsArtifact(path.join(release, "win-unpacked", "chrome_crashpad_handler.exe")), false);
});

test("the signing hook fails closed for Storydex artifacts and skips third-party executables", async () => {
  const previousRequired = process.env.STORYDEX_REQUIRE_SIGNING;
  process.env.STORYDEX_REQUIRE_SIGNING = "1";
  try {
    await signWindowsArtifact({ path: "C:\\release\\python.exe", cscInfo: null });
    await assert.rejects(
      signWindowsArtifact({ path: "C:\\release\\Storydex.exe", cscInfo: null }),
      /no certificate was resolved/i
    );

    const calls = [];
    const configuration = { path: "C:\\release\\Storydex.exe", cscInfo: { file: "certificate.pfx", password: "secret" } };
    const packager = {
      signtoolManager: {
        value: Promise.resolve({
          doSign: async (...args) => calls.push(args)
        })
      }
    };
    await signWindowsArtifact(configuration, packager);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], configuration);
    assert.equal(calls[0][1], packager);
  } finally {
    if (previousRequired === undefined) delete process.env.STORYDEX_REQUIRE_SIGNING;
    else process.env.STORYDEX_REQUIRE_SIGNING = previousRequired;
  }
});
