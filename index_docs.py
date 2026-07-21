"""
index_docs.py — Knowledge Base indexing script for GST Autopilot RAG.

Reads files in the `reference_docs` folder, chunks them, computes embeddings,
and stores them in the SQLite/PostgreSQL `knowledge_base` database table.
"""

import os
import asyncio
import logging
from dotenv import load_dotenv

# Set up logging to console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)-12s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("indexer")

# Load environment configs
load_dotenv()


async def main():
    # Import here to avoid loading imports before logging and environment are ready
    import db
    import rag

    logger.info("Starting Knowledge Base indexing process...")

    # Verify that database tables exist
    await db.init_db()

    # Clear old entries
    logger.info("Clearing old knowledge base entries...")
    rag.clear_knowledge_base()

    # Locate reference docs
    ref_dir = os.path.join(os.getcwd(), "reference_docs")
    if not os.path.exists(ref_dir):
        logger.error("Reference docs directory '%s' does not exist. Please create it first.", ref_dir)
        return

    files = [f for f in os.listdir(ref_dir) if f.endswith(".md") or f.endswith(".txt")]
    if not files:
        logger.warning("No markdown (.md) or text (.txt) files found in %s", ref_dir)
        return

    logger.info("Found %d files to index: %s", len(files), files)

    total_chunks = 0
    for filename in files:
        filepath = os.path.join(ref_dir, filename)
        logger.info("Reading file: %s", filename)
        
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
                
            # Extract H1 title if present, otherwise use filename
            title = filename
            first_line = content.splitlines()[0] if content.splitlines() else ""
            if first_line.startswith("# "):
                title = first_line.replace("# ", "").strip()

            chunks_indexed = await rag.index_document(title=title, text=content)
            logger.info("Successfully indexed %d chunks for document '%s'", chunks_indexed, title)
            total_chunks += chunks_indexed
        except Exception as e:
            logger.exception("Failed to index file %s:", filename)

    logger.info("Seeding completed successfully! Total indexed chunks: %d", total_chunks)


if __name__ == "__main__":
    asyncio.run(main())
