"use strict";

const fs = require("node:fs");
const path = require("node:path");

const desktopRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(desktopRoot, "..", "..");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
const version = String(packageJson.version || "").trim();
const targetRoot = path.join(desktopRoot, "tauri-preview", "target", "release");
const bundleRoot = path.join(targetRoot, "bundle");
const candidateRoot = path.join(desktopRoot, "candidate", "staging");
const releaseRoot = path.join(desktopRoot, "release");
const publicBaseUrl = "https://updates.septemc.com/storydex/windows";

function requireFile(filePath, label) {
  if (!fs.statSync(filePath, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`${label} was not produced: ${filePath}`);
  }
  return filePath;
}

function findSingle(directory, predicate, label) {
  const matches = [];
  const visit = (current) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const absolute = path.join(current, entry.name);
      if (entry.isDirectory()) visit(absolute);
      else if (entry.isFile() && predicate(absolute)) matches.push(absolute);
    }
  };
  visit(directory);
  if (matches.length !== 1) {
    throw new Error(`${label} expected exactly one file under ${directory}, found ${matches.length}`);
  }
  return matches[0];
}

function resetDirectory(directory) {
  const resolved = path.resolve(directory);
  const allowedRoots = [
    path.resolve(desktopRoot, "candidate"),
    path.resolve(desktopRoot, "release")
  ];
  if (!allowedRoots.some((root) => resolved === root || resolved.startsWith(`${root}${path.sep}`))) {
    throw new Error(`refusing to recreate output outside desktop candidate/release roots: ${resolved}`);
  }
  fs.rmSync(resolved, { recursive: true, force: true });
  fs.mkdirSync(resolved, { recursive: true });
}

function copyDirectory(source, destination) {
  if (!fs.statSync(source, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error(`required runtime directory is missing: ${source}`);
  }
  fs.cpSync(source, destination, { recursive: true, force: true });
}

function releaseNotes() {
  const notesPath = path.join(desktopRoot, "build", `release-notes-v${version}.md`);
  return fs.existsSync(notesPath) ? fs.readFileSync(notesPath, "utf8").trim() : `Storydex ${version}`;
}

requireFile(path.join(targetRoot, "storydex-tauri.exe"), "Tauri executable");
requireFile(path.join(targetRoot, "storydex-agentd.exe"), "Rust sidecar");
requireFile(path.join(targetRoot, "storydex-coomi-bridge.exe"), "Coomi bridge");
const mingitRoot = path.join(desktopRoot, "tauri-preview", "resources", "mingit");
const installerName = `Storydex_${version}_x64-setup.exe`;
const installer = findSingle(
  bundleRoot,
  (file) => path.basename(file) === installerName && file.includes(`${path.sep}nsis${path.sep}`),
  `NSIS installer ${installerName}`
);
const updaterSignature = requireFile(`${installer}.sig`, "Tauri updater signature");
const signature = fs.readFileSync(updaterSignature, "utf8").trim();
if (!signature || !/^[A-Za-z0-9+/=]+$/.test(signature)) {
  throw new Error("Tauri updater signature is empty or not base64 encoded");
}

resetDirectory(candidateRoot);
fs.copyFileSync(path.join(targetRoot, "storydex-tauri.exe"), path.join(candidateRoot, "Storydex.exe"));
fs.copyFileSync(path.join(targetRoot, "storydex-agentd.exe"), path.join(candidateRoot, "storydex-agentd.exe"));
fs.copyFileSync(
  path.join(targetRoot, "storydex-coomi-bridge.exe"),
  path.join(candidateRoot, "storydex-coomi-bridge.exe")
);
copyDirectory(mingitRoot, path.join(candidateRoot, "mingit"));

resetDirectory(releaseRoot);
const setupName = `StorydexSetup-x64-${version}.exe`;
const updaterName = setupName;
fs.copyFileSync(installer, path.join(releaseRoot, setupName));
fs.copyFileSync(updaterSignature, path.join(releaseRoot, `${updaterName}.sig`));

const latest = {
  version,
  notes: releaseNotes(),
  pub_date: new Date().toISOString(),
  platforms: {
    "windows-x86_64": {
      signature,
      url: `${publicBaseUrl}/${setupName}`
    }
  }
};
fs.writeFileSync(path.join(releaseRoot, "latest.json"), `${JSON.stringify(latest, null, 2)}\n`, "utf8");
fs.writeFileSync(path.join(releaseRoot, "TAURI_RUNTIME_ROOT.txt"), `${path.relative(projectRoot, candidateRoot).replace(/\\/g, "/")}\n`, "ascii");

console.log(JSON.stringify({ version, setupName, updaterName, candidateRoot, releaseRoot }));
