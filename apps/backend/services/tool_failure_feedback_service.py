from __future__ import annotations

import asyncio
import json
import logging
import re
from time import perf_counter
from typing import Any

from services.coomi_bridge_client import get_bridge_provider


logger = logging.getLogger(__name__)

TOOL_FAILURE_REDACTION_VERSION = "storydex-tool-trace-v1"
MAX_TOOL_TRACE_ITEMS = 40
MAX_SANITIZED_TRACE_BYTES = 28 * 1024
MIN_TOOL_FAILURES = 3

_ANALYSIS_PROMPT = """你是 Storydex 的本地故障分析器。输入仅包含程序脱敏后的本轮工具调用轨迹。
请生成简洁、可执行的工程报告，必须包含：
1. 失败与恢复链；2. 已确认的程序证据；3. 明确标注的推断；
4. 修复建议；5. 测试与验收条件；6. 缺失证据。
不得推测或复原用户对话、小说正文、文件内容、真实路径、URL、密钥或模型隐藏思维。
不得给出与轨迹无关的产品建议。使用中文纯文本，控制在 3000 字以内。"""

_SECRET_KEY = re.compile(r"key|token|secret|password|authorization|credential", re.I)
_SAFE_IDENTIFIER = re.compile(r"[^0-9A-Za-z_.:-]+")
_SECRET_VALUE = re.compile(r"\b(?:sk-|Bearer\s+)[0-9A-Za-z._-]{8,}\b", re.I)
_LABELED_SECRET = re.compile(
    r"\b(api[_-]?key|authorization|access[_-]?token|token|secret|password|credential)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.I,
)
_URL = re.compile(r"https?://[^\s\"']+", re.I)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"']+")
_UNIX_PATH = re.compile(r"/(?:home|Users|data|storage|sdcard|tmp|var|mnt)/[^\s\"']+", re.I)
_EMAIL = re.compile(r"[0-9A-Za-z._%+-]+@[0-9A-Za-z.-]+\.[A-Za-z]{2,}")
_LONG_IDENTIFIER = re.compile(r"\b[0-9a-f]{24,}\b", re.I)


def _identifier(value: Any, limit: int = 80) -> str:
    cleaned = _SAFE_IDENTIFIER.sub("", str(value or ""))[:limit]
    return cleaned or "unknown"


def sanitize_diagnostic_text(value: Any, limit: int = 600) -> str:
    text = str(value or "")[: max(limit * 2, limit)]
    text = _SECRET_VALUE.sub("[redacted_secret]", text)
    text = _LABELED_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted_secret]",
        text,
    )
    text = _URL.sub("[redacted_url]", text)
    text = _WINDOWS_PATH.sub("[redacted_path]", text)
    text = _UNIX_PATH.sub("[redacted_path]", text)
    text = _EMAIL.sub("[redacted_email]", text)
    text = _LONG_IDENTIFIER.sub("[redacted_identifier]", text)
    return text[:limit]


def _sanitize_shape(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 5:
        return "[max_depth]"
    if _SECRET_KEY.search(key):
        return "[redacted_secret]"
    if isinstance(value, dict):
        return {
            _identifier(child_key): _sanitize_shape(child, str(child_key), depth + 1)
            for child_key, child in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_sanitize_shape(item, key, depth + 1) for item in value[:12]]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return "[number]"
    if value is None:
        return "[null]"
    text = str(value).strip()
    if re.fullmatch(r"[0-9A-Za-z_.:-]{1,32}", text):
        return text
    return f"[string length={len(text)}]"


def sanitize_tool_trace(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    sanitized: list[dict[str, Any]] = []
    for index, item in enumerate(items[:MAX_TOOL_TRACE_ITEMS], start=1):
        status = str(item.get("status") or "unknown").lower()
        status = status if status in {"success", "error", "unknown"} else "unknown"
        elapsed = item.get("elapsedMs")
        try:
            elapsed_ms = min(max(int(elapsed), 0), 3_600_000) if elapsed is not None else None
        except (TypeError, ValueError):
            elapsed_ms = None
        record = {
            "sequence": min(max(int(item.get("sequence") or index), 1), 10_000),
            "tool": _identifier(item.get("tool")),
            "status": status,
            "argumentShape": _sanitize_shape(item.get("argumentShape") or {}),
        }
        if elapsed_ms is not None:
            record["elapsedMs"] = elapsed_ms
        if item.get("category"):
            record["category"] = _identifier(item.get("category"))
        if status == "error" and item.get("errorSummary"):
            record["errorSummary"] = sanitize_diagnostic_text(item.get("errorSummary"))
        sanitized.append(record)

    failure_count = sum(1 for item in sanitized if item["status"] == "error")
    if failure_count < MIN_TOOL_FAILURES:
        raise ValueError(f"tool trace requires at least {MIN_TOOL_FAILURES} failures")
    serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_SANITIZED_TRACE_BYTES:
        raise ValueError("sanitized tool trace is too large")
    return sanitized, failure_count


def build_program_evidence(items: list[dict[str, Any]]) -> str:
    lines = [
        "【程序采集的脱敏证据】",
        "不含用户消息、小说正文、原始参数值、文件内容、真实路径、URL、密钥或模型隐藏思维。",
    ]
    for item in items:
        line = f"#{item['sequence']} {item['tool']} | {item['status']}"
        if item.get("category"):
            line += f" | {item['category']}"
        if item.get("elapsedMs") is not None:
            line += f" | {item['elapsedMs']}ms"
        lines.append(line)
        lines.append(f"参数结构: {json.dumps(item['argumentShape'], ensure_ascii=False)}")
        if item.get("errorSummary"):
            lines.append(f"错误摘要: {item['errorSummary']}")
    return sanitize_diagnostic_text("\n".join(lines), 3500)


def sanitize_generated_analysis(value: Any) -> str:
    return sanitize_diagnostic_text(value, 4000).strip()


async def analyze_tool_failures(
    *, provider_id: str | None, trace: list[dict[str, Any]], request_id: str
) -> dict[str, Any]:
    started = perf_counter()
    sanitized, failure_count = sanitize_tool_trace(trace)
    provider = get_bridge_provider(provider_id or None, fast=True, reasoning_effort="low")
    try:
        response = await asyncio.wait_for(
            provider.chat(
                [
                    {"role": "system", "content": _ANALYSIS_PROMPT},
                    {
                        "role": "user",
                        "content": "请分析以下本轮脱敏工具轨迹：\n\n"
                        + json.dumps(sanitized, ensure_ascii=False, indent=2),
                    },
                ],
                tools=[],
                max_output_tokens=4000,
                reasoning_effort="low",
            ),
            timeout=180,
        )
    except asyncio.TimeoutError:
        logger.warning("tool_feedback_analysis request_id=%s category=timeout", request_id)
        raise
    except Exception:
        logger.exception("tool_feedback_analysis request_id=%s category=provider_error", request_id)
        raise
    analysis = sanitize_generated_analysis(getattr(response, "content", ""))
    if not analysis:
        raise ValueError("tool failure analysis returned an empty report")
    elapsed_ms = int((perf_counter() - started) * 1000)
    logger.info(
        "tool_feedback_analysis request_id=%s category=success elapsed_ms=%s failures=%s",
        request_id,
        elapsed_ms,
        failure_count,
    )
    return {
        "analysis": analysis,
        "programEvidence": build_program_evidence(sanitized),
        "failureCount": failure_count,
        "requestId": request_id,
        "elapsedMs": elapsed_ms,
        "responseCategory": "success",
        "redactionVersion": TOOL_FAILURE_REDACTION_VERSION,
    }
