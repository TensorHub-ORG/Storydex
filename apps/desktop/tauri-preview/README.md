# Storydex Tauri 2 Preview

This directory is an isolated candidate shell. It is not referenced by the
Stable Electron entry points, Stable update feed, or release package.

The shell deliberately starts with Tauri Core and one minimal capability. The
only application command currently exposed is `runtime_info`; it reports the
candidate backend URL and sidecar name without granting arbitrary shell or file
system access. Updater, signing, and preview feed wiring remain separate gates.

## Local candidate build

Install a Tauri 2 CLI in the development environment, then run from this
directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package-preview.ps1
```

`scripts/prepare-preview.ps1` builds the release `storydex-agentd` sidecar and
the Vue dist before Tauri bundles the preview. The packaging wrapper creates a
generated config containing the sidecar only after that binary exists; this
keeps `cargo check` deterministic in a clean checkout. The sidecar is copied
with the target-triple suffix required by Tauri's `externalBin` convention. No Python,
FastAPI/Uvicorn, Electron, Node, npm, or package-manager assets are copied into
the candidate runtime output.

The candidate output must be checked separately:

```powershell
$env:STORYDEX_RUST_CANDIDATE_ROOT = "apps/desktop/candidate/staging"
npm --prefix .. run check:rust-candidate
```

Do not point the candidate root at `apps/desktop`, `apps/desktop/app`, or
`apps/desktop/release`; those are Stable/legacy roots and the policy fails closed
if they overlap.
