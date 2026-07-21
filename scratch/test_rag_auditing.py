"""
test_rag_auditing.py — Test script to verify the RAG-based dynamic compliance engine.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

# Ensure the parent directory is in the import path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

load_dotenv()

async def run_tests():
    # Make sure database is initialized and processor is importable
    try:
        import db
        from processor import evaluate_itc_eligibility
    except Exception as e:
        print(f"Failed to import requirements: {e}")
        sys.exit(1)

    print("--- Running Dynamic RAG ITC Auditing Tests ---")

    test_cases = [
        # (Category, is_registered, line_items_summary)
        ("Food & Beverages", True, "Outdoor catering services for corporate annual day"),
        ("Software & SaaS", True, "Slack subscription license renewal"),
        ("Motor Vehicle & Cab Bookings", True, "Toyota Innova purchase for executive travel"),
        ("Office Supplies", True, "Cartridges, A4 printing paper sheets, and staplers"),
        ("Logistics & Freight", True, "Goods Transport Agency services for raw material shipment"),
        ("Food & Beverages", False, "Lunch with a client at Taj Hotel"), # B2C unregistered test
    ]

    for category, is_reg, summary in test_cases:
        print(f"\nEvaluating: Category='{category}' | Registered={is_reg}")
        print(f"Summary: '{summary}'")
        try:
            eligible, reason = await evaluate_itc_eligibility(category, is_reg, summary)
            print(f"RESULT -> Eligible: {eligible}")
            print(f"REASON -> {reason}")
        except Exception as e:
            print(f"Error during evaluation: {e}")

    print("\n--- Auditing Tests Completed ---")

if __name__ == "__main__":
    asyncio.run(run_tests())
