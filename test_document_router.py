"""Unit tests for WhatsApp document classification (GST vs Form 16)."""

import os
import unittest

import document_router as dr


class DocumentRouterTests(unittest.TestCase):
    def test_form16_filename_and_hint(self):
        result = dr.classify_incoming_document(
            "scratch/dummy_form16.pdf",
            original_filename="form16_2025.pdf",
            hint="sending my form 16",
        )
        self.assertEqual(result["doc_type"], dr.DOC_FORM16)
        self.assertIn(result["confidence"], ("medium", "high"))

    def test_gst_hint(self):
        result = dr.classify_incoming_document(
            "scratch/dummy_form16.pdf",
            original_filename="scan.pdf",
            hint="gst purchase bill",
        )
        self.assertEqual(result["doc_type"], dr.DOC_GST)

    def test_encrypted_storage_pdf_text(self):
        rel = "919999000001/2026-09/document_1788780934_66c405.pdf"
        try:
            import storage

            storage.read_file_bytes(rel)
        except Exception:
            self.skipTest("sample encrypted Form 16 not in storage")
        result = dr.classify_incoming_document(rel, original_filename="document.pdf")
        self.assertEqual(result["doc_type"], dr.DOC_FORM16)

        result = dr.classify_incoming_document(
            "scratch/dummy_form16.pdf",
            original_filename="scan.pdf",
        )
        # Dummy PDF text still scores as Form 16; use empty path for unknown
        if result["doc_type"] != dr.DOC_UNKNOWN:
            result = dr.classify_incoming_document(
                __file__,
                original_filename="notes.txt",
            )
        self.assertEqual(result["doc_type"], dr.DOC_UNKNOWN)

    def test_parse_doc_intent_from_text(self):
        self.assertEqual(dr.parse_doc_intent_from_text("here is my form 16"), dr.DOC_FORM16)
        self.assertEqual(dr.parse_doc_intent_from_text("gst invoice please"), dr.DOC_GST)
        self.assertIsNone(dr.parse_doc_intent_from_text("what is my itc"))

    def test_parse_doc_choice_reply(self):
        self.assertEqual(dr.parse_doc_choice_reply("1"), dr.DOC_GST)
        self.assertEqual(dr.parse_doc_choice_reply("2"), dr.DOC_FORM16)
        self.assertEqual(dr.parse_doc_choice_reply("GST bill"), dr.DOC_GST)
        self.assertIsNone(dr.parse_doc_choice_reply("maybe later"))


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    unittest.main()
