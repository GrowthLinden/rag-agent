import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendSecurityTests(unittest.TestCase):
    def test_markdown_rendering_is_sanitized(self):
        script = (ROOT / "frontend" / "script.js").read_text(encoding="utf-8")
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

        self.assertIn("dompurify", html.lower())
        self.assertIn("sanitizeRenderedMarkdown", script)
        self.assertIn("DOMPurify.sanitize", script)
        self.assertIn("return sanitizeRenderedMarkdown(marked.parse(normalized));", script)


if __name__ == "__main__":
    unittest.main()
