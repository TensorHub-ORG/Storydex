from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


COMPACT_SYSTEM_PROMPT = """你是一个创作项目记忆压缩助手。请将以下工作区上下文压缩为结构化摘要。
要求：
1. 保留所有关键实体名称、身份、当前状态。
2. 保留所有未解决的问题、冲突、悬念和伏笔。
3. 保留时间线关键节点。
4. 保留用户最近指令的完整含义。
5. 保留当前正在创作的位置，包括章节、段落、场景等。
6. 删除已解决的问题、已回收的悬念、重复设定和背景铺陈。
7. 不要编造原文中不存在的信息。

输出格式：
## 当前位置
- 位置: 当前创作位置

## 活跃实体
- 实体A: 当前状态 / 最近行为 / 变化

## 未解决问题与冲突
- 问题1: ...

## 活跃悬念与伏笔
- 悬念1: 来源位置 / 当前状态 / 待回收方式

## 时间线
- 节点1: ...

## 用户指令
1. ...

## 核心设定要点
- 关键设定: ...
- 重要规则: ..."""


@dataclass
class CompactResult:
    summary: str
    original_tokens: int
    compacted_tokens: int
    tokens_saved: int
    success: bool
    error: str = ""


class CompactService:
    def __init__(
        self,
        *,
        llm_chat_fn: Optional[Callable] = None,
        token_accounting: Optional[Any] = None,
        max_compact_tokens: int = 4000,
        compact_target_ratio: float = 0.25,
    ) -> None:
        self._llm_chat_fn = llm_chat_fn
        self._token_accounting = token_accounting
        self.max_compact_tokens = max_compact_tokens
        self.compact_target_ratio = compact_target_ratio

    @staticmethod
    def _tscale() -> float:
        return 1.0

    def auto_compact(
        self,
        bundle_text: str,
        *,
        estimated_tokens: int = 0,
        effective_window: int = 59536,
    ) -> CompactResult:
        if not bundle_text or not self._llm_chat_fn:
            return CompactResult(
                summary=bundle_text,
                original_tokens=estimated_tokens,
                compacted_tokens=estimated_tokens,
                tokens_saved=0,
                success=False,
                error="No bundle text or LLM callable",
            )

        try:
            messages = [
                {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": f"请压缩以下工作区上下文：\n\n{bundle_text}"},
            ]
            result = self._llm_chat_fn(
                messages=messages,
                purpose="auto_compact",
                max_tokens=self.max_compact_tokens,
                temperature=0.3,
            )
            summary = ""
            if hasattr(result, "content"):
                summary = str(result.content or "").strip()
            elif isinstance(result, dict):
                summary = str(result.get("content") or result.get("text") or "").strip()
            elif isinstance(result, str):
                summary = result.strip()

            if not summary:
                return CompactResult(
                    summary=bundle_text,
                    original_tokens=estimated_tokens,
                    compacted_tokens=estimated_tokens,
                    tokens_saved=0,
                    success=False,
                    error="LLM returned empty summary",
                )

            compacted_tokens = (
                int(self._token_accounting.estimate_text_tokens(summary))
                if self._token_accounting and hasattr(self._token_accounting, "estimate_text_tokens")
                else _estimate_text_tokens(summary)
            )
            tokens_saved = max(0, estimated_tokens - compacted_tokens)

            return CompactResult(
                summary=summary,
                original_tokens=estimated_tokens,
                compacted_tokens=compacted_tokens,
                tokens_saved=tokens_saved,
                success=True,
            )
        except Exception as exc:
            return CompactResult(
                summary=bundle_text,
                original_tokens=estimated_tokens,
                compacted_tokens=estimated_tokens,
                tokens_saved=0,
                success=False,
                error=str(exc),
            )
    def should_compact(self, compact_status: Dict[str, Any]) -> bool:
        return compact_status.get("status") == "compact_needed"

    @staticmethod
    def build_restored_bundle(
        *,
        compact_summary: str,
        current_content: str = "",
        entity_cards: Optional[List[str]] = None,
        setting_entries: Optional[List[str]] = None,
    ) -> str:
        tscale = CompactService._tscale()
        sections: List[str] = []
        if compact_summary:
            sections.append("[compact_summary]\n" + compact_summary)
        if current_content:
            sections.append("[current_content]\n" + current_content[: int(8000 * tscale)])
        for i, card in enumerate(entity_cards or []):
            sections.append(f"[entity_card_{i + 1}]\n" + card[: int(3000 * tscale)])
        for i, entry in enumerate(setting_entries or []):
            sections.append(f"[setting_{i + 1}]\n" + entry[: int(3000 * tscale)])
        return "\n\n".join(sections) if sections else compact_summary


def _estimate_text_tokens(text: str) -> int:
    return max(1, int(len(str(text or "")) / 3))
