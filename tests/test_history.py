import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import ExportOptions, Student, StudentIndex, plan_export, scan_folder  # noqa: E402
from app.history import History  # noqa: E402


class HistoryDatabase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.h = History(os.path.join(self.d, "history.db"), "test")
        self.h.start_session("students.csv", self.d)
        self.students = [Student("10234", "Nimal", "Perera", "7B", "North"),
                         Student("10235", "Amaya", "Silva", "7C", "South")]
        self.h.save_students(self.students)

    def tearDown(self):
        self.h.close()

    def photo(self, name):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def test_photos_days_and_students(self):
        a = self.photo("10234_Nimal_Perera.jpg")
        self.assertTrue(self.h.record_photo(a, "10234", "2025-10-10 09:00:00"))
        self.assertFalse(self.h.record_photo(a, "10234"))                     # same file only once
        self.h.record_photo(self.photo("10234_Nimal_Perera-1.jpg"), "10234", "2026-10-12 10:00:00")
        self.h.record_photo(self.photo("IMG_1.jpg"), None, "2026-10-12 10:05:00")
        days = {r["day"]: dict(r) for r in self.h.days()}
        self.assertEqual(days["2026-10-12"]["photos"], 2)
        self.assertEqual(days["2026-10-12"]["unlinked"], 1)
        self.assertEqual(days["2025-10-10"]["students"], 1)
        rows = {r["key"]: r for r in self.h.student_rows()}
        self.assertEqual(rows["10234"]["photos"], 2)
        self.assertEqual(rows["10234"]["campus"], "North")
        self.assertEqual(rows["10235"]["photos"], 0)
        self.assertEqual([r["key"] for r in self.h.student_rows("south")], ["10235"])
        self.assertEqual([r["day"] for r in self.h.photos_for("10234")], ["2026-10-12", "2025-10-10"])

    def test_unlinked_photo_gets_linked_later(self):
        p = self.photo("10235.jpg")
        self.h.record_photos([(p, None, "2026-10-12 08:00:00")])
        self.h.record_photos([(p, "10235", "2026-10-12 08:00:00")])
        self.assertEqual(len(self.h.photos_for("10235")), 1)

    def test_one_star_per_student(self):
        a, b = self.photo("a.jpg"), self.photo("b.jpg")
        self.h.record_photo(a, "10234")
        self.h.record_photo(b, "10234")
        self.h.set_star(a, "10234", True)
        self.h.set_star(b, "10234", True)
        self.assertEqual([r["file_name"] for r in self.h.photos_for("10234") if r["starred"]], ["b.jpg"])
        self.h.set_star(b, "10234", False)
        self.assertEqual([r for r in self.h.photos_for("10234") if r["starred"]], [])
        self.assertEqual([e["kind"] for e in self.h.events(student_key="10234")], ["unstar", "star", "star"])

    def test_exports_are_linked_to_photos(self):
        index = StudentIndex(self.students)
        folder = os.path.join(self.d, "shoot")
        os.makedirs(folder)
        for n in ("10234.jpg", "10235.jpg"):
            with open(os.path.join(folder, n), "wb") as f:
                f.write(b"x")
        photos = scan_folder(folder, index)
        self.h.record_photos([(p.path, p.student.key, "2026-10-12 09:00:00") for p in photos])
        plan = plan_export(photos, index, ExportOptions(by_campus=True))
        self.h.record_export("export", os.path.join(self.d, "out"), [folder], {"by_campus": True}, plan, [])
        self.assertEqual(self.h.exports()[0]["copies"], 4)
        self.assertEqual(self.h.photos_for("10234")[0]["times_exported"], 2)   # By ID + By Name
        out = os.path.join(self.d, "history.csv")
        self.assertEqual(self.h.save_csv(out), 2)
        with open(out, encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("North", text)
        self.assertIn("Picture day", text)

    def test_reopen_keeps_history(self):
        self.h.record_photo(self.photo("10234.jpg"), "10234")
        self.h.close()
        h2 = History(os.path.join(self.d, "history.db"), "test")
        h2.start_session()
        self.assertEqual(len(h2.photos_for("10234")), 1)
        self.assertEqual(len(h2.db.execute("SELECT * FROM sessions").fetchall()), 2)
        h2.close()
        self.h = History(os.path.join(self.d, "history.db"))


if __name__ == "__main__":
    unittest.main()
