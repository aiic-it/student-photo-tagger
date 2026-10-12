"""History window: look back over students, picture days, exports and activity."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Student Photo Tagger"
EVENT_LABELS = {
    "list_loaded": "Loaded list", "select": "Selected", "photo": "Photo saved", "star": "Starred",
    "unstar": "Removed star", "student_added": "Added student", "export": "Export", "convert": "Convert",
    "settings": "Settings",
}


def _open(path: str) -> None:
    from .gui import open_folder
    if path and os.path.exists(path):
        open_folder(path)
    else:
        messagebox.showinfo(APP_NAME, f"That file or folder is no longer there:\n\n{path}")


def _table(parent, columns, height=10):
    """columns = [(id, heading, width, anchor)]"""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                        selectmode="browse")
    for cid, head, width, anchor in columns:
        tree.heading(cid, text=head)
        tree.column(cid, width=width, anchor=anchor, stretch=(width >= 200))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    return frame, tree


class HistoryWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.h = app.history
        self.title("History")
        self.geometry("1000x640")
        self.transient(app)
        self._paths: dict[str, str] = {}

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Everything this PC has recorded: photos, stars, added students and exports.").pack(side="left")
        ttk.Button(top, text="Save full history as CSV…", command=self.save_csv).pack(side="right")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right", padx=(0, 6))

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_students()
        self._build_days()
        self._build_exports()
        self._build_activity()
        self.refresh()

    # ------------------------------------------------------------------ students tab
    def _build_students(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text="Students")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="Search").pack(side="left")
        self.s_search = tk.StringVar()
        self.s_search.trace_add("write", lambda *_: self._fill_students())
        ttk.Entry(row, textvariable=self.s_search).pack(side="left", fill="x", expand=True, padx=(6, 0))

        f, self.s_tree = _table(tab, [("number", "Number", 90, "w"), ("name", "Name", 220, "w"),
                                      ("class", "Class", 70, "w"), ("campus", "Campus", 110, "w"),
                                      ("photos", "Photos", 60, "e"), ("last", "Last photo", 140, "w"),
                                      ("star", "Starred photo", 200, "w")], height=10)
        f.pack(fill="both", expand=True)
        self.s_tree.bind("<<TreeviewSelect>>", lambda e: self._fill_student_photos())

        lower = ttk.Frame(tab)
        lower.pack(fill="both", expand=True, pady=(8, 0))
        self.sp_label = ttk.Label(lower, text="Select a student to see all their photos.", foreground="#666666")
        self.sp_label.pack(anchor="w")
        body = ttk.Frame(lower)
        body.pack(fill="both", expand=True)
        f, self.sp_tree = _table(body, [("day", "Picture day", 100, "w"), ("time", "Taken", 80, "w"),
                                        ("file", "File", 260, "w"), ("star", "Best", 50, "center"),
                                        ("exported", "Exported", 70, "e"), ("there", "File still there", 110, "w")],
                                 height=7)
        f.pack(side="left", fill="both", expand=True)
        self.preview = tk.Label(body, text="", width=24, height=8, bg="#F3F3F3", cursor="hand2")
        self.preview.pack(side="left", padx=(8, 0), anchor="n")
        self.preview.bind("<Button-1>", lambda e: self._open_selected(self.sp_tree))
        self.sp_tree.bind("<<TreeviewSelect>>", lambda e: self._show_preview())
        self.sp_tree.bind("<Double-1>", lambda e: self._open_selected(self.sp_tree))

    def _fill_students(self):
        self.s_tree.delete(*self.s_tree.get_children())
        for r in self.h.student_rows(self.s_search.get()):
            name = f"{r['first'] or ''} {r['last'] or ''}".strip() + ("  (added)" if r["added"] else "")
            self.s_tree.insert("", "end", iid=r["key"], values=(
                r["number"], name, r["class"] or "", r["campus"] or "", r["photos"] or "",
                r["last_photo"] or "", r["starred"] or ""))

    def _fill_student_photos(self):
        self.sp_tree.delete(*self.sp_tree.get_children())
        self.preview.configure(image="", text="")
        sel = self.s_tree.selection()
        if not sel:
            return
        rows = self.h.photos_for(sel[0])
        name = self.s_tree.item(sel[0], "values")[1]
        self.sp_label.configure(text=f"{name}: {len(rows)} photo{'s' if len(rows) != 1 else ''}"
                                     "  (double-click a photo to open it)")
        for r in rows:
            iid = f"p{r['id']}"
            self._paths[iid] = r["path"]
            self.sp_tree.insert("", "end", iid=iid, values=(
                r["day"], (r["taken_at"] or "")[11:16], r["file_name"], "★" if r["starred"] else "",
                r["times_exported"] or "", "yes" if os.path.isfile(r["path"]) else "missing"))
        kids = self.sp_tree.get_children()
        if kids:
            self.sp_tree.selection_set(kids[0])

    def _show_preview(self):
        sel = self.sp_tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0], "")
        if not os.path.isfile(path):
            self.preview.configure(image="", text="File not found", width=24, height=8)
            return
        self.preview.configure(image="", text="Loading…", width=24, height=8)

        def done(img, iid=sel[0]):
            if not self.winfo_exists() or self.sp_tree.selection() != (iid,):
                return
            photo = self.app._to_tk("history_preview", img)
            if photo:
                self.preview.configure(image=photo, text="", width=photo.width(), height=photo.height())
            else:
                self.preview.configure(text="No preview", width=24, height=8)

        self.app._request_thumb("history_preview", path, 180, done)

    # ------------------------------------------------------------------ days tab
    def _build_days(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text="Picture days")
        f, self.d_tree = _table(tab, [("day", "Date", 110, "w"), ("photos", "Photos", 80, "e"),
                                      ("students", "Students", 80, "e"), ("unlinked", "Not linked", 90, "e")],
                                height=8)
        f.pack(fill="x")
        self.d_tree.bind("<<TreeviewSelect>>", lambda e: self._fill_day())
        self.dp_label = ttk.Label(tab, text="Select a day to see its photos.", foreground="#666666")
        self.dp_label.pack(anchor="w", pady=(8, 2))
        f, self.dp_tree = _table(tab, [("time", "Taken", 70, "w"), ("file", "File", 260, "w"),
                                       ("number", "Number", 80, "w"), ("name", "Student", 200, "w"),
                                       ("class", "Class", 70, "w"), ("campus", "Campus", 100, "w")], height=12)
        f.pack(fill="both", expand=True)
        self.dp_tree.tag_configure("unlinked", foreground="#8A1F1F")
        self.dp_tree.bind("<Double-1>", lambda e: self._open_selected(self.dp_tree))

    def _fill_days(self):
        self.d_tree.delete(*self.d_tree.get_children())
        for r in self.h.days():
            self.d_tree.insert("", "end", iid=r["day"], values=(r["day"], r["photos"], r["students"],
                                                                r["unlinked"] or ""))

    def _fill_day(self):
        self.dp_tree.delete(*self.dp_tree.get_children())
        sel = self.d_tree.selection()
        if not sel:
            return
        rows = self.h.photos_on(sel[0])
        self.dp_label.configure(text=f"{sel[0]}: {len(rows)} photos  (double-click to open)")
        for r in rows:
            iid = f"d{r['id']}"
            self._paths[iid] = r["path"]
            name = f"{r['first'] or ''} {r['last'] or ''}".strip() if r["student_key"] else "NOT linked"
            self.dp_tree.insert("", "end", iid=iid, tags=() if r["student_key"] else ("unlinked",), values=(
                (r["taken_at"] or "")[11:16], r["file_name"], r["number"] or "", name,
                r["class"] or "", r["campus"] or ""))

    # ------------------------------------------------------------------ exports tab
    def _build_exports(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text="Exports")
        ttk.Label(tab, text="Double-click an export to open its folder.", foreground="#666666").pack(anchor="w", pady=(0, 4))
        f, self.e_tree = _table(tab, [("at", "When", 140, "w"), ("kind", "Type", 80, "w"),
                                      ("copies", "Files", 60, "e"), ("errors", "Errors", 60, "e"),
                                      ("dest", "Exported to", 420, "w")], height=20)
        f.pack(fill="both", expand=True)
        self.e_tree.bind("<Double-1>", lambda e: self._open_selected(self.e_tree))

    def _fill_exports(self):
        self.e_tree.delete(*self.e_tree.get_children())
        for r in self.h.exports():
            iid = f"e{r['id']}"
            self._paths[iid] = r["destination"]
            self.e_tree.insert("", "end", iid=iid, values=(
                r["at"], "Export" if r["kind"] == "export" else "Convert", r["copies"], r["errors"] or "",
                r["destination"]))

    # ------------------------------------------------------------------ activity tab
    def _build_activity(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text="Activity")
        f, self.a_tree = _table(tab, [("at", "When", 140, "w"), ("kind", "What", 110, "w"),
                                      ("number", "Student", 90, "w"), ("detail", "Detail", 500, "w")], height=22)
        f.pack(fill="both", expand=True)

    def _fill_activity(self):
        self.a_tree.delete(*self.a_tree.get_children())
        for r in self.h.events(limit=2000):
            self.a_tree.insert("", "end", values=(r["at"], EVENT_LABELS.get(r["kind"], r["kind"]),
                                                  r["student_key"] or "", r["detail"] or ""))

    # ------------------------------------------------------------------ common
    def refresh(self):
        self._fill_students()
        self._fill_days()
        self._fill_exports()
        self._fill_activity()

    def _open_selected(self, tree):
        sel = tree.selection()
        if sel and sel[0] in self._paths:
            _open(self._paths[sel[0]])

    def save_csv(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile="photo_history.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            n = self.h.save_csv(path)
            messagebox.showinfo(APP_NAME, f"Saved {n} photos to\n{path}", parent=self)
