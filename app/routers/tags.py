from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Tag
from app.schemas import NO_NUL, TagOut, TagPatch, TagSortOption
from app.services import tags as tags_service

router = APIRouter(prefix="/tags", tags=["tags"])


def tag_out(tag: Tag, post_count: int) -> TagOut:
    return TagOut(
        id=tag.id,
        name=tag.name,
        description=tag.description,
        created_at=tag.created_at,
        post_count=post_count,
    )


def load_tag_or_404(db: Session, name: str) -> tuple[Tag, int]:
    found = tags_service.get_tag(db, name)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return found


@router.get("/", response_model=list[TagOut])
def list_tags(
    sort: Annotated[TagSortOption, Query(description="name (A-Z) or posts (most used first)")] = "name",
    db: Session = Depends(get_db),
) -> list[TagOut]:
    return [tag_out(tag, count) for tag, count in tags_service.list_tags(db, sort=sort)]


TagNameParam = Annotated[str, Path(pattern=NO_NUL, description="Tag name (normalised like every other tag input)")]


@router.get("/{name}", response_model=TagOut)
def get_tag(name: TagNameParam, db: Session = Depends(get_db)) -> TagOut:
    tag, count = load_tag_or_404(db, name)
    return tag_out(tag, count)


@router.patch("/{name}", response_model=TagOut)
def update_tag(name: TagNameParam, data: TagPatch, db: Session = Depends(get_db)) -> TagOut:
    tag, count = load_tag_or_404(db, name)
    tag = tags_service.update_tag(db, tag, data)
    return tag_out(tag, count)
