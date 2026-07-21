import sys
import os
os.environ["PYTHONIOENCODING"] = "utf-8"
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import asyncio
import hmac
import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from dotenv import load_dotenv

load_dotenv()

# Set up test environment variables if not present, but preserve API keys
os.environ.setdefault("WEBHOOK_VERIFY_TOKEN", "gst_autopilot_verify_2026")
os.environ.setdefault("APP_SECRET", "afeef357903c2e0db8e9f49b65978bb7")

from main import app


def run_integration_check():
    import asyncio
    import db
    asyncio.run(db.init_db())
    client = TestClient(app)
    
    # Path to real sample invoice
    sample_invoice_path = Path("sample_invoice.png")
    if not sample_invoice_path.exists():
        print(f"❌ Error: {sample_invoice_path} not found in workspace!")
        return

    print("📖 Reading real sample_invoice.png bytes...")
    invoice_bytes = sample_invoice_path.read_bytes()

    # Capture replies
    captured_replies = []
    
    async def mock_send_reply(to, message_id, body):
        captured_replies.append(body)
        return {"status": "success"}

    # Mock WhatsApp media download and reply
    with patch("whatsapp.download_media", new_callable=AsyncMock) as mock_download, \
         patch("whatsapp.mark_as_read", new_callable=AsyncMock) as mock_mark, \
         patch("whatsapp.send_reply", new=mock_send_reply), \
         patch("whatsapp.send_text_message", new_callable=AsyncMock):
        
        mock_download.return_value = (invoice_bytes, "image/png")
        
        # Build mock payload
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "12345",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": [
                                    {
                                        "profile": {"name": "Test User"},
                                        "wa_id": "919999999999"
                                    }
                                ],
                                "messages": [
                                    {
                                        "from": "919999999999",
                                        "id": "wamid.TestInvoiceMessageID123",
                                        "timestamp": "1717372800",
                                        "type": "image",
                                        "image": {
                                            "mime_type": "image/png",
                                            "sha256": "sample_sha256",
                                            "id": "media_id_sample"
                                        }
                                    }
                                ]
                            },
                            "field": "messages"
                        }
                    ]
                }
            ]
        }

        body_bytes = json.dumps(payload).encode("utf-8")
        app_secret = os.environ.get("APP_SECRET", "")
        signature = hmac.new(
            app_secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256
        ).hexdigest()

        print("\n🚀 Sending mock WhatsApp image message payload to /webhook...")
        response = client.post(
            "/webhook",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}"
            }
        )

        print(f"📥 Server Response: {response.status_code} - {response.json()}")
        
        if response.status_code != 200:
            print("❌ Webhook request failed!")
            return

        print("\n--- Captured WhatsApp Messages ---")
        for idx, text in enumerate(captured_replies):
            print(f"\nMessage {idx+1}:")
            print("-" * 40)
            print(text)
            print("-" * 40)

        # Look for the saved files
        print("\n📁 Checking generated files in storage directory...")
        storage_dir = Path("./storage/919999999999")
        if storage_dir.exists():
            files = list(storage_dir.glob("**/*"))
            print(f"Found {len(files)} files in storage:")
            for f in files:
                print(f" - {f} ({f.stat().st_size} bytes)")
                if f.suffix == ".json":
                    print("\n📄 Content of saved extraction metadata JSON:")
                    print(json.dumps(json.loads(f.read_text(encoding="utf-8")), indent=2))
        else:
            print("❌ Error: Storage directory not created!")

        print("\n🗄️ Checking database records...")
        import asyncio
        import db
        invoices = asyncio.run(db.get_invoices(client_phone="919999999999"))
        print(f"Found {len(invoices)} invoices in database:")
        for inv in invoices:
            print(f" - Invoice ID #{inv['id']}: Supplier={inv['supplier_name']}, Total=₹{inv['grand_total']}, Approved={inv['is_approved']}")
            detail = asyncio.run(db.get_invoice_detail(inv['id']))
            print(f"   Line items count: {len(detail['line_items'])}")


if __name__ == "__main__":
    run_integration_check()
