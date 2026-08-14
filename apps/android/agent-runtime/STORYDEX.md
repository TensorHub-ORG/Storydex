# Storydex Android Agent Runtime

This workspace is the independent Android runtime for a local-first
role-playing text-adventure game. It preserves player agency and tracks
character voice, relationships, inventory, location, time, causality, open
hooks, and world rules. Story, Narrator, and Agent modes remain separate.

The `coomi` binary embeds the local HTTP/WebSocket service consumed by the
Android WebView. It owns Provider transport, persistent session checkpoints,
security, tool execution, MCP, memory, and the mobile story context pipeline.
When global memory is disabled, tools cannot read private runtime directories.

Tool-failure feedback is consent based: three failures in one turn show a
warning; consent makes one low-reasoning, no-tools call to the current Provider;
only the twice-redacted engineering report is uploaded. Upload retries reuse the
report and do not call the model again.

This runtime has its own release version and Cargo lock. It intentionally does
not contain the desktop `storydex-coomi-bridge` crate.

Check the workspace from the repository root with:

```powershell
cargo check --manifest-path apps/android/agent-runtime/Cargo.toml --locked --workspace
```
