const path = require("path");

function shouldSignWindowsArtifact(filePath) {
  const name = path.basename(String(filePath || "")).toLowerCase();
  return name === "storydex.exe"
    || (name.startsWith("storydexsetup-") && name.endsWith(".exe"))
    || (name.startsWith("__uninstaller-nsis-storydex") && name.endsWith(".exe"));
}

function signingIsRequired() {
  return process.env.STORYDEX_REQUIRE_SIGNING === "1"
    || Boolean(process.env.CSC_LINK || process.env.WIN_CSC_LINK);
}

async function signWindowsArtifact(configuration, packager) {
  if (!shouldSignWindowsArtifact(configuration?.path)) {
    return;
  }
  if (!configuration?.cscInfo) {
    if (signingIsRequired()) {
      throw new Error(`Windows signing was required but no certificate was resolved for ${configuration?.path || "release artifact"}.`);
    }
    return;
  }
  if (!packager?.signtoolManager?.value) {
    throw new Error("electron-builder did not provide its Windows signing manager.");
  }
  const manager = await packager.signtoolManager.value;
  await manager.doSign(configuration, packager);
}

module.exports = signWindowsArtifact;
module.exports.shouldSignWindowsArtifact = shouldSignWindowsArtifact;
