from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORYKEEPER_BASE_URL = "https://storykeeper.septemc.cn"
DEFAULT_QUOTA_GATEWAY_BASE_URL = "https://api.septemc.cn"

load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(BACKEND_ROOT / ".env", override=False)


class Settings(BaseSettings):
    app_name: str = "Storydex Backend"
    api_host: str = "127.0.0.1"
    api_port: int = 18081
    serve_frontend_static: bool = False
    global_root: Path = Field(
        default=Path.home() / ".storydex",
        validation_alias=AliasChoices("STORYDEX_GLOBAL_ROOT", "GLOBAL_ROOT"),
    )
    workspace_root: Path = Field(
        default=Path.home() / ".storydex" / "workspace",
        validation_alias=AliasChoices("STORYDEX_WORKSPACE_ROOT", "WORKSPACE_ROOT"),
    )
    storydex_dir_name: str = ".storydex"
    frontend_dist_dir: Path = Field(
        default=PROJECT_ROOT / "apps" / "frontend" / "dist",
        validation_alias=AliasChoices("STORYDEX_FRONTEND_DIST_DIR", "FRONTEND_DIST_DIR"),
    )
    novel_database_url: str = Field(
        default="",
        validation_alias=AliasChoices("NOVEL_DATABASE_URL", "DATABASE_URL"),
    )
    storykeeper_base_url: str = Field(
        default=DEFAULT_STORYKEEPER_BASE_URL,
        validation_alias=AliasChoices("STORYKEEPER_BASE_URL", "ADMIN_BASE_URL"),
    )
    storykeeper_internal_token: str = Field(
        default="",
        validation_alias=AliasChoices("STORYKEEPER_INTERNAL_TOKEN", "ADMIN_INTERNAL_TOKEN"),
    )
    storykeeper_writer_path: str = Field(
        default="/api/storydex/writer",
        validation_alias=AliasChoices("STORYKEEPER_WRITER_PATH", "STORYKEEPER_ACCOUNT_QUOTA_PATH"),
    )
    quota_gateway_base_url: str = Field(
        default=DEFAULT_QUOTA_GATEWAY_BASE_URL,
        validation_alias=AliasChoices("QUOTA_GATEWAY_BASE_URL"),
    )

    embedding_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("EMBEDDING_API_KEY"),
    )
    embedding_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("EMBEDDING_BASE_URL"),
    )
    embedding_model: str = Field(
        default="text-embedding-v3",
        validation_alias=AliasChoices("EMBEDDING_MODEL"),
    )

    model_config = SettingsConfigDict(extra="ignore")

    @property
    def storydex_root(self) -> Path:
        return self.workspace_root / self.storydex_dir_name


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


FEATURE_FLAG_DEFAULTS: dict[str, object] = {
    "TOOL_RESULT_COMPRESSION": True,
    "ASYNC_TRACE_ENABLED": False,
    "ASYNC_HOOKS_ENABLED": False,
    "ASYNC_FILE_BACKUP_ENABLED": False,
    "AUTO_COMPACT_ENABLED": False,
    "CONTEXT_PIPELINE_FTS5": False,
    "CONTEXT_LRU_ENABLED": False,
    "CONTEXT_TOKEN_BUDGET_REAL": False,
    "MEMORY_LAYER_V2": False,
    "SUMMARY_PRODUCT_ENABLED": False,
    "ENTITY_MODEL_V2": False,
    "AUTHORITY_ORDER_ENFORCED": False,
    "TOOL_PARALLELISM_ENABLED": True,
    "ABORT_SUPPORT_ENABLED": True,
    "SKILL_LAZY_LOADING": False,
    "JIT_CONTEXT_LOADING_ENABLED": False,
    "TWO_PASS_GENERATION_ENABLED": False,
    "STREAMING_TOOL_LOOP_PROVIDER_AWARE": True,
}
