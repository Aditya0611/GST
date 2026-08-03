"""Tests for WhatsApp month-close nudge copy."""
import unittest

from nudge import build_month_nudge_message, month_label


class TestNudge(unittest.TestCase):
    def test_month_label(self):
        self.assertEqual(month_label("2026-07"), "Jul 2026")

    def test_unmatched_nudge(self):
        invoices = [
            {
                "id": 1,
                "invoice_number": "A-10",
                "gstr2b_match_status": "unmatched",
                "review_status": "needs_review",
                "is_approved": 0,
            },
            {
                "id": 2,
                "invoice_number": "B-20",
                "gstr2b_match_status": "mismatch",
                "review_status": "needs_review",
                "is_approved": 0,
            },
        ]
        out = build_month_nudge_message(
            client_name="Sharma Traders",
            return_period="2026-07",
            invoices=invoices,
        )
        self.assertTrue(out["should_nudge"])
        self.assertEqual(out["counts"]["unmatched"], 1)
        self.assertEqual(out["counts"]["mismatch"], 1)
        self.assertIn("Jul 2026", out["message"])
        self.assertIn("#A-10", out["message"])
        self.assertIn("#B-20", out["message"])
        self.assertIn("not found in GSTR-2B", out["message"])


if __name__ == "__main__":
    unittest.main()
