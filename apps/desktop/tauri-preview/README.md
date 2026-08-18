# Storydex Tauri 2 Preview

This directory is an isolated candidate shell. It is not referenced by the
Stable Electron entry points, Stable update feed, or release package.

The shell uses Tauri Core and one minimal capability. Tauri Core starts the
bundled `storydex-agentd` itself, waits for its dynamic loopback port and random
runtime token, verifies `/api/v1/sys/health`, then creates the Vue window with a
narrow `window.storydexDesktop` adapter. The renderer receives no shell or file
system permission. `runtime_info` reports lifecycle status without returning
the runtime token. Updater, signing, and preview feed wiring remain separate
gates.

## Local candidate build

Install a Tauri 2 CLI in the development environment, then run from this
directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package-preview.ps1
```

If the Tauri CLI is installed in an isolated tool directory instead of the
global Cargo bin directory, set `STORYDEX_TAURI_CLI` to the absolute
`cargo-tauri.exe` path for this command. The wrapper validates the executable
name and does not modify global Cargo configuration.

`scripts/prepare-preview.ps1` builds the release `storydex-agentd` sidecar and
the Vue dist before Tauri bundles the preview. The packaging wrapper creates a
generated config containing the sidecar only after that binary exists; this
keeps `cargo check` deterministic in a clean checkout. The sidecar is copied
with the target-triple suffix required by Tauri's `externalBin` convention.
After a successful bundle, the wrapper recreates `../candidate/staging` with
only `storydex-tauri-preview.exe` and `storydex-agentd.exe`, then runs the Rust
candidate asset policy. No Python, FastAPI/Uvicorn, Electron, Node, npm, or
package-manager assets are copied into the candidate runtime output.

At runtime, stdout/stderr are captured under the preview application's log
directory. The ready token is redacted from logs. Normal exit calls the
authenticated agentd shutdown route; a timeout terminates the Windows Job
Object so child processes cannot survive the preview shell. Startup and health
failures stop before the Vue window is created and never fall back to Python.

Run the packaged lifecycle smoke after staging:

```powershell
npm --prefix .. run smoke:tauri-preview
```

The smoke launches only the staged candidate, redirects its application data
and fixture workspace into a new operating-system temporary directory, checks
the dynamic loopback health endpoint and token-free logs, closes the real Tauri
window, and verifies that both the shell and sidecar exit cleanly. It removes
the temporary directory only after success and preserves diagnostics on
failure. It does not open or scan a real user project.

The candidate output must be checked separately:

```powershell
$env:STORYDEX_RUST_CANDIDATE_ROOT = "apps/desktop/candidate/staging"
npm --prefix .. run check:rust-candidate
```

Do not point the candidate root at `apps/desktop`, `apps/desktop/app`, or
`apps/desktop/release`; those are Stable/legacy roots and the policy fails closed
if they overlap.
