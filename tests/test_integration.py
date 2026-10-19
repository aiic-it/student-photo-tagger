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
TEST_TIME_LIMIT = 120   # seconds; a hung test dumps where it was stuck and stops the run

CALLBACK_ERRORS: list = []
_saved: dict = {}


def setUpModule():
    """Pop-up messages would wait for a click forever on the build machine, so record them instead,
    and make any error inside the app fail loudly rather than opening an error box."""
    if not CAN_RUN:
        return
    import faulthandler
    import traceback
    from tkinter import messagebox
    import app.gui as gui
    for n in ("showinfo", "showerror", "showwarning", "askyesno", "askokcancel"):
        _saved[n] = getattr(messagebox, n)
        setattr(messagebox, n, lambda *a, _n=n, **k: print(f"[dialog {_n}] {a[1] if len(a) > 1 else ''}") or False)
    _saved["report"] = gui.App.report_callback_exception

    def record(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        CALLBACK_ERRORS.append(text)
        print("CALLBACK ERROR\n" + text, file=sys.stderr)

    gui.App.report_callback_exception = record
    faulthandler.enable()


def tearDownModule():
    if not CAN_RUN:
        return
    from tkinter import messagebox
    import app.gui as gui
    for n, f in _saved.items():
        if n == "report":
            gui.App.report_callback_exception = f
        else:
            setattr(messagebox, n, f)


class _Guard(unittest.TestCase):
    """Every window test: time limit, and fail if the app hit an error inside a callback."""

    def run(self, result=None):
        import faulthandler
        faulthandler.dump_traceback_later(TEST_TIME_LIMIT, exit=True)
        if result is not None and not getattr(result, "_guard_wrapped", False):
            # Print each error straight away (short form), so it's visible even if the run later stops.
            import traceback
            for name in ("addError", "addFailure"):
                orig = getattr(result, name)

                def wrapped(test, err, _orig=orig, _name=name):
                    try:
                        lines = traceback.format_exception_only(err[0], err[1])
                        tb = traceback.extract_tb(err[2])[-6:]
                        where = "".join(f"  {f.filename}:{f.lineno} in {f.name}\n    {f.line}\n" for f in tb)
                        sys.stderr.write(f"\n[{_name}] {test.id()}\n{where}{''.join(lines)}\n")
                        sys.stderr.flush()
                    except Exception:
                        pass
                    return _orig(test, err)

                setattr(result, name, wrapped)
            result._guard_wrapped = True
        CALLBACK_ERRORS.clear()
        self.addCleanup(self._check_callbacks)
        try:
            return super().run(result)
        finally:
            faulthandler.cancel_dump_traceback_later()

    def _check_callbacks(self):
        if CALLBACK_ERRORS:
            errors, CALLBACK_ERRORS[:] = list(CALLBACK_ERRORS), []
            self.fail("Error inside the app during the test:\n" + "\n".join(errors))


@unittest.skipUnless(CAN_RUN, "needs pyzmq and a display")
class WithSimulatedSmartShooter(_Guard):
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
        self.addCleanup(lambda: self.ss.stop())   # also runs if setUp fails
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
        self.assertEqual(str(app.tree.item(nimal.key, "values")[5]), "2")

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

    def test_last_photo_preview_and_best_shot(self):
        app, ss = self.app, self.ss
        self.wait(lambda: ss.options["FilenameExpression"] == "[Z]", what="default [Z]")
        nimal = app.index.find_by_number("10234")
        app.select_student(nimal)
        self.wait(lambda: ss.options["Barcode"] == "10234_Nimal_Perera", what="tag")
        ss.shoot()
        time.sleep(1.1)        # different file times so 'newest' is clear
        ss.shoot()
        self.wait(lambda: len(app.photos.get(nimal.key, {})) == 2, what="two photos")

        # Last-photo preview shows a real picture and who it belongs to.
        self.wait(lambda: str(app.last_img.cget("image")) != "", what="last photo preview")
        self.assertIn("Nimal Perera", app.last_caption.cget("text"))
        self.assertIn("10234_Nimal_Perera-1.jpg", app.last_caption.cget("text"))

        # The strip shows both photos, newest first, as pictures.
        self.wait(lambda: len(app.strip_cells) == 2 and
                  all(str(c[0].cget("image")) for c in app.strip_cells.values()), what="strip thumbnails")
        paths = list(app.strip_cells)
        self.assertTrue(paths[0].endswith("10234_Nimal_Perera-1.jpg"))

        # Star the older one; export now uses it instead of the newest.
        older = paths[1]
        app.toggle_best(older)
        self.assertEqual(app.strip_cells[older][1].cget("text"), "★ Best")
        with open(os.path.join(self.photos_dir, "best_photos.json")) as f:
            self.assertEqual(json.load(f), {nimal.key: "10234_Nimal_Perera.jpg"})
        from app.core import ExportOptions, load_best, plan_export, scan_folder
        plan = plan_export(scan_folder(self.photos_dir, app.index), app.index,
                           ExportOptions(by_name=False), load_best([self.photos_dir]))
        self.assertEqual([os.path.basename(c.src) for c in plan.copies if c.student is nimal],
                         ["10234_Nimal_Perera.jpg"])

        # Clicking the starred photo again removes the star.
        app.toggle_best(older)
        self.assertEqual(app.strip_cells[older][1].cget("text"), "☆ Star")
        self.assertEqual(load_best([self.photos_dir]), {})

        # An unlinked photo shows a warning under the preview.
        app.clear_student()
        self.wait(lambda: ss.options["Barcode"] == "NO-STUDENT", what="cleared")
        ss.shoot()
        self.wait(lambda: "NOT linked" in app.last_caption.cget("text"), what="unlinked caption")

    def test_add_student_on_the_spot(self):
        app, ss = self.app, self.ss
        self.wait(lambda: app.client.connected, what="connection")
        import app.gui as gui
        dlg = gui.AddStudentDialog(app)
        app.update()
        dlg.vars["number"].set("10234")
        dlg.vars["first"].set("Someone")
        dlg.submit()
        self.assertIn("already Nimal Perera", dlg.error.cget("text"))
        dlg.vars["number"].set("20001")
        dlg.vars["first"].set("")
        dlg.submit()
        self.assertIn("name", dlg.error.cget("text"))
        dlg.vars["first"].set("Tharindu")
        dlg.vars["last"].set("Bandara")
        dlg.vars["group"].set("7B")
        dlg.submit()
        app.update()
        new = app.index.find_by_number("20001")
        self.assertIsNotNone(new)
        self.assertTrue(new.added)
        self.assertIs(app.current, new)
        self.assertIn("(added)", app.tree.item(new.key, "values")[2])
        self.wait(lambda: ss.options["Barcode"] == "20001_Tharindu_Bandara", what="tag for added student")
        ss.shoot()
        self.wait(lambda: len(app.photos.get(new.key, {})) == 1, what="added student's photo counted")

        # Kept when the list is reloaded...
        app.load_list(self.list_path, quiet=True)
        self.assertIsNotNone(app.index.find_by_number("20001"))
        # ...and dropped from 'added' once the school's list includes them.
        with open(self.list_path, "a", encoding="utf-8") as f:
            f.write("20001,Tharindu,Bandara,7B\n")
        app.load_list(self.list_path, quiet=True)
        self.assertFalse(app.index.find_by_number("20001").added)
        self.assertEqual(app.settings.get("added_students"), [])

    def test_reconnect_after_smart_shooter_restart(self):
        app = self.app
        self.wait(lambda: app.client.connected, what="first connection")
        self.ss.stop()
        time.sleep(0.5)
        from tests.fake_smartshooter import FakeSmartShooter
        self.ss = FakeSmartShooter(self.photos_dir, REQ_PORT, PUB_PORT).start()
        self.addCleanup(lambda: self.ss.stop())   # also runs if setUp fails
        app.select_student(app.index.find_by_number("10235"))
        self.wait(lambda: self.ss.options["Barcode"] == "10235_Amaya_Silva", timeout=15,
                  what="tag after Smart Shooter restart")


@unittest.skipUnless(CAN_RUN, "needs a display")
class ConvertExistingPhotosWindow(_Guard):
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


@unittest.skipUnless(CAN_RUN, "needs pyzmq and a display")
class CampusSortingAndHistory(_Guard):
    def setUp(self):
        from tests.fake_smartshooter import FakeSmartShooter
        import app.gui as gui
        self.gui = gui
        self.tmp = tempfile.mkdtemp()
        os.environ["APPDATA"] = os.path.join(self.tmp, "appdata")
        os.makedirs(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger"))
        self.list_path = os.path.join(self.tmp, "students.csv")
        with open(self.list_path, "w", encoding="utf-8") as f:
            f.write("Student ID,First Name,Surname,Class,Campus\n"
                    "10,Zara,Abeysekara,10A,North\n9,Amaya,Silva,7B,South\n100,Nimal,Perera,7B,North\n")
        with open(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger", "settings.json"), "w") as f:
            json.dump({"last_list": self.list_path, "req_port": REQ_PORT, "pub_port": PUB_PORT}, f)
        self.photos_dir = os.path.join(self.tmp, "photos")
        self.ss = FakeSmartShooter(self.photos_dir, REQ_PORT, PUB_PORT).start()
        self.addCleanup(lambda: self.ss.stop())   # also runs if setUp fails
        self.dialogs = []
        self._orig = {n: getattr(gui.messagebox, n) for n in ("showinfo", "showerror", "showwarning", "askyesno")}
        for n in self._orig:
            setattr(gui.messagebox, n, lambda *a, _n=n, **k: self.dialogs.append((_n, a[1] if len(a) > 1 else "")) or False)
        self.app = gui.App()

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(self.gui.messagebox, n, f)
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

    def shown(self):
        return [self.app.tree.item(i, "values")[2] for i in self.app.tree.get_children()]

    def test_campus_column_sorting_and_filter(self):
        app = self.app
        self.assertEqual(app.tree.item("10", "values")[4], "North")
        app.sort_by.set("Student number"); app._sort_changed()
        self.assertEqual(self.shown(), ["Amaya Silva", "Zara Abeysekara", "Nimal Perera"])     # 9, 10, 100
        app.sort_by.set("Surname"); app._sort_changed()
        self.assertEqual(self.shown(), ["Zara Abeysekara", "Nimal Perera", "Amaya Silva"])
        app.sort_by.set("First name"); app._sort_changed()
        self.assertEqual(self.shown(), ["Amaya Silva", "Nimal Perera", "Zara Abeysekara"])
        app.sort_by.set("Campus"); app._sort_changed()
        self.assertEqual(self.shown(), ["Nimal Perera", "Zara Abeysekara", "Amaya Silva"])      # North 7B, 10A; South
        app.sort_from_heading("Campus")                                                          # click again = reverse
        self.assertTrue(app.sort_reverse.get())
        self.assertEqual(self.shown(), ["Amaya Silva", "Zara Abeysekara", "Nimal Perera"])
        self.assertTrue(app.tree.heading("campus", "text").endswith("▼"))
        app.sort_from_heading("Student number")
        self.assertEqual(self.shown(), ["Amaya Silva", "Zara Abeysekara", "Nimal Perera"])
        self.assertEqual(app.settings["sort_by"], "Student number")
        app.campus_filter.set("North"); app.refresh_list()
        self.assertEqual(self.shown(), ["Zara Abeysekara", "Nimal Perera"])
        self.assertIn("South", app.campus_box.cget("values"))
        # Next student follows the order on screen.
        app.next_student(); app.update()
        self.assertEqual(app.current.number, "10")
        app.next_student(); app.update()
        self.assertEqual(app.current.number, "100")

    def test_export_into_campus_folders_and_history(self):
        app, ss = self.app, self.ss
        self.wait(lambda: app.client.connected and ss.options["FilenameExpression"] == "[Z]", what="connected")
        for number in ("100", "9"):
            app.select_student(app.index.find_by_number(number))
            self.wait(lambda: ss.options["Barcode"].startswith(number + "_"), what=f"tag {number}")
            ss.shoot()
        self.wait(lambda: len(app.photo_log) == 2, what="photos")
        app.toggle_best(app.student_photo_paths(app.index.find_by_number("9"))[0])

        dlg = self.gui.ExportDialog(app, mode="shoot")
        app.update()
        dlg.by_campus.set(True)
        dlg.by_class.set(True)
        dest = os.path.join(self.tmp, "export")
        dlg.dest.set(dest)
        self.assertTrue(dlg.preview(), self.dialogs)
        dlg.export()
        self.wait(lambda: dlg.export_result is not None, what="export")
        for rel in ("By Name/North/7B/Nimal Perera.jpg", "By Name/South/7B/Amaya Silva.jpg",
                    "By ID/North/7B/100.jpg", "By ID/South/7B/9.jpg"):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
        with open(os.path.join(dest, "export_report.csv"), encoding="utf-8-sig") as f:
            self.assertIn("Campus", f.readline())
        dlg.destroy()

        # History records it all, and survives a restart.
        h = app.history
        self.assertEqual(h.student_rows("nimal")[0]["photos"], 1)
        self.assertEqual(h.student_rows("amaya")[0]["starred"], "9_Amaya_Silva.jpg")
        self.assertEqual(h.exports()[0]["copies"], 4)
        self.assertEqual(h.photos_for("100")[0]["times_exported"], 2)
        win = app.open_history()
        app.update()
        self.assertEqual(len(win.s_tree.get_children()), 3)
        win.s_tree.selection_set("100"); app.update()
        self.assertEqual(len(win.sp_tree.get_children()), 1)
        self.assertEqual(len(win.e_tree.get_children()), 1)
        self.assertGreaterEqual(len(win.d_tree.get_children()), 1)
        kinds = [win.a_tree.item(i, "values")[1] for i in win.a_tree.get_children()]
        for k in ("Export", "Starred", "Photo saved", "Selected", "Loaded list"):
            self.assertIn(k, kinds)
        out = os.path.join(self.tmp, "history.csv")
        self.assertEqual(h.save_csv(out), 2)
        win.destroy()

        app._on_close()
        self.app = self.gui.App()
        self.assertEqual(self.app.history.student_rows("nimal")[0]["photos"], 1)


@unittest.skipUnless(CAN_RUN, "needs pyzmq and a display")
class LoadFromTass(_Guard):
    def setUp(self):
        import app.gui as gui
        from tests.fake_tass import FakeTass, make_students
        self.gui = gui
        self.tmp = tempfile.mkdtemp()
        os.environ["APPDATA"] = os.path.join(self.tmp, "appdata")
        os.makedirs(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger"))
        with open(os.path.join(os.environ["APPDATA"], "StudentPhotoTagger", "settings.json"), "w") as f:
            json.dump({"req_port": REQ_PORT, "pub_port": PUB_PORT}, f)
        rows = make_students(40)
        rows[0].update({"preferred_name": "Andy", "privacy_flg": True})
        rows[1].update({"dol": "2020-12-31"})                  # left the school
        self.tass = FakeTass(rows).start()
        self.addCleanup(self.tass.stop)
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

    def wait(self, cond, timeout=15, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            self.app.update()
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {what}; dialogs: {self.dialogs}")

    def test_settings_test_save_and_load(self):
        from tests.fake_tass import KEY, SECRET
        from app.tass_window import TassSettingsDialog
        app = self.app
        dlg = TassSettingsDialog(app)
        app.update()
        dlg.url.set(self.tass.url)
        dlg.key.set(KEY)
        dlg.secret.set("wrong")
        dlg.test()
        self.wait(lambda: "didn't accept" in dlg.status.cget("text"), what="wrong secret message")
        dlg.secret.set(SECRET)
        dlg.test()
        self.wait(lambda: dlg.status.cget("text").startswith("Connected"), what="connected")
        self.assertEqual(dlg.company.get(), "10")
        self.assertIn("39 current students in TASS, 39 match", dlg.status.cget("text"))
        self.assertIn("NTH, STH", dlg.status.cget("text"))
        dlg.campuses.set("NTH")
        dlg.save(load=True)
        self.wait(lambda: len(app.students) == 19, what="NTH students loaded")   # 20 odd rows are NTH; row 1 has left
        self.assertNotIn(SECRET, json.dumps(app.settings))                    # stored encrypted, never plain
        self.assertEqual({s.campus for s in app.students}, {"NTH"})
        self.assertIsNone(app.index.find_by_number("0010001"))                 # left the school

        # Privacy flag shows in the list and the banner.
        app.campus_filter.set("All campuses")
        dlg2 = TassSettingsDialog(app)
        dlg2.campuses.set("")
        dlg2.save(load=True)
        self.wait(lambda: len(app.students) == 39, what="all current students")
        andy = app.index.find_by_number("0010000")
        self.assertEqual((andy.first, andy.privacy), ("Andy", True))
        self.assertTrue(app.tree.item(andy.key, "values")[2].startswith("⚠"))
        app.select_student(andy)
        app.update()
        self.assertIn("PRIVACY FLAG", app.banner_detail.cget("text"))

        # Next start uses the saved copy of the TASS list (works without the network).
        app._on_close()
        self.tass.stop()
        self.app = self.gui.App()
        self.assertEqual(len(self.app.students), 39)
        self.assertTrue(self.app.index.find_by_number("0010000").privacy)

        # A failed refresh says so and keeps the current list.
        from app.tass_window import load_from_tass
        load_from_tass(self.app)
        self.wait(lambda: any(d[0] == "showerror" for d in self.dialogs), what="error dialog")
        self.assertIn("Can't reach TASS", [d for d in self.dialogs if d[0] == "showerror"][-1][1])
        self.assertEqual(len(self.app.students), 39)


if __name__ == "__main__":
    unittest.main()
