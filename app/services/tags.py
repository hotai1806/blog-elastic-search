"""Tag registry queries. Post counts come from ``post_tags`` via one grouped subquery."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Tag, post_tags
from app.schemas import TagPatch, normalize_tag


def _counted_tags_stmt():
    counts = (
        select(post_tags.c.tag_id, func.count(post_tags.c.post_id).label("post_count"))
        .group_by(post_tags.c.tag_id)
        .subquery()
    )
    post_count = func.coalesce(counts.c.post_count, 0).label("post_count")
    stmt = select(Tag, post_count).outerjoin(counts, counts.c.tag_id == Tag.id)
    return stmt, post_count


def list_tags(db: Session, *, sort: str = "name") -> list[tuple[Tag, int]]:
    stmt, post_count = _counted_tags_stmt()
    if sort == "posts":
        stmt = stmt.order_by(post_count.desc(), Tag.name)
    else:
        stmt = stmt.order_by(Tag.name)
    return [(tag, int(count)) for tag, count in db.execute(stmt)]


def get_tag(db: Session, name: str) -> Optional[tuple[Tag, int]]:
    stmt, _ = _counted_tags_stmt()
    row = db.execute(stmt.where(Tag.name == normalize_tag(name))).first()
    if row is None:
        return None
    tag, count = row
    return tag, int(count)


def update_tag(db: Session, tag: Tag, data: TagPatch) -> Tag:
    changes = data.model_dump(exclude_unset=True)
    if "description" in changes:
        tag.description = changes["description"]
        db.commit()
        db.refresh(tag)
    return tag
