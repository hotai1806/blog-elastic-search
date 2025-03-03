# migrations/versions/001_create_blog_posts_table.py
"""create blog posts table

Revision ID: 001
Revises: 
Create Date: 2025-03-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic
revision = '001'
down_revision = None
branch_labels = None
depends_on = None

def table_exists(connection, table_name):
    inspector = Inspector.from_engine(connection)
    return table_name in inspector.get_table_names()

def upgrade():
    bind = op.get_bind()  # Get DB connection
    if not table_exists(bind, 'blog_posts'):  # Check if table exists
        op.create_table(
        'blog_posts',
        sa.Column('id', sa.Integer, primary_key=True, index=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('author', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    
    # Create index for faster searching
    op.create_index('ix_blog_posts_title', 'blog_posts', ['title'])
    op.create_index('ix_blog_posts_author', 'blog_posts', ['author'])

def downgrade():
    op.drop_index('ix_blog_posts_author')
    op.drop_index('ix_blog_posts_title')
    op.drop_table('blog_posts')