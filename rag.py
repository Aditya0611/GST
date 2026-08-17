"""
rag.py — Lightweight Retrieval-Augmented Generation (RAG) system from scratch.

Provides functions for chunking reference documents, generating vector embeddings
using the Google GenAI SDK, computing cosine similarity, performing retrieval,
and drafting context-enriched answers with Gemini.
"""

import os
import json
import math
import asyncio
import logging
import uuid
from google import genai
import db

logger = logging.getLogger("rag")

# ── ChromaDB (lazy init — don't crash app import if Chroma fails on Railway) ──
CHROMA_DB_DIR = os.path.join(os.getenv("STORAGE_DIR", "./storage"), "chromadb")
chroma_client = None
collection = None


def ensure_chroma():
    """Initialize ChromaDB on first use. Returns collection or None."""
    global chroma_client, collection
    if collection is not None:
        return collection
    try:
        import chromadb

        os.makedirs(CHROMA_DB_DIR, exist_ok=True)
        chroma_client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
        collection = chroma_client.get_or_create_collection(
            name="gst_knowledge_base",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB ready at %s", CHROMA_DB_DIR)
        return collection
    except Exception as e:
        logger.exception("ChromaDB init failed (RAG disabled until fixed): %s", e)
        chroma_client = None
        collection = None
        return None


def clear_knowledge_base():
    """Clears all documents from the ChromaDB collection by dropping and recreating it."""
    global collection
    coll = ensure_chroma()
    if coll is None or chroma_client is None:
        return
    try:
        chroma_client.delete_collection("gst_knowledge_base")
    except Exception:
        pass
    collection = chroma_client.get_or_create_collection(
        name="gst_knowledge_base",
        metadata={"hnsw:space": "cosine"},
    )


# ── Gemini Client Configuration ───────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
AGENT_LLM_PROVIDER = os.getenv("AGENT_LLM_PROVIDER", "groq").lower().strip()

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    client = None
    logger.warning("GEMINI_API_KEY not found in environment. RAG embeddings may fail.")

_groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq

        _groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        logger.warning("Failed to init Groq for RAG answers: %s", e)

EMBEDDING_MODEL = "models/gemini-embedding-2"
GENERATION_MODEL = "models/gemini-2.5-flash"
GROQ_GENERATION_MODEL = os.getenv("AGENT_MODEL", "qwen/qwen3.6-27b")


# ── Text Chunking ─────────────────────────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """
    Chunks text using a sliding window. Attempts to align boundaries on word spaces.
    """
    if not text:
        return []
        
    chunks = []
    start = 0
    text_len = len(text)
    
    while start < text_len:
        end = start + chunk_size
        if end < text_len:
            # Look back to find a space or newline to avoid cutting words
            space_idx = text.rfind(' ', end - 100, end)
            if space_idx != -1:
                end = space_idx
                
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
            
        start = end - overlap
        # Prevent infinite loop or very small trailing chunk
        if start >= text_len - 50:
            break
            
    return chunks


# ── RAG Workflows ─────────────────────────────────────────────────────────────
async def get_embedding_async(text: str) -> list[float]:
    """
    Calls Gemini API to generate the vector embedding for the input text asynchronously.
    """
    if not client:
        raise ValueError("Gemini Client is not configured. Add GEMINI_API_KEY to .env")
        
    response = await client.aio.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text
    )
    if not response.embeddings:
        raise ValueError("No embeddings returned by Gemini API")
        
    return response.embeddings[0].values


async def index_document(title: str, text: str) -> int:
    """
    Chunks a text document and saves each chunk with its embedding vector to ChromaDB.
    Returns the number of indexed chunks.
    """
    chunks = chunk_text(text)
    logger.info("Indexing document '%s': split into %d chunks", title, len(chunks))
    
    ids = []
    embeddings = []
    metadatas = []
    documents = []

    for idx, chunk in enumerate(chunks):
        embedding_vector = await get_embedding_async(chunk)
        chunk_id = str(uuid.uuid4())
        
        ids.append(chunk_id)
        embeddings.append(embedding_vector)
        documents.append(chunk)
        metadatas.append({"title": title, "chunk_index": idx})
        
    if ids:
        coll = ensure_chroma()
        if coll is None:
            raise RuntimeError("ChromaDB is not available")
        coll.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas
        )
        
    return len(chunks)


async def search_knowledge_base(query: str, limit: int = 3) -> list[dict]:
    """
    Embeds the search query and searches ChromaDB for the top K most relevant matches.
    """
    coll = ensure_chroma()
    if coll is None:
        return []

    try:
        query_vector = await get_embedding_async(query)
    except Exception as e:
        logger.error("Failed to generate query embedding: %s", e)
        return []
        
    try:
        # Query chroma
        results = coll.query(
            query_embeddings=[query_vector],
            n_results=limit
        )
        
        ranked_chunks = []
        if results['ids'] and len(results['ids'][0]) > 0:
            for i in range(len(results['ids'][0])):
                # Chroma cosine distance is returned, similarity = 1 - distance
                distance = results['distances'][0][i]
                sim = 1.0 - distance
                
                ranked_chunks.append({
                    "id": results['ids'][0][i],
                    "title": results['metadatas'][0][i]["title"],
                    "content": results['documents'][0][i],
                    "chunk_index": results['metadatas'][0][i]["chunk_index"],
                    "similarity": sim
                })
                
        return ranked_chunks
    except Exception as e:
        logger.error("Error during ChromaDB search: %s", e)
        return []


async def answer_query(query: str) -> str:
    """
    Executes a vector search over the knowledge base, constructs a prompt
    with retrieved contexts, and uses Gemini to answer the query.
    """
    if not client:
        return "RAG system not initialized. GEMINI_API_KEY is missing."
        
    # Search for top 3 matching chunks
    matches = await search_knowledge_base(query, limit=3)
    
    # Compile matching text contexts
    contexts = []
    for m in matches:
        # Include matches with a reasonable similarity threshold
        if m["similarity"] > 0.35:
            contexts.append(f"--- Context from '{m['title']}': ---\n{m['content']}")
            
    context_str = "\n\n".join(contexts) if contexts else "No relevant official reference documents found."
    
    # Build prompt
    prompt = f"""You are a helpful and professional AI GST Assistant for Indian GST taxation, embedded inside a tax automation app called Taxova.ai.

Your task is to answer the user query as accurately as possible. 

Use the following context extracted from official GST reference documentation to draft your answer. 
If the context contains relevant information, prioritize it. If the context does not contain enough information, use your general knowledge of Indian GST rules to provide a helpful answer, but clearly state what is or isn't explicitly found in the reference documents.

--- REFERENCE CONTEXT ---
{context_str}
------------------------

USER QUERY:
{query}

ANSWER:
"""

    try:
        use_groq = AGENT_LLM_PROVIDER == "groq" and _groq_client is not None
        if use_groq:
            logger.info("Generating RAG answer with Groq for query: '%s'", query[:60])

            def _call():
                return _groq_client.chat.completions.create(
                    model=GROQ_GENERATION_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a helpful Taxova.ai assistant for Indian GST. "
                                "Use the reference context when relevant; otherwise use general GST knowledge."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )

            response = await asyncio.to_thread(_call)
            return (response.choices[0].message.content or "").strip()

        if not client:
            return "AI is not configured. Add GROQ_API_KEY or GEMINI_API_KEY to .env."

        logger.info("Generating RAG answer with Gemini for query: '%s'", query[:60])
        response = await client.aio.models.generate_content(
            model=GENERATION_MODEL,
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        logger.exception("Failed to generate answer:")
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
            return (
                "I'm temporarily at AI capacity. "
                "Please wait about a minute and try again."
            )
        return "Sorry, I couldn't answer that right now. Please try again in a moment."
