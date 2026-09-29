"""Small fixtures ensure the documentation checker catches broken navigation."""
import tempfile
import unittest
from pathlib import Path

from check_docs import anchors, check_page


class DocumentationChecks(unittest.TestCase):
    def test_heading_fragments_and_duplicates(self):
        self.assertEqual(anchors("# Intro\n## Update a checkout\n## Update a checkout\n```md\n# Not a heading\n```"), {"intro", "update-a-checkout", "update-a-checkout-1"})

    def test_missing_links_and_anchors_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "README.md"
            (root / "guide.md").write_text("# Guide\n\n## Setup\n")
            page.write_text("# Home\n\n[OK](guide.md#setup)\n[Bad](guide.md#missing)\n[Missing](gone.md)\n[Remote](https://example.com)\n")
            errors = check_page(page, root)
            self.assertEqual(len(errors), 2)
            self.assertTrue(any("missing heading" in e for e in errors))
            self.assertTrue(any("missing local target" in e for e in errors))

    def test_code_examples_are_not_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "README.md"
            page.write_text("# Home\n\n```md\n[Example](missing.md)\n```\n")
            self.assertEqual(check_page(page, root), [])

    def test_blank_lines_inside_code_examples_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "README.md"
            page.write_text("# Home\n\n```python\ndef first(): pass\n\n\ndef second(): pass\n```\n")
            self.assertEqual(check_page(page, root), [])

    def test_unclosed_fences_and_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "README.md"
            page.write_text("# Home\n\n\n```bash\necho hello\n")
            errors = check_page(page, root)
            self.assertEqual(len(errors), 2)
            self.assertTrue(any("unclosed" in e for e in errors))
            self.assertTrue(any("blank lines" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
