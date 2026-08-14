---
name: shizuku
description: Run Storydex's Android Shizuku self-check script and explain its result. Use when the user asks to check Shizuku or requests an operation that may depend on Shizuku while the current server, rish environment, or authorization state is unknown.
---

# Shizuku self-check

This Skill packages `scripts/shizuku_check.sh`. It gives Storydex a repeatable way to inspect the current Android Shizuku environment before attempting a Shizuku-dependent operation.

## Use the Skill

1. Run the bundled script in Storydex's Android shell:

   ```sh
   sh "$HOME/.coomi/skills/shizuku/scripts/shizuku_check.sh"
   ```

2. Read the `SHIZUKU_STATE=...` line, exit code, and diagnostic text on stderr.
3. The script supports `-v` for diagnostics and `--fix` for the optional Android 14+ read-only dex repair. Do not invent other options.

## Result contract

| Exit code | State | Meaning |
| ---: | --- | --- |
| 0 | `AVAILABLE` | The end-to-end Shizuku probe completed for Storydex. |
| 1 | `SERVER_NOT_RUNNING` | The Shizuku server is not running. |
| 2 | `NOT_GRANTED` | Shizuku is reachable but Storydex is not authorized. |
| 3 | `ENV_MISSING` | The rish dex or Android process environment is missing or unusable. |
| 4 | `UNKNOWN` | The probe timed out or returned an unclassified error. |

Preserve the original state, exit code, and diagnostic text when reporting the result. Shizuku is an optional capability and is not Root access.

## Input operations

When a user-requested Shizuku operation needs text input, record the current default input method before switching to ADBKeyboard. Restore and verify the original input method afterward, including after failures when restoration is possible.

## Resource

Invoke `scripts/shizuku_check.sh` with `sh` so it works even when extraction does not preserve executable bits.
