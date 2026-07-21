"""
test_update_api.py — Verify that updating category on local server triggers RAG auditing.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

# Ensure the parent directory is in the import path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

load_dotenv()

async def test_update():
    try:
        from main import app
        from fastapi.testclient import TestClient
    except Exception as e:
        print(f"Failed to import: {e}")
        sys.exit(1)

    print("--- Testing CA Update Category API ---")
    client = TestClient(app)

    invoice_id = 9

    # Step 1: Change to Software & SaaS (should be eligible)
    payload_saas = {
        "ca_user": "CA Tester",
        "fields": {
            "business_category": "Software & SaaS",
            "recipient_gstin": "27BBBBB2222B1Z2",
            "is_itc_eligible": True,             # Form sends current form state
            "itc_ineligibility_reason": ""       # Form sends current form state
        }
    }
    print(f"\nStep 1: Changing Invoice {invoice_id} to Software & SaaS...")
    response = client.put(f"/api/invoices/{invoice_id}", json=payload_saas)
    if response.status_code == 200:
        inv = response.json().get("invoice", {})
        print(f"  New Category: {inv.get('business_category')}")
        print(f"  ITC Eligible: {inv.get('is_itc_eligible')} (Expected: True/1)")
        print(f"  Reason:       {inv.get('itc_ineligibility_reason')} (Expected: '')")
    else:
        print(f"Failed: {response.text}")

    # Step 2: Change to Food & Beverages (should trigger auto RAG auditing because category changed, even though payload lists old values)
    payload_food = {
        "ca_user": "CA Tester",
        "fields": {
            "business_category": "Food & Beverages",
            "recipient_gstin": "27BBBBB2222B1Z2",
            "is_itc_eligible": True,             # Frontend still has old value
            "itc_ineligibility_reason": ""       # Frontend still has old value
        }
    }
    print(f"\nStep 2: Changing Invoice {invoice_id} to Food & Beverages...")
    response = client.put(f"/api/invoices/{invoice_id}", json=payload_food)
    if response.status_code == 200:
        inv = response.json().get("invoice", {})
        print(f"  New Category: {inv.get('business_category')}")
        print(f"  ITC Eligible: {inv.get('is_itc_eligible')} (Expected: False/0)")
        print(f"  Reason:       {inv.get('itc_ineligibility_reason')} (Expected: Blocked under Section 17(5)...)")
    else:
        print(f"Failed: {response.text}")

if __name__ == "__main__":
    asyncio.run(test_update())
