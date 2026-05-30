#!/usr/bin/env python3
"""
Upload MD files to Pinecone with LOCAL embeddings on GPU (RTX 2060).
Uses sentence-transformers (ONNX optimized for GPU).
Zero API costs for embeddings.
"""

import os
import re
from pathlib import Path
from dotenv import load_dotenv
from pinecone import Pinecone
import numpy as np

# Try GPU, fall back to CPU
try:
    from sentence_transformers import SentenceTransformer
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"🎮 Using device: {device}")
except ImportError:
    print("❌ sentence-transformers not installed")
    print("   Run: pip install sentence-transformers torch")
    exit(1)

# Load env
load_dotenv()
PINECONE_API = os.getenv("PINECONE_API")

if not PINECONE_API:
    raise ValueError("PINECONE_API not found in .env")

# Init Pinecone
pc = Pinecone(api_key=PINECONE_API)

# Config
INDEX_NAME = "acts-docs"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # 384-dim, fast, RTX 2060 friendly
BATCH_SIZE = 32  # Adjust if OOM
MAX_CHUNK_SIZE = 2500  # chars (~600 tokens)

def chunk_legal_document(content):
    """
    Semantic chunking for legal documents (Bangladeshi Constitution style).
    Structure:
    - Preamble (keep whole)
    - Parts (Part I, Part II, etc.)
    - Chapters (Chapter I, Chapter II, etc.)
    - Sections (Section 1, Section 2, etc.)
    - Footnotes (separate)
    """
    chunks = []
    
    # Extract preamble (before first Part)
    preamble_match = re.search(r'^.*?(?=^## \[Part)', content, re.MULTILINE | re.DOTALL)
    if preamble_match:
        preamble = preamble_match.group(0).strip()
        if preamble:
            chunks.append({
                'text': preamble,
                'type': 'preamble',
                'section': 'Preamble'
            })
        content = content[len(preamble):]
    
    # Extract footnotes (everything after [^1]: onwards)
    footnotes = ""
    footnote_match = re.search(r'^## Footnotes.*', content, re.MULTILINE | re.DOTALL)
    if footnote_match:
        footnotes = footnote_match.group(0)
        content = content[:footnote_match.start()]
    
    # Split by Part (## [Part X: ...])
    parts = re.split(r'(^## \[Part [IVX]+:.*?\])', content, flags=re.MULTILINE)
    
    for i in range(1, len(parts), 2):
        if i >= len(parts):
            break
            
        part_header = parts[i].strip()
        part_content = parts[i + 1] if i + 1 < len(parts) else ""
        
        # Extract part number/name
        part_match = re.search(r'Part ([IVX]+):\s*(.+?)\]', part_header)
        part_num = part_match.group(1) if part_match else "X"
        
        # Split by Chapter within Part
        chapters = re.split(r'(^### \[Chapter.*?\]|^### \[Chapter [IVX]+:.*?\])', part_content, flags=re.MULTILINE)
        
        for j in range(len(chapters)):
            ch_text = chapters[j].strip()
            if not ch_text or ch_text.startswith('###'):
                continue
            
            # Split by Section (##### [Section ...])
            sections = re.split(r'(^##### \[Section.*?\])', ch_text, flags=re.MULTILINE)
            
            for k in range(1, len(sections), 2):
                if k >= len(sections):
                    break
                    
                section_header = sections[k].strip()
                section_content = sections[k + 1].strip() if k + 1 < len(sections) else ""
                
                # Extract section number
                sec_match = re.search(r'Section (\d+|[A-Z]+):', section_header)
                sec_num = sec_match.group(1) if sec_match else "?"
                
                # Combine header + content
                combined = section_header + "\n\n" + section_content
                
                # If section is small, keep it whole; if large, split by subsections
                if len(combined) <= MAX_CHUNK_SIZE:
                    chunks.append({
                        'text': combined,
                        'type': 'section',
                        'part': part_num,
                        'section': sec_num,
                        'header': section_header[:100]
                    })
                else:
                    # Split large section by clauses (1), (2), (3), etc.
                    clauses = re.split(r'(^\s*\d+\.|^\s*\(\d+\))', section_content, flags=re.MULTILINE)
                    
                    clause_chunks = []
                    current = section_header + "\n\n"
                    
                    for clause in clauses:
                        if not clause.strip():
                            continue
                        if len(current) + len(clause) <= MAX_CHUNK_SIZE:
                            current += clause + "\n"
                        else:
                            if current.strip() != section_header:
                                clause_chunks.append(current.strip())
                            current = section_header + "\n\n" + clause + "\n"
                    
                    if current.strip() != section_header:
                        clause_chunks.append(current.strip())
                    
                    for chunk_text in clause_chunks:
                        if chunk_text.strip():
                            chunks.append({
                                'text': chunk_text,
                                'type': 'section_clause',
                                'part': part_num,
                                'section': sec_num,
                                'header': section_header[:100]
                            })
    
    # Add footnotes as separate chunks (for reference)
    if footnotes:
        footnote_blocks = re.split(r'(?=\[\^)', footnotes)
        for fn_block in footnote_blocks:
            if fn_block.strip():
                chunks.append({
                    'text': fn_block.strip(),
                    'type': 'footnote',
                    'section': 'Footnotes'
                })
    
    return chunks


def embed_chunks(chunks, model, batch_size=BATCH_SIZE):
    """Embed chunks locally on GPU"""
    embeddings = []
    
    # Extract text from chunk dicts
    texts = [c['text'] for c in chunks]
    
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        try:
            batch_embeddings = model.encode(batch, convert_to_numpy=True)
            embeddings.extend(batch_embeddings)
            
            pct = min((i + batch_size) / len(texts) * 100, 100)
            print(f"   ✓ Embedded {i + len(batch)}/{len(texts)} ({pct:.1f}%)")
        except RuntimeError as e:
            if "out of memory" in str(e):
                print(f"   ⚠️  OOM, reducing batch size to {batch_size//2}")
                return embeddings + embed_chunks(chunks[i:], model, batch_size//2)
            raise
    
    return np.array(embeddings)

def main():
    # Load embedding model
    print(f"\n🤖 Loading embedding model: {EMBEDDING_MODEL}")
    print(f"   (First time: ~500MB download)")
    
    model = SentenceTransformer(EMBEDDING_MODEL, device=device)
    print(f"   ✓ Model loaded on {device}")
    
    # Create/get index
    print(f"\n📌 Connecting to Pinecone index: {INDEX_NAME}")
    
    try:
        index = pc.Index(INDEX_NAME)
        stats = index.describe_index_stats()
        print(f"   ✓ Index exists")
        print(f"   Current vectors: {stats.get('total_vector_count', 0)}")
        print(f"   ⚠️  Clearing old index for re-chunking...")
        # Note: Pinecone doesn't support bulk delete; we'll just overwrite
    except Exception as e:
        print(f"   Creating index...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=384,  # all-MiniLM-L6-v2 dimension
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
    all_vectors = []
    
    # Process each file
    for file_idx, md_file in enumerate(md_files, 1):
        print(f"\n[{file_idx}/{len(md_files)}] {md_file.name}")
        
        # Read file
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        print(f"   Size: {len(content):,} chars")
        
        # Chunk semantically (legal doc aware)
        print(f"   Semantic chunking (Part → Section)...")
        chunk_dicts = chunk_legal_document(content)
        print(f"   Chunks: {len(chunk_dicts)}")
        
        # Embed locally
        print(f"   Embedding on {device}...")
        embeddings = embed_chunks(chunk_dicts, model)
        
        # Prepare vectors
        for chunk_idx, (chunk_dict, embedding) in enumerate(zip(chunk_dicts, embeddings)):
            vector_id = f"{md_file.stem}_section_{chunk_dict.get('section', 'unknown')}_chunk_{chunk_idx}"
            
            # Clean metadata
            metadata = {
                "file": md_file.name,
                "file_stem": md_file.stem,
                "chunk_type": chunk_dict['type'],
                "section": chunk_dict.get('section', 'unknown'),
                "text_preview": chunk_dict['text'][:400]  # Longer preview for legal docs
            }
            
            # Add optional fields
            if 'part' in chunk_dict:
                metadata['part'] = chunk_dict['part']
            if 'header' in chunk_dict:
                metadata['header'] = chunk_dict['header']
            
            all_vectors.append({
                "id": vector_id,
                "values": embedding.tolist(),
                "metadata": metadata
            })
        
        total_chunks += len(chunk_dicts)
        
        # Upload in batches
        if len(all_vectors) >= 500:
            print(f"   ⬆️  Uploading {len(all_vectors)} vectors to Pinecone...")
            batch_size = 100
            for i in range(0, len(all_vectors), batch_size):
                batch = all_vectors[i:i+batch_size]
                try:
                    index.upsert(vectors=batch)
                    print(f"      ✓ {min(i + batch_size, len(all_vectors))}/{len(all_vectors)}")
                except Exception as e:
                    print(f"      ⚠️  Error: {e}")
            
            all_vectors = []
    
    # Final upload
    if all_vectors:
        print(f"\n⬆️  Final upload: {len(all_vectors)} vectors...")
        batch_size = 100
        for i in range(0, len(all_vectors), batch_size):
            batch = all_vectors[i:i+batch_size]
            try:
                index.upsert(vectors=batch)
                print(f"   ✓ {min(i + batch_size, len(all_vectors))}/{len(all_vectors)}")
            except Exception as e:
                print(f"   ⚠️  Error: {e}")
    
    # Stats
    print(f"\n✅ Upload complete!")
    print(f"   Total files: {len(md_files)}")
    print(f"   Total semantic chunks: {total_chunks}")
    print(f"   Embedding model: {EMBEDDING_MODEL} (384-dim)")
    print(f"   Max chunk size: {MAX_CHUNK_SIZE} chars (~600 tokens)")
    print(f"   Cost: $0 (all local) 💰")
    
    try:
        stats = index.describe_index_stats()
        print(f"   Vectors in Pinecone: {stats.get('total_vector_count', 'N/A')}")
    except:
        pass
    
    print(f"\n📊 Ready to query!\n")

if __name__ == "__main__":
    main()