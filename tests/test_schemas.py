from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas import PostCreate, PostOut, PostPatch, SearchHit, normalize_tag, normalize_tags, parse_tag_filters


def test_normalize_tag_trims_collapses_and_lowercases():
    assert normalize_tag("  Machine   Learning ") == "machine learning"


def test_normalize_tags_dedupes_preserving_order():
    assert normalize_tags(["Python", "python ", "ES", "Python"]) == ["python", "es"]


@pytest.mark.parametrize("bad", [[" "], [""], ["x" * 51]])
def test_normalize_tags_rejects_blank_and_too_long(bad):
    with pytest.raises(ValueError):
        normalize_tags(bad)


def test_normalize_tags_rejects_more_than_the_limit():
    with pytest.raises(ValueError, match="at most 20"):
        normalize_tags([f"t{i}" for i in range(21)])


def test_post_create_strips_fields_and_normalises_tags():
    post = PostCreate(title="  Hi ", content=" body ", author=" me ", tags=[" A ", "a"])
    assert (post.title, post.content, post.author, post.tags) == ("Hi", "body", "me", ["a"])


def test_post_create_rejects_blank_title():
    with pytest.raises(ValidationError):
        PostCreate(title="   ", content="x", author="y")


def test_post_patch_distinguishes_unset_from_null():
    assert PostPatch(title="t").model_dump(exclude_unset=True) == {"title": "t"}
    assert PostPatch(tags=None).model_dump(exclude_unset=True) == {"tags": None}
    assert PostPatch().model_dump(exclude_unset=True) == {}


def test_post_out_treats_null_tags_as_empty_list():
    now = datetime(2025, 3, 3, 10, 0, 0)
    post = PostOut(id=1, title="t", content="c", author="a", tags=None, created_at=now, updated_at=now)
    assert post.tags == []


def test_parse_tag_filters_normalises_and_rejects_blank():
    assert parse_tag_filters([" Python ", "python", "Machine  Learning"]) == ["python", "machine learning"]
    assert parse_tag_filters([]) == []
    with pytest.raises(ValueError, match="blank"):
        parse_tag_filters(["python", "   "])


def test_search_hit_defaults_updated_at_for_legacy_documents():
    hit = SearchHit.model_validate(
        {"id": 1, "title": "t", "content": "c", "author": "a", "created_at": "2024-01-01T00:00:00"}
    )
    assert hit.updated_at == hit.created_at == datetime(2024, 1, 1)
    assert hit.tags == []
    assert hit.score is None and hit.highlights == {}
