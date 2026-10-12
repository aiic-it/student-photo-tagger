"""Core logic for Student Photo Tagger.

Nothing in here touches the GUI or Smart Shooter, so it can be tested on its own:
loading the student list, building the tag Smart Shooter puts in filenames,
working out which student a photo belongs to, and planning/doing exports.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
from dataclasses import dataclass, field

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".bmp",
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".orf", ".rw2", ".dng", ".pef",
}

HEADER_ALIASES = {
    "number": ["student number", "student no", "student id", "studentid", "student code",
               "stud code", "id number", "admission number", "admission no", "id", "number",
               "code"],
    "first": ["first name", "firstname", "first", "given name", "given names", "forename",
              "preferred name", "preferred"],
    "last": ["last name", "lastname", "last", "surname", "family name"],
    "group": ["class", "year", "year level", "form", "homeroom", "roll class", "tutor group",
              "house", "grade", "pc", "pastoral care"],
    "campus": ["campus", "campus name", "campus code", "site", "location", "school campus"],
}

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class Student:
    number: str
    first: str
    last: str
    group: str = ""
    campus: str = ""
    added: bool = field(default=False, compare=False)   # added on the day, not in the loaded list

    @property
    def display(self) -> str:
        return f"{self.first} {self.last}".strip()

    @property
    def key(self) -> str:
        return normalise_number(self.number)


class ListError(ValueError):
    pass


# --------------------------------------------------------------------------- list loading

def _norm_header(h) -> str:
    h = str(h or "").strip().lower()
    h = re.sub(r"[_.\-]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def _read_rows(path: str) -> list[list[str]]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as e:  # pragma: no cover
            raise ListError("Excel support is missing. Save the list as CSV and try again.") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else _cell_text(c) for c in r])
        wb.close()
        return rows
    raw = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                raw = f.read()
            break
        except UnicodeDecodeError:
            continue
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(raw.splitlines(), dialect))


def _cell_text(c) -> str:
    if isinstance(c, float) and c.is_integer():
        return str(int(c))
    return str(c).strip()


def load_students(path: str) -> tuple[list[Student], list[str]]:
    """Read a CSV or Excel list. Returns (students, warnings)."""
    rows = [[str(c).strip() for c in r] for r in _read_rows(path)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        raise ListError("The file is empty.")

    header = [_norm_header(h) for h in rows[0]]
    idx: dict[str, int] = {}
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in header and header.index(alias) not in idx.values():
                idx[key] = header.index(alias)
                break

    if not any(k in idx for k in ("number", "first", "last")):
        # No recognisable headings: assume the columns are first, last, number.
        if len(rows[0]) < 3:
            raise ListError("Expected three columns: first name, last name, student number.")
        idx = {"first": 0, "last": 1, "number": 2}
        data = rows
    else:
        missing = [k for k in ("number", "first", "last") if k not in idx]
        if missing:
            names = {"number": "student number", "first": "first name", "last": "last name"}
            raise ListError(
                "Couldn't find a column for: " + ", ".join(names[m] for m in missing)
                + ".\nColumns in the file: " + ", ".join(rows[0])
            )
        data = rows[1:]

    def get(r, k):
        i = idx.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""

    students: list[Student] = []
    warnings: list[str] = []
    seen: dict[str, Student] = {}
    for n, r in enumerate(data, start=2 if data is not rows else 1):
        s = Student(get(r, "number"), get(r, "first"), get(r, "last"), get(r, "group"), get(r, "campus"))
        if not (s.number or s.first or s.last):
            continue
        if not s.number:
            warnings.append(f"Row {n}: {s.display} has no student number and was skipped.")
            continue
        if s.key in seen:
            warnings.append(f"Row {n}: student number {s.number} appears more than once "
                            f"({seen[s.key].display} and {s.display}); kept the first.")
            continue
        seen[s.key] = s
        students.append(s)
    if not students:
        raise ListError("No students found in the file.")
    return students, warnings


# --------------------------------------------------------------------------- names & tags

def normalise_number(n: str) -> str:
    n = str(n).strip().lower()
    if n.isdigit():
        n = n.lstrip("0") or "0"
    return n


def safe_part(s: str) -> str:
    s = INVALID_FILENAME_CHARS.sub("", s)
    return re.sub(r"\s+", " ", s).strip().strip(".")


TAG_MODES = ("id_name", "id", "name")


def _tag_part(p: str) -> str:
    return safe_part(p).replace(" ", "-")


def make_tag(s: Student, mode: str = "id_name", duplicate_name: bool = False) -> str:
    """The text sent to Smart Shooter's [Z] field.

    id_name -> 10234_Nimal_Perera   id -> 10234   name -> Nimal_Perera
    (name-only adds the number when two students share a name: Nimal_Perera_10234)
    """
    if mode == "id":
        parts = [s.number]
    elif mode == "name":
        parts = [s.first, s.last] + ([s.number] if duplicate_name else [])
    else:
        parts = [s.number, s.first, s.last]
    tag = "_".join(p for p in (_tag_part(x) for x in parts) if p)
    return tag or _tag_part(s.number)


_LEADING_TOKEN = re.compile(r"^\s*([A-Za-z0-9]+)")
_TRAILING_SEQ = re.compile(r"(?:[ _-]\d{1,4}|\s*\(\d+\))$")


def leading_token(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _LEADING_TOKEN.match(stem)
    return m.group(1) if m else ""


def name_key(text: str) -> str:
    """Loose form of a name for matching: 'Anne-Marie De Silva' == 'anne_marie_de_silva'."""
    return re.sub(r"[\s_,\-]+", "_", text.strip().lower()).strip("_")


class StudentIndex:
    def __init__(self, students: list[Student]):
        self.students = students
        self.by_key = {s.key: s for s in students}
        counts: dict[str, int] = {}
        for s in students:
            counts[s.display.lower()] = counts.get(s.display.lower(), 0) + 1
        self.duplicate_names = {k for k, v in counts.items() if v > 1}
        self.by_tag: dict[str, Student] = {}
        for s in students:
            for mode in TAG_MODES:
                self.by_tag.setdefault(self.tag(s, mode), s)
        # Name lookup for files named after the student (Smart Shooter name-only tags and our exports).
        names: dict[str, Student | None] = {}
        for s in students:
            dup = self.has_duplicate_name(s)
            keys = {name_key(f"{s.first} {s.last}"), name_key(f"{s.last} {s.first}")}
            if dup:  # the plain name is ambiguous; only the name + number forms identify them
                keys = {f"{k}_{name_key(s.number)}" for k in keys}
                keys |= {name_key(f"{s.first} {s.last} ({s.number})"), name_key(f"{s.last} {s.first} ({s.number})")}
            for k in keys:
                if not k:
                    continue
                names[k] = None if (k in names and names[k] is not s) else s
        for s in students:
            if self.has_duplicate_name(s):
                for k in (name_key(f"{s.first} {s.last}"), name_key(f"{s.last} {s.first}")):
                    names[k] = None
        self.by_name = names

    def tag(self, s: Student, mode: str = "id_name") -> str:
        return make_tag(s, mode, self.has_duplicate_name(s))

    def find_by_number(self, n: str) -> Student | None:
        return self.by_key.get(normalise_number(n)) if n else None

    def find_by_name(self, stem: str) -> Student | None:
        """Match 'Nimal_Perera_001', 'Nimal Perera', 'Perera, Nimal_2', 'Nimal Perera (10234)'."""
        text = stem.strip()
        for _ in range(3):
            s = self.by_name.get(name_key(text))
            if s:
                return s
            shorter = _TRAILING_SEQ.sub("", text)
            if shorter == text:
                break
            text = shorter
        return None

    def match_file(self, filename: str) -> Student | None:
        s = self.find_by_number(leading_token(filename))
        if s:
            return s
        return self.find_by_name(os.path.splitext(os.path.basename(filename))[0])

    def match_photo(self, barcode: str, filename: str) -> Student | None:
        if barcode:
            s = self.by_tag.get(barcode) or self.match_file(barcode)
            if s:
                return s
        return self.match_file(filename)

    def has_duplicate_name(self, s: Student) -> bool:
        return s.display.lower() in self.duplicate_names


# --------------------------------------------------------------------------- sorting

SORT_OPTIONS = {          # label -> key used by sort_students
    "List order": "list",
    "Campus": "campus",
    "Surname": "last",
    "First name": "first",
    "Student number": "number",
    "Class": "class",
    "Photos": "photos",
}


def _natural(text: str):
    """'7B' < '10A', '9' < '10': compare numbers by value, text case-insensitively."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", (text or "").lower()) if t]


def sort_students(students: list[Student], key: str, reverse: bool = False,
                  photo_count=None) -> list[Student]:
    surname = lambda s: (_natural(s.last), _natural(s.first), _natural(s.number))
    keys = {
        "campus": lambda s: (not s.campus, _natural(s.campus), _natural(s.group)) + surname(s),
        "last": surname,
        "first": lambda s: (_natural(s.first), _natural(s.last), _natural(s.number)),
        "number": lambda s: _natural(s.number),
        "class": lambda s: (not s.group, _natural(s.group)) + surname(s),
        "photos": lambda s: ((photo_count(s) if photo_count else 0),) + surname(s),
    }
    if key not in keys:
        return list(reversed(students)) if reverse else list(students)
    return sorted(students, key=keys[key], reverse=reverse)


# --------------------------------------------------------------------------- scanning

@dataclass
class PhotoFile:
    path: str
    student: Student | None
    status: str          # "Matched", "Not on list", "Not recognised"
    mtime: float = 0.0


def scan_folder(folder: str, index: StudentIndex, recursive: bool = False) -> list[PhotoFile]:
    out: list[PhotoFile] = []
    if recursive:
        walker = os.walk(folder)
    else:
        walker = [(folder, [], os.listdir(folder))]
    for root, dirs, files in walker:
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() not in IMAGE_EXTS or name.startswith("."):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            tok = leading_token(name)
            s = index.match_file(name)
            if s:
                status = "Matched"
            elif tok and any(ch.isdigit() for ch in tok):
                status = "Not on list"
            else:
                status = "Not recognised"
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            out.append(PhotoFile(path, s, status, mtime))
    return out


def scan_folders(folders: list[str], index: StudentIndex, recursive: bool = False) -> list[PhotoFile]:
    """Scan several folders; a file reached twice (e.g. a folder inside another) is listed once."""
    seen: set[str] = set()
    out: list[PhotoFile] = []
    for folder in folders:
        for p in scan_folder(folder, index, recursive):
            key = os.path.normcase(os.path.abspath(p.path))
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


# --------------------------------------------------------------------------- export planning

@dataclass
class ExportOptions:
    by_name: bool = True
    by_id: bool = True
    which: str = "newest"          # "all", "newest", "first"
    name_format: str = "first_last"  # "first_last" or "last_first"
    by_class: bool = False           # put copies in a sub-folder per class
    by_campus: bool = False          # ...and/or per campus (Campus\Class when both)


NO_CLASS_FOLDER = "No class"
NO_CAMPUS_FOLDER = "No campus"


def campus_folder(s: Student) -> str:
    return safe_part(s.campus) or NO_CAMPUS_FOLDER


def class_folder(s: Student) -> str:
    return safe_part(s.group) or NO_CLASS_FOLDER


@dataclass
class PlannedCopy:
    src: str
    dest: str          # path relative to the export folder
    student: Student


@dataclass
class ExportPlan:
    copies: list[PlannedCopy] = field(default_factory=list)
    unmatched: list[PhotoFile] = field(default_factory=list)
    students_without_photo: list[Student] = field(default_factory=list)


def name_base(s: Student, opts: ExportOptions, index: StudentIndex) -> str:
    if opts.name_format == "last_first":
        base = f"{safe_part(s.last)}, {safe_part(s.first)}".strip(", ")
    else:
        base = f"{safe_part(s.first)} {safe_part(s.last)}".strip()
    if not base:
        base = safe_part(s.number)
    if index.has_duplicate_name(s):
        base = f"{base} ({safe_part(s.number)})"
    return base


def plan_export(photos: list[PhotoFile], index: StudentIndex, opts: ExportOptions,
                best: dict[str, str] | None = None) -> ExportPlan:
    """best: student key -> path of the starred photo (used instead of newest/first)."""
    best_paths = {_norm_path(v) for v in (best or {}).values()}
    plan = ExportPlan()
    per_student: dict[str, list[PhotoFile]] = {}
    for p in photos:
        if p.student is None:
            plan.unmatched.append(p)
        else:
            per_student.setdefault(p.student.key, []).append(p)

    for s in index.students:
        files = per_student.get(s.key)
        if not files:
            plan.students_without_photo.append(s)
            continue
        # Group files that are the same shot in different formats (IMG_1.CR3 + IMG_1.JPG).
        # Files in different folders are always different shots, even with the same name.
        shots: dict[tuple[str, str], list[PhotoFile]] = {}
        for p in files:
            stem = os.path.splitext(os.path.basename(p.path))[0].lower()
            shots.setdefault((os.path.normcase(os.path.dirname(os.path.abspath(p.path))), stem), []).append(p)
        ordered = sorted(shots.values(),
                         key=lambda g: (max(p.mtime for p in g), os.path.basename(g[0].path).lower()))
        starred = [g for g in ordered if any(_norm_path(p.path) in best_paths for p in g)]
        if starred:
            ordered = starred[-1:] + [g for g in ordered if g is not starred[-1]]
            if opts.which in ("newest", "first"):
                ordered = ordered[:1]
        elif opts.which == "newest":
            ordered = ordered[-1:]
        elif opts.which == "first":
            ordered = ordered[:1]

        targets = []
        sub = ([campus_folder(s)] if opts.by_campus else []) + ([class_folder(s)] if opts.by_class else [])
        if opts.by_id:
            targets.append((os.path.join("By ID", *sub), safe_part(s.number)))
        if opts.by_name:
            targets.append((os.path.join("By Name", *sub), name_base(s, opts, index)))
        for i, group in enumerate(ordered, start=1):
            suffix = "" if i == 1 else f"_{i}"
            for p in sorted(group, key=lambda p: p.path.lower()):
                ext = os.path.splitext(p.path)[1].lower()
                for folder, base in targets:
                    plan.copies.append(PlannedCopy(p.path, os.path.join(folder, base + suffix + ext), s))
    return plan


# --------------------------------------------------------------------------- starred (best) photos

BEST_FILE = "best_photos.json"


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def load_best(folders: list[str], recursive: bool = False) -> dict[str, str]:
    """Read best_photos.json from each folder: {student key: full path of the starred photo}."""
    import json
    found: dict[str, str] = {}
    for folder in folders:
        walker = os.walk(folder) if recursive else [(folder, [], [BEST_FILE])]
        for root, _dirs, files in walker:
            if BEST_FILE not in files:
                continue
            try:
                with open(os.path.join(root, BEST_FILE), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            for key, name in (data or {}).items():
                path = os.path.join(root, str(name))
                if not os.path.isfile(path):
                    continue
                old = found.get(normalise_number(key))
                if old is None or os.path.getmtime(path) > os.path.getmtime(old):
                    found[normalise_number(key)] = path
    return found


def set_best(photo_path: str, student_key: str, starred: bool = True) -> None:
    """Star (or un-star) a photo for a student, in best_photos.json beside the photo."""
    import json
    folder = os.path.dirname(os.path.abspath(photo_path))
    file = os.path.join(folder, BEST_FILE)
    try:
        with open(file, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    name = os.path.basename(photo_path)
    if starred:
        data[student_key] = name
    elif data.get(student_key) == name:
        del data[student_key]
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def existing_conflicts(plan: ExportPlan, dest_root: str) -> int:
    return sum(1 for c in plan.copies if os.path.exists(os.path.join(dest_root, c.dest)))


def run_export(plan: ExportPlan, dest_root: str, progress=None) -> list[str]:
    """Copy files. Returns a list of error messages (empty if all went well)."""
    errors = []
    total = len(plan.copies)
    for n, c in enumerate(plan.copies, start=1):
        dest = os.path.join(dest_root, c.dest)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(c.src, dest)
        except OSError as e:
            errors.append(f"{os.path.basename(c.src)}: {e}")
        if progress:
            progress(n, total)
    write_report(plan, dest_root, errors)
    return errors


def write_report(plan: ExportPlan, dest_root: str, errors: list[str]) -> None:
    os.makedirs(dest_root, exist_ok=True)
    with open(os.path.join(dest_root, "export_report.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Status", "Student number", "First name", "Last name", "Class", "Campus",
                    "Original file", "Exported as"])
        for c in plan.copies:
            w.writerow(["Exported", c.student.number, c.student.first, c.student.last, c.student.group,
                        c.student.campus, c.src, c.dest])
        for p in plan.unmatched:
            w.writerow([p.status, "", "", "", "", "", p.path, ""])
        for e in errors:
            w.writerow(["Error", "", "", "", "", "", e, ""])
    write_student_list(plan.students_without_photo, os.path.join(dest_root, "students_without_photo.csv"))


def write_student_list(students: list[Student], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Student number", "First name", "Last name", "Class", "Campus"])
        for s in students:
            w.writerow([s.number, s.first, s.last, s.group, s.campus])
