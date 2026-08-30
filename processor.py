"""
processor.py — AI invoice extraction, tax slab verification, and ITC categorization.

Uses the modern Google GenAI SDK to extract structured JSON data directly
from raw invoice files (images/PDFs) and applies Indian GST business rules.
"""

import os
import re
import logging
import base64
import json
from typing import Optional, List
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv
from groq import Groq
from itc_rules import (
    apply_deterministic_itc_rules,
    evaluate_line_items_itc,
    validate_gstin as _validate_gstin_fmt,
)

load_dotenv()

logger = logging.getLogger(__name__)


def validate_gstin(gstin: Optional[str]) -> bool:
    """Validate 15-digit Indian GSTIN format (re-export for callers)."""
    return _validate_gstin_fmt(gstin)

# Configure Google GenAI Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    client = None
    logger.warning("GEMINI_API_KEY not found in environment variables. Gemini extraction will fail.")

# Configure Groq Client
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)
else:
    groq_client = None
    logger.warning("GROQ_API_KEY not found in environment variables. Groq extraction will fail.")


def _get_image_base64(file_path: str) -> tuple[str, str]:
    ext = os.path.splitext(file_path.lower())[1]
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    mime_type = mime_map.get(ext, "image/jpeg")
    # Prefer storage decrypt when path is under STORAGE_DIR
    try:
        import storage as storage_mod

        img_bytes = storage_mod.read_file_bytes(file_path)
    except Exception:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
    b64_str = base64.b64encode(img_bytes).decode("utf-8")
    return b64_str, mime_type


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_invoice_payload(data: dict) -> dict:
    """Fill gaps when vision models omit totals / category."""
    if not isinstance(data, dict):
        return data

    items = data.get("line_items") or []
    if isinstance(items, list) and items:
        if data.get("total_taxable_value") is None:
            data["total_taxable_value"] = round(
                sum(_coerce_float(i.get("taxable_value")) for i in items if isinstance(i, dict)),
                2,
            )
        if data.get("total_cgst") is None:
            data["total_cgst"] = round(
                sum(_coerce_float(i.get("cgst")) for i in items if isinstance(i, dict)),
                2,
            )
        if data.get("total_sgst") is None:
            data["total_sgst"] = round(
                sum(_coerce_float(i.get("sgst")) for i in items if isinstance(i, dict)),
                2,
            )
        if data.get("total_igst") is None:
            data["total_igst"] = round(
                sum(_coerce_float(i.get("igst")) for i in items if isinstance(i, dict)),
                2,
            )
        if data.get("grand_total") is None:
            line_sum = sum(_coerce_float(i.get("line_total")) for i in items if isinstance(i, dict))
            if line_sum > 0:
                data["grand_total"] = round(line_sum, 2)
            else:
                taxable = _coerce_float(data.get("total_taxable_value"))
                tax = (
                    _coerce_float(data.get("total_cgst"))
                    + _coerce_float(data.get("total_sgst"))
                    + _coerce_float(data.get("total_igst"))
                )
                data["grand_total"] = round(taxable + tax, 2)

    if not data.get("business_category"):
        data["business_category"] = "Other"
    if not data.get("supplier_name"):
        data["supplier_name"] = "Unknown Supplier"
    if not data.get("invoice_number"):
        data["invoice_number"] = "UNKNOWN"
    if not data.get("invoice_date"):
        data["invoice_date"] = "1970-01-01"
    if data.get("total_taxable_value") is None:
        data["total_taxable_value"] = 0.0
    if data.get("grand_total") is None:
        data["grand_total"] = 0.0
    if not data.get("line_items"):
        data["line_items"] = []

    return data


def _parse_invoice_json(raw_text: str) -> "InvoiceExtraction":
    """Parse model JSON, normalize missing fields, then validate."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        data = json.loads(cleaned)
    data = _normalize_invoice_payload(data)
    return InvoiceExtraction.model_validate(data)


# ── Pydantic Schemas for Structured JSON Output ────────────────────────────────
class LineItem(BaseModel):
    description: str = Field(description="Description of the goods or services.")
    hsn_or_sac: Optional[str] = Field(None, description="4 or 6 digit HSN code for goods, or SAC code for services.")
    quantity: Optional[float] = Field(1.0, description="Quantity of items.")
    unit_price: Optional[float] = Field(0.0, description="Price per single unit.")
    taxable_value: float = Field(description="Taxable value of the item before tax is applied.")
    gst_rate: float = Field(description="The total combined GST percentage slab applied to this item (e.g., 0, 5, 12, 18, 28). If the invoice lists CGST and SGST separately (e.g., 9% each), this field must be the sum of both (e.g., 18.0).")
    cgst: Optional[float] = Field(0.0, description="Central GST amount for this line item.")
    sgst: Optional[float] = Field(0.0, description="State GST/UTGST amount for this line item.")
    igst: Optional[float] = Field(0.0, description="Integrated GST amount for this line item.")
    line_total: float = Field(description="Total value of the line item including taxes.")


class InvoiceExtraction(BaseModel):
    supplier_name: str = Field(description="Legal name or trade name of the supplier/seller.")
    supplier_gstin: Optional[str] = Field(None, description="15-character GSTIN of the supplier.")
    recipient_name: Optional[str] = Field(None, description="Legal name or trade name of the buyer/customer.")
    recipient_gstin: Optional[str] = Field(None, description="15-character GSTIN of the buyer.")
    
    invoice_number: str = Field(description="Invoice reference number.")
    invoice_date: str = Field(description="Date of the invoice in YYYY-MM-DD format.")
    place_of_supply: Optional[str] = Field(None, description="State name or 2-digit state code where supply occurred.")
    
    line_items: List[LineItem] = Field(description="List of all line items / tables of items in the invoice.")
    
    total_taxable_value: float = Field(description="Total taxable value of all items combined before tax.")
    total_cgst: Optional[float] = Field(0.0, description="Sum of CGST across all line items.")
    total_sgst: Optional[float] = Field(0.0, description="Sum of SGST across all line items.")
    total_igst: Optional[float] = Field(0.0, description="Sum of IGST across all line items.")
    grand_total: float = Field(description="Grand total of the invoice including all taxes.")
    
    business_category: str = Field(
        description="Categorize the expense/sale. Choices: Office Supplies, Electronics & Hardware, "
                    "Software & SaaS, Professional & Consulting Services, Rent & Infrastructure, "
                    "Food & Beverages, Motor Vehicle & Cab Bookings, Travel & Lodging, Logistics & Freight, "
                    "Raw Materials, Marketing & Advertising, Utilities (Electricity/Water/Internet), Other."
    )


# ── GST Verification Rules & Calculations ──────────────────────────────────────
class ProcessingResult(BaseModel):
    extraction: InvoiceExtraction
    is_valid_supplier_gstin: bool
    is_valid_recipient_gstin: bool
    is_calculation_correct: bool
    calculation_errors: List[str]
    is_itc_eligible: bool
    itc_ineligibility_reason: Optional[str]
    supply_type: str = Field(description="Supply type: 'INTRA-STATE', 'INTER-STATE', or 'UNKNOWN'")
    # Line-level ITC rollups (complex / mixed invoices)
    itc_eligible_cgst: float = 0.0
    itc_eligible_sgst: float = 0.0
    itc_eligible_igst: float = 0.0
    itc_blocked_gst: float = 0.0
    itc_partial: bool = False
    line_itc: List[dict] = Field(default_factory=list)


class ITCAuditResult(BaseModel):
    is_itc_eligible: bool = Field(description="True if the expense is eligible for Input Tax Credit (ITC), False if blocked/ineligible under GST laws.")
    reason: Optional[str] = Field(None, description="Clear and detailed explanation referencing the specific Section of the Act (e.g. Section 17(5)) if blocked, or null if eligible.")


async def evaluate_itc_eligibility(
    category: str,
    is_recipient_registered: bool,
    line_items_summary: str = "",
) -> tuple[bool, Optional[str]]:
    """
    Evaluate ITC under Sec 17(5): deterministic rules first, LLM only for gray areas.
    """
    decision = apply_deterministic_itc_rules(
        category,
        is_recipient_registered=is_recipient_registered,
        line_items_summary=line_items_summary,
    )
    if not decision.needs_llm:
        return decision.is_eligible, decision.reason_with_code()

    # Gray area — try RAG + LLM; fall back to deterministic gray decision
    import rag
    from google.genai import types

    query = f"ITC eligibility for category '{category}' with items: {line_items_summary}"
    try:
        matches = await rag.search_knowledge_base(query, limit=5)
    except Exception as e:
        logger.warning("RAG vector search failed, using rule gray decision: %s", e)
        matches = []

    contexts = []
    for m in matches:
        if m["similarity"] > 0.40:
            contexts.append(f"--- Document: {m['title']} ---\n{m['content']}")

    if not contexts or client is None:
        logger.info("No RAG/LLM path for '%s' — using gray rule decision.", category)
        return decision.is_eligible, decision.reason_with_code()

    context_str = "\n\n".join(contexts)
    prompt = f"""You are a strict GST compliance auditing engine. Determine if this expense is eligible for Input Tax Credit (ITC) under Indian GST laws.

--- REFERENCE CONTEXT ---
{context_str}
------------------------

EXPENSE CATEGORY: {category}
LINE ITEMS SUMMARY: {line_items_summary}

Rules:
1. Section 17(5) blocks food/beverages, outdoor catering, passenger motor vehicles (≤13 seats), club/fitness, personal consumption, gifts/free samples, and employee vacation travel (LTC).
2. Business lodging / official travel is generally eligible unless it is personal vacation benefit.
3. If blocked, is_itc_eligible=false and cite the section. If eligible, is_itc_eligible=true and reason=null.

Respond in strict JSON matching the schema.
"""
    try:
        response = client.models.generate_content(
            model=rag.GENERATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ITCAuditResult,
                temperature=0.1,
            ),
        )
        result = ITCAuditResult.model_validate_json(response.text)
        reason = result.reason
        if not result.is_itc_eligible and reason and not reason.startswith("["):
            reason = f"[llm-17(5)] {reason}"
        return result.is_itc_eligible, reason
    except Exception:
        logger.exception("Gemini RAG ITC evaluation failed, using gray rule decision:")
        return decision.is_eligible, decision.reason_with_code()


def verify_taxes_and_supply(ext: InvoiceExtraction) -> tuple[str, bool, List[str]]:
    """
    Verify GST calculations:
      - Supply Type check based on GSTIN state prefixes.
      - Intra-state (CGST + SGST) vs Inter-state (IGST).
      - Cross-verify math (taxable_value * rate = taxes).
    """
    errors = []
    supply_type = "UNKNOWN"
    
    total_cgst = ext.total_cgst or 0.0
    total_sgst = ext.total_sgst or 0.0
    total_igst = ext.total_igst or 0.0

    # 1. Determine supply type
    if ext.supplier_gstin and ext.recipient_gstin:
        sup_state = ext.supplier_gstin[:2]
        rec_state = ext.recipient_gstin[:2]
        if sup_state.isdigit() and rec_state.isdigit():
            if sup_state == rec_state:
                supply_type = "INTRA-STATE"
            else:
                supply_type = "INTER-STATE"

    # 2. Check for supply type consistency
    if supply_type == "INTRA-STATE":
        if total_igst > 0:
            errors.append("Intra-state invoice contains IGST which is invalid.")
        # Ensure CGST & SGST are roughly populated
        if total_cgst == 0 and total_sgst == 0 and ext.total_taxable_value > 0:
            # Check if it was 0% rate
            has_tax = any(item.gst_rate > 0 for item in ext.line_items)
            if has_tax:
                errors.append("Intra-state transaction should have CGST and SGST.")
    elif supply_type == "INTER-STATE":
        if total_cgst > 0 or total_sgst > 0:
            errors.append("Inter-state invoice contains CGST/SGST which is invalid.")
        if total_igst == 0 and ext.total_taxable_value > 0:
            has_tax = any(item.gst_rate > 0 for item in ext.line_items)
            if has_tax:
                errors.append("Inter-state transaction should have IGST.")

    # 3. Sum check
    tolerance = 1.0  # ₹1 rounding error tolerance
    expected_grand_total = ext.total_taxable_value + total_cgst + total_sgst + total_igst
    if abs(expected_grand_total - ext.grand_total) > tolerance:
        errors.append(
            f"Taxable value ({ext.total_taxable_value}) + Taxes "
            f"(CGST={total_cgst}, SGST={total_sgst}, IGST={total_igst}) "
            f"equals {expected_grand_total}, but Grand Total is {ext.grand_total}."
        )

    # 4. Individual line item math check
    for idx, item in enumerate(ext.line_items):
        i_cgst = item.cgst or 0.0
        i_sgst = item.sgst or 0.0
        i_igst = item.igst or 0.0
        item_tax = i_cgst + i_sgst + i_igst
        expected_tax = round(item.taxable_value * (item.gst_rate / 100.0), 2)
        
        # Check if total taxes on the item align with its rate
        if abs(item_tax - expected_tax) > 0.5:
            errors.append(
                f"Line item {idx+1} tax mismatch: expected {expected_tax} (based on GST rate {item.gst_rate}%), "
                f"but got total tax {item_tax} (CGST={i_cgst}, SGST={i_sgst}, IGST={i_igst})."
            )
            # Check if CGST / SGST splits are even (for intra-state)
            if i_cgst > 0 or i_sgst > 0:
                if abs(i_cgst - i_sgst) > 0.1:
                    errors.append(f"Line item {idx+1} CGST ({i_cgst}) and SGST ({i_sgst}) are not split equally.")

    is_valid = len(errors) == 0
    return supply_type, is_valid, errors


# ── AI extraction pipeline ──────────────────────────────────────────────────────
async def process_invoice(file_path: str) -> ProcessingResult:
    """
    Process invoice using either Groq or Gemini (fallback/PDF),
    then validate the calculations and ITC logic.
    """
    import storage as storage_mod

    # Materialize decrypted bytes to a temp path so Gemini/Pillow/Groq keep working
    with storage_mod.plaintext_temp_file(file_path) as plain_path:
        return await _process_invoice_plaintext(plain_path)


async def _process_invoice_plaintext(file_path: str) -> ProcessingResult:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found at: {file_path}")

    ext = os.path.splitext(file_path.lower())[1]
    provider = os.getenv("INVOICE_LLM_PROVIDER", "gemini").lower()

    # Determine provider with fallbacks
    if provider == "groq":
        if ext == ".pdf":
            logger.info("PDF file detected. Groq does not support PDFs natively. Falling back to Gemini.")
            provider = "gemini"
        elif not groq_client:
            logger.warning("Groq API key not configured. Falling back to Gemini.")
            provider = "gemini"

    if provider == "groq":
        # Groq Extraction Flow
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            raise ValueError(f"Unsupported file type for Groq: {ext}")

        b64_str, mime_type = _get_image_base64(file_path)
        prompt_text = (
            "Extract GST invoice fields from this image into ONE JSON object.\n"
            "Required keys:\n"
            "supplier_name, supplier_gstin, recipient_name, recipient_gstin,\n"
            "invoice_number, invoice_date (YYYY-MM-DD), place_of_supply,\n"
            "line_items (array of objects with: description, hsn_or_sac, quantity,\n"
            "unit_price, taxable_value, gst_rate, cgst, sgst, igst, line_total),\n"
            "total_taxable_value, total_cgst, total_sgst, total_igst, grand_total,\n"
            "business_category (one of: Office Supplies, Electronics & Hardware, "
            "Software & SaaS, Professional & Consulting Services, Rent & Infrastructure, "
            "Food & Beverages, Motor Vehicle & Cab Bookings, Travel & Lodging, "
            "Logistics & Freight, Raw Materials, Marketing & Advertising, "
            "Utilities (Electricity/Water/Internet), Other).\n"
            "Use numbers for amounts. If a total is missing on the bill, sum line items.\n"
            "Return only valid JSON, no markdown."
        )

        logger.info("Calling Groq API (qwen/qwen3.6-27b) to parse invoice image...")
        last_err = None
        raw_text = None
        for attempt in range(2):
            try:
                response = groq_client.chat.completions.create(
                    model="qwen/qwen3.6-27b",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt_text},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{mime_type};base64,{b64_str}"
                                    },
                                },
                            ],
                        }
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                )
                raw_text = response.choices[0].message.content or ""
                if not raw_text.strip():
                    raise ValueError("Groq returned empty JSON content")
                extraction = _parse_invoice_json(raw_text)
                last_err = None
                break
            except Exception as e:
                last_err = e
                logger.warning("Groq invoice extract attempt %d failed: %s", attempt + 1, e)
        if last_err is not None:
            if client:
                logger.warning("Falling back to Gemini for invoice extraction after Groq failure")
                provider = "gemini"
            else:
                raise last_err

    if provider == "gemini":
        # Gemini Extraction Flow
        if not client:
            raise ValueError("Gemini API Client is not configured. Please add GEMINI_API_KEY to your .env file.")

        logger.info("Uploading file / loading image to Gemini: %s", file_path)
        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            image_data = Image.open(file_path)
            contents = [
                "Extract the invoice details as a structured JSON object containing all requested fields. "
                "Verify all items, quantities, taxable values, and individual CGST, SGST, IGST tax amounts accurately.",
                image_data
            ]
        elif ext == ".pdf":
            pdf_file = client.files.upload(file=file_path)
            contents = [
                "Extract the invoice details as a structured JSON object containing all requested fields. "
                "Verify all items, quantities, taxable values, and individual CGST, SGST, IGST tax amounts accurately.",
                pdf_file
            ]
        else:
            raise ValueError(f"Unsupported file type: {ext}")

        logger.info("Calling Gemini API to parse invoice...")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=InvoiceExtraction,
                temperature=0.1,
            )
        )

        if ext == ".pdf":
            try:
                client.files.delete(name=pdf_file.name)
                logger.debug("Deleted temp pdf file from Gemini storage")
            except Exception as e:
                logger.warning("Failed to delete temp file from Gemini storage: %s", e)

        extraction = _parse_invoice_json(response.text)
    elif provider != "groq":
        raise ValueError(f"Unknown invoice LLM provider: {provider}")

    
    # ── Post-Extraction GST Rules Validations ──
    is_valid_supplier = validate_gstin(extraction.supplier_gstin)
    is_valid_recipient = validate_gstin(extraction.recipient_gstin)
    
    supply_type, is_calc_correct, calc_errors = verify_taxes_and_supply(extraction)
    
    # Construct line items summary for RAG check / line ITC
    line_dicts = [item.model_dump() for item in extraction.line_items]
    line_items_summary = ", ".join(
        [f"{item.description} (sac/hsn: {item.hsn_or_sac or 'N/A'})" for item in extraction.line_items]
    )

    # Prefer deterministic line-level ITC for complex / mixed bills
    line_eval = evaluate_line_items_itc(
        line_dicts,
        invoice_category=extraction.business_category or "Other",
        is_recipient_registered=is_valid_recipient,
    )
    is_itc_eligible = line_eval["is_itc_eligible"]
    itc_reason = line_eval["itc_ineligibility_reason"]

    # If no lines and gray category still needs LLM, keep legacy path
    if not line_dicts:
        is_itc_eligible, itc_reason = await evaluate_itc_eligibility(
            extraction.business_category,
            is_recipient_registered=is_valid_recipient,
            line_items_summary=line_items_summary,
        )

    return ProcessingResult(
        extraction=extraction,
        is_valid_supplier_gstin=is_valid_supplier,
        is_valid_recipient_gstin=is_valid_recipient,
        is_calculation_correct=is_calc_correct,
        calculation_errors=calc_errors,
        is_itc_eligible=is_itc_eligible,
        itc_ineligibility_reason=itc_reason,
        supply_type=supply_type,
        itc_eligible_cgst=float(line_eval.get("eligible_cgst") or 0),
        itc_eligible_sgst=float(line_eval.get("eligible_sgst") or 0),
        itc_eligible_igst=float(line_eval.get("eligible_igst") or 0),
        itc_blocked_gst=float(line_eval.get("blocked_gst") or 0),
        itc_partial=bool(line_eval.get("partial")),
        line_itc=line_eval.get("lines") or [],
    )
