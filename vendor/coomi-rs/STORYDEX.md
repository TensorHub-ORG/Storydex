# Storydex Coomi Runtime

This vendored workspace is the Storydex-specific Coomi 2.x agent base. The
`storydex-coomi-bridge` binary is the supported application integration point.
It accepts one JSON request on stdin and emits versioned JSONL events on stdout.

Storydex keeps its HTTP, SSE, project orchestration, and narrative-domain logic
in the application backend. The Rust runtime owns provider transport, context
management, sessions, security boundaries, tool-loop execution, MCP, memory,
and agent scheduling. Storydex-only tools use bidirectional JSONL callbacks so
the Rust base does not duplicate application services.

Build the release runtime with:

```powershell
cargo build --release --locked -p storydex-coomi-bridge
```
