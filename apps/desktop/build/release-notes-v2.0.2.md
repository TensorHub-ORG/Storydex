# Storydex v2.0.2

This release carries the updater reliability fix from v2.0.1 and is the recommended update for existing installations.

## Fixes

- Fixed the Windows updater helper readiness handshake so a helper that completes before the polling interval is not reported as a failed update.
- Published the updater cache under the stable `storydex-desktop-updater` directory name.
- Added startup logging and a filesystem fallback for update lock replacement on Windows.

## Windows artifacts

- `StorydexSetup-x64-2.0.2.exe`
- `StorydexSetup-x64-2.0.2.exe.blockmap`
- `Storydex-win-unpacked.zip`
- `latest.yml`, `SHA256SUMS.txt`, `BUILD_MANIFEST.json`, and `DEPENDENCIES.json`
