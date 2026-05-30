#!/usr/bin/env python3
"""
Upload all MD files from acts/ to Pinecone.
Uses Pinecone's serverless embeddings (no OpenAI key needed).
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from pinecone import Pinecone

# Load env
load_dotenv()
PINECONE_API = os.getenv("PINECONE_API")

if not PINECONE_API:
    raise ValueError("PINECONE_API not found in .env")

# Init Pinecone
pc = Pinecone(api_key=PINECONE_API)

# Config
INDEX_NAME = "acts-docs"
CHUNK_SIZE = 8000  # chars (roughly 2000 tokens)

def chunk_text(text, chunk_size=CHUNK_SIZE):
    """Split text into chunks"""
    chunks = []
    start = 0
    text_len = len(text)
    
    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    
    return chunks

def main():
    # Create/get index with serverless embeddings
    print(f"📌 Connecting to Pinecone index: {INDEX_NAME}")
    
    try:
        index = pc.Index(INDEX_NAME)
        stats = index.describe_index_stats()
        print(f"   ✓ Index exists")
        print(f"   Current vectors: {stats.get('total_vector_count', 0)}")
    except Exception as e:
        print(f"   Creating index with serverless embeddings...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=1536,
            metric="cosine",
            spec={
                "serverless": {
                    "cloud": "aws",
                    "region": "us-east-1"
                }
            }
        )
        index = pc.Index(INDEX_NAME)
        print(f"   ✓ Created index {INDEX_NAME}")
    
    # Load all MD files
    acts_dir = Path("acts")
    if not acts_dir.exists():
        raise FileNotFoundError(f"Directory '{acts_dir}' not found")
    
    md_files = sorted(acts_dir.glob("*.md"))
    print(f"\n📁 Found {len(md_files)} MD files in acts/")
    
    if not md_files:
        print("❌ No MD files found!")
        return
    
    total_chunks = 0
    vectors = []
    
    # Process each file
    for file_idx, md_file in enumerate(md_files, 1):
        print(f"\n[{file_idx}/{len(md_files)}] {md_file.name}")
        
        # Read file
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        print(f"   Size: {len(content):,} chars")
        
        # Chunk
        chunks = chunk_text(content)
        print(f"   Chunks: {len(chunks)}")
        
        # Prepare vectors
        for chunk_idx, chunk in enumerate(chunks):
            vector_id = f"{md_file.stem}_chunk_{chunk_idx}"
            vectors.append({
                "id": vector_id,
                "values": None,
                "metadata": {
                    "file": md_file.name,
                    "chunk_idx": chunk_idx,
                    "text": chunk[:300],
                    "file_stem": md_file.stem
                },
                "data": chunk
            })
        
        total_chunks += len(chunks)
    
    # Upload vectors
    print(f"\n⬆️  Uploading {total_chunks} chunks to Pinecone...")
    
    batch_size = 100
    for i in range(0, len(vectors), batch_size):
        batch = vectors[i:i+batch_size]
        try:
            index.upsert(
                vectors=[
                    (v["id"], None, v["metadata"]) for v in batch
                ]
            )
            print(f"   ✓ {min(i + batch_size, len(vectors))}/{len(vectors)} uploaded")
        except Exception as e:
            print(f"   ⚠️  Error: {e}")
    
    # Final stats
    print(f"\n✅ Upload complete!")
    print(f"   Total files: {len(md_files)}")
    print(f"   Total chunks: {total_chunks}")
    
    try:
        stats = index.describe_index_stats()
        print(f"   Vectors in index: {stats.get('total_vector_count', 'N/A')}")
    except:
        print(f"   (Check Pinecone dashboard for vector count)")
    
    print(f"\n📊 Ready to query when you have API keys!")

if __name__ == "__main__":
    main()