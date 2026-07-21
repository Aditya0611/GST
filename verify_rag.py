"""
verify_rag.py — Script to verify the RAG system embeddings and search/answers.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

async def test_rag():
    import db
    import rag

    print("--- Testing Database Connection ---")
    chunks_count = rag.collection.count()
    print(f"Total chunks in DB: {chunks_count}")
    if not chunks_count:
        print("Database is empty. Please run: python index_docs.py")
        sys.exit(1)

    print("\n--- Testing Vector Search ---")
    query = "Is outdoor catering for an office party eligible for Input Tax Credit?"
    print(f"Query: '{query}'")
    matches = await rag.search_knowledge_base(query, limit=2)
    print(f"Matches found: {len(matches)}")
    for i, m in enumerate(matches):
        print(f" {i+1}. Doc: '{m['title']}' | Similarity: {m['similarity']:.4f}")
        print(f"    Content snippet: {m['content'][:150]}...")

    print("\n--- Testing Gemini RAG Generation ---")
    answer = await rag.answer_query(query)
    print("Answer generated:")
    print(answer)
    print("\nVerification completed successfully!")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(test_rag())
