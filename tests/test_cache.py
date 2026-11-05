from datetime import datetime

import fakeredis
import redis

from app import cache
from app.schemas import PostOut


class BrokenRedis:
    def get(self, *_):
        raise redis.ConnectionError("down")

    setex = delete = get


def sample_post() -> PostOut:
    now = datetime(2025, 3, 3, 10, 0, 0)
    return PostOut(id=5, title="t", content="c", author="a", tags=["x"], created_at=now, updated_at=now)


def test_round_trip_with_ttl():
    client = fakeredis.FakeRedis()
    cache.cache_post(client, sample_post(), 60)
    assert cache.get_cached_post(client, 5) == sample_post()
    assert 0 < client.ttl("post:5") <= 60
    cache.invalidate_post(client, 5)
    assert cache.get_cached_post(client, 5) is None


def test_redis_errors_are_swallowed(caplog):
    broken = BrokenRedis()
    assert cache.get_cached_post(broken, 1) is None
    cache.cache_post(broken, sample_post(), 60)
    cache.invalidate_post(broken, 1)
    assert caplog.text.count("Redis") == 3


def test_malformed_entries_are_ignored():
    client = fakeredis.FakeRedis()
    client.set("post:1", "{oops")
    assert cache.get_cached_post(client, 1) is None
