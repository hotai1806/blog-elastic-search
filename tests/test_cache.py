from datetime import datetime

import fakeredis
import redis

from app import cache
from app.schemas import PostOut


class BrokenRedis:
    def get(self, *_, **__):
        raise redis.ConnectionError("down")

    set = get


def sample_post(title: str = "t") -> PostOut:
    now = datetime(2025, 3, 3, 10, 0, 0)
    return PostOut(id=5, title=title, content="c", author="a", tags=["x"], created_at=now, updated_at=now)


def test_round_trip_ttl_and_tombstone():
    client = fakeredis.FakeRedis()
    cache.cache_post(client, sample_post(), 60)
    assert cache.get_cached_post(client, 5) == sample_post()
    assert 0 < client.ttl("post:5") <= 60
    cache.mark_post_deleted(client, 5, 60)
    assert client.get("post:5") == cache.TOMBSTONE
    assert cache.get_cached_post(client, 5) is None


def test_reader_never_overwrites_an_existing_entry():
    client = fakeredis.FakeRedis()
    cache.cache_post(client, sample_post("fresh"), 60)
    cache.cache_post(client, sample_post("stale"), 60, only_if_absent=True)
    assert cache.get_cached_post(client, 5).title == "fresh"


def test_tombstone_blocks_repopulation_by_a_slow_reader():
    client = fakeredis.FakeRedis()
    cache.mark_post_deleted(client, 5, 60)
    cache.cache_post(client, sample_post("stale"), 60, only_if_absent=True)
    assert client.get("post:5") == cache.TOMBSTONE
    assert cache.get_cached_post(client, 5) is None


def test_writer_overwrites_whatever_is_there():
    client = fakeredis.FakeRedis()
    cache.cache_post(client, sample_post("old"), 60, only_if_absent=True)
    cache.cache_post(client, sample_post("new"), 60)
    assert cache.get_cached_post(client, 5).title == "new"


def test_redis_errors_are_swallowed(caplog):
    broken = BrokenRedis()
    assert cache.get_cached_post(broken, 1) is None
    cache.cache_post(broken, sample_post(), 60)
    cache.mark_post_deleted(broken, 1, 60)
    assert caplog.text.count("Redis") == 3


def test_malformed_entries_are_ignored():
    client = fakeredis.FakeRedis()
    client.set("post:1", "{oops")
    assert cache.get_cached_post(client, 1) is None
