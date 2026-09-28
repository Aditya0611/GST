"""
billing.py — Plan catalog + Razorpay order helpers for Taxova live checkout.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

logger = logging.getLogger("billing")

# Amounts in paise (INR × 100)
PLANS: dict[str, dict[str, Any]] = {
    "starter": {
        "id": "starter",
        "name": "Starter",
        "tagline": "Single business",
        "who": "1 GSTIN / WhatsApp client",
        "amount_paise": 99900,  # ₹999
        "amount_display": "₹999",
        "period": "month",
        "client_cap": 1,
        "seat_limit": 2,
        "monthly_invoice_cap": 200,
        "features": [
            "Invoice photos & PDFs on WhatsApp",
            "AI extraction + ITC checks",
            "Client CONFIRM / REJECT",
            "CA dashboard for that GSTIN",
            "Email support",
        ],
    },
    "growth": {
        "id": "growth",
        "name": "Growth",
        "tagline": "CA firms & growing practices",
        "who": "Multi-client workspace",
        "amount_paise": 199900,  # ₹1,999
        "amount_display": "₹1,999",
        "period": "month",
        "client_cap": 100,
        "seat_limit": 10,
        "monthly_invoice_cap": 2000,
        "features": [
            "Everything in Starter",
            "Multi-client CA dashboard",
            "CA review / audit queue",
            "GSTR-2B import / pull",
            "GSTR-1 & 3B draft exports",
            "WhatsApp month-end nudges",
            "Priority support",
        ],
    },
}


def get_plan(plan_id: str) -> dict[str, Any] | None:
    key = (plan_id or "").strip().lower()
    return PLANS.get(key)


def list_plans() -> list[dict[str, Any]]:
    return [PLANS["starter"], PLANS["growth"]]


def razorpay_configured() -> bool:
    return bool(
        (os.getenv("RAZORPAY_KEY_ID") or "").strip()
        and (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
    )


def get_razorpay_key_id() -> str:
    return (os.getenv("RAZORPAY_KEY_ID") or "").strip()


def create_razorpay_order(
    *,
    amount_paise: int,
    receipt: str,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a Razorpay order. Raises RuntimeError if keys missing or API fails."""
    key_id = get_razorpay_key_id()
    key_secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
    if not key_id or not key_secret:
        raise RuntimeError(
            "Razorpay is not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET."
        )

    try:
        import razorpay
    except ImportError as e:
        raise RuntimeError(
            "razorpay package not installed. Run: pip install razorpay"
        ) from e

    client = razorpay.Client(auth=(key_id, key_secret))
    payload = {
        "amount": int(amount_paise),
        "currency": "INR",
        "receipt": receipt[:40],
        "notes": notes or {},
        "payment_capture": 1,
    }
    order = client.order.create(data=payload)
    return dict(order)


def verify_payment_signature(
    *,
    order_id: str,
    payment_id: str,
    signature: str,
) -> bool:
    """Verify Razorpay checkout signature (HMAC SHA256)."""
    key_secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
    if not key_secret or not order_id or not payment_id or not signature:
        return False
    body = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(
        key_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())
