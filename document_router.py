"""
document_router.py — Classify WhatsApp uploads as GST invoice vs Form 16 (Phase 1).

Uses client text hints + filename + cheap PDF/text heuristics (no LLM).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("document_router")

DOC_GST = "gst_invoice"
DOC_FORM16 = "form16"
DOC_UNKNOWN = "unknown"

_FORM16_HINTS = (
    "form 16",
    "form16",
    "form-16",
    "form no 16",
    "form no. 16",
    "itr",
    "income tax",
    "salary certificate",
    "tds certificate",
    "tds on salary",
)

_GST_HINTS = (
    "gst bill",
    "gst invoice",
    "tax invoice",
    "purchase bill",
    "purchase invoice",
    "expense bill",
    "vendor bill",
    "invoice bill",
)

_FORM16_TEXT_MARKERS = (
    ("form no. 16", 3),
    ("form no 16", 3),
    ("certificate under section 203", 3),
    ("section 203", 2),
    ("tds on salary", 2),
    ("tax deducted at source on salary", 3),
    ("part b summary", 2),
    ("gross salary", 2),
    ("total tax deducted", 2),
    ("deductions under chapter vi-a", 2),
    ("permanent account number", 1),
)

_GST_TEXT_MARKERS = (
    ("tax invoice", 3),
    ("gstin", 2),
    ("cgst", 1),
    ("sgst", 1),
    ("igst", 1),
    ("hsn", 1),
    ("sac", 1),
    ("place of supply", 2),
    ("taxable value", 1),
    ("grand total", 1),
    ("input tax credit", 2),
    ("reverse charge", 2),
)


def _read_pdf_text(file_path: str) -> str:
    """Extract readable text from a PDF (decrypts storage files when needed)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    try:
        import storage as storage_mod

        with storage_mod.plaintext_temp_file(file_path) as tmp:
            reader = PdfReader(tmp)
            parts = []
            for page in reader.pages[:5]:
                parts.append(page.extract_text() or "")
            return re.sub(r"\s+", " ", " ".join(parts)).lower()
    except Exception as exc:
        logger.debug("PDF text extract failed for %s: %s", file_path, exc)
        return ""


def _read_text_snippet(file_path: str, max_bytes: int = 250_000) -> str:
    """Best-effort text for PDFs/images — PDF parse or ASCII runs from file bytes."""
    path_lower = (file_path or "").lower()
    if path_lower.endswith(".pdf") or path_lower.endswith(".pdf.enc"):
        pdf_text = _read_pdf_text(file_path)
        if pdf_text:
            return pdf_text

    try:
        import storage as storage_mod

        raw = storage_mod.read_file_bytes(file_path)
    except Exception:
        try:
            with open(file_path, "rb") as f:
                raw = f.read(max_bytes)
        except OSError:
            return ""

    if len(raw) > max_bytes:
        raw = raw[:max_bytes]

    # PDF literal strings often appear as (text) or plain ASCII in stream
    chunks = re.findall(rb"[\x20-\x7e]{4,}", raw)
    text = " ".join(c.decode("ascii", errors="ignore") for c in chunks[:400])
    return re.sub(r"\s+", " ", text).lower()


def _score_markers(text: str, markers: tuple[tuple[str, int], ...]) -> int:
    score = 0
    for phrase, weight in markers:
        if phrase in text:
            score += weight
    return score


def _hint_score(hint: Optional[str], doc_type: str) -> int:
    if not hint:
        return 0
    h = hint.strip().lower()
    if doc_type == DOC_FORM16:
        return 4 if any(k in h for k in _FORM16_HINTS) else 0
    if doc_type == DOC_GST:
        return 3 if any(k in h for k in _GST_HINTS) else 0
    return 0


def _filename_score(filename: Optional[str]) -> tuple[int, int]:
    name = (filename or "").lower()
    f16 = 0
    gst = 0
    if any(k in name for k in ("form16", "form_16", "form-16", "form 16")):
        f16 += 4
    elif re.search(r"form[^\w]*16|16[^\w]*form", name):
        f16 += 3
    if any(k in name for k in ("tds", "salary", "itr")):
        f16 += 2
    if any(k in name for k in ("invoice", "bill", "gst", "purchase", "expense")):
        gst += 2
    return f16, gst


def classify_incoming_document(
    file_path: str,
    *,
    original_filename: Optional[str] = None,
    hint: Optional[str] = None,
) -> dict[str, Any]:
    """
    Returns:
        doc_type: gst_invoice | form16 | unknown
        confidence: high | medium | low
        method: hint | text | filename | mixed
        scores: {form16, gst}
    """
    text = _read_text_snippet(file_path)
    f16 = _score_markers(text, _FORM16_TEXT_MARKERS)
    gst = _score_markers(text, _GST_TEXT_MARKERS)

    fn_f16, fn_gst = _filename_score(original_filename)
    f16 += fn_f16
    gst += fn_gst

    f16 += _hint_score(hint, DOC_FORM16)
    gst += _hint_score(hint, DOC_GST)

    method_parts = []
    if hint and (_hint_score(hint, DOC_FORM16) or _hint_score(hint, DOC_GST)):
        method_parts.append("hint")
    if fn_f16 or fn_gst:
        method_parts.append("filename")
    if f16 or gst:
        method_parts.append("text")
    method = "+".join(method_parts) if method_parts else "none"

    scores = {"form16": f16, "gst": gst}

    if f16 >= 4 and f16 >= gst + 2:
        return {
            "doc_type": DOC_FORM16,
            "confidence": "high" if f16 >= 6 else "medium",
            "method": method,
            "scores": scores,
        }
    if gst >= 3 and gst >= f16 + 2:
        return {
            "doc_type": DOC_GST,
            "confidence": "high" if gst >= 5 else "medium",
            "method": method,
            "scores": scores,
        }
    if f16 >= 3 and f16 > gst:
        return {
            "doc_type": DOC_FORM16,
            "confidence": "medium",
            "method": method,
            "scores": scores,
        }
    if gst >= 2 and gst > f16:
        return {
            "doc_type": DOC_GST,
            "confidence": "medium",
            "method": method,
            "scores": scores,
        }
    if hint and _hint_score(hint, DOC_FORM16) and f16 >= 1:
        return {
            "doc_type": DOC_FORM16,
            "confidence": "medium",
            "method": method,
            "scores": scores,
        }
    if hint and _hint_score(hint, DOC_GST):
        return {
            "doc_type": DOC_GST,
            "confidence": "medium",
            "method": method,
            "scores": scores,
        }

    return {
        "doc_type": DOC_UNKNOWN,
        "confidence": "low",
        "method": method,
        "scores": scores,
    }


def parse_doc_intent_from_text(text: str) -> Optional[str]:
    """If user text sets intent for the *next* upload, return doc type."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if any(k in t for k in _FORM16_HINTS):
        return DOC_FORM16
    if re.match(r"^2$", t) or t in ("form 16", "form16", "sixteen"):
        return DOC_FORM16
    if any(k in t for k in _GST_HINTS) or re.match(r"^1$", t):
        return DOC_GST
    if "invoice" in t or "bill" in t or "gst" in t:
        return DOC_GST
    return None


def parse_doc_choice_reply(text: str) -> Optional[str]:
    """Reply to '1 = GST, 2 = Form 16' prompt."""
    t = re.sub(r"[^\w\s]", "", (text or "").strip().lower())
    if t in ("1", "gst", "bill", "invoice", "gst bill", "gst invoice"):
        return DOC_GST
    if t in ("2", "form16", "form 16", "itr", "salary", "form sixteen"):
        return DOC_FORM16
    return None
