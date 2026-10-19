import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import Student  # noqa: E402
from app.update import is_newer, version_tuple  # noqa: E402

try:
    from PIL import Image
    from app.reports import contact_sheets, group_by_class, retake_list_csv, retake_list_pdf, retake_rows
    HAVE_LIBS = True
except ImportError:
    HAVE_LIBS = False


def students():
    return [Student("3", "Cara", "Zed", "7B", "North"), Student("1", "Abe", "Young", "7B", "North", privacy=True),
            Student("2", "Bea", "Xu", "10A", "North"), Student("4", "Dan", "West", "7A", "South"),
            Student("5", "Eve", "Vale", "", "")]


class Updates(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(version_tuple("v1.10.2"), (1, 10, 2))
        self.assertTrue(is_newer("v1.10.0", "1.9.3"))
        self.assertFalse(is_newer("v1.7.0", "1.7.0"))
        self.assertFalse(is_newer("", "1.7.0"))


@unittest.skipUnless(HAVE_LIBS, "needs Pillow and reportlab")
class Reports(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_grouping(self):
        groups = group_by_class(students())
        self.assertEqual([h for h, _ in groups], ["7B — North", "10A — North", "7A — South", "No class"])
        self.assertEqual([s.first for s in groups[0][1]], ["Abe", "Cara"])        # by surname

    def test_contact_sheets(self):
        photo = os.path.join(self.d, "p.jpg")
        Image.new("RGB", (1200, 1600), (120, 130, 150)).save(photo)
        many = [Student(str(100 + i), f"F{i}", f"L{i:02d}", "7B", "North") for i in range(23)] + students()
        out = os.path.join(self.d, "sheets.pdf")
        pages = contact_sheets(many, lambda s: photo if int(s.number) % 3 else None, out, school="Test")
        self.assertEqual(pages, 2 + 1 + 1 + 1)   # 7B has 25 -> 2 pages; 10A, 7A, No class
        with open(out, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"%PDF"))

    def test_retake_list(self):
        st = students()
        best = {st[0].key: "x"}
        rows = retake_rows(st, lambda s: 0 if s.number == "4" else 1, {st[2].key: "eyes closed"}, True, best)
        reasons = {s.number: r for s, r in rows}
        self.assertEqual(reasons, {"1": "No best photo chosen", "2": "Marked for retake (eyes closed)",
                                   "4": "No photo", "5": "No best photo chosen"})
        rows2 = retake_rows(st, lambda s: 0 if s.number == "4" else 1, {}, False, best)
        self.assertEqual([s.number for s, _ in rows2], ["4"])
        out = os.path.join(self.d, "retakes.pdf")
        self.assertEqual(retake_list_pdf(rows, out), 4)
        retake_list_csv(rows, os.path.join(self.d, "retakes.csv"))
        with open(os.path.join(self.d, "retakes.csv"), encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("eyes closed", text)
        self.assertIn("Privacy", text.splitlines()[0])

    def test_empty_reports(self):
        self.assertEqual(contact_sheets([], lambda s: None, os.path.join(self.d, "e.pdf")), 1)
        self.assertEqual(retake_list_pdf([], os.path.join(self.d, "e2.pdf")), 1)


if __name__ == "__main__":
    unittest.main()
