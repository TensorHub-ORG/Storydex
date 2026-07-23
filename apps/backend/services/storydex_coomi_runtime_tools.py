"""Workspace-bound overrides for Coomi's default tools.

Coomi 0.1.x resolves relative paths and shell working directories from the
backend process CWD (``os.getcwd()``), which historically forced Storydex to
``os.chdir`` into the novel project for the whole agent turn. That chdir is
process-global and races with other requests. These subclasses pin every
filesystem-facing tool to the active Storydex workspace explicitly so the
chdir is no longer needed:

- Bash / PowerShell run with ``subprocess.run(..., cwd=workspace_root)``.
- Read / Write / Edit / Glob / Grep normalize relative path arguments against
  the workspace root before delegating to the original implementation.

Registered names match Coomi's defaults, and ``ToolRegistry.register``
replaces same-name tools, so ``create_workspace_bound_tool_overrides`` simply
re-registers on top of ``create_default_registry()``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from coomi.tools.base import ToolResult
from coomi.tools.file_ops import EditTool, ReadTool, WriteTool
from coomi.tools.search import GlobTool, GrepTool
from coomi.tools.shell import BashTool, PowerShellTool
from coomi.tools.shell.bash import EXIT_CODE_HINTS, _truncate_output
from coomi.tools.web import WebFetchTool, WebSearchTool


class _WorkspaceBoundMixin:
    def __init__(self, *, workspace_root: Path, turn_contract: Dict[str, Any] | None = None) -> None:
        super().__init__()
        self.workspace_root = Path(workspace_root).resolve()
        self.turn_contract = dict(turn_contract) if isinstance(turn_contract, dict) else {}

    def set_workspace_root(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def _strict_story_generation_turn(self) -> bool:
        intent = self.turn_contract.get("intentFrame") if isinstance(self.turn_contract.get("intentFrame"), dict) else {}
        return str(intent.get("primary") or "") == "story_generation"

    def _targets_chapter_file(self, arguments: Dict[str, Any]) -> bool:
        for key in _PATH_ARGUMENT_KEYS:
            raw = str((arguments or {}).get(key) or "").strip()
            if not raw:
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.workspace_root / candidate
            try:
                relative = candidate.resolve().relative_to(self.workspace_root).as_posix()
            except ValueError:
                continue
            if relative.startswith("chapters/"):
                return True
        return False


_PATH_ARGUMENT_KEYS = ("file_path", "path", "directory")


class _WorkspacePathNormalizerMixin(_WorkspaceBoundMixin):
    """Rewrite relative path arguments to workspace-absolute before running."""

    def _normalized_arguments(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(arguments or {})
        for key in _PATH_ARGUMENT_KEYS:
            value = normalized.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = Path(value.strip())
            if candidate.is_absolute():
                continue
            normalized[key] = (self.workspace_root / candidate).as_posix()
        return normalized

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        return super().run(self._normalized_arguments(arguments))


class StorydexReadTool(_WorkspacePathNormalizerMixin, ReadTool):
    pass


class StorydexWriteTool(_WorkspacePathNormalizerMixin, WriteTool):
    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        if self._strict_story_generation_turn() and self._targets_chapter_file(arguments):
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Story chapter writes for this turn must use StorydexApplyStoryIncrement. "
                    "That tool enforces the selected chapter template and Storydex's objective word count."
                ),
            )
        return super().run(arguments)


class StorydexEditTool(_WorkspacePathNormalizerMixin, EditTool):
    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        if self._strict_story_generation_turn() and self._targets_chapter_file(arguments):
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Story chapter edits for this turn must use StorydexApplyStoryIncrement. "
                    "That tool enforces the selected chapter template and Storydex's objective word count."
                ),
            )
        return super().run(arguments)


class StorydexGlobTool(_WorkspacePathNormalizerMixin, GlobTool):
    def _normalized_arguments(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        normalized = super()._normalized_arguments(arguments)
        # GlobTool defaults path to "." (process CWD); pin it to the workspace.
        if not str(normalized.get("path") or "").strip():
            normalized["path"] = self.workspace_root.as_posix()
        return normalized


class StorydexGrepTool(_WorkspacePathNormalizerMixin, GrepTool):
    def _normalized_arguments(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        normalized = super()._normalized_arguments(arguments)
        if not str(normalized.get("path") or "").strip():
            normalized["path"] = self.workspace_root.as_posix()
        return normalized


class StorydexBashTool(_WorkspaceBoundMixin, BashTool):
    """BashTool that executes in the workspace instead of the process CWD."""

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        command = arguments["command"]
        shell_command = f"chcp 65001>nul & {command}" if os.name == "nt" else command
        timeout = arguments.get("timeout", 120000) / 1000
        cwd = self.workspace_root.as_posix()

        try:
            result = subprocess.run(
                shell_command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout,
                cwd=str(self.workspace_root),
            )

            output = _truncate_output(result.stdout)
            if result.stderr:
                stderr_truncated = _truncate_output(result.stderr)
                output += f"\n[stderr]\n{stderr_truncated}"

            if result.returncode != 0:
                hint = EXIT_CODE_HINTS.get(result.returncode, "")
                error_parts = [
                    f"Command exited with code {result.returncode}",
                    f"  Command: {command}",
                    f"  Working directory: {cwd}",
                    f"  Timeout: {timeout}s",
                ]
                if hint:
                    error_parts.append(f"  Hint: {hint}")
                return ToolResult(success=False, output=output, error="\n".join(error_parts))

            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command timed out after {timeout} seconds\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - mirror upstream tool behavior
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command execution failed: {type(exc).__name__}: {exc}\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}"
                ),
            )


class StorydexPowerShellTool(_WorkspaceBoundMixin, PowerShellTool):
    """PowerShellTool that executes in the workspace instead of the process CWD."""

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        command = arguments["command"]
        timeout = arguments.get("timeout", 120000) / 1000
        cwd = self.workspace_root.as_posix()

        try:
            result = subprocess.run(
                ["powershell.exe", "-Command", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.workspace_root),
            )

            output = _truncate_output(result.stdout)
            if result.stderr:
                stderr_truncated = _truncate_output(result.stderr)
                output += f"\n[stderr]\n{stderr_truncated}"

            if result.returncode != 0:
                return ToolResult(
                    success=False,
                    output=output,
                    error=(
                        f"Command exited with code {result.returncode}\n"
                        f"  Command: {command}\n"
                        f"  Working directory: {cwd}\n"
                        f"  Timeout: {timeout}s"
                    ),
                )

            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command timed out after {timeout} seconds\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}"
                ),
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"PowerShell not found\n"
                    f"  Command: {command}\n"
                    f"  Hint: powershell.exe 未找到，请确认系统已安装 PowerShell"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - mirror upstream tool behavior
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command execution failed: {type(exc).__name__}: {exc}\n"
                    f"  Command: {command}\n"
                    f"  Working directory: {cwd}"
                ),
            )


class _ReplayableExternalToolMixin:
    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        from services.llm_replay import replayable_external_tool_call

        live_run = super().run
        return replayable_external_tool_call(
            self.name,
            arguments,
            lambda: live_run(arguments),
        )


class StorydexWebSearchTool(_ReplayableExternalToolMixin, WebSearchTool):
    pass


class StorydexWebFetchTool(_ReplayableExternalToolMixin, WebFetchTool):
    pass


def create_workspace_bound_tool_overrides(
    workspace_root: Path,
    turn_contract: Dict[str, Any] | None = None,
) -> List[Any]:
    root = Path(workspace_root).resolve()
    return [
        StorydexReadTool(workspace_root=root, turn_contract=turn_contract),
        StorydexWriteTool(workspace_root=root, turn_contract=turn_contract),
        StorydexEditTool(workspace_root=root, turn_contract=turn_contract),
        StorydexGlobTool(workspace_root=root, turn_contract=turn_contract),
        StorydexGrepTool(workspace_root=root, turn_contract=turn_contract),
        StorydexBashTool(workspace_root=root, turn_contract=turn_contract),
        StorydexPowerShellTool(workspace_root=root, turn_contract=turn_contract),
    ]


def create_replayable_external_tool_overrides() -> List[Any]:
    return [StorydexWebSearchTool(), StorydexWebFetchTool()]
