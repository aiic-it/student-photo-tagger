import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import (ExportOptions, ListError, StudentIndex, leading_token, load_students,  # noqa: E402
                      make_tag, plan_export, run_export, scan_folder, scan_folders,
                      load_best, set_best)


def write(path, text, enc="utf-8"):
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(text)


def touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")
    if mtime:
        os.utime(path, (mtime, mtime))


class ListLoading(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_named_headers_any_order_and_class(self):
        p = os.path.join(self.d, "s.csv")
        write(p, "Student ID,Surname,Given Name,Roll Class\n010234,Perera,Nimal,7B\n10235,Silva,Amaya,7B\n")
        students, warnings = load_students(p)
        self.assertEqual([s.display for s in students], ["Nimal Perera", "Amaya Silva"])
        self.assertEqual(students[0].group, "7B")
        self.assertEqual(warnings, [])

    def test_headerless_three_columns(self):
        p = os.path.join(self.d, "s.csv")
        write(p, "Nimal,Perera,10234\nAmaya,Silva,10235\n")
        students, _ = load_students(p)
        self.assertEqual(students[1].number, "10235")

    def test_semicolon_and_cp1252(self):
        p = os.path.join(self.d, "s.csv")
        write(p, "First Name;Last Name;Student Number\nJosé;Núñez;1\n", enc="cp1252")
        students, _ = load_students(p)
        self.assertEqual(students[0].display, "José Núñez")

    def test_duplicates_and_missing_numbers_warn(self):
        p = os.path.join(self.d, "s.csv")
        write(p, "first name,last name,student number\nA,B,1\nC,D,001\nE,F,\n")
        students, warnings = load_students(p)
        self.assertEqual(len(students), 1)
        self.assertEqual(len(warnings), 2)

    def test_missing_column_error(self):
        p = os.path.join(self.d, "s.csv")
        write(p, "first name,last name\nA,B\n")
        with self.assertRaises(ListError):
            load_students(p)


class Matching(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.csv")
        write(p, "first name,last name,student number\nNimal,Perera,10234\nAmaya,Silva,10235\n"
                 "Nimal,Perera,10299\nAnne Marie,De Silva,A77\n")
        self.students, _ = load_students(p)
        self.index = StudentIndex(self.students)

    def test_tag(self):
        self.assertEqual(make_tag(self.students[0]), "10234_Nimal_Perera")
        self.assertEqual(make_tag(self.students[3]), "A77_Anne-Marie_De-Silva")

    def test_filename_variants(self):
        for name in ("10234.jpg", "10234_2.jpg", "10234 (1).jpg", "10234-final.JPG", "010234.png",
                     "10234_Nimal_Perera_001.CR3"):
            self.assertEqual(self.index.match_file(name).number, "10234", name)
        self.assertIsNone(self.index.match_file("1023.jpg"))
        self.assertIsNone(self.index.match_file("IMG_4411.jpg"))
        self.assertEqual(self.index.match_file("a77.jpg").number, "A77")
        self.assertEqual(leading_token("  10234 copy.jpg"), "10234")

    def test_match_photo_prefers_barcode(self):
        s = self.index.match_photo("10235_Amaya_Silva", "IMG_0001.JPG")
        self.assertEqual(s.number, "10235")
        self.assertEqual(self.index.match_photo("", "10234_x_001.jpg").number, "10234")


class Export(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = os.path.join(self.d, "s.csv")
        write(p, "first name,last name,student number\nNimal,Perera,10234\nAmaya,Silva,10235\n"
                 "Kasun,Perera,10236\nNimal,Perera,10299\n")
        students, _ = load_students(p)
        self.index = StudentIndex(students)
        self.src = os.path.join(self.d, "photos")
        t = time.time() - 1000
        touch(os.path.join(self.src, "10234_Nimal_Perera_001.jpg"), t)
        touch(os.path.join(self.src, "10234_Nimal_Perera_002.jpg"), t + 10)
        touch(os.path.join(self.src, "10234_Nimal_Perera_002.cr3"), t + 11)
        touch(os.path.join(self.src, "10235.jpg"), t)
        touch(os.path.join(self.src, "99999.jpg"), t)
        touch(os.path.join(self.src, "IMG_4411.jpg"), t)
        touch(os.path.join(self.src, "notes.txt"), t)
        touch(os.path.join(self.src, "sub", "10236.jpg"), t)

    def dests(self, plan):
        return sorted(c.dest.replace("\\", "/") for c in plan.copies)

    def test_scan_statuses(self):
        photos = scan_folder(self.src, self.index)
        status = {os.path.basename(p.path): p.status for p in photos}
        self.assertEqual(status["99999.jpg"], "Not on list")
        self.assertEqual(status["IMG_4411.jpg"], "Not recognised")
        self.assertNotIn("notes.txt", status)
        self.assertNotIn("10236.jpg", status)
        self.assertIn("10236.jpg", {os.path.basename(p.path) for p in scan_folder(self.src, self.index, True)})

    def test_newest_by_name_and_id_keeps_raw_pair(self):
        plan = plan_export(scan_folder(self.src, self.index), self.index, ExportOptions())
        self.assertEqual(self.dests(plan), [
            "By ID/10234.cr3", "By ID/10234.jpg", "By ID/10235.jpg",
            "By Name/Amaya Silva.jpg", "By Name/Nimal Perera (10234).cr3", "By Name/Nimal Perera (10234).jpg",
        ])
        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual([s.number for s in plan.students_without_photo], ["10236", "10299"])

    def test_all_and_last_first(self):
        opts = ExportOptions(by_id=False, which="all", name_format="last_first")
        plan = plan_export(scan_folder(self.src, self.index), self.index, opts)
        self.assertEqual(self.dests(plan), [
            "By Name/Perera, Nimal (10234).jpg", "By Name/Perera, Nimal (10234)_2.cr3",
            "By Name/Perera, Nimal (10234)_2.jpg", "By Name/Silva, Amaya.jpg",
        ])

    def test_first_taken(self):
        plan = plan_export(scan_folder(self.src, self.index), self.index,
                           ExportOptions(by_name=False, which="first"))
        self.assertEqual(self.dests(plan), ["By ID/10234.jpg", "By ID/10235.jpg"])
        src = {os.path.basename(c.src) for c in plan.copies}
        self.assertIn("10234_Nimal_Perera_001.jpg", src)

    def test_run_export_writes_files_and_reports(self):
        out = os.path.join(self.d, "out")
        plan = plan_export(scan_folder(self.src, self.index), self.index, ExportOptions())
        errors = run_export(plan, out)
        self.assertEqual(errors, [])
        self.assertTrue(os.path.isfile(os.path.join(out, "By Name", "Amaya Silva.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(out, "export_report.csv")))
        with open(os.path.join(out, "students_without_photo.csv"), encoding="utf-8-sig") as f:
            self.assertIn("10236", f.read())


class TagModes(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.csv")
        write(p, "first name,last name,student number\nNimal,Perera,10234\nAmaya,Silva,10235\n"
                 "Kasun,Perera,10236\nKasun,Perera,10299\nAnne Marie,De Silva,A77\n")
        students, _ = load_students(p)
        self.index = StudentIndex(students)
        self.s = {st.number: st for st in students}

    def test_tags(self):
        t = self.index.tag
        self.assertEqual(t(self.s["10234"], "id_name"), "10234_Nimal_Perera")
        self.assertEqual(t(self.s["10234"], "id"), "10234")
        self.assertEqual(t(self.s["10234"], "name"), "Nimal_Perera")
        self.assertEqual(t(self.s["10236"], "name"), "Kasun_Perera_10236")   # shared name gets the number
        self.assertEqual(t(self.s["A77"], "name"), "Anne-Marie_De-Silva")

    def test_every_mode_matches_back(self):
        for st in self.s.values():
            for mode in ("id_name", "id", "name"):
                tag = self.index.tag(st, mode)
                self.assertIs(self.index.match_photo(tag, "IMG_0001.JPG"), st, (mode, tag))
                self.assertIs(self.index.match_file(tag + "_001.jpg"), st, (mode, tag))
                self.assertIs(self.index.match_file(tag + "_012.CR3"), st, (mode, tag))

    def test_name_forms(self):
        m = self.index.match_file
        self.assertEqual(m("Nimal Perera.jpg").number, "10234")
        self.assertEqual(m("Nimal Perera_2.jpg").number, "10234")
        self.assertEqual(m("Perera, Nimal.jpg").number, "10234")
        self.assertEqual(m("nimal perera (1).JPG").number, "10234")
        self.assertEqual(m("Kasun Perera (10299).jpg").number, "10299")
        self.assertIsNone(m("Kasun Perera.jpg"))           # two Kasun Pereras: ambiguous
        self.assertIsNone(m("Nimal.jpg"))
        self.assertIsNone(m("IMG_4411.jpg"))


class ClassesAndFolders(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = os.path.join(self.d, "s.csv")
        write(p, "Student Number,First Name,Last Name,Class\n10234,Nimal,Perera,7B\n10235,Amaya,Silva,7/C\n"
                 "10236,Kasun,Fernando,\n")
        students, _ = load_students(p)
        self.index = StudentIndex(students)
        t = time.time() - 1000
        self.a = os.path.join(self.d, "2024 photos")
        self.b = os.path.join(self.d, "2025 photos")
        touch(os.path.join(self.a, "10234.jpg"), t)
        touch(os.path.join(self.b, "10234.jpg"), t + 50)
        touch(os.path.join(self.b, "10235.jpg"), t)
        touch(os.path.join(self.a, "10236.jpg"), t)

    def dests(self, plan):
        return sorted(c.dest.replace("\\", "/") for c in plan.copies)

    def test_class_folders(self):
        photos = scan_folders([self.a, self.b], self.index)
        plan = plan_export(photos, self.index, ExportOptions(by_class=True))
        self.assertEqual(self.dests(plan), [
            "By ID/7B/10234.jpg", "By ID/7C/10235.jpg", "By ID/No class/10236.jpg",
            "By Name/7B/Nimal Perera.jpg", "By Name/7C/Amaya Silva.jpg", "By Name/No class/Kasun Fernando.jpg",
        ])

    def test_same_name_in_two_folders_are_two_photos(self):
        photos = scan_folders([self.a, self.b], self.index)
        self.assertEqual(len(photos), 4)
        newest = plan_export(photos, self.index, ExportOptions(by_name=False))
        src = [c.src for c in newest.copies if c.student.number == "10234"]
        self.assertEqual(src, [os.path.join(self.b, "10234.jpg")])
        every = plan_export(photos, self.index, ExportOptions(by_name=False, which="all"))
        self.assertIn("By ID/10234_2.jpg", self.dests(every))

    def test_overlapping_folders_counted_once(self):
        photos = scan_folders([self.d, self.a], self.index, recursive=True)
        self.assertEqual(len(photos), 4)


class StarredPhotos(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = os.path.join(self.d, "s.csv")
        write(p, "Student Number,First Name,Last Name\n10234,Nimal,Perera\n10235,Amaya,Silva\n")
        students, _ = load_students(p)
        self.index = StudentIndex(students)
        self.f = os.path.join(self.d, "shoot")
        t = time.time() - 1000
        for i, name in enumerate(("10234_Nimal_Perera.jpg", "10234_Nimal_Perera-1.jpg", "10234_Nimal_Perera-2.jpg")):
            touch(os.path.join(self.f, name), t + i * 10)
        touch(os.path.join(self.f, "10235.jpg"), t)

    def test_star_overrides_newest_and_first(self):
        star = os.path.join(self.f, "10234_Nimal_Perera-1.jpg")
        set_best(star, "10234")
        best = load_best([self.f])
        self.assertEqual(best, {"10234": star})
        photos = scan_folder(self.f, self.index)
        for which in ("newest", "first"):
            plan = plan_export(photos, self.index, ExportOptions(by_name=False, which=which), best)
            src = {c.student.number: os.path.basename(c.src) for c in plan.copies}
            self.assertEqual(src["10234"], "10234_Nimal_Perera-1.jpg", which)
            self.assertEqual(src["10235"], "10235.jpg")
        plan = plan_export(photos, self.index, ExportOptions(by_name=False, which="all"), best)
        first = [c for c in plan.copies if c.dest.replace("\\", "/") == "By ID/10234.jpg"]
        self.assertEqual(os.path.basename(first[0].src), "10234_Nimal_Perera-1.jpg")
        self.assertEqual(len([c for c in plan.copies if c.student.number == "10234"]), 3)

    def test_unstar_and_restar(self):
        a = os.path.join(self.f, "10234_Nimal_Perera.jpg")
        b = os.path.join(self.f, "10234_Nimal_Perera-2.jpg")
        set_best(a, "10234")
        set_best(b, "10234")              # moving the star replaces it
        self.assertEqual(load_best([self.f])["10234"], b)
        set_best(a, "10234", starred=False)   # un-starring a different photo changes nothing
        self.assertEqual(load_best([self.f])["10234"], b)
        set_best(b, "10234", starred=False)
        self.assertEqual(load_best([self.f]), {})

    def test_star_file_is_not_a_photo(self):
        set_best(os.path.join(self.f, "10235.jpg"), "10235")
        names = {os.path.basename(p.path) for p in scan_folder(self.f, self.index)}
        self.assertNotIn("best_photos.json", names)


if __name__ == "__main__":
    unittest.main()
