import type { AgentExecutionRun } from "@/types/agent";
import type { ToolFailureTracePayload } from "@/types/system";

const SECRET_KEY = /key|token|secret|password|authorization|credential/i;

function safeIdentifier(value: unknown, fallback: string): string {
  return String(value || "").replace(/[^0-9A-Za-z_.:-]/g, "").slice(0, 80) || fallback;
}

function sanitizeDiagnosticText(value: unknown): string {
  return String(value || "")
    .slice(0, 1200)
    .replace(/\b(?:sk-|Bearer\s+)[0-9A-Za-z._-]{8,}\b/gi, "[redacted_secret]")
    .replace(/https?:\/\/[^\s"']+/gi, "[redacted_url]")
    .replace(/\b[A-Za-z]:\\[^\s"']+/g, "[redacted_path]")
    .replace(/\/(?:home|Users|data|storage|sdcard|tmp|var|mnt)\/[^\s"']+/gi, "[redacted_path]")
    .replace(/[0-9A-Za-z._%+-]+@[0-9A-Za-z.-]+\.[A-Za-z]{2,}/g, "[redacted_email]")
    .replace(/\b[0-9a-f]{24,}\b/gi, "[redacted_identifier]")
    .slice(0, 600);
}

function sanitizeArgumentShape(value: unknown, key = "", depth = 0): unknown {
  if (depth > 4) return "[max_depth]";
  if (SECRET_KEY.test(key)) return "[redacted_secret]";
  if (Array.isArray(value)) return value.slice(0, 12).map((item) => sanitizeArgumentShape(item, key, depth + 1));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .slice(0, 30)
        .map(([childKey, child]) => [
          safeIdentifier(childKey, "field"),
          sanitizeArgumentShape(child, childKey, depth + 1)
        ])
    );
  }
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return "[number]";
  if (value === null || value === undefined) return "[null]";
  const text = String(value).trim();
  const lowerKey = key.toLowerCase();
  if (/path|file|dir|cwd|destination|source/.test(lowerKey) || /^(?:\/|[A-Za-z]:\\)/.test(text)) {
    return "[path]";
  }
  if (/command|cmd|script/.test(lowerKey) || /[\s;&|><]/.test(text)) {
    return {
      kind: "command_shape",
      token_count: text.split(/\s+/).filter(Boolean).length,
      has_shell_operators: /[;&|><]/.test(text)
    };
  }
  if (/^https?:\/\//i.test(text)) return "[url_redacted]";
  if (/^[0-9A-Za-z_.:-]{1,32}$/.test(text)) return text;
  return `[string length=${text.length}]`;
}

function classifyToolError(value: unknown): string {
  const text = String(value || "").toLowerCase();
  if (/permission|denied|allowed area|sandbox/.test(text)) return "permission_or_sandbox";
  if (/timeout|timed out/.test(text)) return "timeout";
  if (/not found|enoent/.test(text)) return "not_found";
  if (/invalid|schema|argument|parse/.test(text)) return "invalid_arguments";
  if (/network|connect|dns|http/.test(text)) return "network_or_upstream";
  return "execution_error";
}

export function buildToolFailureTrace(run: AgentExecutionRun): ToolFailureTracePayload[] {
  return run.items
    .filter((item) => item.type === "tool")
    .slice(0, 40)
    .map((item, index) => {
      const status = item.status === "error" ? "error" : item.status === "success" ? "success" : "unknown";
      const trace: ToolFailureTracePayload = {
        sequence: index + 1,
        tool: safeIdentifier(item.toolName, "unknown_tool"),
        status,
        argumentShape: sanitizeArgumentShape(item.arguments || {})
      };
      const elapsedMs = Number(item.raw?.duration_ms ?? item.raw?.elapsedMs);
      if (Number.isFinite(elapsedMs) && elapsedMs >= 0) trace.elapsedMs = Math.min(Math.round(elapsedMs), 3_600_000);
      if (status === "error") {
        trace.category = classifyToolError(item.resultPreview || item.content);
        trace.errorSummary = sanitizeDiagnosticText(item.resultPreview || item.content);
      }
      return trace;
    });
}
