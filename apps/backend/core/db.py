from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from core.config import get_settings
from core.exceptions import StorydexError


@lru_cache(maxsize=1)
def get_account_engine() -> Engine:
    settings = get_settings()
    database_url = settings.novel_database_url.strip()
    if not database_url:
        raise StorydexError(
            "Account database is not configured.",
            code="account_database_not_configured",
            status_code=503,
        )

    return create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
    )
