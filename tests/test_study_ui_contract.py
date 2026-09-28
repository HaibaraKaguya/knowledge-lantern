from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1] / "static" / "study"

class StudyUiContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "app.js").read_text(encoding="utf-8")

    def test_required_static_assets_exist(self):
        for name in ("index.html", "styles.css", "app.js", "README.md"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_four_views_and_mobile_navigation_exist(self):
        for view in ("capture", "library", "search", "review"):
            self.assertIn(f'id="{view}"', self.html)
            self.assertIn(f'data-view="{view}"', self.html)

    def test_search_exposes_concepts_evidence_and_provenance(self):
        for text in ("从问题提取的知识点", "可能遗漏的知识", "5 条证据", "BM25", "向量", "图谱关联", "为什么推荐"):
            self.assertIn(text, self.html + self.js)
        self.assertIn("aria-expanded", self.html + self.js)

    def test_library_separates_structured_sorting_from_semantic_search(self):
        self.assertIn("按条件精确筛选与排序", self.html)
        self.assertIn("按意思找题，请使用“检索”", self.html)
        self.assertIn('id="sort-records"', self.html)

if __name__ == "__main__":
    unittest.main()
