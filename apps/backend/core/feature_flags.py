"""WP-0.4 · Feature Flag 单点入口（07 §3.3）。

优先级（高 → 低）：
  1. 项目级 ``.storydex/config/feature-flags.json``（项目灰度，最高优先）
  2. 进程环境变量
  3. ``core.config.FEATURE_FLAG_DEFAULTS``

设计要点
--------

* 所有 Flag 走这一个 helper，不要在别处再 ``os.environ.get`` 散养。
* ``get_flags()`` 用 ``lru_cache(maxsize=1)`` 缓存；测试需要切换状态时调
  ``get_flags.cache_clear()``。
* 项目级 JSON 文件可能不存在或被错误编码——读失败时 fallback 到下一层，
  不抛异常。

API
---

* ``get_bool(name)``: 返回 bool；项目文件 / env var 用宽松解析（"1/true/yes/on"）。
* ``get_int(name, fallback=0)``: 返回 int；项目文件 / env var 解析失败时
  落到 ``FEATURE_FLAG_DEFAULTS[name]`` 或 fallback。
* ``snapshot()``: 返回当前所有已知 Flag 的解析结果，便于 trace / metrics。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

_PROJECT_FLAG_FILE = ".storydex/config/feature-flags.json"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


class FeatureFlags:
    """单点 Feature Flag 入口。"""

    def __init__(
        self,
        project_root: Optional[Path],
        defaults: Dict[str, Any],
    ) -> None:
        self._defaults: Dict[str, Any] = dict(defaults)
        self._project_root: Optional[Path] = project_root
        self._project: Dict[str, Any] = (
            self._load_project_flags(project_root) if project_root else {}
        )

    @staticmethod
    def _load_project_flags(project_root: Path) -> Dict[str, Any]:
        path = project_root / _PROJECT_FLAG_FILE
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def get_bool(self, name: str) -> bool:
        if name in self._project:
            value = self._project[name]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.lower().strip()
                if lowered in _TRUTHY:
                    return True
                if lowered in _FALSY:
                    return False
            return bool(value)

        env_value = os.environ.get(name)
        if env_value is not None:
            lowered = env_value.lower().strip()
            if lowered in _TRUTHY:
                return True
            if lowered in _FALSY:
                return False
            # 非合法布尔字符串视作未设置，fallback 到默认
            return bool(self._defaults.get(name, False))

        return bool(self._defaults.get(name, False))

    def get_int(self, name: str, fallback: int = 0) -> int:
        if name in self._project:
            try:
                return int(self._project[name])
            except (TypeError, ValueError):
                pass

        env_value = os.environ.get(name)
        if env_value is not None:
            try:
                return int(env_value)
            except ValueError:
                pass

        default = self._defaults.get(name, fallback)
        try:
            return int(default)
        except (TypeError, ValueError):
            return fallback

    def snapshot(self) -> Dict[str, Any]:
        """返回所有已知 Flag 的当前解析结果。

        bool 默认值走 ``get_bool``，int 默认值走 ``get_int``。
        """
        result: Dict[str, Any] = {}
        for name, default in self._defaults.items():
            if isinstance(default, bool):
                result[name] = self.get_bool(name)
            elif isinstance(default, int):
                result[name] = self.get_int(name, fallback=default)
            else:
                # 未来扩展：直接给 raw fallback
                result[name] = self._project.get(
                    name, os.environ.get(name, default)
                )
        return result

    @property
    def project_root(self) -> Optional[Path]:
        return self._project_root


@lru_cache(maxsize=1)
def get_flags() -> FeatureFlags:
    """单例入口；测试切换需调 ``get_flags.cache_clear()``。"""
    from core.config import FEATURE_FLAG_DEFAULTS, get_settings

    settings = get_settings()
    project_root = (
        Path(settings.workspace_root) if getattr(settings, "workspace_root", None) else None
    )
    return FeatureFlags(project_root, FEATURE_FLAG_DEFAULTS)


def reset_cache() -> None:
    """在测试 / 配置切换时清缓存。"""
    get_flags.cache_clear()


# 模块级编码自检（与 WP-0.2 同款）
_ENCODING_SELFTEST = "FeatureFlags 编码自检：项目级 / 环境变量 / 默认值"
assert "中" not in _ENCODING_SELFTEST or "中" in _ENCODING_SELFTEST  # 永真，仅用于触发字符
assert "�" not in _ENCODING_SELFTEST, "feature_flags.py 含 replacement char"
