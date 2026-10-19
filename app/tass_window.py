"""TASS connection settings, and loading the student list from TASS."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .core import write_student_list
from .tass import CLASS_SOURCES, TassClient, TassError, cache_path, protect, to_students, unprotect

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
        "future": settings.get("tass_future", False),
        "campuses": settings.get("tass_campuses", ""),
        "years": settings.get("tass_years", ""),
    }


def is_configured(settings: dict) -> bool:
    t = tass_settings(settings)
    return bool(t["url"] and t["key"] and t["secret"])


def _split(text: str) -> list[str]:
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]


def fetch_students(t: dict, progress=None):
    client = TassClient(t["url"], t["key"], t["secret"], t["company"])
    client.sign_in()
    rows = client.students(progress)
    students = to_students(rows, t["class_source"], t["preferred"], t["future"],
                           _split(t["campuses"]), _split(t["years"]))
    return client, rows, students


def load_from_tass(app) -> None:
    """Pull students from TASS, save them as the app's cached list, and load it."""
    if not is_configured(app.settings):
        TassSettingsDialog(app, load_after=True)
        return
    if getattr(app, "_tass_busy", False):
        return
    app._tass_busy = True
    t = tass_settings(app.settings)
    app.log("Loading students from TASS…")
    app.config(cursor="watch")

    def work():
        return fetch_students(t)

    def done(result, error):
        app._tass_busy = False
        app.config(cursor="")
        if error:
            app.log(f"TASS: {error}", warn=True)
            app.hist("log", "tass_error", None, str(error))
            messagebox.showerror(APP_NAME, f"Couldn't load students from TASS.\n\n{error}")
            return
        client, rows, students = result
        if not students:
            messagebox.showwarning(APP_NAME, f"TASS returned {len(rows)} students, but none matched your "
                                             "filters (current students, campuses, year groups).")
            return
        path = cache_path(os.path.dirname(app.settings_file))
        write_student_list(students, path)
        app.load_list(path, quiet=True)
        app.settings["list_source"] = "tass"
        app.save_settings()
        flagged = sum(1 for s in students if s.privacy)
        app.log(f"Loaded {len(students)} students from TASS (company {client.company})"
                + (f" — {flagged} with the privacy flag set" if flagged else ""))
        app.hist("log", "tass_loaded", None, f"{len(students)} of {len(rows)} students, company {client.company}")

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
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Read the student list straight from TASS. Your TASS administrator creates an API user\n"
                            "with Students > Information > Student: Read and gives you its ClientKey and ClientSecret.",
                  foreground="#555555").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.url = tk.StringVar(value=t["url"])
        self.key = tk.StringVar(value=t["key"])
        self.secret = tk.StringVar(value=t["secret"])
        self.company = tk.StringVar(value=t["company"])
        self.class_source = tk.StringVar(value=t["class_source"] if t["class_source"] in CLASS_SOURCES else "Tutor group")
        self.preferred = tk.BooleanVar(value=t["preferred"])
        self.future = tk.BooleanVar(value=t["future"])
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
        add("Class column from", ttk.Combobox(frm, textvariable=self.class_source, values=list(CLASS_SOURCES),
                                              state="readonly", width=37))
        add("Only these campuses", ttk.Entry(frm, textvariable=self.campuses, width=40), "codes, comma separated; blank = all")
        add("Only these year groups", ttk.Entry(frm, textvariable=self.years, width=40), "e.g. 7, 8; blank = all")
        ttk.Checkbutton(frm, text="Use preferred names", variable=self.preferred).grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Checkbutton(frm, text="Include future enrolments", variable=self.future).grid(row=row, column=1, sticky="w")
        row += 1
        self.status = ttk.Label(frm, text="", wraplength=520, justify="left")
        self.status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        row += 1
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="we", pady=(12, 0))
        self.test_btn = ttk.Button(btns, text="Test connection", command=self.test)
        self.test_btn.pack(side="left")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save and load students", command=lambda: self.save(load=True)).pack(side="right", padx=(0, 6))
        ttk.Button(btns, text="Save", command=lambda: self.save(load=False)).pack(side="right", padx=(0, 6))
        self.bind("<Escape>", lambda e: self.destroy())
        self.test_result = None

    def _values(self) -> dict:
        return {"url": self.url.get().strip(), "key": self.key.get().strip(), "secret": self.secret.get(),
                "company": self.company.get().split(" ")[0].strip(), "class_source": self.class_source.get(),
                "preferred": self.preferred.get(), "future": self.future.get(),
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
            campuses = sorted({str(r.get("campus_code")) for r in rows if r.get("campus_code")})
            self.status.configure(foreground="#1E5E2F", text=(
                f"Connected. Company {client.company}: {len(rows)} students in TASS, "
                f"{len(students)} match your settings (current"
                + (" and future" if t["future"] else "") + ")."
                + (f"\nCampus codes in TASS: {', '.join(campuses)}" if campuses else "")))

        run_in_background(self, lambda: fetch_students(t), done)

    def save(self, load: bool) -> None:
        t = self._values()
        if not (t["url"] and t["key"] and t["secret"]):
            self.status.configure(text="Enter the web address, ClientKey and ClientSecret.", foreground="#8A1F1F")
            return
        s = self.app.settings
        s.update({"tass_url": t["url"], "tass_company": t["company"], "tass_key": protect(t["key"]),
                  "tass_secret": protect(t["secret"]), "tass_class_source": t["class_source"],
                  "tass_preferred": t["preferred"], "tass_future": t["future"],
                  "tass_campuses": t["campuses"], "tass_years": t["years"]})
        self.app.save_settings()
        self.app.log("Saved TASS connection settings")
        self.destroy()
        if load or self.load_after:
            load_from_tass(self.app)
