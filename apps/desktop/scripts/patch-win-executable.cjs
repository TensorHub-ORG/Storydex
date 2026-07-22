const fs = require("fs");
const path = require("path");
const ResEdit = require("resedit");

const desktopRoot = path.resolve(__dirname, "..");
const packageJsonPath = path.join(desktopRoot, "package.json");
const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));

const productName = packageJson.build?.productName || "Storydex";
const executableName = packageJson.build?.win?.executableName || productName;
const version = packageJson.build?.extraMetadata?.version || packageJson.version || "1.0.0";
const iconPath = path.resolve(
  desktopRoot,
  "..",
  "..",
  "assets",
  "Storydex_icon",
  "storydex_icon_01.ico"
);

function ensureFileExists(filePath, label) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`[Storydex Desktop] Missing ${label}: ${filePath}`);
  }
}

function patchExecutable(executablePath) {
  ensureFileExists(executablePath, "packaged executable");
  ensureFileExists(iconPath, "desktop icon");

  const executableBinary = fs.readFileSync(executablePath);
  const executable = ResEdit.NtExecutable.from(executableBinary);
  const resources = ResEdit.NtExecutableResource.from(executable);
  const iconFile = ResEdit.Data.IconFile.from(fs.readFileSync(iconPath));

  const iconGroupEntry = resources.entries.find((entry) => entry.type === 14);
  const iconGroupId = iconGroupEntry?.id || 1;
  const iconLanguage = iconGroupEntry?.lang || 1033;

  ResEdit.Resource.IconGroupEntry.replaceIconsForResource(
    resources.entries,
    iconGroupId,
    iconLanguage,
    iconFile.icons.map((item) => item.data)
  );

  const versionInfo = ResEdit.Resource.VersionInfo.fromEntries(resources.entries)[0] ||
    ResEdit.Resource.VersionInfo.createEmpty();

  versionInfo.setFileVersion(version, iconLanguage);
  versionInfo.setProductVersion(version, iconLanguage);
  versionInfo.setStringValues(
    { lang: iconLanguage, codepage: 1200 },
    {
      FileDescription: productName,
      ProductName: productName,
      InternalName: executableName,
      OriginalFilename: `${executableName}.exe`,
      FileVersion: version,
      ProductVersion: version
    },
    true
  );
  versionInfo.outputToResourceEntries(resources.entries);

  resources.outputResource(executable);

  const tempPath = `${executablePath}.tmp`;
  fs.writeFileSync(tempPath, Buffer.from(executable.generate()));
  fs.renameSync(tempPath, executablePath);

  console.log(`[Storydex Desktop] Patched executable icon/version: ${executablePath}`);
}

function main() {
  patchExecutable(path.join(desktopRoot, "release", "win-unpacked", `${executableName}.exe`));
}

if (require.main === module) {
  main();
}

module.exports = { patchExecutable };
