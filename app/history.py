"""History database: a single SQLite file on this PC (%APPDATA%\\StudentPhotoTagger\\history.db).

Records every session (each time the app runs = part of a picture day), every photo and the
student it belongs to, stars, students (including ones added on the day) and every export,
plus an activity log. Nothing here is required for the app to work: if the database can't be
opened, the app carries on and simply doesn't keep history.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS students (
    key        TEXT PRIMARY KEY,          -- normalised student number
    number     TEXT NOT NULL,
    first      TEXT, last TEXT, class TEXT, campus TEXT,
    added      INTEGER DEFAULT 0,         -- added on the day, not from the school list
    first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    day         TEXT NOT NULL,            -- YYYY-MM-DD, groups sessions into picture days
    list_file   TEXT, photo_folder TEXT, app_version TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    file_name   TEXT NOT NULL,
    student_key TEXT,                     -- NULL = not linked to a student
    session_id  INTEGER,
    taken_at    TEXT NOT NULL,
    starred     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS photos_student ON photos(student_key);

CREATE TABLE IF NOT EXISTS exports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    session_id  INTEGER,
    kind        TEXT,                     -- 'export' or 'convert'
    destination TEXT, sources TEXT, options TEXT,
    copies      INTEGER, errors INTEGER
);

CREATE TABLE IF NOT EXISTS export_items (
    export_id   INTEGER, student_key TEXT, source TEXT, dest TEXT
);
CREATE INDEX IF NOT EXISTS export_items_student ON export_items(student_key);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    session_id  INTEGER,
    kind        TEXT NOT NULL,
    student_key TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS events_student ON events(student_key);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class History:
    def __init__(self, path: str, app_version: str = ""):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()
        self.session_id: int | None = None
        self.app_version = app_version

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ writing
    def start_session(self, list_file: str = "", photo_folder: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO sessions (started_at, day, list_file, photo_folder, app_version) VALUES (?,?,?,?,?)",
            (now(), time.strftime("%Y-%m-%d"), list_file, photo_folder, self.app_version))
        self.db.commit()
        self.session_id = cur.lastrowid
        return self.session_id

    def update_session(self, list_file: str | None = None, photo_folder: str | None = None) -> None:
        if self.session_id is None:
            return
        if list_file is not None:
            self.db.execute("UPDATE sessions SET list_file=? WHERE id=?", (list_file, self.session_id))
        if photo_folder is not None:
            self.db.execute("UPDATE sessions SET photo_folder=? WHERE id=?", (photo_folder, self.session_id))
        self.db.commit()

    def save_students(self, students) -> None:
        t = now()
        self.db.executemany(
            """INSERT INTO students (key, number, first, last, class, campus, added, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET number=excluded.number, first=excluded.first, last=excluded.last,
                   class=excluded.class, campus=excluded.campus, added=excluded.added, last_seen=excluded.last_seen""",
            [(s.key, s.number, s.first, s.last, s.group, s.campus, int(s.added), t, t) for s in students])
        self.db.commit()

    def log(self, kind: str, student_key: str | None = None, detail: str = "") -> None:
        self.db.execute("INSERT INTO events (at, session_id, kind, student_key, detail) VALUES (?,?,?,?,?)",
                        (now(), self.session_id, kind, student_key, detail))
        self.db.commit()

    _UPSERT_PHOTO = ("INSERT INTO photos (path, file_name, student_key, session_id, taken_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(path) DO UPDATE SET student_key = COALESCE(photos.student_key, excluded.student_key)")

    def record_photo(self, path: str, student_key: str | None, taken_at: str | None = None) -> bool:
        """Returns True if this photo is new to the history."""
        path = os.path.normpath(path)
        is_new = self.db.execute("SELECT 1 FROM photos WHERE path=?", (path,)).fetchone() is None
        self.db.execute(self._UPSERT_PHOTO,
                        (path, os.path.basename(path), student_key, self.session_id, taken_at or now()))
        self.db.commit()
        return is_new

    def record_photos(self, items) -> int:
        """Bulk add photos already on disk: items = [(path, student_key or None, taken_at)]."""
        before = self.db.total_changes
        self.db.executemany(
            self._UPSERT_PHOTO,
            [(os.path.normpath(p), os.path.basename(p), k, self.session_id, t) for p, k, t in items])
        self.db.commit()
        return self.db.total_changes - before

    def set_star(self, path: str, student_key: str, starred: bool) -> None:
        path = os.path.normpath(path)
        if starred:   # one star per student
            self.db.execute("UPDATE photos SET starred=0 WHERE student_key=?", (student_key,))
        self.db.execute("INSERT OR IGNORE INTO photos (path, file_name, student_key, session_id, taken_at) "
                        "VALUES (?,?,?,?,?)", (path, os.path.basename(path), student_key, self.session_id, now()))
        self.db.execute("UPDATE photos SET starred=? WHERE path=?", (int(starred), path))
        self.db.commit()
        self.log("star" if starred else "unstar", student_key, os.path.basename(path))

    def record_export(self, kind: str, destination: str, sources: list[str], options: dict, plan, errors) -> int:
        cur = self.db.execute(
            "INSERT INTO exports (at, session_id, kind, destination, sources, options, copies, errors) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now(), self.session_id, kind, destination, json.dumps(sources), json.dumps(options),
             len(plan.copies) - len(errors), len(errors)))
        export_id = cur.lastrowid
        self.db.executemany("INSERT INTO export_items VALUES (?,?,?,?)",
                            [(export_id, c.student.key, os.path.normpath(c.src), c.dest) for c in plan.copies])
        self.db.commit()
        self.log(kind, None, f"{len(plan.copies) - len(errors)} files to {destination}")
        return export_id

    # ------------------------------------------------------------------ reading
    def student_rows(self, search: str = ""):
        q = """SELECT s.*, COUNT(p.id) AS photos, MAX(p.taken_at) AS last_photo,
                      (SELECT file_name FROM photos WHERE student_key=s.key AND starred=1 LIMIT 1) AS starred
               FROM students s LEFT JOIN photos p ON p.student_key = s.key"""
        args: list = []
        if search.strip():
            parts = search.lower().split()
            q += " WHERE " + " AND ".join(
                ["lower(s.number||' '||ifnull(s.first,'')||' '||ifnull(s.last,'')||' '||ifnull(s.class,'')"
                 "||' '||ifnull(s.campus,'')) LIKE ?"] * len(parts))
            args = [f"%{p}%" for p in parts]
        q += " GROUP BY s.key ORDER BY lower(s.last), lower(s.first)"
        return self.db.execute(q, args).fetchall()

    def photos_for(self, student_key: str):
        return self.db.execute(
            """SELECT p.*, substr(p.taken_at, 1, 10) AS day,
                      (SELECT COUNT(*) FROM export_items e WHERE e.source = p.path) AS times_exported
               FROM photos p WHERE p.student_key=? ORDER BY p.taken_at DESC""", (student_key,)).fetchall()

    def days(self):
        """Picture days = the dates photos were taken."""
        return self.db.execute(
            """SELECT substr(taken_at, 1, 10) AS day,
                      COUNT(*) AS photos,
                      COUNT(DISTINCT student_key) AS students,
                      SUM(CASE WHEN student_key IS NULL THEN 1 ELSE 0 END) AS unlinked
               FROM photos GROUP BY day ORDER BY day DESC""").fetchall()

    def photos_on(self, day: str):
        return self.db.execute(
            """SELECT p.*, s.number, s.first, s.last, s.class, s.campus
               FROM photos p LEFT JOIN students s ON s.key = p.student_key
               WHERE substr(p.taken_at, 1, 10)=? ORDER BY p.taken_at""", (day,)).fetchall()

    def exports(self):
        return self.db.execute("SELECT * FROM exports ORDER BY at DESC").fetchall()

    def events(self, limit: int = 1000, student_key: str | None = None):
        if student_key:
            return self.db.execute("SELECT * FROM events WHERE student_key=? ORDER BY id DESC LIMIT ?",
                                   (student_key, limit)).fetchall()
        return self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def save_csv(self, path: str) -> int:
        rows = self.db.execute(
            """SELECT substr(p.taken_at, 1, 10) AS day, p.taken_at, s.number, s.first, s.last, s.class, s.campus,
                      p.file_name, p.path, p.starred,
                      (SELECT COUNT(*) FROM export_items e WHERE e.source = p.path) AS times_exported
               FROM photos p LEFT JOIN students s ON s.key = p.student_key ORDER BY p.taken_at""").fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Picture day", "Taken", "Student number", "First name", "Last name", "Class", "Campus",
                        "File", "Full path", "Starred", "Times exported"])
            for r in rows:
                w.writerow([r["day"], r["taken_at"], r["number"] or "", r["first"] or "", r["last"] or "",
                            r["class"] or "", r["campus"] or "", r["file_name"], r["path"],
                            "yes" if r["starred"] else "", r["times_exported"]])
        return len(rows)
