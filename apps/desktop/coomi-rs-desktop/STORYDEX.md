# Storydex Desktop Agent Runtime

This workspace is the independent Windows desktop runtime. Its product role is
a professional long-form fiction workbench: the author remains the creative
authority, project canon is distinguished from inference, and edits preserve
voice, point of view, pacing, continuity, and explicit constraints.

`storydex-coomi-bridge` is the supported integration point. It reads one
versioned JSON request from stdin and emits JSONL events on stdout. The FastAPI
backend owns HTTP/SSE and Storydex project services; this runtime owns Provider
transport, context/session checkpoints, security, tool execution, MCP, memory,
and scheduling. Storydex project tools cross the bidirectional bridge.

This runtime has its own release version and Cargo lock. Do not import Android
Web UI behavior or mobile role-playing prompts into it.

Build from the repository root with:

```powershell
cargo build --manifest-path apps/desktop/coomi-rs-desktop/Cargo.toml --release --locked -p storydex-coomi-bridge
```
