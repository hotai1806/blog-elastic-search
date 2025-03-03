# migrate.py
import os
import argparse
import subprocess
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from elasticsearch import Elasticsearch
from main import BlogPost, Base

def run_migrations():
    """Run database migrations using Alembic"""
    print("Running database migrations...")
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    print("Migrations completed successfully.")

def setup_elasticsearch():
    """Initialize Elasticsearch index"""
    print("Setting up Elasticsearch index...")
    es_url = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
    es = Elasticsearch(es_url)
    
    # Create index if it doesn't exist
    if not es.indices.exists(index="blog_posts"):
        es.indices.create(
            index="blog_posts",
            body={
                "mappings": {
                    "properties": {
                        "id": {"type": "integer"},
                        "title": {"type": "text", "analyzer": "standard", "fields": {"keyword": {"type": "keyword"}}},
                        "content": {"type": "text", "analyzer": "standard"},
                        "author": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "tags": {"type": "keyword"}
                    }
                },
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0
                }
            }
        )
    print("Elasticsearch index created.")

def sync_data_to_elasticsearch():
    """Sync existing data from PostgreSQL to Elasticsearch"""
    print("Syncing data to Elasticsearch...")
    
    # Setup database connection
    db_url = os.getenv("DATABASE_URL", "postgresql://bloguser:blogpassword@postgres:5432/blogdb")
    engine = create_engine(db_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    # Setup Elasticsearch connection
    es_url = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
    es = Elasticsearch(es_url)
    
    # Get all blog posts
    posts = db.query(BlogPost).all()
    
    # Index each post in Elasticsearch
    for post in posts:
        post_dict = post.to_dict()
        es.index(index="blog_posts", id=post.id, document=post_dict)
    
    print(f"Successfully synced {len(posts)} posts to Elasticsearch.")
    db.close()

def main():
    parser = argparse.ArgumentParser(description="Database migration utility")
    parser.add_argument("--init", action="store_true", help="Initialize migrations")
    parser.add_argument("--sync", action="store_true", help="Sync data to Elasticsearch")
    
    args = parser.parse_args()
    
    if args.init:
        run_migrations()
        setup_elasticsearch()
    elif args.sync:
        sync_data_to_elasticsearch()
    else:
        run_migrations()

if __name__ == "__main__":
    main()