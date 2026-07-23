from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List

from core.config import get_settings
from services.secure_storage_service import SecureStorageError, SecureStorageService


MAX_RECENT_PROJECTS = 8
WORKBENCH_MODES = {"storydex"}
MIN_PANE_FONT_SCALE = 75
MAX_PANE_FONT_SCALE = 150


class GlobalConfigService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return Path(self.settings.global_root).expanduser().resolve()

    def ensure_structure(self) -> None:
        with self._lock:
            for directory in [
                self.root,
                self.root / "config",
                self.root / "auth",
                self.root / "ui",
                self.root / "state",
                self.root / "memories",
            ]:
                directory.mkdir(parents=True, exist_ok=True)

            global_memory = self.root / "memories" / "GLOBAL_MEMORY.md"
            if not global_memory.exists():
                global_memory.write_text(
                    "# Storydex Global Memory\n\n"
                    "- Record user-wide writing habits, workflow preferences, and reusable conventions here.\n",
                    encoding="utf-8",
                )

    def auth_token_path(self) -> Path:
        self.ensure_structure()
        return self.root / "auth" / "user-token.json"

    def auth_session_path(self) -> Path:
        self.ensure_structure()
        return self.root / "auth" / "active-session.json"

    def auth_sessions_root(self) -> Path:
        self.ensure_structure()
        return self.root / "auth" / "sessions"

    def ui_preferences_path(self) -> Path:
        self.ensure_structure()
        return self.root / "ui" / "preferences.json"

    def workspace_state_path(self) -> Path:
        self.ensure_structure()
        return self.root / "state" / "workspace.json"

    def agent_settings_path(self) -> Path:
        self.ensure_structure()
        return self.root / "config" / "agent.json"

    def global_memory_path(self) -> Path:
        self.ensure_structure()
        return self.root / "memories" / "GLOBAL_MEMORY.md"

    def read_auth_session(self) -> Dict[str, Any]:
        payload = self._read_single_auth_token()
        if payload:
            return payload

        legacy_payload = self._read_legacy_auth_session()
        if not legacy_payload:
            return self._empty_auth_session()

        try:
            migrated = self.write_auth_session(legacy_payload)
        except Exception:
            return legacy_payload

        self._cleanup_legacy_auth_session(legacy_payload)
        return migrated

    def write_auth_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            raise ValueError("userId is required")
        server_base_url = str(payload.get("serverBaseUrl") or "").strip()
        server_key = self._server_key(server_base_url)
        normalized = {
            "version": 2,
            "accessToken": str(payload.get("accessToken") or "").strip(),
            "userId": user_id,
            "username": str(payload.get("username") or "").strip(),
            "serverBaseUrl": server_base_url,
            "serverKey": server_key,
            "user": dict(payload.get("user") or {}) if isinstance(payload.get("user"), dict) else None,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }
        encrypted = self._secure_storage().encrypt_json(normalized, user_id=user_id)
        self._write_json(self.auth_token_path(), encrypted)
        return normalized

    def clear_auth_session(self, *, remove_record: bool = True) -> None:
        if remove_record:
            token_path = self.auth_token_path()
            if token_path.exists():
                token_path.unlink()

        pointer_path = self.auth_session_path()
        if pointer_path.exists():
            pointer_path.unlink()
        legacy_root = self.auth_sessions_root()
        if legacy_root.exists():
            for legacy_record in legacy_root.glob("*/*.json"):
                legacy_record.unlink()
            for server_dir in legacy_root.glob("*"):
                if server_dir.is_dir():
                    try:
                        server_dir.rmdir()
                    except OSError:
                        pass

    def find_auth_session_by_token(self, token: str) -> Dict[str, Any]:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            return self._empty_auth_session()
        current = self.read_auth_session()
        if normalized_token == str(current.get("accessToken") or "").strip():
            return current
        return self._empty_auth_session()

    def read_ui_preferences(self) -> Dict[str, Any]:
        payload = self._read_json(self.ui_preferences_path())
        return self._normalize_ui_preferences(payload if isinstance(payload, dict) else {})

    def write_ui_preferences(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_ui_preferences(payload if isinstance(payload, dict) else {})
        self._write_json(self.ui_preferences_path(), normalized)
        return normalized

    def read_workspace_state(self) -> Dict[str, Any]:
        payload = self._read_json(self.workspace_state_path())
        return self._normalize_workspace_state(payload if isinstance(payload, dict) else {})

    def write_workspace_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_workspace_state(payload if isinstance(payload, dict) else {})
        self._write_json(self.workspace_state_path(), normalized)
        return normalized

    def read_agent_settings(self) -> Dict[str, Any]:
        payload = self._read_json(self.agent_settings_path())
        return self._normalize_agent_settings(payload if isinstance(payload, dict) else {})

    def write_agent_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_agent_settings(payload if isinstance(payload, dict) else {})
        normalized["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self.agent_settings_path(), normalized)
        return normalized

    def record_recent_project(self, *, project_name: str, workspace_root: str, opened_at: str = "") -> Dict[str, Any]:
        normalized_workspace_root = str(workspace_root or "").strip()
        if not normalized_workspace_root:
            return self.read_workspace_state()

        now_iso = opened_at or datetime.now(timezone.utc).isoformat()
        current_state = self.read_workspace_state()
        recent_projects: List[Dict[str, Any]] = [
            {
                "projectName": str(project_name or Path(normalized_workspace_root).name or normalized_workspace_root),
                "workspaceRoot": normalized_workspace_root,
                "openedAt": now_iso,
            }
        ]

        for item in current_state.get("recentProjects", []) if isinstance(current_state.get("recentProjects"), list) else []:
            if not isinstance(item, dict):
                continue
            existing_root = str(item.get("workspaceRoot") or "").strip()
            if not existing_root or existing_root == normalized_workspace_root:
                continue
            recent_projects.append(
                {
                    "projectName": str(item.get("projectName") or Path(existing_root).name or existing_root),
                    "workspaceRoot": existing_root,
                    "openedAt": str(item.get("openedAt") or now_iso),
                }
            )
            if len(recent_projects) >= MAX_RECENT_PROJECTS:
                break

        return self.write_workspace_state(
            {
                "lastProjectPath": normalized_workspace_root,
                "recentProjects": recent_projects[:MAX_RECENT_PROJECTS],
                "updatedAt": now_iso,
            }
        )

    @staticmethod
    def _normalize_ui_preferences(payload: Dict[str, Any]) -> Dict[str, Any]:
        file_font_size = GlobalConfigService._clamp_int(payload.get("fileFontSize"), 16, minimum=12, maximum=24)
        legacy_center_scale = ((file_font_size * 5 + 2) // 4) * 5
        return {
            "theme": str(payload.get("theme") or "default").strip() or "default",
            "activeActivity": str(payload.get("activeActivity") or "resources").strip() or "resources",
            "workbenchMode": GlobalConfigService._normalize_choice(
                payload.get("workbenchMode"),
                fallback="storydex",
                allowed=WORKBENCH_MODES,
            ),
            "sidebarWidth": GlobalConfigService._clamp_int(payload.get("sidebarWidth"), 320, minimum=220, maximum=520),
            "sidebarCollapsed": bool(payload.get("sidebarCollapsed", False)),
            "agentCollapsed": bool(payload.get("agentCollapsed", False)),
            "agentWidth": GlobalConfigService._clamp_int(payload.get("agentWidth"), 560, minimum=320, maximum=760),
            "leftPaneFontScale": GlobalConfigService._clamp_int(
                payload.get("leftPaneFontScale"),
                100,
                minimum=MIN_PANE_FONT_SCALE,
                maximum=MAX_PANE_FONT_SCALE,
            ),
            "centerPaneFontScale": GlobalConfigService._clamp_int(
                payload.get("centerPaneFontScale"),
                legacy_center_scale,
                minimum=MIN_PANE_FONT_SCALE,
                maximum=MAX_PANE_FONT_SCALE,
            ),
            "rightPaneFontScale": GlobalConfigService._clamp_int(
                payload.get("rightPaneFontScale"),
                100,
                minimum=MIN_PANE_FONT_SCALE,
                maximum=MAX_PANE_FONT_SCALE,
            ),
            "fileFontSize": file_font_size,
            "playerFontSize": GlobalConfigService._clamp_int(
                payload.get("playerFontSize"),
                14,
                minimum=12,
                maximum=28,
            ),
            "updatedAt": str(payload.get("updatedAt") or datetime.now(timezone.utc).isoformat()),
        }

    @staticmethod
    def _normalize_workspace_state(payload: Dict[str, Any]) -> Dict[str, Any]:
        recent_projects: List[Dict[str, Any]] = []
        for item in payload.get("recentProjects", []) if isinstance(payload.get("recentProjects"), list) else []:
            if not isinstance(item, dict):
                continue
            workspace_root = str(item.get("workspaceRoot") or "").strip()
            if not workspace_root:
                continue
            recent_projects.append(
                {
                    "projectName": str(item.get("projectName") or Path(workspace_root).name or workspace_root),
                    "workspaceRoot": workspace_root,
                    "openedAt": str(item.get("openedAt") or datetime.now(timezone.utc).isoformat()),
                }
            )
            if len(recent_projects) >= MAX_RECENT_PROJECTS:
                break

        last_project_path = str(payload.get("lastProjectPath") or "").strip()
        if not last_project_path and recent_projects:
            last_project_path = str(recent_projects[0].get("workspaceRoot") or "").strip()

        return {
            "lastProjectPath": last_project_path,
            "recentProjects": recent_projects,
            "updatedAt": str(payload.get("updatedAt") or datetime.now(timezone.utc).isoformat()),
        }

    @staticmethod
    def _normalize_agent_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            # T2 only introduces switches; changing either default is a separate
            # ADR-gated product decision.
            "coomiMemoryEnabled": GlobalConfigService._strict_bool(
                payload.get("coomiMemoryEnabled"),
                True,
            ),
            "wikiContextEnabled": GlobalConfigService._strict_bool(
                payload.get("wikiContextEnabled"),
                True,
            ),
            "updatedAt": str(payload.get("updatedAt") or ""),
        }

    @staticmethod
    def _clamp_int(value: Any, fallback: int, *, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _normalize_choice(value: Any, *, fallback: str, allowed: set[str]) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in allowed else fallback

    @staticmethod
    def _strict_bool(value: Any, fallback: bool) -> bool:
        return value if isinstance(value, bool) else fallback

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _secure_storage(self) -> SecureStorageService:
        return SecureStorageService(root=self.root)

    def _read_single_auth_token(self) -> Dict[str, Any]:
        encrypted_payload = self._read_json(self.auth_token_path())
        if not isinstance(encrypted_payload, dict):
            return {}
        user_id = str(encrypted_payload.get("userId") or "").strip()
        if not user_id:
            return {}
        try:
            payload = self._secure_storage().decrypt_json(encrypted_payload, user_id=user_id)
        except SecureStorageError:
            return {}
        return self._normalize_auth_payload(payload, fallback_server_key=self._server_key(payload.get("serverBaseUrl")))

    def _read_legacy_auth_session(self) -> Dict[str, Any]:
        active_pointer = self._read_json(self.auth_session_path())
        if not isinstance(active_pointer, dict):
            return {}

        user_id = str(active_pointer.get("userId") or "").strip()
        server_key = str(active_pointer.get("serverKey") or "").strip()
        if not user_id or not server_key:
            return {}

        session_path = self.auth_sessions_root() / server_key / f"{user_id}.json"
        encrypted_payload = self._read_json(session_path)
        if not isinstance(encrypted_payload, dict):
            return {}

        try:
            payload = self._secure_storage().decrypt_json(encrypted_payload, user_id=user_id)
        except SecureStorageError:
            return {}
        return self._normalize_auth_payload(payload, fallback_server_key=server_key)

    def _cleanup_legacy_auth_session(self, payload: Dict[str, Any]) -> None:
        user_id = str(payload.get("userId") or "").strip()
        server_key = str(payload.get("serverKey") or "").strip()

        pointer_path = self.auth_session_path()
        if pointer_path.exists():
            pointer_path.unlink()

        if user_id and server_key:
            legacy_record = self.auth_sessions_root() / server_key / f"{user_id}.json"
            if legacy_record.exists():
                legacy_record.unlink()

    @staticmethod
    def _normalize_auth_payload(payload: Dict[str, Any], *, fallback_server_key: str = "") -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        server_base_url = str(payload.get("serverBaseUrl") or "").strip()
        return {
            "accessToken": str(payload.get("accessToken") or "").strip(),
            "userId": str(payload.get("userId") or "").strip(),
            "username": str(payload.get("username") or "").strip(),
            "savedAt": str(payload.get("savedAt") or payload.get("updatedAt") or "").strip(),
            "serverBaseUrl": server_base_url,
            "serverKey": str(payload.get("serverKey") or fallback_server_key or GlobalConfigService._server_key(server_base_url)).strip(),
            "user": dict(payload.get("user") or {}) if isinstance(payload.get("user"), dict) else None,
        }

    @staticmethod
    def _empty_auth_session() -> Dict[str, Any]:
        return {
            "accessToken": "",
            "userId": "",
            "username": "",
            "savedAt": "",
            "serverBaseUrl": "",
            "serverKey": "",
            "user": None,
        }

    @staticmethod
    def _server_key(server_base_url: str) -> str:
        normalized = str(server_base_url or "").strip().rstrip("/").lower() or "default"
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"{digest}"


@lru_cache(maxsize=1)
def get_global_config_service() -> GlobalConfigService:
    return GlobalConfigService()
