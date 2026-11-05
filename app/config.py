"""Application settings, read from the environment (a local ``.env`` file is honoured)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://bloguser:blogpassword@postgres:5432/blogdb"
        )
    )
    elasticsearch_url: str = field(
        default_factory=lambda: os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
    )
    elasticsearch_index: str = field(
        default_factory=lambda: os.getenv("ELASTICSEARCH_INDEX", "blog_posts")
    )
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://redis:6379"))
    cache_ttl_seconds: int = field(default_factory=lambda: _int_env("CACHE_TTL_SECONDS", 3600))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())


settings = Settings()
