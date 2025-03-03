# migrations/versions/002_add_tags_to_posts.py
"""add tags to posts

Revision ID: 002
Revises: 001
Create Date: 2025-03-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None

def upgrade():
    # Add tags array column
    op.add_column('blog_posts', sa.Column('tags', ARRAY(sa.String(50)), nullable=True))
    
    # Create tags table for advanced tag management
    op.create_table(
        'tags',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.String(50), nullable=False, unique=True),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    
    # Create many-to-many relationship table
    op.create_table(
        'post_tags',
        sa.Column('post_id', sa.Integer, sa.ForeignKey('blog_posts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('tag_id', sa.Integer, sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )

def downgrade():
    op.drop_table('post_tags')
    op.drop_table('tags')
    op.drop_column('blog_posts', 'tags')