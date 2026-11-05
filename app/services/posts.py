"""Post lifecycle. PostgreSQL is authoritative; Elasticsearch is written inside the transaction.

Write pattern: mutate ORM objects -> ``flush`` (ids/defaults assigned) -> Elasticsearch
write -> ``commit``. If Elasticsearch fails the session is rolled back and
``SearchBackendError`` propagates, so nothing is persisted on one side only.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Optional

from elasticsearch import Elasticsearch
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import search
from app.models import BlogPost, Tag, utcnow
from app.schemas import PostCreate, PostPatch, PostUpdate


def _existing_tags(db: Session, names: list[str]) -> dict[str, Tag]:
    return {tag.name: tag for tag in db.scalars(select(Tag).where(Tag.name.in_(names)))}


def resolve_tags(db: Session, names: list[str]) -> list[Tag]:
    """Return ``Tag`` rows for ``names`` (already normalised), creating missing ones.

    Each new tag is inserted inside a SAVEPOINT so that a concurrent request creating
    the same tag only costs a retry of the lookup instead of failing the whole request.
    Call this *before* mutating other objects: the flush inside the savepoint would
    otherwise write (and on conflict, discard) those changes too.
    """
    if not names:
        return []
    found = _existing_tags(db, names)
    for name in names:
        if name in found:
            continue
        try:
            with db.begin_nested():
                tag = Tag(name=name)
                db.add(tag)
                db.flush()
        except IntegrityError:
            tag = db.scalar(select(Tag).where(Tag.name == name))
            if tag is None:  # pragma: no cover - the conflicting row vanished again
                raise
        found[name] = tag
    return [found[name] for name in names]


def set_post_tags(post: BlogPost, tags: list[Tag]) -> None:
    """Assign resolved tags to a post, keeping the denormalised ``tags`` column in sync."""
    post.tag_relations = list(tags)
    post.tags = [tag.name for tag in tags]


def _write_through(db: Session, action: Callable[[], None]) -> None:
    try:
        action()
    except search.SearchBackendError:
        db.rollback()
        raise


def get_post(db: Session, post_id: int, *, for_update: bool = False) -> Optional[BlogPost]:
    """Load a post; ``for_update`` takes a row lock (``SELECT ... FOR UPDATE``) so concurrent
    writers queue up and each sees the previous one's committed state."""
    return db.get(BlogPost, post_id, with_for_update=True if for_update else None)


def list_posts(
    db: Session,
    *,
    page: int,
    size: int,
    author: Optional[str] = None,
    tags: Sequence[str] = (),
) -> tuple[list[BlogPost], int]:
    """Newest-first page of posts filtered by exact author and/or by every tag in ``tags``."""
    stmt = select(BlogPost)
    if author:
        stmt = stmt.where(BlogPost.author == author)
    for tag in tags:
        stmt = stmt.where(BlogPost.tag_relations.any(Tag.name == tag))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(
        stmt.order_by(BlogPost.created_at.desc(), BlogPost.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return list(items), int(total)


def create_post(db: Session, es: Elasticsearch, index: str, data: PostCreate) -> BlogPost:
    tags = resolve_tags(db, data.tags)
    now = utcnow()
    post = BlogPost(
        title=data.title, content=data.content, author=data.author, created_at=now, updated_at=now
    )
    set_post_tags(post, tags)
    db.add(post)
    db.flush()
    _write_through(db, lambda: search.index_post(es, index, post))
    db.commit()
    db.refresh(post)
    return post


def replace_post(
    db: Session, es: Elasticsearch, index: str, post: BlogPost, data: PostUpdate
) -> BlogPost:
    tags = resolve_tags(db, data.tags)
    post.title = data.title
    post.content = data.content
    post.author = data.author
    set_post_tags(post, tags)
    post.updated_at = utcnow()
    db.flush()
    _write_through(db, lambda: search.index_post(es, index, post))
    db.commit()
    db.refresh(post)
    return post


def patch_post(
    db: Session, es: Elasticsearch, index: str, post: BlogPost, data: PostPatch
) -> BlogPost:
    changes = {
        name: value
        for name, value in data.model_dump(exclude_unset=True).items()
        if value is not None
    }
    new_tags = changes.pop("tags", None)
    if not changes and new_tags is None:
        return post
    tags = resolve_tags(db, new_tags) if new_tags is not None else None
    for name, value in changes.items():
        setattr(post, name, value)
    if tags is not None:
        set_post_tags(post, tags)
    post.updated_at = utcnow()
    db.flush()
    _write_through(db, lambda: search.index_post(es, index, post))
    db.commit()
    db.refresh(post)
    return post


def delete_post(db: Session, es: Elasticsearch, index: str, post: BlogPost) -> None:
    post_id = post.id
    db.delete(post)
    db.flush()
    _write_through(db, lambda: search.remove_post(es, index, post_id))
    db.commit()
