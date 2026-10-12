"""End-to-end test: the real app window talking to a simulated Smart Shooter.

Needs pyzmq and a display (runs on the Windows build machine). Skipped otherwise.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import zmq  # noqa: F401
    import tkinter
    tkinter.Tk().destroy()
    CAN_RUN = True
except Exception as e:  # no zmq, no tkinter, or no display
    CAN_RUN = False
    if os.environ.get("REQUIRE_INTEGRATION"):
        raise RuntimeError(f"Integration tests must run on the build machine: {e}")

REQ_PORT, PUB_PORT = 55544, 55543


@unittest.skipUnless(CAN_RUN, "needs pyzmq and a display")
class WithSimulatedSmartShooter(unittest.TestCase):
    def setUp(self):
        from tests.fake_smartshooter import FakeSmartShooter
        self.tmp = tempfile.mkdtemp()
        os.environ["APPDATA"] = os.path.join(self.tmp, "appdata")
        os.makedirs(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger"))
        self.list_path = os.path.join(self.tmp, "students.csv")
        with open(self.list_path, "w", encoding="utf-8") as f:
            f.write("Student Number,First Name,Last Name,Class\n10234,Nimal,Perera,7B\n10235,Amaya,Silva,7B\n")
        with open(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger", "settings.json"), "w") as f:
            json.dump({"last_list": self.list_path, "req_port": REQ_PORT, "pub_port": PUB_PORT}, f)
        self.photos_dir = os.path.join(self.tmp, "photos")
        self.ss = FakeSmartShooter(self.photos_dir, REQ_PORT, PUB_PORT).start()
        from app.gui import App
        self.app = App()

    def tearDown(self):
        self.app._on_close()
        self.ss.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, cond, timeout=10, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            self.app.update()
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {what}")

    def test_full_session(self):
        app, ss = self.app, self.ss
        self.assertEqual(len(app.students), 2)
        self.wait(lambda: app.client.connected and app.download_path == self.photos_dir, what="connection + sync")
        # Default: no shot number, so the app switches Smart Shooter from [Z]_[S] to [Z].
        self.wait(lambda: ss.options["FilenameExpression"] == "[Z]", what="pattern set to [Z]")

        nimal = app.index.find_by_number("10234")
        app.select_student(nimal)
        self.wait(lambda: ss.options["Barcode"] == "10234_Nimal_Perera", what="tag sent to Smart Shooter")
        self.wait(lambda: app.banner_big.cget("bg") == "#DFF3E4", what="green banner")

        ss.shoot()
        ss.shoot()
        self.wait(lambda: len(app.photos.get(nimal.key, ())) == 2, what="two photos counted")
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10234_Nimal_Perera.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10234_Nimal_Perera-1.jpg")))  # 2nd shot
        self.assertEqual(str(app.tree.item(nimal.key, "values")[4]), "2")

        app.next_student()
        self.wait(lambda: ss.options["Barcode"] == "10235_Amaya_Silva", what="next student tag")
        ss.shoot()
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10235_Amaya_Silva.jpg")))
        self.wait(lambda: len(app.photo_log) == 3, what="third photo logged")

        app.clear_student()
        self.wait(lambda: ss.options["Barcode"] == "NO-STUDENT", what="cleared tag")
        ss.shoot()
        self.wait(lambda: len(app.photo_log) == 4, what="unlinked photo logged")
        self.assertEqual(app.photo_log[-1][3], "")

        # Export from the session folder.
        from app.core import ExportOptions, plan_export, run_export, scan_folder
        out = os.path.join(self.tmp, "export")
        plan = plan_export(scan_folder(app.download_path, app.index), app.index, ExportOptions())
        self.assertEqual(run_export(plan, out), [])
        for rel in ("By ID/10234.jpg", "By ID/10235.jpg", "By Name/Nimal Perera.jpg", "By Name/Amaya Silva.jpg"):
            self.assertTrue(os.path.isfile(os.path.join(out, rel)), rel)

    def test_number_only_and_name_only_file_names(self):
        app, ss = self.app, self.ss
        self.wait(lambda: app.client.connected, what="connection")
        self.wait(lambda: ss.options["FilenameExpression"] == "[Z]", what="pattern set to [Z]")
        nimal = app.index.find_by_number("10234")
        app.select_student(nimal)
        self.wait(lambda: ss.options["Barcode"] == "10234_Nimal_Perera", what="default tag")

        app.tag_name.set(False)                       # untick Student name
        app._tag_option_changed(app.tag_name)
        self.wait(lambda: ss.options["Barcode"] == "10234", what="number-only tag")
        self.assertEqual(app.example_filename(), "10234.jpg")
        ss.shoot()
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10234.jpg")))
        self.wait(lambda: len(app.photos.get(nimal.key, ())) == 1, what="number-only photo counted")

        app.tag_name.set(True)
        app.tag_number.set(False)                     # untick Student number
        app._tag_option_changed(app.tag_number)
        self.wait(lambda: ss.options["Barcode"] == "Nimal_Perera", what="name-only tag")
        ss.shoot()
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "Nimal_Perera.jpg")))
        self.wait(lambda: len(app.photos.get(nimal.key, ())) == 2, what="name-only photo counted")

        app.tag_name.set(False)                       # can't untick both
        app._tag_option_changed(app.tag_name)
        self.assertTrue(app.tag_name.get())
        self.assertFalse(app.tag_number.get())

        from app.core import ExportOptions, plan_export, scan_folder
        plan = plan_export(scan_folder(self.photos_dir, app.index), app.index, ExportOptions(which="all"))
        self.assertEqual(len([c for c in plan.copies if c.student is nimal]), 4)   # 2 photos x By ID + By Name
        self.assertEqual(plan.unmatched, [])

        with open(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger", "settings.json")) as f:
            saved = json.load(f)
        self.assertEqual((saved["tag_number"], saved["tag_name"]), (False, True))

    def test_add_shot_number_tick_box(self):
        app, ss = self.app, self.ss
        self.wait(lambda: ss.options["FilenameExpression"] == "[Z]", what="default [Z]")
        app.select_student(app.index.find_by_number("10235"))
        self.wait(lambda: ss.options["Barcode"] == "10235_Amaya_Silva", what="tag")
        ss.shoot()
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10235_Amaya_Silva.jpg")))

        app.shot_number.set(True)
        app._shot_number_changed()
        self.wait(lambda: ss.options["FilenameExpression"] == "[Z]_[S]", what="pattern [Z]_[S]")
        self.assertEqual(app.example_filename(), "10235_Amaya_Silva_001.jpg")
        app.next_student()   # changing student restarts the shot number
        app.select_student(app.index.find_by_number("10234"))
        self.wait(lambda: ss.options["Barcode"] == "10234_Nimal_Perera", what="tag")
        ss.shoot()
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10234_Nimal_Perera_001.jpg")))
        self.wait(lambda: len(app.photo_log) == 2, what="both photos logged")

        app.shot_number.set(False)
        app._shot_number_changed()
        self.wait(lambda: ss.options["FilenameExpression"] == "[Z]", what="back to [Z]")
        with open(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger", "settings.json")) as f:
            self.assertFalse(json.load(f)["shot_number"])

    def test_reconnect_after_smart_shooter_restart(self):
        app = self.app
        self.wait(lambda: app.client.connected, what="first connection")
        self.ss.stop()
        time.sleep(0.5)
        from tests.fake_smartshooter import FakeSmartShooter
        self.ss = FakeSmartShooter(self.photos_dir, REQ_PORT, PUB_PORT).start()
        app.select_student(app.index.find_by_number("10235"))
        self.wait(lambda: self.ss.options["Barcode"] == "10235_Amaya_Silva", timeout=15,
                  what="tag after Smart Shooter restart")


@unittest.skipUnless(CAN_RUN, "needs a display")
class ConvertExistingPhotosWindow(unittest.TestCase):
    """Drives the real 'Convert existing photos' window: preview, then export."""

    def setUp(self):
        import app.gui as gui
        self.gui = gui
        self.tmp = tempfile.mkdtemp()
        os.environ["APPDATA"] = os.path.join(self.tmp, "appdata")
        os.makedirs(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger"))
        self.list_path = os.path.join(self.tmp, "students.csv")
        with open(self.list_path, "w", encoding="utf-8") as f:
            f.write("Student Number,First Name,Last Name\n10234,Nimal,Perera\n10235,Amaya,Silva\n10236,Kasun,Fernando\n")
        with open(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger", "settings.json"), "w") as f:
            json.dump({"last_list": self.list_path, "req_port": REQ_PORT, "pub_port": PUB_PORT}, f)
        self.src = os.path.join(self.tmp, "old photos")
        os.makedirs(self.src)
        for name in ("10234.jpg", "010235.JPG", "99999.jpg", "IMG_1.jpg"):
            with open(os.path.join(self.src, name), "wb") as f:
                f.write(b"x")
        self.dialogs = []
        self._orig = {n: getattr(gui.messagebox, n) for n in ("showinfo", "showerror", "showwarning", "askyesno")}
        for n in self._orig:
            setattr(gui.messagebox, n, lambda *a, _n=n, **k: self.dialogs.append((_n, a[1] if len(a) > 1 else "")) or False)
        self.app = gui.App()

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(self.gui.messagebox, n, f)
        self.app._on_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_opens_without_a_list(self):
        self.app.students = []
        self.app.index = self.gui.StudentIndex([])
        for mode in ("convert", "shoot"):
            dlg = self.gui.ExportDialog(self.app, mode=mode)
            self.app.update()
            self.assertTrue(dlg.winfo_exists())
            self.assertIn("No student list", dlg.list_label.cget("text"))
            self.assertFalse(dlg.preview())
            self.assertEqual(self.dialogs[-1][0], "showerror")
            dlg.destroy()

    def test_buttons_open_the_window(self):
        before = set(self.app.winfo_children())
        for b in self._find_buttons(self.app, "Convert existing photos"):
            b.invoke()
        self.app.update()
        new = [w for w in self.app.winfo_children() if w not in before and isinstance(w, self.gui.ExportDialog)]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].title(), "Convert existing photos")
        new[0].destroy()

    def _find_buttons(self, w, text):
        out = []
        for c in w.winfo_children():
            try:
                if c.cget("text") == text:
                    out.append(c)
            except Exception:
                pass
            out += self._find_buttons(c, text)
        return out

    def test_preview_and_export(self):
        self.assertEqual(len(self.app.students), 3)
        dlg = self.gui.ExportDialog(self.app, mode="convert")
        self.app.update()
        dlg.set_sources([self.src])
        dest = os.path.join(self.tmp, "exported")
        dlg.dest.set(dest)
        self.assertTrue(dlg.preview(), self.dialogs)
        statuses = sorted(dlg.table.item(i, "values")[3] for i in dlg.table.get_children())
        self.assertEqual(statuses, ["Matched", "Matched", "Not on list", "Not recognised"])
        dlg.export()
        end = time.time() + 15
        while dlg.export_result is None and time.time() < end:
            self.app.update()
            time.sleep(0.05)
        self.assertIsNotNone(dlg.export_result, f"export never finished; dialogs: {self.dialogs}")
        self.assertEqual(dlg.export_result[1], [])
        for rel in ("By ID/10234.jpg", "By ID/10235.jpg", "By Name/Nimal Perera.jpg", "By Name/Amaya Silva.jpg",
                    "export_report.csv", "students_without_photo.csv"):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
        dlg.destroy()

    def test_two_folders_into_class_folders(self):
        with open(self.list_path, "w", encoding="utf-8") as f:
            f.write("Student Number,First Name,Last Name,Class\n10234,Nimal,Perera,7B\n"
                    "10235,Amaya,Silva,7C\n10236,Kasun,Fernando,7C\n")
        self.app.load_list(self.list_path)
        second = os.path.join(self.tmp, "more photos")
        os.makedirs(second)
        with open(os.path.join(second, "10236.jpg"), "wb") as f:
            f.write(b"x")
        dlg = self.gui.ExportDialog(self.app, mode="convert")
        self.app.update()
        dlg.set_sources([self.src, second, self.src])  # duplicate ignored
        self.assertEqual(dlg.folder_list.size(), 2)
        dlg.by_class.set(True)
        dest = os.path.join(self.tmp, "by class")
        dlg.dest.set(dest)
        self.assertTrue(dlg.preview(), self.dialogs)
        dlg.export()
        end = time.time() + 15
        while dlg.export_result is None and time.time() < end:
            self.app.update()
            time.sleep(0.05)
        self.assertEqual(dlg.export_result[1], [])
        for rel in ("By Name/7B/Nimal Perera.jpg", "By Name/7C/Amaya Silva.jpg", "By Name/7C/Kasun Fernando.jpg",
                    "By ID/7C/10236.jpg"):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
        self.assertEqual(self.app.settings["convert_sources"], [os.path.normpath(self.src), os.path.normpath(second)])
        dlg.destroy()


if __name__ == "__main__":
    unittest.main()
