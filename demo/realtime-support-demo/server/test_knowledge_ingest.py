"""Tests for raw/hammer-data dashboard ingestion."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_ingest import (
    convert_upload_to_markdown,
    ingest_hammer_raw_from_text,
    ingest_hammer_raw_upload,
)


class KnowledgeIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmpdir.name)
        (self.repo / "raw" / "hammer-data").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_text_upload_becomes_markdown_in_hammer_data(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            result = ingest_hammer_raw_upload(
                self.repo,
                filename="trade-in-policy.txt",
                data=b"We accept trade-ins on most units.",
                title="Trade-in policy",
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["doc_id"].startswith("raw/hammer-data/dashboard-uploads/"))
        path = self.repo / result["path"]
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Trade-in policy", text)
        self.assertIn("trade-ins", text)

    def test_markdown_paste_ingest(self) -> None:
        result = ingest_hammer_raw_from_text(
            self.repo,
            filename="crm-notes",
            markdown_content="## CRM\n\nWe integrate with VinSolutions.",
        )
        self.assertTrue(result["ok"])
        self.assertIn("raw/hammer-data/dashboard-uploads/crm-notes.md", result["path"])

    def test_convert_pdf_output_name(self) -> None:
        with patch("knowledge_ingest.pdf_bytes_to_markdown", return_value="# Demo.pdf\n\nBody\n"):
            md, out_name = convert_upload_to_markdown(
                filename="Hammer Demo.pdf",
                data=b"fake-pdf-bytes",
                title="Hammer Demo",
            )
        self.assertEqual(out_name, "Hammer-Demo.pdf.md")
        self.assertIn("Demo.pdf", md)


if __name__ == "__main__":
    unittest.main()
