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
    # FTS5 混合检索已接入上下文组装（related_passages 块），默认开启；
    # 项目可通过 .storydex/config/feature-flags.json 关闭。
    "CONTEXT_PIPELINE_FTS5": True,
    "CONTEXT_LRU_ENABLED": False,
    "CONTEXT_TOKEN_BUDGET_REAL": False,
    # P1-7 token controls remain shadow/disabled by default.  Projects can
    # override these integers through the same feature-flag file/env path.
    "CONTEXT_TOKEN_WINDOW": 256000,
    "CONTEXT_OUTPUT_RESERVE_TOKENS": 8192,
    "MEMORY_LAYER_V2": False,
    "SUMMARY_PRODUCT_ENABLED": False,
    "ENTITY_MODEL_V2": False,
    "AUTHORITY_ORDER_ENFORCED": False,
    "TOOL_PARALLELISM_ENABLED": True,
    "ABORT_SUPPORT_ENABLED": True,
    "SKILL_LAZY_LOADING": False,
    "JIT_CONTEXT_LOADING_ENABLED": False,
    "TWO_PASS_GENERATION_ENABLED": False,
    "SEMANTIC_BUDGET_GENERATION_ENABLED": False,
    # Keep semantic length tiers behind an explicit project/env opt-in. The
    # controlled acceptance matrix must pass quality and separation gates before
    # this can become a default product control.
    "STORY_LENGTH_TIER_ENABLED": False,
    # 有界正文路径：一次草稿 + 最多一次局部长度补丁 + 一次写入，替代
    # Agent 工具回合里的「写入后再补写」。默认开启；精确字数开关另由
    # TurnContract 的 wordCountPolicy.precision 控制，默认关闭。
    "BOUNDED_STORY_GENERATION_ENABLED": True,
    # New elastic manuscript protocol. Keep disabled until the full fixed-sample
    # live acceptance run meets every length, quality, call and efficiency gate.
    "ELASTIC_STORY_MANUSCRIPT_ENABLED": False,
    # Asymmetric ordinary-mode gate with at most one independent whole-chapter
    # second draft. It stays off until the isolated live acceptance run passes.
    "ASYMMETRIC_STORY_LENGTH_ENABLED": False,
    # 段数配额仅保留为显式实验分支；默认使用章级软字符目标和程序硬验收带。
    "PARAGRAPH_QUOTA_GENERATION_ENABLED": False,
    "STREAMING_TOOL_LOOP_PROVIDER_AWARE": True,
    # B/C/D intent-routing experiment. Hybrid is the default: clear ordinary
    # turns use deterministic RouteHints, while specialized/ambiguous workflows
    # can still request structured intent classification. Projects may switch
    # to legacy/direct/workflow without code changes.
    "AGENT_INTENT_ROUTING_MODE": "hybrid",
}
