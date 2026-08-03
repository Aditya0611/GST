"""Unit tests for Sandbox OTP / GSTR-2B helpers (mocked HTTP)."""
import unittest
from unittest.mock import MagicMock, patch

import sandbox


class TestSandboxGstr2bHelpers(unittest.TestCase):
    def test_return_period_to_year_month(self):
        self.assertEqual(sandbox.return_period_to_year_month("2026-07"), ("2026", "07"))
        with self.assertRaises(ValueError):
            sandbox.return_period_to_year_month("07-2026")

    def test_unwrap_gstr2b_document_nested(self):
        payload = {
            "code": 200,
            "data": {
                "data": {
                    "chksum": "abc",
                    "data": {
                        "rtnprd": "072026",
                        "docdata": {
                            "b2b": [
                                {
                                    "ctin": "27AAAAA1111A1Z1",
                                    "trdnm": "Office Mart",
                                    "inv": [
                                        {
                                            "inum": "INV-1",
                                            "idt": "15-07-2026",
                                            "txval": 1000,
                                            "camt": 90,
                                            "samt": 90,
                                            "iamt": 0,
                                            "val": 1180,
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                }
            },
        }
        doc = sandbox.unwrap_gstr2b_document(payload)
        self.assertIn("docdata", doc)
        self.assertEqual(doc.get("fp") or doc.get("rtnprd"), "072026")

        from gstr2b import parse_gstr2b_json

        period, entries = parse_gstr2b_json(doc)
        self.assertEqual(period, "2026-07")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["supplier_gstin"], "27AAAAA1111A1Z1")

    @patch("sandbox.get_access_token", return_value="sandbox-jwt")
    @patch("sandbox._credentials", return_value=("key_test", "secret_test"))
    @patch("sandbox.httpx.Client")
    def test_request_taxpayer_otp_ok(self, mock_client_cls, _creds, _tok):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"status_cd": "1"},
            "transaction_id": "tx-1",
        }
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        out = sandbox.request_taxpayer_otp("user.gst", "33ABKCS2033B1ZW")
        self.assertTrue(out["ok"])
        self.assertEqual(out["username"], "user.gst")

    @patch("sandbox.get_access_token", return_value="sandbox-jwt")
    @patch("sandbox._credentials", return_value=("key_test", "secret_test"))
    @patch("sandbox.httpx.Client")
    def test_verify_taxpayer_otp_ok(self, mock_client_cls, _creds, _tok):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": {
                "status_cd": "1",
                "access_token": "taxpayer-token",
                "token_expiry": 9999999999999,
                "session_expiry": 9999999999999,
            },
        }
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        out = sandbox.verify_taxpayer_otp("user.gst", "33ABKCS2033B1ZW", "575757")
        self.assertEqual(out["access_token"], "taxpayer-token")

    @patch("sandbox._credentials", return_value=("key_test", "secret_test"))
    @patch("sandbox.httpx.Client")
    def test_fetch_gstr2b_document(self, mock_client_cls, _creds):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "data": {
                "data": {
                    "rtnprd": "072026",
                    "docdata": {
                        "b2b": [
                            {
                                "ctin": "27AAAAA1111A1Z1",
                                "inv": [{"inum": "A1", "idt": "01-07-2026", "txval": 10}],
                            }
                        ]
                    },
                }
            },
        }
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        doc = sandbox.fetch_gstr2b_document(
            "2026-07", taxpayer_access_token="taxpayer-token"
        )
        self.assertTrue(doc.get("docdata") or doc.get("b2b"))


if __name__ == "__main__":
    unittest.main()
