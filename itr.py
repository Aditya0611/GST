"""
itr.py — Phase 1 Income Tax prep helpers (ITR-1 style estimate).

Not e-filing. Produces rough old vs new regime estimates from Form 16
extraction or manual fields for CA review.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Rough FY 2025-26 style slabs (indicative for product demo — not legal advice)
NEW_REGIME_SLABS = [
    (400_000, 0.00),
    (800_000, 0.05),
    (1_200_000, 0.10),
    (1_600_000, 0.15),
    (2_000_000, 0.20),
    (2_400_000, 0.25),
    (float("inf"), 0.30),
]

OLD_REGIME_SLABS = [
    (250_000, 0.00),
    (500_000, 0.05),
    (1_000_000, 0.20),
    (float("inf"), 0.30),
]

STANDARD_DEDUCTION_NEW = 75_000
STANDARD_DEDUCTION_OLD = 50_000
CESS_RATE = 0.04
REBATE_87A_NEW_LIMIT = 1_200_000  # taxable income ceiling for full rebate (simplified)
REBATE_87A_OLD_LIMIT = 500_000


def validate_pan(pan: str) -> bool:
    return bool(PAN_RE.match((pan or "").strip().upper()))


def _tax_from_slabs(taxable: float, slabs: list[tuple[float, float]]) -> float:
    if taxable <= 0:
        return 0.0
    tax = 0.0
    prev = 0.0
    remaining = taxable
    for upper, rate in slabs:
        band = min(remaining, upper - prev) if upper != float("inf") else remaining
        if band <= 0:
            break
        tax += band * rate
        remaining -= band
        prev = upper
        if remaining <= 0:
            break
    return round(tax, 2)


def compute_estimate(
    *,
    gross_salary: float = 0.0,
    exemptions: float = 0.0,
    other_income: float = 0.0,
    deductions_80c: float = 0.0,
    tds: float = 0.0,
    advance_tax: float = 0.0,
) -> dict[str, Any]:
    """
    Simplified ITR-1 style estimate.
    Old regime: std deduction + chapter VI-A (capped 1.5L for 80C bucket).
    New regime: higher std deduction, no 80C.
    """
    gross_salary = max(0.0, float(gross_salary or 0))
    exemptions = max(0.0, float(exemptions or 0))
    other_income = max(0.0, float(other_income or 0))
    deductions_80c = min(150_000.0, max(0.0, float(deductions_80c or 0)))
    tds = max(0.0, float(tds or 0))
    advance_tax = max(0.0, float(advance_tax or 0))

    salary_after_exempt = max(0.0, gross_salary - exemptions)

    # New regime
    taxable_new = max(0.0, salary_after_exempt - STANDARD_DEDUCTION_NEW + other_income)
    tax_new = _tax_from_slabs(taxable_new, NEW_REGIME_SLABS)
    rebate_new = tax_new if taxable_new <= REBATE_87A_NEW_LIMIT else 0.0
    tax_new_after_rebate = max(0.0, tax_new - rebate_new)
    cess_new = round(tax_new_after_rebate * CESS_RATE, 2)
    net_new = round(tax_new_after_rebate + cess_new, 2)

    # Old regime
    taxable_old = max(
        0.0,
        salary_after_exempt - STANDARD_DEDUCTION_OLD - deductions_80c + other_income,
    )
    tax_old = _tax_from_slabs(taxable_old, OLD_REGIME_SLABS)
    rebate_old = tax_old if taxable_old <= REBATE_87A_OLD_LIMIT else 0.0
    tax_old_after_rebate = max(0.0, tax_old - rebate_old)
    cess_old = round(tax_old_after_rebate * CESS_RATE, 2)
    net_old = round(tax_old_after_rebate + cess_old, 2)

    prepaid = tds + advance_tax
    payable_new = round(net_new - prepaid, 2)
    payable_old = round(net_old - prepaid, 2)

    preferred = "new" if net_new <= net_old else "old"

    return {
        "gross_salary": round(gross_salary, 2),
        "exemptions": round(exemptions, 2),
        "other_income": round(other_income, 2),
        "deductions_80c": round(deductions_80c, 2),
        "tds": round(tds, 2),
        "advance_tax": round(advance_tax, 2),
        "standard_deduction_new": STANDARD_DEDUCTION_NEW,
        "standard_deduction_old": STANDARD_DEDUCTION_OLD,
        "taxable_new": round(taxable_new, 2),
        "taxable_old": round(taxable_old, 2),
        "tax_new": tax_new,
        "tax_old": tax_old,
        "rebate_new": round(rebate_new, 2),
        "rebate_old": round(rebate_old, 2),
        "cess_new": cess_new,
        "cess_old": cess_old,
        "net_tax_new": net_new,
        "net_tax_old": net_old,
        "prepaid": round(prepaid, 2),
        "payable_or_refund_new": payable_new,
        "payable_or_refund_old": payable_old,
        "regime_preferred": preferred,
        "disclaimer": (
            "Indicative estimate for CA review only — not a filed return. "
            "Verify slabs/deductions for the selected FY before filing on incometax.gov.in."
        ),
    }


async def extract_form16_fields(file_path: str) -> dict[str, Any]:
    """
    Vision/LLM extract of Form 16 key fields.
    Returns dict with pan, employee_name, gross_salary, exemptions, tds, etc.
    Raises RuntimeError if AI unavailable / parse fails.
    """
    import storage as storage_mod

    prompt = (
        "Extract Indian Form 16 (salary TDS certificate) fields into ONE JSON object.\n"
        "Keys (use numbers for amounts, null if missing):\n"
        "pan, employee_name, employer_name, financial_year (YYYY-YY),\n"
        "gross_salary, exemptions_under_10, standard_deduction,\n"
        "deductions_80c, total_tds, other_income.\n"
        "Return only valid JSON, no markdown."
    )

    provider = os.getenv("INVOICE_LLM_PROVIDER", "gemini").lower()
    text = None

    with storage_mod.plaintext_temp_file(file_path) as plain:
        ext = os.path.splitext(plain.lower())[1]
        # Prefer Gemini for PDFs
        if provider == "groq" and ext == ".pdf":
            provider = "gemini"

        if provider == "groq":
            text = await _extract_with_groq(plain, prompt)
        else:
            text = await _extract_with_gemini(plain, prompt)

    if not text:
        raise RuntimeError("Form 16 extraction returned empty response.")

    data = _parse_json_object(text)
    return _normalize_form16(data)


async def _extract_with_gemini(plain_path: str, prompt: str) -> str:
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured for Form 16 extraction.")

    client = genai.Client(api_key=api_key)
    model = os.getenv("GEMINI_AGENT_MODEL", "models/gemini-2.5-flash")
    uploaded = client.files.upload(file=plain_path)
    response = client.models.generate_content(
        model=model,
        contents=[uploaded, prompt],
        config=types.GenerateContentConfig(temperature=0.1),
    )
    return (response.text or "").strip()


async def _extract_with_groq(plain_path: str, prompt: str) -> str:
    import base64
    from groq import Groq

    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not configured for Form 16 extraction.")

    with open(plain_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = os.path.splitext(plain_path.lower())[1]
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
        ext, "image/jpeg"
    )
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="qwen/qwen3.6-27b",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        temperature=0.1,
    )
    return (response.choices[0].message.content or "").strip()


def _parse_json_object(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", t)
        if not m:
            raise RuntimeError("Could not parse Form 16 JSON from model output.")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise RuntimeError("Form 16 extraction did not return a JSON object.")
    return data


def _coerce_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_form16(data: dict) -> dict[str, Any]:
    pan = (data.get("pan") or "").strip().upper()
    fy = (data.get("financial_year") or "").strip()
    # Normalize FY like 2025-26
    if re.match(r"^\d{4}-\d{2}$", fy):
        pass
    elif re.match(r"^\d{4}-\d{4}$", fy):
        a, b = fy.split("-")
        fy = f"{a}-{b[-2:]}"
    return {
        "pan": pan if validate_pan(pan) else pan,
        "employee_name": (data.get("employee_name") or "").strip() or None,
        "employer_name": (data.get("employer_name") or "").strip() or None,
        "financial_year": fy or None,
        "gross_salary": _coerce_float(data.get("gross_salary")),
        "exemptions": _coerce_float(data.get("exemptions_under_10")),
        "standard_deduction": _coerce_float(data.get("standard_deduction")),
        "deductions_80c": _coerce_float(data.get("deductions_80c")),
        "tds": _coerce_float(data.get("total_tds")),
        "other_income": _coerce_float(data.get("other_income")),
        "raw": data,
    }


def build_export_html(client: dict, itr: dict, estimate: dict) -> str:
    name = client.get("name") or "Client"
    phone = client.get("phone_number") or ""
    pan = itr.get("pan") or client.get("pan") or "—"
    fy = itr.get("financial_year") or "—"
    preferred = estimate.get("regime_preferred", "new").upper()
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>ITR Prep Draft — {name} — FY {fy}</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;max-width:720px;margin:2rem auto;color:#111;}}
h1{{font-size:1.4rem;}} .muted{{color:#666;font-size:.9rem;}}
table{{width:100%;border-collapse:collapse;margin:1rem 0;}}
td,th{{border:1px solid #ddd;padding:.5rem .75rem;text-align:left;}}
th{{background:#f5f5f5;}} .badge{{display:inline-block;padding:.2rem .5rem;background:#e6f7f3;border-radius:4px;}}
.disclaimer{{margin-top:2rem;padding:1rem;background:#fff8e6;border:1px solid #f0d78c;font-size:.85rem;}}
@media print{{button{{display:none;}}}}
</style></head><body>
<button onclick="window.print()">Print / Save PDF</button>
<h1>Taxova.ai — Income Tax Prep Draft</h1>
<p class="muted">Not filed. For CA review before submitting on incometax.gov.in.</p>
<p><strong>{name}</strong> · WhatsApp {phone}<br/>PAN: {pan} · FY: {fy} · Status: {itr.get("status")}</p>
<p>Preferred regime (estimate): <span class="badge">{preferred}</span></p>
<table>
<tr><th>Item</th><th>Old regime</th><th>New regime</th></tr>
<tr><td>Taxable income</td><td>₹{estimate.get("taxable_old",0):,.2f}</td><td>₹{estimate.get("taxable_new",0):,.2f}</td></tr>
<tr><td>Tax before cess</td><td>₹{estimate.get("tax_old",0):,.2f}</td><td>₹{estimate.get("tax_new",0):,.2f}</td></tr>
<tr><td>Net tax (incl. cess)</td><td>₹{estimate.get("net_tax_old",0):,.2f}</td><td>₹{estimate.get("net_tax_new",0):,.2f}</td></tr>
<tr><td>TDS + advance tax</td><td colspan="2">₹{estimate.get("prepaid",0):,.2f}</td></tr>
<tr><td>Payable / (Refund)</td><td>₹{estimate.get("payable_or_refund_old",0):,.2f}</td><td>₹{estimate.get("payable_or_refund_new",0):,.2f}</td></tr>
</table>
<p>Gross salary: ₹{estimate.get("gross_salary",0):,.2f} · Exemptions: ₹{estimate.get("exemptions",0):,.2f} · 80C bucket: ₹{estimate.get("deductions_80c",0):,.2f}</p>
<div class="disclaimer">{estimate.get("disclaimer","")}</div>
</body></html>"""
