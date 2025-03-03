from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from elasticsearch import Elasticsearch
from fastapi.openapi.utils import get_openapi


import redis
import os
from datetime import datetime
import json

# FastAPI app
app = FastAPI(title="Blog Search API")

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bloguser:blogpassword@postgres:5432/blogdb")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Elasticsearch setup
ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
es = Elasticsearch(ELASTICSEARCH_URL)

# Redis setup
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
redis_client = redis.from_url(REDIS_URL)

# Define models
class BlogPost(Base):
    __tablename__ = "blog_posts"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    author = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "author": self.author,
            "created_at": self.created_at.isoformat()
        }

# Create tables
Base.metadata.create_all(bind=engine)

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Ensure Elasticsearch index exists
@app.on_event("startup")
async def startup_event():
    # Create index if it doesn't exist
    if not es.indices.exists(index="blog_posts"):
        es.indices.create(
            index="blog_posts",
            body={
                "mappings": {
                    "properties": {
                        "title": {"type": "text"},
                        "content": {"type": "text"},
                        "author": {"type": "keyword"},
                        "created_at": {"type": "date"}
                    }
                }
            }
        )
    print("Elasticsearch index created")

# API Endpoints
@app.post("/posts/", response_model=dict)
def create_post(title: str, content: str, author: str, db: Session = Depends(get_db)):
    # Create blog post in PostgreSQL
    db_post = BlogPost(title=title, content=content, author=author)
    db.add(db_post)
    db.commit()
    db.refresh(db_post)
    
    # Index in Elasticsearch
    post_dict = db_post.to_dict()
    es.index(index="blog_posts", id=db_post.id, document=post_dict)
    
    return {"message": "Post created successfully", "post": post_dict}

@app.get("/posts/{post_id}")
def get_post(post_id: int, db: Session = Depends(get_db)):
    # Try to get from Redis cache first
    cached_post = redis_client.get(f"post:{post_id}")
    if cached_post:
        return json.loads(cached_post)
    
    # If not in cache, get from database
    post = db.query(BlogPost).filter(BlogPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    # Save to cache
    post_dict = post.to_dict()
    redis_client.setex(f"post:{post_id}", 3600, json.dumps(post_dict))  # Cache for 1 hour
    
    return post_dict

@app.get("/search/")
def search_posts(query: str):
    # Search in Elasticsearch
    search_results = es.search(
        index="blog_posts",
        body={
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["title^2", "content"]  # Title has higher weight
                }
            },
            "highlight": {
                "fields": {
                    "title": {},
                    "content": {}
                }
            }
        }
    )
    # Format results
    hits = search_results["hits"]["hits"]
    results = []
    for hit in hits:
        result = hit["_source"]
        result["score"] = hit["_score"]
        if "highlight" in hit:
            result["highlights"] = hit["highlight"]
        results.append(result)
    
    return {"results": results, "count": len(results)}

@app.get("/docs")
async def get_openapi_documentation():
    return get_openapi(
        title="Blog Search API",
        version="1.0.0",
        description="API for searching and managing blog posts",
        routes=app.routes,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)