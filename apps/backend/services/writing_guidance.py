"""Writing defaults shared with the Rust runtime and the preset editor."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TEMPLATES = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


def effective_writing_guidance(overrides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {module.get("id"): module for module in overrides}
    defaults = [{**module, **by_id.pop(module["id"], {})} for module in _TEMPLATES]
    return defaults + list(by_id.values())


def default_writing_guidance_text() -> str:
    return "\n\n".join(module["content"] for module in _TEMPLATES)
