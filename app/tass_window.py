"""TASS connection settings, and loading the student list from TASS."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .core import write_student_list
from .tass import (CLASS_SOURCES, STAFF_GROUP_SOURCES, TassClient, TassError, cache_path, protect, to_staff,
                   to_students, unprotect)

APP_NAME = "Student Photo Tagger"


def run_in_background(widget, work, done) -> None:
    """Run work() on a thread; call done(result, error) on the Tk thread."""
    q: "queue.Queue[tuple]" = queue.Queue()

    def target():
        try:
            q.put((work(), None))
        except Exception as e:  # reported to the person, never crashes the app
            q.put((None, e))

    def poll():
        try:
            result, error = q.get_nowait()
        except queue.Empty:
            if widget.winfo_exists():
                widget.after(100, poll)
            return
        done(result, error)

    threading.Thread(target=target, daemon=True).start()
    widget.after(100, poll)


def tass_settings(settings: dict) -> dict:
    return {
        "url": settings.get("tass_url", ""),
        "company": settings.get("tass_company", ""),
        "key": unprotect(settings.get("tass_key", "")),
        "secret": unprotect(settings.get("tass_secret", "")),
        "class_source": settings.get("tass_class_source", "Tutor group"),
        "preferred": settings.get("tass_preferred", True),
        "campuses": settings.get("tass_campuses", ""),
        "years": settings.get("tass_years", ""),
    }


def is_configured(settings: dict) -> bool:
    t = tass_settings(settings)
    return bool(t["url"] and t["key"] and t["secret"])


def _split(text: str) -> list[str]:
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]


def fetch_students(t: dict, progress=None, staff: bool = False):
    client = TassClient(t["url"], t["key"], t["secret"], t["company"])
    client.sign_in()
    if staff:
        rows = client.employees(progress)
        people = to_staff(rows, t["class_source"], t["preferred"], _split(t["campuses"]))
    else:
        rows = client.students(progress)
        people = to_students(rows, t["class_source"], t["preferred"], _split(t["campuses"]), _split(t["years"]))
    return client, rows, people


def _is_staff(app) -> bool:
    return getattr(app, "mode", "students") == "staff"


def load_from_tass(app) -> None:
    """Pull students from TASS, save them as the app's cached list, and load it."""
    if not is_configured(app.settings):
        TassSettingsDialog(app, load_after=True)
        return
    if getattr(app, "_tass_busy", False):
        return
    app._tass_busy = True
    t = tass_settings(app.settings)
    staff = _is_staff(app)
    who = "staff" if staff else "students"
    app.log(f"Loading {who} from TASS…")
    app.config(cursor="watch")

    def work():
        return fetch_students(t, staff=staff)

    def done(result, error):
        app._tass_busy = False
        app.config(cursor="")
        if error:
            app.log(f"TASS: {error}", warn=True)
            app.hist("log", "tass_error", None, str(error))
            messagebox.showerror(APP_NAME, f"Couldn't load {who} from TASS.\n\n{error}")
            return
        client, rows, students = result
        if not students:
            messagebox.showwarning(APP_NAME, f"TASS returned {len(rows)} current {who}, but none matched your "
                                             "filter settings.")
            return
        path = cache_path(os.path.dirname(app.settings_file))
        write_student_list(students, path)
        app.load_list(path, quiet=True)
        app.settings["list_source"] = "tass"
        app.save_settings()
        flagged = sum(1 for s in students if s.privacy)
        app.log(f"Loaded {len(students)} {who} from TASS (company {client.company})"
                + (f" — {flagged} with the privacy flag set" if flagged else ""))
        app.hist("log", "tass_loaded", None, f"{len(students)} of {len(rows)} {who}, company {client.company}")

    run_in_background(app, work, done)


class TassSettingsDialog(tk.Toplevel):
    def __init__(self, app, load_after: bool = False):
        super().__init__(app)
        self.app = app
        self.load_after = load_after
        self.title("TASS connection")
        self.transient(app)
        self.resizable(False, False)
        t = tass_settings(app.settings)
        self.staff = staff = _is_staff(app)
        sources = STAFF_GROUP_SOURCES if staff else CLASS_SOURCES
        default_source = "Position title" if staff else "Tutor group"
        who = "staff" if staff else "students"
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        permission = ("Employee/HR > Information > Employee: Read" if staff
                      else "Students > Information > Student: Read")
        ttk.Label(frm, text=f"Read the {who} list straight from TASS. Your TASS administrator creates an API user\n"
                            f"with {permission} and gives you its ClientKey and ClientSecret.",
                  foreground="#555555").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.url = tk.StringVar(value=t["url"])
        self.key = tk.StringVar(value=t["key"])
        self.secret = tk.StringVar(value=t["secret"])
        self.company = tk.StringVar(value=t["company"])
        self.class_source = tk.StringVar(value=t["class_source"] if t["class_source"] in sources else default_source)
        self.preferred = tk.BooleanVar(value=t["preferred"])
        self.campuses = tk.StringVar(value=t["campuses"])
        self.years = tk.StringVar(value=t["years"])

        row = 1

        def add(label, widget, hint=""):
            nonlocal row
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            widget.grid(row=row, column=1, sticky="we", padx=(8, 0), pady=3)
            if hint:
                ttk.Label(frm, text=hint, foreground="#777777").grid(row=row, column=2, sticky="w", padx=(8, 0))
            row += 1
            return widget

        add("TASS web address", ttk.Entry(frm, textvariable=self.url, width=40), "e.g. tass.yourschool.edu.au")
        add("ClientKey", ttk.Entry(frm, textvariable=self.key, width=40))
        add("ClientSecret", ttk.Entry(frm, textvariable=self.secret, width=40, show="•"),
            "saved encrypted on this PC")
        self.company_box = add("Company code", ttk.Combobox(frm, textvariable=self.company, width=37),
                               "filled in by Test connection")
        add("Position column from" if staff else "Class column from",
            ttk.Combobox(frm, textvariable=self.class_source, values=list(sources), state="readonly", width=37))
        if staff:
            add("Only these positions", ttk.Entry(frm, textvariable=self.campuses, width=40),
                "e.g. Teacher; blank = all staff")
        else:
            add("Only these campuses", ttk.Entry(frm, textvariable=self.campuses, width=40), "codes, comma separated; blank = all")
            add("Only these year groups", ttk.Entry(frm, textvariable=self.years, width=40), "e.g. 7, 8; blank = all")
        ttk.Checkbutton(frm, text="Use preferred names", variable=self.preferred).grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(frm, text=f"Only current {who} are loaded: not those who have left or haven't started yet.",
                  foreground="#555555").grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        row += 1
        self.status = ttk.Label(frm, text="", wraplength=520, justify="left")
        self.status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        row += 1
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="we", pady=(12, 0))
        self.test_btn = ttk.Button(btns, text="Test connection", command=self.test)
        self.test_btn.pack(side="left")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text=f"Save and load {who}", command=lambda: self.save(load=True)).pack(side="right", padx=(0, 6))
        ttk.Button(btns, text="Save", command=lambda: self.save(load=False)).pack(side="right", padx=(0, 6))
        self.bind("<Escape>", lambda e: self.destroy())
        self.test_result = None

    def _values(self) -> dict:
        return {"url": self.url.get().strip(), "key": self.key.get().strip(), "secret": self.secret.get(),
                "company": self.company.get().split(" ")[0].strip(), "class_source": self.class_source.get(),
                "preferred": self.preferred.get(),
                "campuses": self.campuses.get().strip(), "years": self.years.get().strip()}

    def test(self) -> None:
        t = self._values()
        self.status.configure(text="Connecting to TASS…", foreground="#7A4B00")
        self.test_btn.state(["disabled"])

        def done(result, error):
            if not self.winfo_exists():
                return
            self.test_btn.state(["!disabled"])
            if error:
                self.test_result = None
                self.status.configure(text=str(error), foreground="#8A1F1F")
                return
            client, rows, students = result
            self.test_result = result
            names = [f"{c.get('cmpy_code')} {c.get('name_text', '')}".strip() for c in client.companies]
            self.company_box.configure(values=names)
            if client.company:
                self.company.set(client.company)
            key = "position_title" if self.staff else "campus_code"
            campuses = sorted({str(r.get(key)) for r in rows if r.get(key)})
            if len(campuses) > 15:
                campuses = campuses[:15] + ["…"]
            self.status.configure(foreground="#1E5E2F", text=(
                f"Connected. Company {client.company}: {len(rows)} current {'staff' if self.staff else 'students'} in TASS, "
                f"{len(students)} match your settings."
                + (f"\n{'Position titles' if self.staff else 'Campus codes'} in TASS: {', '.join(campuses)}"
                   if campuses else "")))

        run_in_background(self, lambda: fetch_students(t, staff=self.staff), done)

    def save(self, load: bool) -> None:
        t = self._values()
        if not (t["url"] and t["key"] and t["secret"]):
            self.status.configure(text="Enter the web address, ClientKey and ClientSecret.", foreground="#8A1F1F")
            return
        s = self.app.settings
        s.update({"tass_url": t["url"], "tass_company": t["company"], "tass_key": protect(t["key"]),
                  "tass_secret": protect(t["secret"]), "tass_class_source": t["class_source"],
                  "tass_preferred": t["preferred"],
                  "tass_campuses": t["campuses"], "tass_years": t["years"]})
        self.app.save_settings()
        self.app.log("Saved TASS connection settings")
        self.destroy()
        if load or self.load_after:
            load_from_tass(self.app)
