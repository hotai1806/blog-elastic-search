import pytest

from app.search import build_search_query


def test_query_with_filters_builds_expected_dsl():
    params = build_search_query(
        query="elastic search", author="alice", tags=["python", "search"], sort="relevance", page=2, size=5
    )
    assert params == {
        "query": {
            "bool": {
                "must": [{"multi_match": {"query": "elastic search", "fields": ["title^2", "content"]}}],
                "filter": [
                    {"term": {"author": "alice"}},
                    {"term": {"tags": "python"}},
                    {"term": {"tags": "search"}},
                ],
            }
        },
        "sort": [{"_score": {"order": "desc"}}, {"created_at": {"order": "desc"}}, {"id": {"order": "desc"}}],
        "from_": 5,
        "size": 5,
        "track_total_hits": True,
        "highlight": {
            "fields": {
                "title": {"number_of_fragments": 0},
                "content": {"fragment_size": 150, "number_of_fragments": 3},
            }
        },
    }


def test_browse_without_query_uses_match_all_newest_and_no_highlight():
    params = build_search_query(query=None, author=None, tags=[], sort="relevance", page=1, size=10)
    assert params["query"] == {"bool": {"must": [{"match_all": {}}]}}
    assert params["sort"] == [{"created_at": {"order": "desc"}}, {"id": {"order": "desc"}}]
    assert params["from_"] == 0
    assert "highlight" not in params


def test_oldest_sort_and_offset():
    params = build_search_query(query="x", author=None, tags=[], sort="oldest", page=3, size=20)
    assert params["sort"] == [{"created_at": {"order": "asc"}}, {"id": {"order": "asc"}}]
    assert params["from_"] == 40


def test_newest_sort_with_query_keeps_highlight():
    params = build_search_query(query="x", author=None, tags=[], sort="newest", page=1, size=1)
    assert params["sort"][0] == {"created_at": {"order": "desc"}}
    assert "highlight" in params


@pytest.mark.parametrize("kwargs", [
    {"sort": "bogus", "page": 1, "size": 10},
    {"sort": "newest", "page": 0, "size": 10},
    {"sort": "newest", "page": 1, "size": 0},
])
def test_invalid_arguments_raise(kwargs):
    with pytest.raises(ValueError):
        build_search_query(query=None, author=None, tags=[], **kwargs)
