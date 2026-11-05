"""Best-effort Redis cache for single posts.

Interleaving rules, so that a slow reader can never leave a stale entry behind:

- readers fill the cache only when the key is absent (``SET NX``);
- updates write the fresh post through (plain ``SET``);
- deletes leave a tombstone for the cache lifetime, so a reader that fetched the row just
  before the delete cannot re-populate the key, and later readers go to the database.

Redis being unavailable never fails a request: every error is logged and ignored.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis

from app.schemas import PostOut

logger = logging.getLogger(__name__)

TOMBSTONE = b"__deleted__"


def post_key(post_id: int) -> str:
    return f"post:{post_id}"


def get_cached_post(client: redis.Redis, post_id: int) -> Optional[PostOut]:
    """The cached post, or ``None`` on a miss, a tombstone, a malformed entry or a Redis error."""
    try:
        raw = client.get(post_key(post_id))
    except redis.RedisError as exc:
        logger.warning("Redis read failed for post %s: %s", post_id, exc)
        return None
    if raw is None or raw == TOMBSTONE:
        return None
    try:
        return PostOut.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding malformed cache entry for post %s", post_id)
        return None


def cache_post(
    client: redis.Redis, post: PostOut, ttl_seconds: int, *, only_if_absent: bool = False
) -> None:
    """Store ``post``. Readers pass ``only_if_absent=True`` so they never overwrite a writer."""
    try:
        client.set(post_key(post.id), post.model_dump_json(), ex=ttl_seconds, nx=only_if_absent)
    except redis.RedisError as exc:
        logger.warning("Redis write failed for post %s: %s", post.id, exc)


def mark_post_deleted(client: redis.Redis, post_id: int, ttl_seconds: int) -> None:
    """Replace the entry with a tombstone so concurrent readers cannot resurrect the post."""
    try:
        client.set(post_key(post_id), TOMBSTONE, ex=ttl_seconds)
    except redis.RedisError as exc:
        logger.warning("Redis tombstone failed for post %s: %s", post_id, exc)
