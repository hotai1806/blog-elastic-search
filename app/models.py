"""SQLAlchemy models. The schema is owned by the Alembic migrations in ``migrations/``."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import ARRAY, JSON, Column, DateTime, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp; the columns are ``TIMESTAMP WITHOUT TIME ZONE``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("blog_posts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

# PostgreSQL stores tag names as a VARCHAR(50)[]; the SQLite variant exists only so the
# unit tests can run without a PostgreSQL server.
TagNameArray = ARRAY(String(50)).with_variant(JSON(), "sqlite")


class BlogPost(Base):
    __tablename__ = "blog_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    # Denormalised copy of the tag names, kept in sync with ``tag_relations`` by
    # ``app.services.posts.set_post_tags``. It is what the API and the search index read.
    tags: Mapped[Optional[list[str]]] = mapped_column(TagNameArray, nullable=True)

    tag_relations: Mapped[list["Tag"]] = relationship(
        "Tag", secondary=post_tags, back_populates="posts", order_by="Tag.name"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"BlogPost(id={self.id!r}, title={self.title!r})"


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    posts: Mapped[list[BlogPost]] = relationship(
        "BlogPost", secondary=post_tags, back_populates="tag_relations"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Tag(id={self.id!r}, name={self.name!r})"
