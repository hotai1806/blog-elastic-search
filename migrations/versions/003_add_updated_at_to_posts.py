# migrations/versions/003_add_updated_at_to_posts.py
"""add updated_at to blog posts

Revision ID: 003
Revises: 002
Create Date: 2026-11-05

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'blog_posts',
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    # Existing rows have never been updated: use their creation time.
    op.execute("UPDATE blog_posts SET updated_at = created_at")


def downgrade():
    op.drop_column('blog_posts', 'updated_at')
