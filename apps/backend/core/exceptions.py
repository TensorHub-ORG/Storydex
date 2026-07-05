from __future__ import annotations

from typing import Any, Dict, Optional


class StorydexError(Exception):
    """Base domain error for Storydex backend."""

    default_code = "storydex_error"
    default_status_code = 400

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.status_code = status_code or self.default_status_code
        self.details = details or {}


class InvalidWorkspacePathError(StorydexError):
    """Raised when a path escapes allowed workspace scope."""

    default_code = "invalid_workspace_path"
    default_status_code = 400


class ToolNotFoundError(StorydexError):
    """Raised when coordinator asks for missing tool."""

    default_code = "tool_not_found"
    default_status_code = 404


class ToolValidationError(StorydexError):
    """Raised when tool input payload is invalid."""

    default_code = "tool_validation_error"
    default_status_code = 422


class ToolPermissionError(StorydexError):
    """Raised when tool invocation is not allowed by policy."""

    default_code = "tool_permission_denied"
    default_status_code = 403


class ToolExecutionError(StorydexError):
    """Raised when tool execution fails unexpectedly."""

    default_code = "tool_execution_error"
    default_status_code = 500


class CoordinatorExecutionError(StorydexError):
    """Raised when coordinator main loop fails."""

    default_code = "coordinator_execution_error"
    default_status_code = 500


class LLMConfigurationError(StorydexError):
    """Raised when LLM provider configuration is missing or invalid."""

    default_code = "llm_configuration_error"
    default_status_code = 500


class LLMRequestError(StorydexError):
    """Raised when LLM provider request fails."""

    default_code = "llm_request_error"
    default_status_code = 502


class LLMResponseFormatError(StorydexError):
    """Raised when LLM response payload format is invalid."""

    default_code = "llm_response_format_error"
    default_status_code = 502


class AtomicWriteError(StorydexError):
    """Raised when atomic file write fails and rollback is triggered."""

    default_code = "atomic_write_error"
    default_status_code = 500


class ConfirmationTokenInvalidError(StorydexError):
    """Raised when confirmation token does not match pending write plan."""

    default_code = "confirmation_token_invalid"
    default_status_code = 404


class ConfirmationTokenExpiredError(StorydexError):
    """Raised when confirmation token has expired."""

    default_code = "confirmation_token_expired"
    default_status_code = 410


class ProjectPathNotFoundError(StorydexError):
    """Raised when the requested project path does not exist."""

    default_code = "project_path_not_found"
    default_status_code = 404


class ProjectPathInvalidError(StorydexError):
    """Raised when the requested project path is invalid."""

    default_code = "project_path_invalid"
    default_status_code = 400


class GitServiceError(StorydexError):
    """Raised when local Git version-control actions fail."""

    default_code = "git_service_error"
    default_status_code = 500
