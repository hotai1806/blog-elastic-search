"""Lazily created Elasticsearch and Redis clients, exposed as FastAPI dependencies.

Tests override ``get_es``/``get_redis`` on the app; the clients themselves are only
built on first use so importing the app never opens a network connection.
"""
from __future__ import annotations

from functools import lru_cache

import redis
from elasticsearch import Elasticsearch

from app.config import settings


@lru_cache(maxsize=1)
def get_es_client() -> Elasticsearch:
    return Elasticsearch(settings.elasticsearch_url, request_timeout=10)


@lru_cache(maxsize=1)
def get_redis_client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)


def get_es() -> Elasticsearch:
    return get_es_client()


def get_redis() -> redis.Redis:
    return get_redis_client()
