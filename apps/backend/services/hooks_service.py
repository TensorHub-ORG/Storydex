from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from services.project_service import get_project_service


class HooksService:
    """Small local hook runner compatible with Storydex's preview/commit flow.

    Hooks are optional and configured in `.storydex/hooks.json`:
    {
      "preWorkspaceWrite": [{"name": "format-check", "command": "python -m compileall apps/backend"}],
      "postWorkspaceWrite": [{"name": "notify", "command": "echo done"}]
    }
    """

    def __init__(self) -> None:
        self.project_service = get_project_service()

    def run(self, event: str, payload: Dict[str, Any], *, timeout_seconds: int = 20) -> List[Dict[str, Any]]:
        hooks = self._load_hooks().get(event)
        if not isinstance(hooks, list) or not hooks:
            return []

        # WP-3.3 · ASYNC_HOOKS_ENABLED：Popen 不 wait，主路径不阻塞 timeout_seconds。
        # 单个 hook 配 "sync": true 仍走同步分支。
        from core.feature_flags import get_flags
        async_default = get_flags().get_bool("ASYNC_HOOKS_ENABLED")

        results: List[Dict[str, Any]] = []
        for index, hook in enumerate(hooks, start=1):
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command") or "").strip()
            if not command:
                continue
            name = str(hook.get("name") or f"{event}:{index}")
            started_at = datetime.now(timezone.utc).isoformat()
            sync_required = bool(hook.get("sync", False))
            if async_default and not sync_required:
                # fire-and-forget：不 wait
                try:
                    subprocess.Popen(
                        command,
                        cwd=str(self.project_service.workspace_root),
                        shell=True,
                        text=True,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    result = {
                        "name": name,
                        "event": event,
                        "status": "fire_and_forget",
                        "exitCode": None,
                        "startedAt": started_at,
                        "stdout": "",
                        "stderr": "",
                    }
                except Exception as exc:
                    result = {
                        "name": name,
                        "event": event,
                        "status": "spawn_error",
                        "exitCode": None,
                        "startedAt": started_at,
                        "stdout": "",
                        "stderr": f"{exc.__class__.__name__}: {exc}",
                    }
                results.append(result)
                continue
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.project_service.workspace_root),
                    shell=True,
                    text=True,
                    input=json.dumps(payload, ensure_ascii=False),
                    capture_output=True,
                    timeout=timeout_seconds,
                )
                result = {
                    "name": name,
                    "event": event,
                    "status": "ok" if completed.returncode == 0 else "error",
                    "exitCode": completed.returncode,
                    "startedAt": started_at,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                }
            except subprocess.TimeoutExpired as exc:
                result = {
                    "name": name,
                    "event": event,
                    "status": "timeout",
                    "exitCode": None,
                    "startedAt": started_at,
                    "stdout": str(exc.stdout or "")[-4000:],
                    "stderr": str(exc.stderr or "")[-4000:],
                }
            results.append(result)
        self._append_hook_log(results)
        return results

    def _load_hooks(self) -> Dict[str, Any]:
        path = self.project_service.storydex_root / "hooks.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _append_hook_log(self, results: List[Dict[str, Any]]) -> None:
        if not results:
            return
        log_dir = self.project_service.storydex_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "hooks.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            for result in results:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")


_hooks_service = HooksService()


def get_hooks_service() -> HooksService:
    return _hooks_service
