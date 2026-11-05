"""Best-effort Redis cache for single posts. Redis being down never fails a request."""
from __future__ import annotations

import logging
from typing import Optional

import redis

from app.schemas import PostOut

logger = logging.getLogger(__name__)


def post_key(post_id: int) -> str:
    return f"post:{post_id}"


def get_cached_post(client: redis.Redis, post_id: int) -> Optional[PostOut]:
    try:
        raw = client.get(post_key(post_id))
    except redis.RedisError as exc:
        logger.warning("Redis read failed for post %s: %s", post_id, exc)
        return None
    if raw is None:
        return None
    try:
        return PostOut.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding malformed cache entry for post %s", post_id)
        return None


def cache_post(client: redis.Redis, post: PostOut, ttl_seconds: int) -> None:
    try:
        client.setex(post_key(post.id), ttl_seconds, post.model_dump_json())
    except redis.RedisError as exc:
        logger.warning("Redis write failed for post %s: %s", post.id, exc)


def invalidate_post(client: redis.Redis, post_id: int) -> None:
    try:
        client.delete(post_key(post_id))
    except redis.RedisError as exc:
        logger.warning("Redis invalidation failed for post %s: %s", post_id, exc)
