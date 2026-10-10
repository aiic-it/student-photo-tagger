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
from .core import (ExportOptions, ListError, Student, StudentIndex, existing_conflicts,
                   load_students, make_tag, plan_export, run_export, scan_folder,
                   write_student_list)
from .ss_client import SmartShooterClient

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
        self.photos: dict[str, set[str]] = {}       # student key -> photo file names
        self.seen_photo_keys: set[str] = set()
        self.photo_log: list[list[str]] = []
        self.was_connected = False
        self.last_sync_attempt = 0.0

        self._build_ui()
        self.client = self._new_client()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        last = self.settings.get("last_list")
        if last and os.path.isfile(last):
            self.load_list(last, quiet=True)
        self.after(200, self._pump)
        self.after(1000, self._heartbeat)

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
        reports = ttk.Menubutton(bar, text="Reports")
        menu = tk.Menu(reports, tearoff=False)
        menu.add_command(label="Save photo log…", command=self.save_photo_log)
        menu.add_command(label="Save not-yet-photographed list…", command=self.save_missing_list)
        reports["menu"] = menu
        reports.pack(side="left", padx=(6, 0))
        setup = ttk.Menubutton(bar, text="Smart Shooter")
        smenu = tk.Menu(setup, tearoff=False)
        smenu.add_command(label="Set up file naming ([Z]_[S])…", command=self.apply_naming)
        smenu.add_command(label="Reconnect", command=self.reconnect)
        smenu.add_command(label="Connection address…", command=self.change_host)
        setup["menu"] = smenu
        setup.pack(side="left", padx=(6, 0))
        self.conn_label = ttk.Label(bar, text="Connecting to Smart Shooter…", foreground="#7A4B00")
        self.conn_label.pack(side="right")

        # Banner
        self.banner = tk.Frame(self, bd=0, highlightthickness=1, highlightbackground="#CCCCCC")
        self.banner.pack(fill="x", padx=10, pady=(0, 8))
        self.banner_small = tk.Label(self.banner, text="Now shooting", font=("Segoe UI", 10), anchor="w")
        self.banner_small.pack(fill="x", padx=14, pady=(8, 0))
        self.banner_big = tk.Label(self.banner, text="No student selected", font=("Segoe UI", 24, "bold"), anchor="w")
        self.banner_big.pack(fill="x", padx=14)
        self.banner_detail = tk.Label(self.banner, text="Load a student list, then click a name.", font=("Segoe UI", 10), anchor="w")
        self.banner_detail.pack(fill="x", padx=14)
        btns = tk.Frame(self.banner)
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

        cols = ("done", "number", "name", "group", "photos")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, label, w, anchor in (("done", "", 30, "center"), ("number", "Number", 90, "w"),
                                    ("name", "Name", 240, "w"), ("group", "Class", 90, "w"),
                                    ("photos", "Photos", 60, "e")):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        self.tree.tag_configure("done", foreground="#1E5E2F")
        self.tree.tag_configure("current", background="#DCEBFA")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        right = ttk.Frame(body)
        body.add(right, weight=2)
        self.progress_label = ttk.Label(right, text="No student list loaded")
        self.progress_label.pack(anchor="w")
        ttk.Label(right, text="Activity", foreground="#666666").pack(anchor="w", pady=(10, 2))
        self.activity = tk.Listbox(right, activestyle="none", highlightthickness=0, borderwidth=1)
        self.activity.pack(fill="both", expand=True)

        self.bind_all("<Control-f>", lambda e: (self.search.focus_set(), self.search.select_range(0, "end")))
        self.bind_all("<Control-n>", lambda e: self.next_student())

    def _paint_banner(self, state: str) -> None:
        bg, fg = COLOURS[state]
        self.banner.configure(bg=bg)
        for w in (self.banner_small, self.banner_big, self.banner_detail, self.banner_btns):
            w.configure(bg=bg)
        for w in (self.banner_small, self.banner_big, self.banner_detail):
            w.configure(fg=fg)

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
        self.students = students
        self.index = StudentIndex(students)
        self.photos = {}
        self.current = None
        self.settings["last_list"] = path
        self.save_settings()
        self.recount_from_folder()
        self.refresh_list()
        self._show_current()
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
                self.photos.setdefault(f.student.key, set()).add(os.path.basename(f.path).lower())

    def refresh_list(self) -> None:
        q = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for s in self.students:
            hay = f"{s.number} {s.first} {s.last} {s.last} {s.first} {s.group}".lower()
            if q and not all(part in hay for part in q.split()):
                continue
            self.tree.insert("", "end", iid=s.key, values=self._row_values(s), tags=self._row_tags(s))
        if self.current and self.tree.exists(self.current.key):
            self.tree.selection_set(self.current.key)
            self.tree.see(self.current.key)
        self._update_progress()

    def _row_values(self, s: Student):
        n = len(self.photos.get(s.key, ()))
        return ("✓" if n else "", s.number, s.display, s.group, n or "")

    def _row_tags(self, s: Student):
        tags = []
        if self.photos.get(s.key):
            tags.append("done")
        if self.current and s.key == self.current.key:
            tags.append("current")
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
        self.sent_tag = make_tag(s)
        self.client.set_tag(self.sent_tag, tag="SetTag:" + self.sent_tag)
        self._show_current(pending=True)
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
        elif tag == "ApplyNaming":
            if ok:
                messagebox.showinfo(APP_NAME, "Smart Shooter will now name photos as\n[student tag]_[shot number], e.g.\n10234_Nimal_Perera_001.jpg")
                self.log("Smart Shooter file naming set to [Z]_[S]")
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
            self.client.set_tag(make_tag(self.current), tag="SetTag:" + make_tag(self.current))
        self._show_current()

    def _read_options(self, opts: dict) -> None:
        if "Barcode" in opts:
            self.ss_tag = opts.get("Barcode") or ""
        path = opts.get("DownloadPath")
        if path and path != self.download_path:
            self.download_path = path
            self.settings["download_path"] = path
            self.save_settings()
        expr = opts.get("FilenameExpression")
        if expr is not None and "[Z]" not in expr and not getattr(self, "_warned_expr", False):
            self._warned_expr = True
            self.log("Smart Shooter's file naming doesn't include the student tag [Z]. "
                     "Use Smart Shooter ▸ Set up file naming.", warn=True)

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
            self.photos.setdefault(s.key, set()).add(name.lower())
            if live:
                self._refresh_row(s)
                self.log(f"Saved {name}")
                if self.current and s.key == self.current.key:
                    self._show_current()
        elif live:
            self.log(f"Saved {name} — NOT linked to a student", warn=True)
            self.bell()
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

    def apply_naming(self) -> None:
        if messagebox.askyesno(APP_NAME, "Set Smart Shooter's filename pattern to [Z]_[S]?\n\n"
                                         "Photos will be saved as e.g. 10234_Nimal_Perera_001.jpg, "
                                         "with the shot number restarting for each student."):
            self.client.apply_naming()

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
        self.destroy()


class ExportDialog(tk.Toplevel):
    """Used for both 'Export photos' (from Smart Shooter) and 'Convert existing photos'."""

    def __init__(self, app: App, mode: str):
        super().__init__(app)
        self.app = app
        self.mode = mode
        if not app.students:
            self.destroy()
            messagebox.showinfo(APP_NAME, "Load a student list first.")
            return
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
        ttk.Label(frm, text=intro).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        src_default = app.download_path if mode == "shoot" else s.get("convert_source", "")
        self.src = tk.StringVar(value=src_default)
        self.dest = tk.StringVar(value=s.get("export_dest", ""))
        self._path_row(frm, 1, "Photos folder", self.src)
        self._path_row(frm, 2, "Export to", self.dest)
        self.recursive = tk.BooleanVar(value=s.get("recursive", False))
        ttk.Checkbutton(frm, text="Include subfolders", variable=self.recursive).grid(row=3, column=1, sticky="w")

        opts = ttk.Frame(frm)
        opts.grid(row=4, column=0, columnspan=3, sticky="we", pady=8)
        self.by_name = tk.BooleanVar(value=s.get("by_name", True))
        self.by_id = tk.BooleanVar(value=s.get("by_id", True))
        self.which = tk.StringVar(value=s.get("which", "newest"))
        self.fmt = tk.StringVar(value=s.get("name_format", "first_last"))
        g1 = ttk.LabelFrame(opts, text="Make copies", padding=8)
        g1.pack(side="left", fill="y")
        ttk.Checkbutton(g1, text="By Name", variable=self.by_name).pack(anchor="w")
        ttk.Checkbutton(g1, text="By ID", variable=self.by_id).pack(anchor="w")
        g2 = ttk.LabelFrame(opts, text="Photos per student", padding=8)
        g2.pack(side="left", fill="y", padx=8)
        for val, label in (("newest", "One (newest)"), ("first", "One (first taken)"), ("all", "All photos")):
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
        self.summary = ttk.Label(btns, text="Click Preview to check what will be copied.")
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

        for var in (self.src, self.recursive, self.by_name, self.by_id, self.which, self.fmt):
            var.trace_add("write", lambda *_: self._invalidate())

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

    def _options(self) -> ExportOptions:
        return ExportOptions(by_name=self.by_name.get(), by_id=self.by_id.get(),
                             which=self.which.get(), name_format=self.fmt.get())

    def preview(self) -> bool:
        src = self.src.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror(APP_NAME, "Choose the folder that has the photos.", parent=self)
            return False
        opts = self._options()
        if not (opts.by_name or opts.by_id):
            messagebox.showerror(APP_NAME, "Tick By Name, By ID, or both.", parent=self)
            return False
        try:
            photos = scan_folder(src, self.app.index, self.recursive.get())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't read the folder.\n\n{e}", parent=self)
            return False
        self.plan = plan_export(photos, self.app.index, opts)
        new_names: dict[str, list[str]] = {}
        for c in self.plan.copies:
            new_names.setdefault(c.src, []).append(c.dest.replace("\\", "/"))
        self.table.delete(*self.table.get_children())
        for p in photos:
            name = os.path.relpath(p.path, src)
            if p.student is None:
                self.table.insert("", "end", values=(name, "—", "—", p.status), tags=("warn",))
            elif p.path in new_names:
                self.table.insert("", "end", values=(name, p.student.display, "   ".join(new_names[p.path]), "Matched"))
            else:
                self.table.insert("", "end", values=(name, p.student.display, "—", "Skipped (older shot)"), tags=("skip",))
        students = len({c.student.key for c in self.plan.copies})
        self.summary.configure(text=f"{len(self.plan.copies)} copies for {students} students  ·  "
                                    f"{len(self.plan.unmatched)} unmatched files  ·  "
                                    f"{len(self.plan.students_without_photo)} students without a photo")
        return True

    def export(self) -> None:
        if self.plan is None and not self.preview():
            return
        dest = self.dest.get().strip()
        if not dest:
            messagebox.showerror(APP_NAME, "Choose where to export to.", parent=self)
            return
        if os.path.abspath(dest) == os.path.abspath(self.src.get().strip()):
            messagebox.showerror(APP_NAME, "Export to a different folder from the photos folder.", parent=self)
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

        def work():
            errors = run_export(plan, dest, progress=lambda n, t: self.after(0, self._progress, n, t))
            self.after(0, self._done, dest, errors)

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, n, total) -> None:
        self.bar.configure(maximum=total, value=n)

    def _done(self, dest, errors) -> None:
        self.export_btn.state(["!disabled"])
        msg = f"Exported {len(self.plan.copies) - len(errors)} files to:\n{dest}\n\nA report (export_report.csv) is in the same folder."
        if errors:
            msg += f"\n\n{len(errors)} files couldn't be copied — see the report."
        self.app.log(f"Exported {len(self.plan.copies) - len(errors)} files")
        if messagebox.askyesno(APP_NAME, msg + "\n\nOpen the folder now?", parent=self):
            open_folder(dest)

    def _remember(self, dest) -> None:
        s = self.app.settings
        s.update({"export_dest": dest, "recursive": self.recursive.get(), "by_name": self.by_name.get(),
                  "by_id": self.by_id.get(), "which": self.which.get(), "name_format": self.fmt.get()})
        if self.mode == "convert":
            s["convert_source"] = self.src.get().strip()
        self.app.save_settings()


def main() -> None:
    App().mainloop()
