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

load_dotenv()

logger = logging.getLogger(__name__)

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
    with open(file_path, "rb") as f:
        img_bytes = f.read()
    b64_str = base64.b64encode(img_bytes).decode("utf-8")
    return b64_str, mime_type


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


def validate_gstin(gstin: Optional[str]) -> bool:
    """Validate 15-digit Indian GSTIN format using standard regex."""
    if not gstin:
        return False
    gstin_regex = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
    return bool(re.match(gstin_regex, gstin.upper()))


def evaluate_itc_eligibility(category: str, is_recipient_registered: bool) -> tuple[bool, Optional[str]]:
    """
    Evaluate Input Tax Credit (ITC) eligibility under Sec 17(5) CGST rules.
    Blocked credits include Food & Beverages, Cab/Motor Vehicle Hire, Club memberships, etc.
    """
    if not is_recipient_registered:
        return False, "Recipient is not a registered GST holder (B2C transactions are not eligible for ITC)."

    blocked_categories = {
        "Food & Beverages": "Blocked under Section 17(5)(b)(i) of CGST Act (Food, beverages, outdoor catering).",
        "Motor Vehicle & Cab Bookings": "Blocked under Section 17(5)(a) of CGST Act (Motor vehicles for passenger transport unless used for specific business purposes).",
        "Travel & Lodging": "Subject to Section 17(5) restriction depending on whether it is personal or employee vacation benefits.",
    }

    if category in blocked_categories:
        return False, blocked_categories[category]
    
    return True, None


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
        schema_json = json.dumps(InvoiceExtraction.model_json_schema())

        logger.info("Calling Groq API (meta-llama/llama-4-scout-17b-16e-instruct) to parse invoice image...")
        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Extract all invoice details from the attached invoice image.\n"
                                "Return a JSON object conforming exactly to this JSON schema:\n"
                                f"{schema_json}\n"
                                "Do not include any explanation or markdown formatting, just the raw JSON."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_str}"
                            }
                        }
                    ]
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw_text = response.choices[0].message.content
        extraction = InvoiceExtraction.model_validate_json(raw_text)
    else:
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

        extraction = InvoiceExtraction.model_validate_json(response.text)

    
    # ── Post-Extraction GST Rules Validations ──
    is_valid_supplier = validate_gstin(extraction.supplier_gstin)
    is_valid_recipient = validate_gstin(extraction.recipient_gstin)
    
    supply_type, is_calc_correct, calc_errors = verify_taxes_and_supply(extraction)
    
    is_itc_eligible, itc_reason = evaluate_itc_eligibility(
        extraction.business_category, 
        is_recipient_registered=is_valid_recipient
    )

    return ProcessingResult(
        extraction=extraction,
        is_valid_supplier_gstin=is_valid_supplier,
        is_valid_recipient_gstin=is_valid_recipient,
        is_calculation_correct=is_calc_correct,
        calculation_errors=calc_errors,
        is_itc_eligible=is_itc_eligible,
        itc_ineligibility_reason=itc_reason,
        supply_type=supply_type
    )
