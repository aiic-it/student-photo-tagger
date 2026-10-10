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

        nimal = app.index.find_by_number("10234")
        app.select_student(nimal)
        self.wait(lambda: ss.options["Barcode"] == "10234_Nimal_Perera", what="tag sent to Smart Shooter")
        self.wait(lambda: app.banner_big.cget("bg") == "#DFF3E4", what="green banner")

        ss.shoot()
        ss.shoot()
        self.wait(lambda: len(app.photos.get(nimal.key, ())) == 2, what="two photos counted")
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10234_Nimal_Perera_001.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10234_Nimal_Perera_002.jpg")))
        self.assertEqual(str(app.tree.item(nimal.key, "values")[4]), "2")

        app.next_student()
        self.wait(lambda: ss.options["Barcode"] == "10235_Amaya_Silva", what="next student tag")
        ss.shoot()
        self.assertTrue(os.path.isfile(os.path.join(self.photos_dir, "10235_Amaya_Silva_001.jpg")))
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
        dlg.src.set(self.src)
        dest = os.path.join(self.tmp, "exported")
        dlg.dest.set(dest)
        self.assertTrue(dlg.preview(), self.dialogs)
        statuses = sorted(dlg.table.item(i, "values")[3] for i in dlg.table.get_children())
        self.assertEqual(statuses, ["Matched", "Matched", "No student number", "Not on list"])
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


if __name__ == "__main__":
    unittest.main()
