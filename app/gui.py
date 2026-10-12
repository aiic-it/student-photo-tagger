"""Student Photo Tagger - main window."""
from __future__ import annotations

import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .core import (SORT_OPTIONS, sort_students, ExportOptions, ListError, Student, StudentIndex, existing_conflicts,
                   load_best, load_students, make_tag, plan_export, run_export,
                   scan_folder, scan_folders, set_best, write_student_list)
from .ss_client import SmartShooterClient
from .thumbs import Thumbnailer
from .history import History
from .history_window import HistoryWindow

try:
    from PIL import ImageTk
except ImportError:  # pragma: no cover
    ImageTk = None

LAST_PREVIEW = 150     # px, photo beside the banner
STRIP_PREVIEW = 110    # px, photos of the selected student
STRIP_MAX = 8

APP_NAME = "Student Photo Tagger"
NO_STUDENT_TAG = "NO-STUDENT"

COLOURS = {
    "idle":    ("#EEEEEE", "#444444"),
    "pending": ("#FFF4D6", "#7A4B00"),
    "ok":      ("#DFF3E4", "#1E5E2F"),
    "error":   ("#FBE3E3", "#8A1F1F"),
}


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "StudentPhotoTagger")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.json")


def open_folder(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("1050x700")
        self.minsize(820, 560)
        self.settings = self._load_settings()

        self.students: list[Student] = []
        self.index = StudentIndex([])
        self.current: Student | None = None
        self.sent_tag: str | None = None
        self.ss_tag: str | None = None
        self.download_path: str = self.settings.get("download_path", "")
        self.photos: dict[str, dict[str, str]] = {}  # student key -> {file name (lower): full path}
        self.thumbs = Thumbnailer()
        self._thumb_handlers: dict[str, object] = {}
        self._tk_images: dict[str, object] = {}
        self.last_photo: tuple[str, Student | None] | None = None
        self.best: dict[str, str] = {}
        self.seen_photo_keys: set[str] = set()
        self.photo_log: list[list[str]] = []
        self.was_connected = False
        self.last_sync_attempt = 0.0

        self.history: History | None = None
        try:
            self.history = History(os.path.join(os.path.dirname(settings_path()), "history.db"), __version__)
            self.history.start_session(photo_folder=self.download_path)
        except Exception as e:  # history is a bonus; never stop the app
            self.history = None
            self._history_error = str(e)

        self._build_ui()
        if self.history is None:
            self.log(f"History is off: couldn't open the history database ({self._history_error})", warn=True)
        self.client = self._new_client()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        last = self.settings.get("last_list")
        if last and os.path.isfile(last):
            try:
                self.load_list(last, quiet=True)
            except Exception:
                self.client.stop()      # don't leave background threads running if start-up fails
                raise
        self.after(200, self._pump)
        self.after(1000, self._heartbeat)

    def report_callback_exception(self, exc, val, tb) -> None:
        import traceback
        detail = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(os.path.join(os.path.dirname(settings_path()), "error_log.txt"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + detail + "\n")
        except OSError:
            pass
        messagebox.showerror(APP_NAME, f"Something went wrong:\n\n{val}\n\nDetails were saved to "
                                       f"%APPDATA%\\StudentPhotoTagger\\error_log.txt")

    def hist(self, method: str, *args, **kwargs):
        """Call the history database without ever letting a database problem stop the app."""
        if self.history is None:
            return None
        try:
            return getattr(self.history, method)(*args, **kwargs)
        except Exception as e:
            if not getattr(self, "_hist_warned", False):
                self._hist_warned = True
                self.log(f"Couldn't save to history: {e}", warn=True)
            return None

    # =================================================================== settings
    def _load_settings(self) -> dict:
        try:
            with open(settings_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_settings(self) -> None:
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except OSError:
            pass

    def _new_client(self) -> SmartShooterClient:
        c = SmartShooterClient(host=self.settings.get("host", "127.0.0.1"),
                               req_port=int(self.settings.get("req_port", 54544)),
                               pub_port=int(self.settings.get("pub_port", 54543)))
        c.start()
        return c

    # =================================================================== layout
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24)
        base_font = ("Segoe UI", 10)
        self.option_add("*Font", base_font)

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Load student list", command=self.ask_load_list).pack(side="left")
        ttk.Button(bar, text="Export photos", command=lambda: ExportDialog(self, mode="shoot")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Convert existing photos", command=lambda: ExportDialog(self, mode="convert")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="History", command=self.open_history).pack(side="left", padx=(6, 0))
        reports = ttk.Menubutton(bar, text="Reports")
        menu = tk.Menu(reports, tearoff=False)
        menu.add_command(label="Save photo log…", command=self.save_photo_log)
        menu.add_command(label="Save not-yet-photographed list…", command=self.save_missing_list)
        menu.add_command(label="Save added students list…", command=self.save_added_list)
        reports["menu"] = menu
        reports.pack(side="left", padx=(6, 0))
        setup = ttk.Menubutton(bar, text="Smart Shooter")
        smenu = tk.Menu(setup, tearoff=False)
        smenu.add_command(label="Set up file naming…", command=self.apply_naming)
        smenu.add_command(label="Reconnect", command=self.reconnect)
        smenu.add_command(label="Connection address…", command=self.change_host)
        setup["menu"] = smenu
        setup.pack(side="left", padx=(6, 0))
        names = ttk.LabelFrame(self, text="Smart Shooter file names", padding=(8, 2, 8, 6))
        self.tag_number = tk.BooleanVar(value=self.settings.get("tag_number", True))
        self.tag_name = tk.BooleanVar(value=self.settings.get("tag_name", True))
        ttk.Checkbutton(names, text="Student number", variable=self.tag_number,
                        command=lambda: self._tag_option_changed(self.tag_number)).pack(side="left")
        ttk.Checkbutton(names, text="Student name", variable=self.tag_name,
                        command=lambda: self._tag_option_changed(self.tag_name)).pack(side="left", padx=(12, 0))
        self.shot_number = tk.BooleanVar(value=self.settings.get("shot_number", False))
        ttk.Checkbutton(names, text="Add shot number (_001)", variable=self.shot_number,
                        command=self._shot_number_changed).pack(side="left", padx=(24, 0))
        self.tag_example = ttk.Label(names, foreground="#666666")
        self.tag_example.pack(side="left", padx=(16, 0))
        self._names_frame = names
        self.conn_label = ttk.Label(bar, text="Connecting to Smart Shooter…", foreground="#7A4B00")
        self.conn_label.pack(side="right")

        self._names_frame.pack(fill="x", padx=10, pady=(0, 8))
        self._update_tag_example()

        # Banner
        self.banner = tk.Frame(self, bd=0, highlightthickness=1, highlightbackground="#CCCCCC")
        self.banner.pack(fill="x", padx=10, pady=(0, 8))
        self.last_frame = tk.Frame(self.banner, width=LAST_PREVIEW + 40)
        self.last_frame.pack(side="right", fill="y", padx=(0, 10), pady=6)
        self.last_title = tk.Label(self.last_frame, text="Last photo", font=("Segoe UI", 9))
        self.last_title.pack()
        self.last_img = tk.Label(self.last_frame, text="No photos yet", width=22, height=6, cursor="hand2",
                                 relief="flat", bg="#F7F7F7", fg="#777777")
        self.last_img.pack()
        self.last_img.bind("<Button-1>", lambda e: self.last_photo and open_folder(self.last_photo[0]))
        self.last_caption = tk.Label(self.last_frame, text="", font=("Segoe UI", 9), wraplength=LAST_PREVIEW + 40)
        self.last_caption.pack()
        self.banner_text = tk.Frame(self.banner)
        self.banner_text.pack(side="left", fill="both", expand=True)
        self.banner_small = tk.Label(self.banner_text, text="Now shooting", font=("Segoe UI", 10), anchor="w")
        self.banner_small.pack(fill="x", padx=14, pady=(8, 0))
        self.banner_big = tk.Label(self.banner_text, text="No student selected", font=("Segoe UI", 24, "bold"), anchor="w")
        self.banner_big.pack(fill="x", padx=14)
        self.banner_detail = tk.Label(self.banner_text, text="Load a student list, then click a name.", font=("Segoe UI", 10), anchor="w")
        self.banner_detail.pack(fill="x", padx=14)
        btns = tk.Frame(self.banner_text)
        btns.pack(fill="x", padx=14, pady=(6, 10))
        self.banner_btns = btns
        ttk.Button(btns, text="Next student  ▶", command=self.next_student).pack(side="left")
        ttk.Button(btns, text="Clear", command=self.clear_student).pack(side="left", padx=(6, 0))
        self._paint_banner("idle")

        # Body
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(body)
        body.add(left, weight=3)
        search_row = ttk.Frame(left)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        self.search = ttk.Entry(search_row, textvariable=self.search_var)
        self.search.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.search.bind("<Return>", self._search_enter)
        self.search.bind("<Down>", lambda e: self._focus_tree())
        ttk.Button(search_row, text="+ Add student", command=self.add_student_dialog).pack(side="left", padx=(6, 0))
        sort_row = ttk.Frame(left)
        sort_row.pack(fill="x", pady=(0, 6))
        ttk.Label(sort_row, text="Sort by").pack(side="left")
        self.sort_by = tk.StringVar(value=self.settings.get("sort_by", "List order"))
        if self.sort_by.get() not in SORT_OPTIONS:
            self.sort_by.set("List order")
        sort_box = ttk.Combobox(sort_row, textvariable=self.sort_by, values=list(SORT_OPTIONS),
                                state="readonly", width=16)
        sort_box.pack(side="left", padx=(6, 0))
        sort_box.bind("<<ComboboxSelected>>", lambda e: self._sort_changed())
        self.sort_reverse = tk.BooleanVar(value=self.settings.get("sort_reverse", False))
        ttk.Checkbutton(sort_row, text="Reverse (Z→A)", variable=self.sort_reverse,
                        command=self._sort_changed).pack(side="left", padx=(10, 0))
        self.campus_filter = tk.StringVar(value="All campuses")
        self.campus_box = ttk.Combobox(sort_row, textvariable=self.campus_filter, state="readonly", width=18,
                                       values=["All campuses"])
        self.campus_box.pack(side="right")
        self.campus_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())
        ttk.Label(sort_row, text="Show").pack(side="right", padx=(0, 6))

        cols = ("done", "number", "name", "group", "campus", "photos")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        self._headings = {"done": "", "number": "Number", "name": "Name", "group": "Class",
                          "campus": "Campus", "photos": "Photos"}
        heading_sort = {"number": "Student number", "name": "Surname", "group": "Class",
                        "campus": "Campus", "photos": "Photos"}
        for c, w, anchor in (("done", 30, "center"), ("number", 90, "w"), ("name", 220, "w"),
                             ("group", 80, "w"), ("campus", 110, "w"), ("photos", 60, "e")):
            cmd = (lambda k=heading_sort[c]: self.sort_from_heading(k)) if c in heading_sort else None
            self.tree.heading(c, text=self._headings[c], command=cmd)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        self.tree.tag_configure("done", foreground="#1E5E2F")
        self.tree.tag_configure("current", background="#DCEBFA")
        self.tree.tag_configure("added", foreground="#1F4E8C")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        right = ttk.Frame(body)
        body.add(right, weight=2)
        self.progress_label = ttk.Label(right, text="No student list loaded")
        self.progress_label.pack(anchor="w")
        self.strip_box = ttk.LabelFrame(right, text="Photos of the selected student", padding=6)
        self.strip_box.pack(fill="x", pady=(10, 0))
        self.strip_hint = ttk.Label(self.strip_box, foreground="#666666",
                                    text="Click a photo to star it as the best one. Export uses the starred photo.")
        self.strip_hint.pack(anchor="w")
        self.strip_canvas = tk.Canvas(self.strip_box, height=STRIP_PREVIEW + 34, highlightthickness=0)
        self.strip_scroll = ttk.Scrollbar(self.strip_box, orient="horizontal", command=self.strip_canvas.xview)
        self.strip_canvas.configure(xscrollcommand=self.strip_scroll.set)
        self.strip_canvas.pack(fill="x", pady=(4, 0))
        self.strip_scroll.pack(fill="x")
        self.strip = tk.Frame(self.strip_canvas)
        self.strip_canvas.create_window((0, 0), window=self.strip, anchor="nw")
        self.strip.bind("<Configure>", lambda e: self.strip_canvas.configure(scrollregion=self.strip_canvas.bbox("all")))
        self.strip_cells: dict[str, tuple] = {}
        ttk.Label(right, text="Activity", foreground="#666666").pack(anchor="w", pady=(10, 2))
        self.activity = tk.Listbox(right, activestyle="none", highlightthickness=0, borderwidth=1)
        self.activity.pack(fill="both", expand=True)

        self.bind_all("<Control-f>", lambda e: (self.search.focus_set(), self.search.select_range(0, "end")))
        self.bind_all("<Control-n>", lambda e: self.next_student())

    def _paint_banner(self, state: str) -> None:
        bg, fg = COLOURS[state]
        self.banner.configure(bg=bg)
        for w in (self.banner_small, self.banner_big, self.banner_detail, self.banner_btns,
                  self.banner_text, self.last_frame, self.last_title, self.last_caption):
            w.configure(bg=bg)
        for w in (self.banner_small, self.banner_big, self.banner_detail, self.last_title):
            w.configure(fg=fg)
        if not (self.last_photo and self.last_photo[1] is None):
            self.last_caption.configure(fg=fg)

    def log(self, text: str, warn: bool = False) -> None:
        self.activity.insert(0, f"{time.strftime('%H:%M')}  {text}")
        if warn:
            self.activity.itemconfig(0, foreground="#8A1F1F")
        if self.activity.size() > 500:
            self.activity.delete(500, "end")

    # =================================================================== student list
    def ask_load_list(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose the student list",
            filetypes=[("Student lists", "*.csv *.xlsx *.txt"), ("All files", "*.*")])
        if path:
            self.load_list(path)

    def load_list(self, path: str, quiet: bool = False) -> None:
        try:
            students, warnings = load_students(path)
        except (ListError, OSError) as e:
            if not quiet:
                messagebox.showerror(APP_NAME, f"Couldn't read that list.\n\n{e}")
            return
        students, now_listed = self._merge_added(students)
        if now_listed and not quiet:
            self.log(f"{now_listed} student(s) added earlier are now in the list")
        self.students = students
        self.index = StudentIndex(students)
        self.photos = {}
        self.best = {}
        self.current = None
        self.settings["last_list"] = path
        self.save_settings()
        self.hist("save_students", students)
        self.hist("update_session", list_file=path)
        self.hist("log", "list_loaded", None, f"{len(students)} students from {path}")
        self.recount_from_folder()
        self._update_campus_choices()
        self.refresh_list()
        self._show_current()
        self._update_tag_example()
        self.log(f"Loaded {len(students)} students from {os.path.basename(path)}")
        if warnings and not quiet:
            more = f"\n…and {len(warnings) - 10} more" if len(warnings) > 10 else ""
            messagebox.showwarning(APP_NAME, "The list loaded with some issues:\n\n" + "\n".join(warnings[:10]) + more)

    def recount_from_folder(self) -> None:
        if not (self.download_path and os.path.isdir(self.download_path) and self.students):
            return
        try:
            files = scan_folder(self.download_path, self.index)
        except OSError:
            return
        for f in files:
            if f.student:
                self.photos.setdefault(f.student.key, {})[os.path.basename(f.path).lower()] = f.path
        self.hist("record_photos", [(f.path, f.student.key if f.student else None,
                                     time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.mtime)))
                                    for f in files])
        try:
            self.best.update(load_best([self.download_path]))
        except OSError:
            pass

    def refresh_list(self) -> None:
        q = self.search_var.get().strip().lower()
        campus = self.campus_filter.get()
        self.tree.delete(*self.tree.get_children())
        key = SORT_OPTIONS.get(self.sort_by.get(), "list")
        ordered = sort_students(self.students, key, self.sort_reverse.get(),
                                photo_count=lambda st: len(self.photos.get(st.key, ())))
        self._paint_headings(key)
        for s in ordered:
            if campus not in ("", "All campuses") and (s.campus or "(No campus)") != campus:
                continue
            hay = f"{s.number} {s.first} {s.last} {s.last} {s.first} {s.group} {s.campus}".lower()
            if q and not all(part in hay for part in q.split()):
                continue
            self.tree.insert("", "end", iid=s.key, values=self._row_values(s), tags=self._row_tags(s))
        if self.current and self.tree.exists(self.current.key):
            self.tree.selection_set(self.current.key)
            self.tree.see(self.current.key)
        self._update_progress()

    def _row_values(self, s: Student):
        n = len(self.photos.get(s.key, ()))
        name = s.display + ("  (added)" if s.added else "")
        return ("✓" if n else "", s.number, name, s.group, s.campus, n or "")

    # ------------------------------------------------------------------ sorting / campus filter
    def _sort_changed(self) -> None:
        self.settings["sort_by"] = self.sort_by.get()
        self.settings["sort_reverse"] = self.sort_reverse.get()
        self.save_settings()
        self.refresh_list()

    def sort_from_heading(self, label: str) -> None:
        if self.sort_by.get() == label:
            self.sort_reverse.set(not self.sort_reverse.get())
        else:
            self.sort_by.set(label)
            self.sort_reverse.set(False)
        self._sort_changed()

    def _paint_headings(self, key: str) -> None:
        col_for = {"number": "number", "last": "name", "first": "name", "class": "group",
                   "campus": "campus", "photos": "photos"}
        arrow = " ▼" if self.sort_reverse.get() else " ▲"
        for c, text in self._headings.items():
            self.tree.heading(c, text=text + (arrow if col_for.get(key) == c else ""))

    def _update_campus_choices(self) -> None:
        campuses = sorted({s.campus for s in self.students if s.campus}, key=str.lower)
        if any(not s.campus for s in self.students) and campuses:
            campuses.append("(No campus)")
        self.campus_box.configure(values=["All campuses"] + campuses)
        if self.campus_filter.get() not in ["All campuses"] + campuses:
            self.campus_filter.set("All campuses")

    def _row_tags(self, s: Student):
        tags = []
        if self.photos.get(s.key):
            tags.append("done")
        if self.current and s.key == self.current.key:
            tags.append("current")
        if s.added:
            tags.append("added")
        return tags

    def _refresh_row(self, s: Student) -> None:
        if self.tree.exists(s.key):
            self.tree.item(s.key, values=self._row_values(s), tags=self._row_tags(s))
        self._update_progress()

    def _update_progress(self) -> None:
        if not self.students:
            self.progress_label.configure(text="No student list loaded")
            return
        done = sum(1 for s in self.students if self.photos.get(s.key))
        self.progress_label.configure(text=f"{done} of {len(self.students)} students photographed")

    def _search_enter(self, _e=None) -> None:
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])

    def _focus_tree(self) -> None:
        kids = self.tree.get_children()
        if kids:
            self.tree.focus_set()
            if not self.tree.selection():
                self.tree.selection_set(kids[0])
            self.tree.focus(self.tree.selection()[0])

    def _on_tree_select(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        s = self.index.by_key.get(sel[0])
        if s and (not self.current or s.key != self.current.key):
            self.select_student(s)

    # =================================================================== selecting students
    def select_student(self, s: Student) -> None:
        old = self.current
        self.current = s
        if old:
            self._refresh_row(old)
        self._refresh_row(s)
        self.sent_tag = self.tag_for(s)
        self.client.set_tag(self.sent_tag, tag="SetTag:" + self.sent_tag)
        self._show_current(pending=True)
        self._update_tag_example()
        self._refresh_strip()
        self.hist("log", "select", s.key, s.display)
        if self.photos.get(s.key):
            n = len(self.photos[s.key])
            self.log(f"Selected {s.display} ({s.number}) — already has {n} photo{'s' if n != 1 else ''}")
        else:
            self.log(f"Selected {s.display} ({s.number})")

    def next_student(self) -> None:
        kids = list(self.tree.get_children())
        if not kids:
            return
        if self.current and self.current.key in kids:
            i = kids.index(self.current.key) + 1
            if i >= len(kids):
                self.bell()
                return
        else:
            i = 0
        self.tree.selection_set(kids[i])
        self.tree.see(kids[i])

    def clear_student(self) -> None:
        old = self.current
        self.current = None
        self.after_idle(self._refresh_strip)
        self.tree.selection_remove(*self.tree.selection())
        if old:
            self._refresh_row(old)
        self.sent_tag = NO_STUDENT_TAG
        self.client.set_tag(NO_STUDENT_TAG, tag="SetTag:" + NO_STUDENT_TAG)
        self._show_current()

    def _show_current(self, pending: bool = False) -> None:
        s = self.current
        if not s:
            self.banner_big.configure(text="No student selected")
            self.banner_detail.configure(text="Photos taken now won't be linked to a student." if self.students
                                         else "Load a student list, then click a name.")
            self._paint_banner("idle")
            return
        self.banner_big.configure(text=s.display)
        extra = f"  ·  {s.group}" if s.group else ""
        if not self.client.connected and not pending:
            self.banner_detail.configure(text=f"{s.number}{extra}  ·  Smart Shooter is not connected — photos will NOT be named")
            self._paint_banner("error")
        elif pending or self.ss_tag != self.sent_tag:
            self.banner_detail.configure(text=f"{s.number}{extra}  ·  Waiting for Smart Shooter to confirm…")
            self._paint_banner("pending")
        else:
            n = len(self.photos.get(s.key, ()))
            self.banner_detail.configure(text=f"{s.number}{extra}  ·  Ready — photos so far: {n}")
            self._paint_banner("ok")

    # =================================================================== Smart Shooter messages
    def _pump(self) -> None:
        while True:
            try:
                item = self.client.inbox.get_nowait()
            except queue.Empty:
                break
            kind, name, msg = item if len(item) == 3 else (item[0], item[1], None)
            try:
                if kind == "error":
                    self.log(name, warn=True)
                elif kind == "reply":
                    self._on_reply(name, msg)
                elif kind == "event":
                    self._on_event(name, msg)
            except Exception as e:  # keep the app running whatever Smart Shooter sends
                self.log(f"Problem reading a Smart Shooter message: {e}", warn=True)
        while True:
            try:
                key, img = self.thumbs.results.get_nowait()
            except queue.Empty:
                break
            handler = self._thumb_handlers.pop(key, None)
            if handler:
                handler(img)
        self.after(150, self._pump)

    def _on_reply(self, tag: str, msg: dict | None) -> None:
        ok = bool(msg) and msg.get("msg_result", True) is not False
        if tag.startswith("SetTag:"):
            text = tag[len("SetTag:"):]
            if ok:
                self.ss_tag = text
            elif text == self.sent_tag:
                if msg is None:
                    # No answer: keep trying until Smart Shooter is back.
                    self.after(1500, lambda t=text: t == self.sent_tag and self.ss_tag != t
                               and self.client.set_tag(t, tag="SetTag:" + t))
                else:
                    self.log("Smart Shooter didn't accept the student name. Is External API turned on?", warn=True)
            if text == self.sent_tag:
                self._show_current()
        elif tag == "Synchronise" and msg:
            self._on_sync(msg)
        elif tag == "AutoNaming":
            if ok:
                self.log(f"Smart Shooter file naming set to {self.filename_expression}")
            else:
                self.log("Couldn't change Smart Shooter's file naming — use Smart Shooter ▸ Set up file naming",
                         warn=True)
        elif tag == "ApplyNaming":
            if ok:
                messagebox.showinfo(APP_NAME, "Smart Shooter will now name photos like\n" + self.example_filename())
                self.log(f"Smart Shooter file naming set to {self.filename_expression}")
            else:
                messagebox.showerror(APP_NAME, "Couldn't change Smart Shooter's settings. Check that it's running "
                                               "and that External API is turned on in its options.")

    def _on_sync(self, msg: dict) -> None:
        opts = msg.get("OptionsInfo") or {}
        self._read_options(opts)
        photos = msg.get("PhotoInfo") or []
        added = sum(1 for p in photos if self._record_photo(p, live=False))
        if added:
            self.log(f"Found {added} photos already in this Smart Shooter session")
        self.recount_from_folder()
        self.refresh_list()
        if self.current:
            t = self.tag_for(self.current)
            self.sent_tag = t
            self.client.set_tag(t, tag="SetTag:" + t)
        self._show_current()

    def _read_options(self, opts: dict) -> None:
        if "Barcode" in opts:
            self.ss_tag = opts.get("Barcode") or ""
        path = opts.get("DownloadPath")
        if path and path != self.download_path:
            self.download_path = path
            self.settings["download_path"] = path
            self.save_settings()
            self.hist("update_session", photo_folder=path)
        expr = opts.get("FilenameExpression")
        if expr is None or expr == self.filename_expression:
            return
        if expr in ("[Z]", "[Z]_[S]"):
            # Set up by this app before; bring it in line with the 'Add shot number' tick box.
            self.client.apply_naming(self.filename_expression, tag="AutoNaming")
        elif not getattr(self, "_warned_expr", False):
            self._warned_expr = True
            self.log(f"Smart Shooter's file naming is '{expr}', so photos won't be named like "
                     f"{self.example_filename()}. Use Smart Shooter ▸ Set up file naming.", warn=True)

    def _on_event(self, name: str, msg: dict) -> None:
        if name == "OptionsUpdated":
            before = self.ss_tag
            self._read_options(msg)
            if self.ss_tag != before and self.sent_tag and self.ss_tag != self.sent_tag:
                self.log(f"Smart Shooter's tag was changed to '{self.ss_tag}' outside this app", warn=True)
            self._show_current()
        elif name == "PhotoUpdated":
            self._record_photo(msg, live=True)

    def _record_photo(self, p: dict, live: bool) -> bool:
        if p.get("PhotoLocation") != "Local Disk":
            return False
        key = p.get("PhotoKey") or p.get("PhotoUUID") or p.get("PhotoFilename")
        if not key or key in self.seen_photo_keys:
            return False
        self.seen_photo_keys.add(key)
        path = p.get("PhotoFilename") or ""
        name = p.get("PhotoName") or os.path.basename(path)
        if path and os.path.dirname(path) and not self.download_path:
            self.download_path = os.path.dirname(path)
        s = self.index.match_photo(p.get("PhotoBarcode") or "", name)
        self.photo_log.append([time.strftime("%Y-%m-%d %H:%M:%S"), name, path,
                               s.number if s else "", s.first if s else "", s.last if s else ""])
        if s:
            full = path or os.path.join(self.download_path, name)
            self.photos.setdefault(s.key, {})[name.lower()] = full
            if live:
                self._refresh_row(s)
                self.log(f"Saved {name}")
                if self.current and s.key == self.current.key:
                    self._show_current()
                    self._refresh_strip()
        elif live:
            self.log(f"Saved {name} — NOT linked to a student", warn=True)
            self.bell()
        full_path = path or os.path.join(self.download_path, name)
        taken = None
        if not live:
            try:
                taken = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(full_path)))
            except OSError:
                pass
        if self.hist("record_photo", full_path, s.key if s else None, taken):
            self.hist("log", "photo", s.key if s else None, name + ("" if s else " (not linked)"))
        if live and (path or self.download_path):
            self._show_last_photo(full_path, s)
        return True

    def _heartbeat(self) -> None:
        connected = self.client.connected
        now = time.time()
        if connected:
            self.conn_label.configure(text="● Smart Shooter connected", foreground="#1E5E2F")
        else:
            self.conn_label.configure(text="● Smart Shooter not connected", foreground="#8A1F1F")
            if now - self.last_sync_attempt > 8:
                self.last_sync_attempt = now
                self.client.synchronise()
        if connected and not self.was_connected:
            self.log("Connected to Smart Shooter")
        elif self.was_connected and not connected:
            self.log("Lost connection to Smart Shooter", warn=True)
        if connected != self.was_connected:
            self.was_connected = connected
            self._show_current()
        self.after(1000, self._heartbeat)

    # ------------------------------------------------------------------ file-name options
    @property
    def tag_mode(self) -> str:
        if self.tag_number.get() and self.tag_name.get():
            return "id_name"
        return "id" if self.tag_number.get() else "name"

    def tag_for(self, s: Student) -> str:
        return self.index.tag(s, self.tag_mode)

    @property
    def filename_expression(self) -> str:
        return "[Z]_[S]" if self.shot_number.get() else "[Z]"

    def example_filename(self) -> str:
        sample = self.current or (self.students[0] if self.students else Student("10234", "Nimal", "Perera"))
        tag = self.tag_for(sample) if sample in self.students else make_tag(sample, self.tag_mode)
        return f"{tag}_001.jpg" if self.shot_number.get() else f"{tag}.jpg"

    def _shot_number_changed(self) -> None:
        self.settings["shot_number"] = self.shot_number.get()
        self.save_settings()
        self._update_tag_example()
        self.client.apply_naming(self.filename_expression, tag="AutoNaming")
        self.log(f"Smart Shooter file names now look like {self.example_filename()}")

    def _update_tag_example(self) -> None:
        self.tag_example.configure(text="e.g. " + self.example_filename())

    def _tag_option_changed(self, changed: tk.BooleanVar) -> None:
        if not (self.tag_number.get() or self.tag_name.get()):
            changed.set(True)
            self.bell()
            self.log("Keep at least one of Student number / Student name ticked", warn=True)
            return
        self.settings["tag_number"] = self.tag_number.get()
        self.settings["tag_name"] = self.tag_name.get()
        self.save_settings()
        self._update_tag_example()
        self.log(f"Smart Shooter file names now look like {self.example_filename()}")
        self.hist("log", "settings", None, f"file names like {self.example_filename()}")
        if self.current:
            self.sent_tag = self.tag_for(self.current)
            self.client.set_tag(self.sent_tag, tag="SetTag:" + self.sent_tag)
            self._show_current(pending=True)

    def apply_naming(self) -> None:
        extra = (" The shot number restarts for each student." if self.shot_number.get() else
                 " If a student has more than one photo, Smart Shooter adds a number to the later ones"
                 " so nothing is overwritten.")
        if messagebox.askyesno(APP_NAME, f"Set Smart Shooter's filename pattern to {self.filename_expression}?\n\n"
                                         f"Photos will be saved as e.g. {self.example_filename()}.{extra}"):
            self.client.apply_naming(self.filename_expression)

    def reconnect(self) -> None:
        self.client.stop()
        self.client = self._new_client()
        self.last_sync_attempt = 0.0
        self.log("Reconnecting to Smart Shooter…")

    def change_host(self) -> None:
        host = simpledialog.askstring(APP_NAME, "Address of the computer running Smart Shooter\n"
                                                "(127.0.0.1 = this computer):",
                                      initialvalue=self.settings.get("host", "127.0.0.1"), parent=self)
        if host:
            self.settings["host"] = host.strip()
            self.save_settings()
            self.reconnect()

    # =================================================================== reports
    def save_photo_log(self) -> None:
        if not self.photo_log:
            messagebox.showinfo(APP_NAME, "No photos have been taken in this session yet.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="photo_log.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Time", "File", "Full path", "Student number", "First name", "Last name"])
                w.writerows(self.photo_log)
            self.log(f"Saved photo log to {os.path.basename(path)}")

    def save_missing_list(self) -> None:
        if not self.students:
            messagebox.showinfo(APP_NAME, "Load a student list first.")
            return
        missing = [s for s in self.students if not self.photos.get(s.key)]
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="not_yet_photographed.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            write_student_list(missing, path)
            self.log(f"Saved {len(missing)} students not yet photographed")

    def _on_close(self) -> None:
        self.client.stop()
        self.thumbs.stop()
        if self.history:
            self.history.close()
        self.destroy()

    # =================================================================== previews
    def _request_thumb(self, key: str, path: str, size: int, handler) -> None:
        self._thumb_handlers[key] = handler
        self.thumbs.request(key, path, size)

    def _to_tk(self, key: str, img):
        if img is None or ImageTk is None:
            return None
        photo = ImageTk.PhotoImage(img, master=self)
        self._tk_images[key] = photo          # keep a reference or Tk drops the picture
        return photo

    def _show_last_photo(self, path: str, s: Student | None) -> None:
        self.last_photo = (path, s)
        name = os.path.basename(path)
        if s:
            self.last_caption.configure(text=f"{s.display}\n{name}")
        else:
            self.last_caption.configure(text=f"NOT linked to a student\n{name}", fg="#8A1F1F")
        self.last_img.configure(text="Loading…", image="")

        def done(img, path=path):
            if not self.last_photo or self.last_photo[0] != path:
                return
            photo = self._to_tk("last", img)
            if photo:
                self.last_img.configure(image=photo, text="", width=photo.width(), height=photo.height())
            else:
                self.last_img.configure(text="No preview\n(click to open folder)", image="", width=22, height=6)

        self._request_thumb("last", path, LAST_PREVIEW, done)

    def student_photo_paths(self, s: Student) -> list[str]:
        paths = [p for p in self.photos.get(s.key, {}).values() if os.path.isfile(p)]
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return paths

    def _refresh_strip(self) -> None:
        for w in self.strip.winfo_children():
            w.destroy()
        self.strip_cells = {}
        s = self.current
        if not s:
            self.strip_box.configure(text="Photos of the selected student")
            return
        paths = self.student_photo_paths(s)
        star = self.best.get(s.key)
        if star and not any(os.path.normcase(p) == os.path.normcase(star) for p in paths[:STRIP_MAX]):
            if os.path.isfile(star):
                paths = [star] + paths
        self.strip_box.configure(text=f"Photos of {s.display} ({len(paths)})")
        if not paths:
            ttk.Label(self.strip, text="No photos yet.", foreground="#777777").pack(side="left", pady=10)
            return
        for i, path in enumerate(paths[:STRIP_MAX]):
            cell = tk.Frame(self.strip, padx=4)
            cell.pack(side="left", anchor="n")
            pic = tk.Label(cell, text="…", width=14, height=6, bg="#F3F3F3", cursor="hand2", bd=2, relief="flat")
            pic.pack()
            cap = tk.Label(cell, font=("Segoe UI", 9))
            cap.pack()
            for w in (pic, cap):
                w.bind("<Button-1>", lambda e, p=path: self.toggle_best(p))
            self.strip_cells[path] = (pic, cap)
            self._paint_cell(path)
            key = f"strip:{i}:{path}"

            def done(img, path=path, key=key):
                cell = self.strip_cells.get(path)
                if not cell or not cell[0].winfo_exists():
                    return
                photo = self._to_tk(key, img)
                if photo:
                    cell[0].configure(image=photo, text="", width=photo.width(), height=photo.height())
                else:
                    cell[0].configure(text=os.path.splitext(os.path.basename(path))[1].upper().strip(".") or "?")

            self._request_thumb(key, path, STRIP_PREVIEW, done)

    def _paint_cell(self, path: str) -> None:
        pic, cap = self.strip_cells[path]
        starred = self.current and os.path.normcase(self.best.get(self.current.key, "")) == os.path.normcase(path)
        pic.configure(relief="solid" if starred else "flat", highlightthickness=0,
                      bd=3 if starred else 2, bg="#E8B400" if starred else "#F3F3F3")
        cap.configure(text="★ Best" if starred else "☆ Star", fg="#7A4B00" if starred else "#777777")

    def toggle_best(self, path: str) -> None:
        s = self.current
        if not s:
            return
        already = os.path.normcase(self.best.get(s.key, "")) == os.path.normcase(path)
        try:
            if self.best.get(s.key) and not already:
                set_best(self.best[s.key], s.key, starred=False)
            set_best(path, s.key, starred=not already)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save the star.\n\n{e}")
            return
        self.hist("set_star", path, s.key, not already)
        if already:
            self.best.pop(s.key, None)
            self.log(f"Removed the star from {os.path.basename(path)}")
        else:
            self.best[s.key] = path
            self.log(f"Starred {os.path.basename(path)} as the best photo of {s.display}")
        for p in self.strip_cells:
            self._paint_cell(p)

    # =================================================================== added students
    def _merge_added(self, students: list[Student]) -> tuple[list[Student], int]:
        listed = {s.key for s in students}
        keep, now_listed = [], 0
        for d in self.settings.get("added_students", []):
            st = Student(str(d.get("number", "")), d.get("first", ""), d.get("last", ""), d.get("group", ""),
                         d.get("campus", ""), added=True)
            if not st.number:
                continue
            if st.key in listed:
                now_listed += 1
                continue
            keep.append(d)
            students.append(st)
            listed.add(st.key)
        if now_listed:
            self.settings["added_students"] = keep
            self.save_settings()
        return students, now_listed

    def add_student(self, number: str, first: str, last: str, group: str = "", campus: str = "") -> Student:
        st = Student(number.strip(), first.strip(), last.strip(), group.strip(), campus.strip(), added=True)
        self.settings.setdefault("added_students", []).append(
            {"number": st.number, "first": st.first, "last": st.last, "group": st.group, "campus": st.campus})
        self.save_settings()
        self.students.append(st)
        self.index = StudentIndex(self.students)
        self.hist("save_students", [st])
        self.hist("log", "student_added", st.key, f"{st.display}, {st.group}, {st.campus}".strip(", "))
        self.search_var.set("")
        self._update_campus_choices()
        if self.campus_filter.get() not in ("All campuses", st.campus or "(No campus)"):
            self.campus_filter.set("All campuses")
        self.refresh_list()
        self.log(f"Added {st.display} ({st.number})")
        self.tree.selection_set(st.key)
        self.tree.see(st.key)
        return st

    def open_history(self) -> None:
        if self.history is None:
            messagebox.showinfo(APP_NAME, "History isn't available on this PC (the history database couldn't be opened).")
            return
        return HistoryWindow(self)

    def add_student_dialog(self) -> None:
        AddStudentDialog(self)

    def save_added_list(self) -> None:
        added = [s for s in self.students if s.added]
        if not added:
            messagebox.showinfo(APP_NAME, "No students have been added.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="added_students.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            write_student_list(added, path)
            self.log(f"Saved {len(added)} added students")


class AddStudentDialog(tk.Toplevel):
    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Add a student")
        self.transient(app)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="For a student who isn't in the list yet (e.g. a new enrolment).").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.vars = {}
        choices = {"group": sorted({s.group for s in app.students if s.group}),
                   "campus": sorted({s.campus for s in app.students if s.campus})}
        for row, (key, label) in enumerate((("number", "Student number"), ("first", "First name"),
                                            ("last", "Last name"), ("group", "Class"), ("campus", "Campus")), start=1):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            if key in choices:
                w = ttk.Combobox(frm, textvariable=var, values=choices[key], width=28)
                current = getattr(app.current, key, "") if app.current else ""
                if current:
                    var.set(current)
            else:
                w = ttk.Entry(frm, textvariable=var, width=31)
            w.grid(row=row, column=1, sticky="we", padx=(8, 0), pady=3)
            self.vars[key] = var
            if row == 1:
                w.focus_set()
        self.error = ttk.Label(frm, text="", foreground="#8A1F1F")
        self.error.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Add and select", command=self.submit).pack(side="right", padx=(0, 6))
        self.bind("<Return>", lambda e: self.submit())
        self.bind("<Escape>", lambda e: self.destroy())
        for v in self.vars.values():
            v.trace_add("write", lambda *_: self.error.configure(text=""))

    def submit(self) -> None:
        v = {k: var.get().strip() for k, var in self.vars.items()}
        if not v["number"]:
            self.error.configure(text="Enter the student number.")
            return
        if not (v["first"] or v["last"]):
            self.error.configure(text="Enter the student's name.")
            return
        existing = self.app.index.find_by_number(v["number"])
        if existing:
            self.error.configure(text=f"{v['number']} is already {existing.display}.")
            return
        self.app.add_student(v["number"], v["first"], v["last"], v["group"], v["campus"])
        self.destroy()


class ExportDialog(tk.Toplevel):
    """Used for both 'Export photos' (from Smart Shooter) and 'Convert existing photos'."""

    def __init__(self, app: App, mode: str):
        super().__init__(app)
        self.app = app
        self.mode = mode
        self.title("Export photos" if mode == "shoot" else "Convert existing photos")
        self.geometry("900x620")
        self.transient(app)
        s = app.settings
        self.plan = None

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        intro = ("Copies today's photos into clean names." if mode == "shoot"
                 else "Finds photos named by student number and copies them into name format.")

        list_row = ttk.Frame(frm)
        list_row.grid(row=0, column=0, columnspan=3, sticky="we", pady=(0, 8))
        self.list_label = ttk.Label(list_row)
        self.list_label.pack(side="left")
        ttk.Button(list_row, text="Load student list…", command=self._load_list).pack(side="right")
        self._show_list_name()

        if mode == "shoot":
            sources = [app.download_path] if app.download_path else []
        else:
            sources = s.get("convert_sources") or ([s["convert_source"]] if s.get("convert_source") else [])
        ttk.Label(frm, text="Photo folders").grid(row=1, column=0, sticky="nw", pady=2)
        self.folder_list = tk.Listbox(frm, height=4, activestyle="none", exportselection=False)
        self.folder_list.grid(row=1, column=1, sticky="we", padx=6, pady=2)
        fbtns = ttk.Frame(frm)
        fbtns.grid(row=1, column=2, sticky="n", pady=2)
        ttk.Button(fbtns, text="Add folder…", command=self._add_folder).pack(fill="x")
        ttk.Button(fbtns, text="Remove", command=self._remove_folder).pack(fill="x", pady=(4, 0))
        self.set_sources(sources)
        self.dest = tk.StringVar(value=s.get("export_dest", ""))
        self._path_row(frm, 2, "Export to", self.dest)
        self.recursive = tk.BooleanVar(value=s.get("recursive", False))
        ttk.Checkbutton(frm, text="Include subfolders", variable=self.recursive).grid(row=3, column=1, sticky="w")

        opts = ttk.Frame(frm)
        opts.grid(row=4, column=0, columnspan=3, sticky="we", pady=8)
        self.by_name = tk.BooleanVar(value=s.get("by_name", True))
        self.by_id = tk.BooleanVar(value=s.get("by_id", True))
        self.which = tk.StringVar(value=s.get("which", "newest"))
        self.fmt = tk.StringVar(value=s.get("name_format", "first_last"))
        self.by_class = tk.BooleanVar(value=s.get("by_class", False))
        self.by_campus = tk.BooleanVar(value=s.get("by_campus", False))
        g1 = ttk.LabelFrame(opts, text="Make copies", padding=8)
        g1.pack(side="left", fill="y")
        ttk.Checkbutton(g1, text="By Name", variable=self.by_name).pack(anchor="w")
        ttk.Checkbutton(g1, text="By ID", variable=self.by_id).pack(anchor="w")
        ttk.Checkbutton(g1, text="In campus folders", variable=self.by_campus).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(g1, text="In class folders", variable=self.by_class).pack(anchor="w")
        g2 = ttk.LabelFrame(opts, text="Photos per student", padding=8)
        g2.pack(side="left", fill="y", padx=8)
        for val, label in (("newest", "One: starred, else newest"), ("first", "One: starred, else first"),
                           ("all", "All photos (starred first)")):
            ttk.Radiobutton(g2, text=label, value=val, variable=self.which).pack(anchor="w")
        g3 = ttk.LabelFrame(opts, text="Name format", padding=8)
        g3.pack(side="left", fill="y")
        ttk.Radiobutton(g3, text="Nimal Perera.jpg", value="first_last", variable=self.fmt).pack(anchor="w")
        ttk.Radiobutton(g3, text="Perera, Nimal.jpg", value="last_first", variable=self.fmt).pack(anchor="w")

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="we")
        ttk.Button(btns, text="Preview", command=self.preview).pack(side="left")
        self.export_btn = ttk.Button(btns, text="Export", command=self.export)
        self.export_btn.pack(side="left", padx=6)
        self.summary = ttk.Label(btns, text=intro + " Click Preview to check first.")
        self.summary.pack(side="left", padx=8)

        cols = ("file", "student", "new", "status")
        self.table = ttk.Treeview(frm, columns=cols, show="headings")
        for c, label, w in (("file", "Existing file", 200), ("student", "Student", 180),
                            ("new", "New name", 320), ("status", "Status", 120)):
            self.table.heading(c, text=label)
            self.table.column(c, width=w, stretch=(c == "new"))
        self.table.tag_configure("warn", foreground="#8A1F1F")
        self.table.tag_configure("skip", foreground="#888888")
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set)
        self.table.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        sb.grid(row=6, column=2, sticky="ns", pady=(8, 0))
        frm.rowconfigure(6, weight=1)
        self.bar = ttk.Progressbar(frm, mode="determinate")
        self.bar.grid(row=7, column=0, columnspan=3, sticky="we", pady=(8, 0))

        for var in (self.recursive, self.by_name, self.by_id, self.by_class, self.by_campus, self.which, self.fmt):
            var.trace_add("write", lambda *_: self._invalidate())
        self.lift()
        self.focus_set()

    def _path_row(self, frm, row, label, var) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="we", padx=6, pady=2)
        ttk.Button(frm, text="Browse…", command=lambda: self._browse(var)).grid(row=row, column=2, pady=2)

    def _browse(self, var) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=var.get() or None)
        if path:
            var.set(path)

    def _invalidate(self) -> None:
        self.plan = None

    def set_sources(self, folders) -> None:
        self.sources: list[str] = []
        self.folder_list.delete(0, "end")
        for f in folders:
            self._append_source(f)
        self._invalidate()

    def _append_source(self, folder: str) -> None:
        folder = os.path.normpath(folder)
        if any(os.path.normcase(folder) == os.path.normcase(f) for f in self.sources):
            return
        self.sources.append(folder)
        self.folder_list.insert("end", folder)

    def _add_folder(self) -> None:
        start = self.sources[-1] if self.sources else None
        path = filedialog.askdirectory(parent=self, initialdir=start, title="Add a folder of photos")
        if path:
            self._append_source(path)
            self._invalidate()

    def _remove_folder(self) -> None:
        for i in reversed(self.folder_list.curselection()):
            self.folder_list.delete(i)
            del self.sources[i]
        self._invalidate()

    def _show_list_name(self) -> None:
        path = self.app.settings.get("last_list", "")
        if self.app.students:
            self.list_label.configure(text=f"Student list: {os.path.basename(path)} ({len(self.app.students)} students)",
                                      foreground="")
        else:
            self.list_label.configure(text="No student list loaded — load one first", foreground="#8A1F1F")

    def _load_list(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Choose the student list",
                                          filetypes=[("Student lists", "*.csv *.xlsx *.txt"), ("All files", "*.*")])
        if path:
            self.app.load_list(path)
            self._show_list_name()
            self._invalidate()

    def _export_options(self) -> ExportOptions:
        return ExportOptions(by_name=self.by_name.get(), by_id=self.by_id.get(), which=self.which.get(),
                             name_format=self.fmt.get(), by_class=self.by_class.get(),
                             by_campus=self.by_campus.get())

    def preview(self) -> bool:
        if not self.app.students:
            messagebox.showerror(APP_NAME, "Load a student list first (button at the top of this window).", parent=self)
            return False
        if not self.sources:
            messagebox.showerror(APP_NAME, "Add at least one folder of photos.", parent=self)
            return False
        missing = [f for f in self.sources if not os.path.isdir(f)]
        if missing:
            messagebox.showerror(APP_NAME, "These folders can't be found:\n\n" + "\n".join(missing), parent=self)
            return False
        opts = self._export_options()
        if not (opts.by_name or opts.by_id):
            messagebox.showerror(APP_NAME, "Tick By Name, By ID, or both.", parent=self)
            return False
        try:
            photos = scan_folders(self.sources, self.app.index, self.recursive.get())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't read the folder.\n\n{e}", parent=self)
            return False
        try:
            self.best = load_best(self.sources, self.recursive.get())
        except OSError:
            self.best = {}
        self.plan = plan_export(photos, self.app.index, opts, self.best)
        starred = {os.path.normcase(os.path.abspath(p)) for p in self.best.values()}
        new_names: dict[str, list[str]] = {}
        for c in self.plan.copies:
            new_names.setdefault(c.src, []).append(c.dest.replace("\\", "/"))
        self.table.delete(*self.table.get_children())
        for p in photos:
            name = self._short_name(p.path)
            if p.student is None:
                self.table.insert("", "end", values=(name, "—", "—", p.status), tags=("warn",))
            elif p.path in new_names:
                star = os.path.normcase(os.path.abspath(p.path)) in starred
                self.table.insert("", "end", values=(name, p.student.display, "   ".join(new_names[p.path]),
                                                     "Matched ★ best" if star else "Matched"))
            else:
                self.table.insert("", "end", values=(name, p.student.display, "—", "Skipped (older shot)"), tags=("skip",))
        students = len({c.student.key for c in self.plan.copies})
        self.summary.configure(text=f"{len(self.plan.copies)} copies for {students} students  ·  "
                                    f"{len(self.plan.unmatched)} unmatched files  ·  "
                                    f"{len(self.plan.students_without_photo)} students without a photo")
        return True

    def _short_name(self, path: str) -> str:
        best = None
        for f in self.sources:
            try:
                rel = os.path.relpath(path, f)
            except ValueError:  # different drive
                continue
            if not rel.startswith(".."):
                best = rel if len(self.sources) == 1 else os.path.join(os.path.basename(f) or f, rel)
                break
        return best or path

    def export(self) -> None:
        if self.plan is None and not self.preview():
            return
        dest = self.dest.get().strip()
        if not dest:
            messagebox.showerror(APP_NAME, "Choose where to export to.", parent=self)
            return
        if any(os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(f)) for f in self.sources):
            messagebox.showerror(APP_NAME, "Export to a different folder from the photo folders.", parent=self)
            return
        if not self.plan.copies:
            messagebox.showinfo(APP_NAME, "Nothing to export.", parent=self)
            return
        clash = existing_conflicts(self.plan, dest)
        if clash and not messagebox.askyesno(
                APP_NAME, f"{clash} files with the same names are already in the export folder.\n\nReplace them?",
                parent=self):
            return
        self._remember(dest)
        self.export_btn.state(["disabled"])
        plan = self.plan

        # Copy on a background thread; it reports back through a queue that the window polls,
        # because Tk must only be touched from the main thread.
        self._results: "queue.Queue[tuple]" = queue.Queue()
        self.export_result = None

        def work():
            try:
                errors = run_export(plan, dest, progress=lambda n, t: self._results.put(("progress", n, t)))
            except Exception as e:  # never leave the window stuck
                errors = [f"Export stopped: {e}"]
            self._results.put(("done", dest, errors))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll, plan)

    def _poll(self, plan) -> None:
        while True:
            try:
                kind, a, b = self._results.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.bar.configure(maximum=b, value=a)
            else:
                self._done(plan, a, b)
                return
        self.after(100, self._poll, plan)

    def _done(self, plan, dest, errors) -> None:
        self.export_btn.state(["!disabled"])
        copied = len(plan.copies) - len(errors)
        self.export_result = (copied, errors)
        opts = self._export_options()
        self.app.hist("record_export", "export" if self.mode == "shoot" else "convert", dest, list(self.sources),
                      {k: getattr(opts, k) for k in ("by_name", "by_id", "by_class", "by_campus", "which",
                                                     "name_format")}, plan, errors)
        msg = f"Exported {copied} files to:\n{dest}\n\nA report (export_report.csv) is in the same folder."
        if errors:
            msg += f"\n\n{len(errors)} files couldn't be copied — see the report."
        self.app.log(f"Exported {copied} files")
        if messagebox.askyesno(APP_NAME, msg + "\n\nOpen the folder now?", parent=self):
            open_folder(dest)

    def _remember(self, dest) -> None:
        s = self.app.settings
        s.update({"export_dest": dest, "recursive": self.recursive.get(), "by_name": self.by_name.get(),
                  "by_id": self.by_id.get(), "by_class": self.by_class.get(), "by_campus": self.by_campus.get(), "which": self.which.get(),
                  "name_format": self.fmt.get()})
        if self.mode == "convert":
            s["convert_sources"] = list(self.sources)
        self.app.save_settings()


def main() -> None:
    App().mainloop()
