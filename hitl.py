"""
hitl.py — Human-in-the-Loop routing for Taxova.ai invoices.

Decides whether an extracted invoice can go to the client for CONFIRM,
or must land in the CA review queue first.
"""

from __future__ import annotations

from typing import Any

from processor import validate_gstin

# review_status values
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_AWAITING_CLIENT = "awaiting_client"
STATUS_CLIENT_CONFIRMED = "client_confirmed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

HITL_STATUSES = {
    STATUS_NEEDS_REVIEW,
    STATUS_AWAITING_CLIENT,
    STATUS_CLIENT_CONFIRMED,
    STATUS_APPROVED,
    STATUS_REJECTED,
}


def decide_hitl_route(result: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Inspect extraction + validation output and choose an HITL path.

    Returns:
        (review_status, reasons)
    """
    reasons: list[str] = []
    ext = result.get("extraction") or {}

    if not result.get("is_calculation_correct", True):
        errs = result.get("calculation_errors") or []
        if errs:
            reasons.append(f"Tax math issues: {errs[0]}")
        else:
            reasons.append("Tax calculation mismatch detected")

    supplier_gstin = ext.get("supplier_gstin")
    if supplier_gstin and not validate_gstin(supplier_gstin):
        reasons.append(f"Invalid supplier GSTIN format: {supplier_gstin}")
    if not supplier_gstin:
        reasons.append("Supplier GSTIN missing or unreadable")

    if not (ext.get("invoice_number") or "").strip():
        reasons.append("Invoice number missing")

    if not (ext.get("invoice_date") or "").strip():
        reasons.append("Invoice date missing")

    try:
        grand = float(ext.get("grand_total") or 0)
        if grand <= 0:
            reasons.append("Grand total missing or zero")
    except (TypeError, ValueError):
        reasons.append("Grand total unreadable")

    if not (ext.get("supplier_name") or "").strip():
        reasons.append("Supplier name missing")

    # Blocked ITC is informational — still allow client confirm, but note it
    # (does not force CA queue by itself)

    if reasons:
        return STATUS_NEEDS_REVIEW, reasons

    return STATUS_AWAITING_CLIENT, []


def format_hitl_whatsapp_footer(invoice_id: int, review_status: str, reasons: list[str]) -> str:
    """Extra WhatsApp lines explaining what the human should do next."""
    if review_status == STATUS_NEEDS_REVIEW:
        reason_txt = reasons[0] if reasons else "Extraction needs verification"
        return (
            "\n\n👤 *Human review required*\n"
            f"Reason: {reason_txt}\n"
            "Your CA will verify this on the dashboard.\n"
            f"You can still reply: *REJECT {invoice_id} <reason>* if this bill is wrong."
        )

    if review_status == STATUS_AWAITING_CLIENT:
        return (
            "\n\n👤 *Please confirm*\n"
            f"Reply *CONFIRM {invoice_id}* if details look correct.\n"
            f"Or *REJECT {invoice_id} <reason>* if something is wrong."
        )

    return ""
