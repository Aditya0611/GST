"""
rag.py — Retrieval-Augmented Generation for Taxova.ai GST knowledge.

Improvements over basic vector RAG:
- Markdown-aware chunking (keeps section headings with each chunk)
- Query expansion for common GST synonyms / section aliases
- Hybrid retrieval: vector similarity + keyword/section boost, then re-rank
- Grounded answer prompt that prefers reference context and cites sources
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from typing import Any, Optional

from google import genai

logger = logging.getLogger("rag")

# ── ChromaDB (lazy init — don't crash app import if Chroma fails on Railway) ──
CHROMA_DB_DIR = os.path.join(os.getenv("STORAGE_DIR", "./storage"), "chromadb")
chroma_client = None
collection = None

COLLECTION_NAME = "gst_knowledge_base"
COLLECTION_VERSION = "v2"  # bump when chunk/metadata schema changes meaningfully


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
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine", "schema": COLLECTION_VERSION},
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
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine", "schema": COLLECTION_VERSION},
    )


# ── Gemini / Groq clients ─────────────────────────────────────────────────────
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

# Retrieval knobs (override via env if needed)
VECTOR_FETCH_MULTIPLIER = int(os.getenv("RAG_FETCH_MULTIPLIER", "4"))
MIN_VECTOR_SIM = float(os.getenv("RAG_MIN_VECTOR_SIM", "0.28"))
MIN_HYBRID_SCORE = float(os.getenv("RAG_MIN_HYBRID_SCORE", "0.32"))
DEFAULT_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "900"))
DEFAULT_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "150"))

# Synonyms / aliases appended to the *embedding query* (not shown to the user)
_QUERY_EXPANSIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bitc\b", re.I), "Input Tax Credit ITC Section 16 Section 17(5)"),
    (re.compile(r"\bblocked credit", re.I), "Section 17(5) blocked ITC ineligible credit"),
    (re.compile(r"\boutdoor catering|food and beverage|restaurant", re.I), "Section 17(5) food beverages outdoor catering blocked ITC"),
    (re.compile(r"\brcm\b|reverse charge", re.I), "Reverse Charge Mechanism RCM"),
    (re.compile(r"\bgstr[\s\-]?1\b", re.I), "GSTR-1 outward supplies filing"),
    (re.compile(r"\bgstr[\s\-]?3b\b", re.I), "GSTR-3B monthly return filing"),
    (re.compile(r"\bgstr[\s\-]?2b\b", re.I), "GSTR-2B auto-drafted ITC statement"),
    (re.compile(r"\bplace of supply\b", re.I), "place of supply IGST CGST SGST"),
    (re.compile(r"\bcomposition\b", re.I), "composition scheme Section 10"),
    (re.compile(r"\be[\s\-]?invoice", re.I), "e-invoicing e-invoice IRN"),
    (re.compile(r"\blut\b|letter of undertaking", re.I), "export LUT bond zero-rated"),
]


def expand_query_for_embedding(query: str) -> str:
    """Light GST synonym expansion to improve embedding recall."""
    q = (query or "").strip()
    if not q:
        return q
    extras: list[str] = []
    for pattern, extra in _QUERY_EXPANSIONS:
        if pattern.search(q):
            extras.append(extra)
    # Always surface explicit section citations if present
    for m in re.finditer(r"\b(?:section|sec\.?)\s*(\d+\s*(?:\([a-z0-9]+\))?)", q, re.I):
        extras.append(f"Section {m.group(1).replace(' ', '')}")
    if not extras:
        return q
    # Dedupe while preserving order
    seen = set()
    uniq = []
    for e in extras:
        key = e.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return f"{q}\n\nRelated GST terms: {'; '.join(uniq)}"


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|\d+\([a-z0-9]+\)|\d+", re.I)
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "been", "being", "with", "as", "by", "from", "at",
    "this", "that", "these", "those", "it", "its", "my", "our", "your", "can",
    "i", "me", "we", "you", "what", "how", "when", "where", "why", "which",
    "do", "does", "did", "please", "tell", "about", "under", "into",
}


def _tokenize(text: str) -> list[str]:
    return [
        t.lower()
        for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOPWORDS and len(t) > 1
    ]


def _keyword_score(query: str, content: str, title: str = "", section: str = "") -> float:
    """
    Simple lexical overlap with boosts for section numbers and title/section hits.
    Returns 0..1-ish score (can slightly exceed 1 before clamp).
    """
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    blob = f"{title}\n{section}\n{content}".lower()
    hits = sum(1 for t in q_tokens if t in blob)
    base = hits / max(len(q_tokens), 1)

    boost = 0.0
    # Exact-ish section patterns from the query
    for m in re.finditer(r"17\s*\(\s*5\s*\)|section\s*17\s*\(?\s*5", query, re.I):
        if "17(5)" in blob or "section 17(5)" in blob or "sec 17(5)" in blob:
            boost += 0.25
            break
    for m in re.finditer(r"section\s*(\d+)", query, re.I):
        sec = m.group(1)
        if f"section {sec}" in blob or f"sec {sec}" in blob:
            boost += 0.12

    title_l = (title or "").lower()
    section_l = (section or "").lower()
    for t in q_tokens:
        if len(t) >= 4 and t in title_l:
            boost += 0.05
        if len(t) >= 4 and t in section_l:
            boost += 0.04

    return min(1.0, base + boost)


def _hybrid_score(vector_sim: float, keyword: float) -> float:
    # Prefer semantic match, but let lexical/section cues break ties
    return (0.65 * max(0.0, vector_sim)) + (0.35 * max(0.0, keyword))


# ── Text Chunking ─────────────────────────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Backward-compatible plain sliding-window chunker (returns strings only)."""
    return [c["content"] for c in chunk_markdown(text, chunk_size=chunk_size, overlap=overlap)]


def chunk_markdown(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """
    Split markdown by headings when possible; keep heading path on each chunk
    so retrieval keeps section context (e.g. 'Blocked Credits > Food').
    """
    if not text or not text.strip():
        return []

    lines = text.replace("\r\n", "\n").split("\n")
    sections: list[tuple[str, list[str]]] = []  # (section_title, body lines)
    current_title = ""
    current_lines: list[str] = []

    heading_re = re.compile(r"^(#{1,4})\s+(.+?)\s*$")

    for line in lines:
        hm = heading_re.match(line)
        if hm:
            if current_lines and any(l.strip() for l in current_lines):
                sections.append((current_title, current_lines))
            level = len(hm.group(1))
            heading_text = hm.group(2).strip()
            # Keep a breadcrumb of the last H1/H2 when nesting
            if level <= 2 or not current_title:
                current_title = heading_text
            else:
                current_title = f"{current_title} > {heading_text}" if current_title else heading_text
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines and any(l.strip() for l in current_lines):
        sections.append((current_title, current_lines))

    if not sections:
        sections = [("", lines)]

    chunks: list[dict[str, Any]] = []
    for section_title, sec_lines in sections:
        body = "\n".join(sec_lines).strip()
        if not body:
            continue
        # Prefix section title inside the stored text for better embeddings
        prefix = f"## {section_title}\n\n" if section_title and not body.lstrip().startswith("#") else ""
        full = (prefix + body).strip()

        if len(full) <= chunk_size:
            chunks.append({"content": full, "section": section_title or ""})
            continue

        start = 0
        text_len = len(full)
        while start < text_len:
            end = min(start + chunk_size, text_len)
            if end < text_len:
                # Prefer break on paragraph, then space
                para = full.rfind("\n\n", start + max(0, chunk_size // 2), end)
                if para != -1:
                    end = para
                else:
                    space_idx = full.rfind(" ", end - 120, end)
                    if space_idx != -1 and space_idx > start:
                        end = space_idx
            piece = full[start:end].strip()
            if piece:
                # Ensure section heading survives mid-section windows
                if section_title and not piece.startswith("#") and start > 0:
                    piece = f"(Section: {section_title})\n{piece}"
                chunks.append({"content": piece, "section": section_title or ""})
            if end >= text_len:
                break
            start = max(end - overlap, start + 1)
            if start >= text_len - 40:
                break

    return chunks


# ── Embeddings ────────────────────────────────────────────────────────────────
async def get_embedding_async(text: str) -> list[float]:
    """Calls Gemini API to generate the vector embedding for the input text asynchronously."""
    if not client:
        raise ValueError("Gemini Client is not configured. Add GEMINI_API_KEY to .env")

    response = await client.aio.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
    )
    if not response.embeddings:
        raise ValueError("No embeddings returned by Gemini API")

    return response.embeddings[0].values


async def index_document(
    title: str,
    text: str,
    *,
    source_file: Optional[str] = None,
) -> int:
    """
    Chunks a text document and saves each chunk with its embedding vector to ChromaDB.
    Returns the number of indexed chunks.
    """
    chunks = chunk_markdown(text)
    logger.info(
        "Indexing document '%s'%s: split into %d chunks",
        title,
        f" ({source_file})" if source_file else "",
        len(chunks),
    )

    ids: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict] = []
    documents: list[str] = []

    for idx, chunk in enumerate(chunks):
        content = chunk["content"]
        section = chunk.get("section") or ""
        embedding_vector = await get_embedding_async(content)
        chunk_id = str(uuid.uuid4())

        ids.append(chunk_id)
        embeddings.append(embedding_vector)
        documents.append(content)
        meta: dict[str, Any] = {
            "title": title,
            "chunk_index": idx,
            "section": section[:200],
            "schema": COLLECTION_VERSION,
        }
        if source_file:
            meta["source_file"] = source_file
        metadatas.append(meta)

    if ids:
        coll = ensure_chroma()
        if coll is None:
            raise RuntimeError("ChromaDB is not available")
        # Chroma add in batches to avoid huge payloads
        batch = 32
        for i in range(0, len(ids), batch):
            coll.add(
                ids=ids[i : i + batch],
                embeddings=embeddings[i : i + batch],
                documents=documents[i : i + batch],
                metadatas=metadatas[i : i + batch],
            )

    return len(chunks)


async def search_knowledge_base(query: str, limit: int = 5) -> list[dict]:
    """
    Hybrid search: embed (expanded) query, fetch a wider vector candidate set,
    re-rank with keyword/section boosts, filter weak matches.
    """
    coll = ensure_chroma()
    if coll is None:
        return []

    q = (query or "").strip()
    if not q:
        return []

    limit = max(1, min(int(limit or 5), 8))
    fetch_n = max(limit * VECTOR_FETCH_MULTIPLIER, 12)

    embed_text = expand_query_for_embedding(q)
    try:
        query_vector = await get_embedding_async(embed_text)
    except Exception as e:
        logger.error("Failed to generate query embedding: %s", e)
        return []

    try:
        # Don't request more than collection size if known
        try:
            total = coll.count()
            if total and total > 0:
                fetch_n = min(fetch_n, total)
        except Exception:
            pass

        results = coll.query(
            query_embeddings=[query_vector],
            n_results=max(fetch_n, limit),
        )

        ranked: list[dict] = []
        if not results.get("ids") or not results["ids"][0]:
            return []

        for i in range(len(results["ids"][0])):
            distance = results["distances"][0][i]
            vector_sim = 1.0 - float(distance)
            meta = results["metadatas"][0][i] or {}
            content = results["documents"][0][i] or ""
            title = meta.get("title") or ""
            section = meta.get("section") or ""
            kw = _keyword_score(q, content, title=title, section=section)
            hybrid = _hybrid_score(vector_sim, kw)

            ranked.append(
                {
                    "id": results["ids"][0][i],
                    "title": title,
                    "section": section,
                    "source_file": meta.get("source_file"),
                    "content": content,
                    "chunk_index": meta.get("chunk_index"),
                    "similarity": vector_sim,
                    "keyword_score": kw,
                    "score": hybrid,
                }
            )

        ranked.sort(key=lambda m: m["score"], reverse=True)

        filtered = [
            m
            for m in ranked
            if m["score"] >= MIN_HYBRID_SCORE
            or (m["similarity"] >= MIN_VECTOR_SIM and m["keyword_score"] >= 0.15)
        ]
        # Always keep the best hit if everything was filtered (avoid empty tool results)
        if not filtered and ranked:
            filtered = ranked[:1]

        top = filtered[:limit]
        logger.info(
            "RAG search q=%r → %d candidates, %d after filter, returning %d (best score=%.3f)",
            q[:80],
            len(ranked),
            len(filtered),
            len(top),
            top[0]["score"] if top else 0.0,
        )
        return top
    except Exception as e:
        logger.error("Error during ChromaDB search: %s", e)
        return []


def _format_context_blocks(matches: list[dict]) -> tuple[str, list[str]]:
    """Build prompt context and a list of citation labels."""
    contexts: list[str] = []
    citations: list[str] = []
    for i, m in enumerate(matches, start=1):
        title = m.get("title") or "GST reference"
        section = (m.get("section") or "").strip()
        label = f"[{i}] {title}" + (f" — {section}" if section else "")
        citations.append(label)
        score = m.get("score", m.get("similarity", 0))
        contexts.append(
            f"--- SOURCE {label} (relevance {float(score):.2f}) ---\n{m.get('content') or ''}"
        )
    return "\n\n".join(contexts), citations


async def answer_query(query: str) -> str:
    """
    Retrieve grounded GST context and generate an answer that prefers references.
    """
    matches = await search_knowledge_base(query, limit=5)
    # Drop very weak matches for answer generation
    strong = [m for m in matches if float(m.get("score") or 0) >= MIN_HYBRID_SCORE]
    if not strong:
        strong = matches[:2]

    context_str, citations = _format_context_blocks(strong)
    if not context_str:
        context_str = "No relevant official reference documents found."

    cite_line = "; ".join(citations) if citations else "none"

    system = (
        "You are Taxova.ai's Indian GST assistant. "
        "Answer using the REFERENCE CONTEXT when it is relevant. "
        "If the context is insufficient, say what is missing and give only cautious "
        "general guidance — never invent section numbers, rates, or case outcomes. "
        "Be concise and practical for WhatsApp/CA use. "
        "When you use a source, mention it briefly (document/section title)."
    )

    prompt = f"""REFERENCE CONTEXT (from Taxova GST knowledge base):
{context_str}

SOURCES AVAILABLE: {cite_line}

USER QUERY:
{query}

Instructions:
1. Prefer facts from the reference context over general knowledge.
2. If context does not cover the question, say so clearly.
3. Do not fabricate GST sections or penalties.
4. End with a one-line caveat: this is guidance, not legal advice / CA review may be needed for filing decisions.
"""

    try:
        use_groq = AGENT_LLM_PROVIDER == "groq" and _groq_client is not None
        if use_groq:
            logger.info("Generating RAG answer with Groq for query: '%s'", query[:60])

            def _call():
                return _groq_client.chat.completions.create(
                    model=GROQ_GENERATION_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.15,
                )

            response = await asyncio.to_thread(_call)
            return (response.choices[0].message.content or "").strip()

        if not client:
            return "AI is not configured. Add GROQ_API_KEY or GEMINI_API_KEY to .env."

        logger.info("Generating RAG answer with Gemini for query: '%s'", query[:60])
        response = await client.aio.models.generate_content(
            model=GENERATION_MODEL,
            contents=f"{system}\n\n{prompt}",
        )
        return (response.text or "").strip()
    except Exception as e:
        logger.exception("Failed to generate answer:")
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
            return (
                "I'm temporarily at AI capacity. "
                "Please wait about a minute and try again."
            )
        return "Sorry, I couldn't answer that right now. Please try again in a moment."
