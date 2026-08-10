# Storydex v2.0.3

This release fixes a UTF-8 truncation crash in the Coomi Rust runtime and is the recommended update for all existing installations, especially when analyzing long Chinese novels.

## Fixes

- Fixed the Coomi Rust tool output truncation panic (`String::truncate` at a non-UTF-8 character boundary). Tool output is now truncated on a character boundary, so analyzing long Chinese text no longer crashes the runtime.
- Hardened the bounded-read protocol for file reading so large files are streamed within the output budget without corrupting multibyte characters.

## Windows artifacts

- `StorydexSetup-x64-2.0.3.exe`
- `StorydexSetup-x64-2.0.3.exe.blockmap`
- `Storydex-win-unpacked.zip`
- `latest.yml`, `SHA256SUMS.txt`, `BUILD_MANIFEST.json`, and `DEPENDENCIES.json`
