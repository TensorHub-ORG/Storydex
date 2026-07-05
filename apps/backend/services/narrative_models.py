"""WP-5.3 · 主线 / 伏笔 / 未完结实体（07 §5.6）。

把"主线推进"、"伏笔库"、"未完结事项"建成独立实体而非散在 memory.md 里。
schema 与 P5 ContextAssembler 直接消费的 ``ContextPayload.thread_state /
foreshadowing / unresolved_items`` 对齐。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Thread(BaseModel):
    id: str
    title: str
    premise: str = ""
    current_stage: str = ""
    completed_beats: List[str] = Field(default_factory=list)
    pending_beats: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    latest_relevance: str = ""
    related_characters: List[str] = Field(default_factory=list)


class Foreshadowing(BaseModel):
    id: str
    title: str
    introduced_in: str
    related_characters: List[str] = Field(default_factory=list)
    related_threads: List[str] = Field(default_factory=list)
    current_status: str = "active"  # active / resolved / abandoned
    last_touched_in: Optional[str] = None
    resolution_condition: str = ""


class Unresolved(BaseModel):
    id: str
    title: str
    introduced_in: str
    resolution_condition: str
    priority: str = "normal"  # high / normal / low


_ENCODING_SELFTEST = "Thread / Foreshadowing / Unresolved 编码自检"
assert "�" not in _ENCODING_SELFTEST
