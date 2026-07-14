import tempfile
import unittest
from pathlib import Path

from backend.file_security import (
    milvus_string_literal,
    resolve_upload_target,
    sanitize_upload_filename,
    validate_upload_size,
)


class FileSecurityTests(unittest.TestCase):
    def test_sanitize_upload_filename_blocks_path_traversal(self):
        filename = sanitize_upload_filename("../商品 A.pdf")

        self.assertTrue(filename.endswith(".pdf"))
        self.assertNotIn("..", filename)
        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)

    def test_sanitize_upload_filename_rejects_unknown_extension(self):
        with self.assertRaises(ValueError):
            sanitize_upload_filename("payload.exe")

    def test_resolve_upload_target_stays_inside_upload_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp) / "uploads"
            safe_name, target = resolve_upload_target(upload_dir, "..\\nested\\report.docx")

            self.assertTrue(safe_name.endswith(".docx"))
            self.assertEqual(upload_dir.resolve(), target.parent)

    def test_validate_upload_size_raises_when_too_large(self):
        with self.assertRaises(ValueError):
            validate_upload_size(b"12345", max_bytes=4)

    def test_milvus_string_literal_escapes_quotes_and_backslashes(self):
        self.assertEqual(milvus_string_literal('a"b\\c'), '"a\\"b\\\\c"')


if __name__ == "__main__":
    unittest.main()
