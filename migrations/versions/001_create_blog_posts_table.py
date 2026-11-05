# migrations/versions/001_create_blog_posts_table.py
"""create blog posts table

Revision ID: 001
Revises: 
Create Date: 2025-03-03

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # The table may already exist on databases that were created by the original
    # application's ``create_all()`` before Alembic was introduced.
    if 'blog_posts' not in sa.inspect(bind).get_table_names():
        op.create_table(
            'blog_posts',
            sa.Column('id', sa.Integer, primary_key=True, index=True),
            sa.Column('title', sa.String(255), nullable=False),
            sa.Column('content', sa.Text, nullable=False),
            sa.Column('author', sa.String(100), nullable=False),
            sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        )

    # Create indexes for faster searching (idempotent for the pre-Alembic case above)
    existing_indexes = {index['name'] for index in sa.inspect(bind).get_indexes('blog_posts')}
    if 'ix_blog_posts_title' not in existing_indexes:
        op.create_index('ix_blog_posts_title', 'blog_posts', ['title'])
    if 'ix_blog_posts_author' not in existing_indexes:
        op.create_index('ix_blog_posts_author', 'blog_posts', ['author'])


def downgrade():
    op.drop_index('ix_blog_posts_author', table_name='blog_posts')
    op.drop_index('ix_blog_posts_title', table_name='blog_posts')
    op.drop_table('blog_posts')
