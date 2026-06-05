import hmac
import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# Mock environment variables before importing main to prevent loading/failures
os.environ["WHATSAPP_TOKEN"] = "test_token"
os.environ["WHATSAPP_PHONE_ID"] = "test_phone_id"
os.environ["WEBHOOK_VERIFY_TOKEN"] = "test_verify_token"
os.environ["APP_SECRET"] = "test_app_secret"

from main import app, APP_SECRET, WEBHOOK_VERIFY_TOKEN
from processor import ProcessingResult, InvoiceExtraction, LineItem


class TestWebhook(unittest.TestCase):
    def setUp(self):
        import asyncio
        import db
        asyncio.run(db.init_db())
        self.client = TestClient(app)

    def tearDown(self):
        import os
        import db
        if os.path.exists(db.SQLITE_DB_PATH):
            try:
                os.remove(db.SQLITE_DB_PATH)
            except Exception:
                pass

    def test_webhook_verification_success(self):
        """Test GET /webhook with correct verify token."""
        response = self.client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "123456"
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "123456")

    def test_webhook_verification_failure(self):
        """Test GET /webhook with incorrect verify token."""
        response = self.client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "123456"
            }
        )
        self.assertEqual(response.status_code, 403)

    @patch("whatsapp.download_media", new_callable=AsyncMock)
    @patch("storage.save_file", new_callable=AsyncMock)
    @patch("whatsapp.mark_as_read", new_callable=AsyncMock)
    @patch("whatsapp.send_reply", new_callable=AsyncMock)
    @patch("main.process_invoice", new_callable=AsyncMock)
    @patch("storage.save_json", new_callable=AsyncMock)
    def test_handle_webhook_image_message(
        self,
        mock_save_json,
        mock_process_invoice,
        mock_send_reply,
        mock_mark_as_read,
        mock_save_file,
        mock_download_media
    ):
        """Test POST /webhook receives message, saves media, and triggers background processing."""
        # 1. Setup mock responses
        mock_download_media.return_value = (b"fake_bytes", "image/png")
        mock_save_file.return_value = "919876543210/2026-06/photo_123.png"

        # Mock processing result
        mock_extraction = InvoiceExtraction(
            supplier_name="Test Supplier Inc",
            supplier_gstin="27AAAAA1111A1Z1",
            recipient_name="Test Recipient Ltd",
            recipient_gstin="27BBBBB2222B1Z2",
            invoice_number="INV-001",
            invoice_date="2026-06-05",
            place_of_supply="27",
            line_items=[
                LineItem(
                    description="Test Item",
                    hsn_or_sac="1234",
                    quantity=1.0,
                    unit_price=100.0,
                    taxable_value=100.0,
                    gst_rate=18.0,
                    cgst=9.0,
                    sgst=9.0,
                    igst=0.0,
                    line_total=118.0
                )
            ],
            total_taxable_value=100.0,
            total_cgst=9.0,
            total_sgst=9.0,
            total_igst=0.0,
            grand_total=118.0,
            business_category="Office Supplies"
        )
        mock_process_invoice.return_value = ProcessingResult(
            extraction=mock_extraction,
            is_valid_supplier_gstin=True,
            is_valid_recipient_gstin=True,
            is_calculation_correct=True,
            calculation_errors=[],
            is_itc_eligible=True,
            itc_ineligibility_reason=None,
            supply_type="INTRA-STATE"
        )

        # 2. Build mock WhatsApp webhook payload
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "12345",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15555555555",
                                    "phone_number_id": "test_phone_id"
                                },
                                "contacts": [
                                    {
                                        "profile": {"name": "Alice Developer"},
                                        "wa_id": "919876543210"
                                    }
                                ],
                                "messages": [
                                    {
                                        "from": "919876543210",
                                        "id": "wamid.HBgLOTExMTExMTE1NTVGKhIA",
                                        "timestamp": "1717372800",
                                        "type": "image",
                                        "image": {
                                            "mime_type": "image/png",
                                            "sha256": "abcdef...",
                                            "id": "media_id_999"
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

        # Compute HMAC signature for headers
        signature = hmac.new(
            "test_app_secret".encode("utf-8"),
            body_bytes,
            hashlib.sha256
        ).hexdigest()

        # 3. Call the API POST /webhook using TestClient
        # Note: TestClient runs background tasks synchronously by default, which is perfect for testing!
        response = self.client.post(
            "/webhook",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}"
            }
        )

        # 4. Verify API response is immediate 200 OK
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        # 5. Verify mocked methods were called
        mock_download_media.assert_called_once_with("media_id_999")
        mock_save_file.assert_called_once()
        mock_mark_as_read.assert_called_once_with("wamid.HBgLOTExMTExMTE1NTVGKhIA")
        
        # There should be two replies:
        # First: Immediate acknowledgment
        # Second: Detailed summary report
        self.assertEqual(mock_send_reply.call_count, 2)

        # Verify first message (Immediate acknowledgment)
        first_call = mock_send_reply.call_args_list[0]
        self.assertEqual(first_call.kwargs["to"], "919876543210")
        self.assertIn("analyzing the invoice", first_call.kwargs["body"])

        # Verify second message (Summary report)
        second_call = mock_send_reply.call_args_list[1]
        self.assertEqual(second_call.kwargs["to"], "919876543210")
        self.assertIn("Test Supplier Inc", second_call.kwargs["body"])
        self.assertIn("INV-001", second_call.kwargs["body"])
        self.assertIn("Office Supplies", second_call.kwargs["body"])
        self.assertIn("Math Check", second_call.kwargs["body"])
        self.assertIn("ITC Eligibility", second_call.kwargs["body"])

        # Verify JSON was saved next to the invoice
        mock_save_json.assert_called_once_with(
            phone_number="919876543210",
            invoice_path_str="919876543210/2026-06/photo_123.png",
            data=mock_process_invoice.return_value.model_dump()
        )


if __name__ == "__main__":
    unittest.main()
