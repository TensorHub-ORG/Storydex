from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict


_LENGTH_LINE_RE = re.compile(r"(?m)^[ \t]*字数[：:][^\r\n]*(?:\r?\n|$)")
_INCOMPATIBLE_CONTENT_RE = re.compile(
    r"<\s*/?\s*(?:assistant_definition|role|game_settings|user_role|instructions|thinking|"
    r"eroticism|writing_style|something_else|format|content|summary|details|background|refine)\b|"
    r"<\||\{\{\s*(?:char|user|//)|思维模式要求|小猫之神的留言",
    re.IGNORECASE,
)
_INCOMPATIBLE_TITLE_RE = re.compile(
    r"^(?:char|/char|user|别关)$|初始化|角色前|角色后|思维模式|格式要求|content.*标签|"
    r"小总结|留言|SPreset|^-{2,}|\bnsfw\b|asmr|18\+|色情|情色|涩涩|很黄|人称|视角",
    re.IGNORECASE,
)
_INCOMPATIBLE_DIRECTIVE_RE = re.compile(
    r"\bnsfw\b|18\+|色情|情色|性爱|性描写|下流词汇|肉棒|鸡巴|小穴|"
    r"(?:第一|第二|第三)人称|人称\s*[：:]|称呼用户",
    re.IGNORECASE,
)


def scene_compatible_preset_module(module: Dict[str, Any]) -> tuple[str, str] | None:
    if not isinstance(module, dict) or module.get("enabledByDefault") is False:
        return None
    title = str(module.get("title") or "创作约束").strip()
    content = str(module.get("content") or "").strip()
    if (
        not content
        or _INCOMPATIBLE_TITLE_RE.search(title)
        or _INCOMPATIBLE_CONTENT_RE.search(content)
        or _INCOMPATIBLE_DIRECTIVE_RE.search(content)
    ):
        return None
    content = _LENGTH_LINE_RE.sub("", content).strip()
    if not content:
        return None
    return title, content


def read_scene_constraint_context(
    workspace_root: Path,
    *,
    limit: int = 6000,
) -> tuple[str, list[Dict[str, Any]]]:
    root = Path(workspace_root).resolve()
    active_root = root / ".storydex" / "presets" / "active"
    chunks: list[str] = []
    audit: list[Dict[str, Any]] = []
    for sidecar in sorted(active_root.glob("*.preset.json")):
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        modules = payload.get("modules") if isinstance(payload, dict) else None
        if not isinstance(modules, list):
            continue
        for module in modules:
            compatible = scene_compatible_preset_module(module) if isinstance(module, dict) else None
            if compatible is None:
                continue
            title, content = compatible
            chunk = f"【{title}】\n{content}"
            remaining = max(0, int(limit) - sum(len(item) for item in chunks) - 2 * len(chunks))
            if remaining <= 0:
                break
            accepted = chunk[:remaining]
            chunks.append(accepted)
            audit.append(
                {
                    "presetPath": sidecar.relative_to(root).as_posix(),
                    "moduleId": str(module.get("id") or ""),
                    "moduleTitle": title,
                    "includedCharacters": len(accepted),
                    "truncated": len(accepted) < len(chunk),
                }
            )
            if len(accepted) < len(chunk):
                break
    return "\n\n".join(chunks), audit
