const path = require("path");
const { patchExecutable } = require("./patch-win-executable.cjs");

/**
 * Patch Windows resources before electron-builder signs the application.
 */
exports = module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "win32") {
    return;
  }
  const executableName = context.packager.appInfo.productFilename;
  const executablePath = path.join(context.appOutDir, `${executableName}.exe`);
  patchExecutable(executablePath);
  if (process.env.STORYDEX_REQUIRE_SIGNING === "1" || process.env.CSC_LINK || process.env.WIN_CSC_LINK) {
    await context.packager.sign(executablePath);
  }
};
