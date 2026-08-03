"""Build WhatsApp month-close nudges for clients (missing / unmatched bills)."""
from __future__ import annotations

from calendar import month_abbr


def month_label(return_period: str) -> str:
    s = (return_period or "").strip()
    if len(s) >= 7 and s[4] == "-":
        try:
            y, m = int(s[:4]), int(s[5:7])
            if 1 <= m <= 12:
                return f"{month_abbr[m]} {y}"
        except ValueError:
            pass
    return s or "this period"


def _inv_ref(inv: dict) -> str:
    num = (inv.get("invoice_number") or "").strip()
    if num:
        return f"#{num}"
    return f"ID {inv.get('id')}"


def build_month_nudge_message(
    *,
    client_name: str | None,
    return_period: str,
    invoices: list[dict],
) -> dict:
    """
    Compose a WhatsApp-friendly nudge from month invoices.
    Focus: unmatched / mismatch vs GSTR-2B, plus still-pending uploads cue.
    """
    period = (return_period or "").strip()
    label = month_label(period)
    name = (client_name or "there").strip() or "there"

    unmatched: list[dict] = []
    mismatch: list[dict] = []
    pending: list[dict] = []

    for inv in invoices or []:
        review = (inv.get("review_status") or "").strip()
        approved = inv.get("is_approved") in (True, 1, "1")
        if review not in ("rejected", "skipped", "approved") and not approved:
            pending.append(inv)
        m2b = (inv.get("gstr2b_match_status") or "none").strip().lower()
        if m2b == "unmatched":
            unmatched.append(inv)
        elif m2b == "mismatch":
            mismatch.append(inv)

    lines = [
        f"Hi {name} 👋",
        f"Quick check for *{label}* GST:",
    ]

    if unmatched:
        n = len(unmatched)
        lines.append(f"• {n} bill(s) not found in GSTR-2B yet")
        sample = ", ".join(_inv_ref(i) for i in unmatched[:5])
        more = f" (+{n - 5} more)" if n > 5 else ""
        lines.append(f"  Please re-send or confirm: {sample}{more}")

    if mismatch:
        n = len(mismatch)
        lines.append(f"• {n} bill(s) differ from GSTR-2B (amount/date)")
        sample = ", ".join(_inv_ref(i) for i in mismatch[:5])
        more = f" (+{n - 5} more)" if n > 5 else ""
        lines.append(f"  Please check with supplier: {sample}{more}")

    if not unmatched and not mismatch:
        if pending:
            lines.append(
                f"• {len(pending)} bill(s) still with your CA for review — "
                "reply here if you have clearer photos/PDFs."
            )
        else:
            lines.append(
                f"• No open GSTR-2B gaps for {label}. "
                "If any invoice is missing, please send the photo/PDF here."
            )
    else:
        lines.append("Reply in this chat with the missing/clearer invoice photos or PDFs.")

    lines.append("Thank you — Taxova.ai")
    message = "\n".join(lines)

    return {
        "return_period": period,
        "month_label": label,
        "message": message,
        "counts": {
            "unmatched": len(unmatched),
            "mismatch": len(mismatch),
            "pending": len(pending),
            "bills": len(invoices or []),
        },
        "unmatched_refs": [_inv_ref(i) for i in unmatched[:10]],
        "mismatch_refs": [_inv_ref(i) for i in mismatch[:10]],
        "should_nudge": bool(unmatched or mismatch or pending),
    }
