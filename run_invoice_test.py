"""
run_invoice_test.py -- Full pipeline test
Processes a new invoice image through:
  1. AI extraction (Groq/Gemini)
  2. GST validation rules
  3. Save to SQLite database
  4. Print full summary
"""

import asyncio
import logging
import sys
import os
os.environ["PYTHONIOENCODING"] = "utf-8"
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("invoice_test")


async def main():
    invoice_path = r"c:\Users\rajni\OneDrive\Desktop\GST\sample_invoice_sharma.png"
    test_phone   = "919999999999"
    test_name    = "Test User"

    print("\n" + "="*60)
    print("  📄 GST AUTOPILOT — INVOICE PROCESSING TEST")
    print("="*60)
    print(f"  Invoice : {invoice_path}")
    print(f"  Client  : {test_phone} ({test_name})")
    print("="*60 + "\n")

    # ── Step 1: Init DB ──────────────────────────────────────────
    import db
    await db.init_db()
    logger.info("✅ Database initialized")

    # ── Step 2: Ensure client exists ─────────────────────────────
    client = await db.get_or_create_client(test_phone, test_name)
    logger.info("✅ Client: %s", client)

    # ── Step 3: AI Extraction + GST Validation ───────────────────
    from processor import process_invoice
    logger.info("🤖 Running AI extraction...")
    result = await process_invoice(invoice_path)
    ext = result.extraction

    # ── Step 4: Save to DB ───────────────────────────────────────
    import storage
    # Simulate saved_path (relative to STORAGE_DIR)
    fake_saved_path = f"{test_phone}/manual_test/sample_invoice_sharma.png"

    invoice_id = await db.save_invoice(
        client_phone=test_phone,
        file_path=fake_saved_path,
        result=result.model_dump()
    )
    logger.info("💾 Saved to DB with invoice ID: %d", invoice_id)

    # ── Step 5: Print Full Report ─────────────────────────────────
    total_gst = (ext.total_cgst or 0) + (ext.total_sgst or 0) + (ext.total_igst or 0)

    print("\n" + "="*60)
    print("  🎯 EXTRACTION RESULTS")
    print("="*60)
    print(f"  Supplier  : {ext.supplier_name}")
    print(f"  GSTIN     : {ext.supplier_gstin}  [Valid: {result.is_valid_supplier_gstin}]")
    print(f"  Recipient : {ext.recipient_name}")
    print(f"  GSTIN     : {ext.recipient_gstin}  [Valid: {result.is_valid_recipient_gstin}]")
    print(f"  Invoice # : {ext.invoice_number}")
    print(f"  Date      : {ext.invoice_date}")
    print(f"  Supply    : {result.supply_type}")
    print(f"  Category  : {ext.business_category}")

    print("\n  📦 LINE ITEMS:")
    print(f"  {'#':<3} {'Description':<30} {'HSN':<8} {'Qty':<5} {'Taxable':>10} {'GST%':>6} {'CGST':>8} {'SGST':>8} {'IGST':>8} {'Total':>10}")
    print("  " + "-"*100)
    for i, item in enumerate(ext.line_items, 1):
        print(f"  {i:<3} {(item.description or '')[:30]:<30} {(item.hsn_or_sac or 'N/A'):<8} {(item.quantity or 1):<5.0f} {item.taxable_value:>10,.2f} {item.gst_rate:>6.1f}% {(item.cgst or 0):>8,.2f} {(item.sgst or 0):>8,.2f} {(item.igst or 0):>8,.2f} {item.line_total:>10,.2f}")

    print("\n  💰 TOTALS:")
    print(f"  {'Taxable Value':<25}: ₹{ext.total_taxable_value:>10,.2f}")
    print(f"  {'Total CGST':<25}: ₹{(ext.total_cgst or 0):>10,.2f}")
    print(f"  {'Total SGST':<25}: ₹{(ext.total_sgst or 0):>10,.2f}")
    print(f"  {'Total IGST':<25}: ₹{(ext.total_igst or 0):>10,.2f}")
    print(f"  {'Total GST':<25}: ₹{total_gst:>10,.2f}")
    print(f"  {'Grand Total':<25}: ₹{ext.grand_total:>10,.2f}")

    print("\n  🔍 GST VALIDATION:")
    if result.is_calculation_correct:
        print("  ✅ Math Check     : All calculations CORRECT")
    else:
        print("  ⚠️  Math Check     : ERRORS FOUND!")
        for err in result.calculation_errors:
            print(f"     → {err}")

    if result.is_itc_eligible:
        print("  ✅ ITC Eligibility: ELIGIBLE for Input Tax Credit")
    else:
        print("  ❌ ITC Eligibility: NOT eligible")
        print(f"     Reason: {result.itc_ineligibility_reason}")

    print(f"\n  🗄️  Saved to Database — Invoice ID: #{invoice_id}")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
