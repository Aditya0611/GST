"""
test_processor.py — CLI test script for Step 2.

Usage:
    python test_processor.py <path_to_invoice_file>
"""

import sys
import asyncio
import json
import logging
from dotenv import load_dotenv

# Enable basic logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_processor")

load_dotenv()

async def main():
    if len(sys.argv) < 2:
        logger.error("Usage: python test_processor.py <path_to_invoice_file>")
        sys.exit(1)
        
    file_path = sys.argv[1]
    
    # Import here to verify imports inside running process
    try:
        from processor import process_invoice
    except ImportError as e:
        logger.error("Could not import processor.py. Make sure dependencies are installed. %s", e)
        sys.exit(1)

    logger.info("Starting processing for: %s", file_path)
    
    try:
        result = await process_invoice(file_path)
        
        # Pretty print results
        print("\n" + "="*50)
        print("🎯 EXTRACTION & ANALYSIS RESULTS")
        print("="*50)
        print(f"Supplier:   {result.extraction.supplier_name} (GSTIN: {result.extraction.supplier_gstin}) [Valid: {result.is_valid_supplier_gstin}]")
        print(f"Recipient:  {result.extraction.recipient_name} (GSTIN: {result.extraction.recipient_gstin}) [Valid: {result.is_valid_recipient_gstin}]")
        print(f"Inv Number: {result.extraction.invoice_number} | Date: {result.extraction.invoice_date}")
        print(f"Supply Type: {result.supply_type} | Category: {result.extraction.business_category}")
        
        print("\n--- Line Items ---")
        for idx, item in enumerate(result.extraction.line_items):
            print(f" {idx+1}. {item.description}")
            print(f"    HSN/SAC: {item.hsn_or_sac or 'None'} | Taxable: ₹{item.taxable_value} | GST: {item.gst_rate}% (CGST: ₹{item.cgst}, SGST: ₹{item.sgst}, IGST: ₹{item.igst}) | Total: ₹{item.line_total}")

        print("\n--- Totals ---")
        print(f"  Taxable Value: ₹{result.extraction.total_taxable_value}")
        print(f"  Total CGST:    ₹{result.extraction.total_cgst}")
        print(f"  Total SGST:    ₹{result.extraction.total_sgst}")
        print(f"  Total IGST:    ₹{result.extraction.total_igst}")
        print(f"  Grand Total:   ₹{result.extraction.grand_total}")
        
        print("\n--- Tax Rules & Validation ---")
        print(f"  Calculation Audit Passed: {result.is_calculation_correct}")
        if not result.is_calculation_correct:
            print("  ⚠️ Errors:")
            for err in result.calculation_errors:
                print(f"    - {err}")
                
        print(f"  ITC Eligible:             {result.is_itc_eligible}")
        if not result.is_itc_eligible:
            print(f"  Reason:                   {result.itc_ineligibility_reason}")
        print("="*50 + "\n")
        
    except Exception as e:
        logger.exception("An error occurred during extraction:")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
