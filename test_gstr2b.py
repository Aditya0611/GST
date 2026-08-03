"""Unit tests for GSTR-2B parse + match."""
import unittest

from gstr2b import (
    parse_gstr2b_json,
    parse_gstr2b_csv,
    normalize_invoice_number,
    compare_books_to_2b,
    reconcile_purchase_invoices,
    MATCH_MATCHED,
    MATCH_MISMATCH,
    MATCH_UNMATCHED,
)


SAMPLE_JSON = {
    "return_period": "2026-07",
    "b2b": [
        {
            "ctin": "27AAAAA1111A1Z1",
            "trdnm": "Office Mart",
            "inv": [
                {
                    "inum": "DUMMY-ITC-20260722-001",
                    "idt": "22-07-2026",
                    "val": 11800,
                    "txval": 10000,
                    "camt": 900,
                    "samt": 900,
                    "iamt": 0,
                },
                {
                    "inum": "COMPLEX-20260723-001",
                    "idt": "23-07-2026",
                    "val": 30000,
                    "txval": 25000,
                    "camt": 2215.8,
                    "samt": 2215.8,
                    "iamt": 0,
                },
            ],
        }
    ],
}


class TestGstr2b(unittest.TestCase):
    def test_normalize_invoice_number(self):
        self.assertEqual(normalize_invoice_number(" INV-001 "), "INV001")
        self.assertEqual(normalize_invoice_number("#14/AB"), "14AB")

    def test_parse_json(self):
        period, entries = parse_gstr2b_json(SAMPLE_JSON)
        self.assertEqual(period, "2026-07")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["supplier_gstin"], "27AAAAA1111A1Z1")
        self.assertEqual(entries[0]["invoice_number_norm"], "DUMMYITC20260722001")

    def test_parse_csv(self):
        csv_text = (
            "supplier_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst\n"
            "27AAAAA1111A1Z1,INV-9,15-07-2026,1000,90,90,0\n"
        )
        period, entries = parse_gstr2b_csv(csv_text)
        self.assertEqual(period, "2026-07")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["cgst"], 90.0)

    def test_compare_matched(self):
        inv = {
            "invoice_date": "2026-07-22",
            "total_taxable_value": 10000,
            "total_cgst": 900,
            "total_sgst": 900,
            "total_igst": 0,
        }
        ent = {
            "invoice_date": "2026-07-22",
            "taxable_value": 10000,
            "cgst": 900,
            "sgst": 900,
            "igst": 0,
        }
        status, reason = compare_books_to_2b(inv, ent)
        self.assertEqual(status, MATCH_MATCHED)
        self.assertIsNone(reason)

    def test_compare_mismatch(self):
        inv = {
            "invoice_date": "2026-07-22",
            "total_taxable_value": 10000,
            "total_cgst": 900,
            "total_sgst": 900,
            "total_igst": 0,
        }
        ent = {
            "invoice_date": "2026-07-22",
            "taxable_value": 9500,
            "cgst": 855,
            "sgst": 855,
            "igst": 0,
        }
        status, reason = compare_books_to_2b(inv, ent)
        self.assertEqual(status, MATCH_MISMATCH)
        self.assertIn("Taxable", reason)

    def test_reconcile_flow(self):
        entries = parse_gstr2b_json(SAMPLE_JSON)[1]
        entries[0]["id"] = 1
        entries[1]["id"] = 2
        invoices = [
            {
                "id": 14,
                "supplier_gstin": "27AAAAA1111A1Z1",
                "recipient_gstin": "27BBBBB2222B1Z2",
                "invoice_number": "DUMMY-ITC-20260722-001",
                "invoice_date": "2026-07-22",
                "total_taxable_value": 10000,
                "total_cgst": 900,
                "total_sgst": 900,
                "total_igst": 0,
            },
            {
                "id": 99,
                "supplier_gstin": "27AAAAA1111A1Z1",
                "recipient_gstin": "27BBBBB2222B1Z2",
                "invoice_number": "MISSING-BILL",
                "invoice_date": "2026-07-10",
                "total_taxable_value": 500,
                "total_cgst": 45,
                "total_sgst": 45,
                "total_igst": 0,
            },
        ]
        summary = reconcile_purchase_invoices(
            invoices, entries, client_gstin="27BBBBB2222B1Z2"
        )
        by_id = {r["invoice_id"]: r for r in summary["invoice_results"]}
        self.assertEqual(by_id[14]["gstr2b_match_status"], MATCH_MATCHED)
        self.assertEqual(by_id[99]["gstr2b_match_status"], MATCH_UNMATCHED)
        self.assertEqual(summary["counts"][MATCH_MATCHED], 1)
        self.assertEqual(summary["counts"][MATCH_UNMATCHED], 1)
        self.assertGreaterEqual(summary["orphan_2b_count"], 1)


if __name__ == "__main__":
    unittest.main()
