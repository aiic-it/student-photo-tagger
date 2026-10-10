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
}

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class Student:
    number: str
    first: str
    last: str
    group: str = ""

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
        s = Student(get(r, "number"), get(r, "first"), get(r, "last"), get(r, "group"))
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


def make_tag(s: Student) -> str:
    """The text sent to Smart Shooter's [Z] field, e.g. 10234_Nimal_Perera."""
    parts = [safe_part(p).replace(" ", "-") for p in (s.number, s.first, s.last)]
    return "_".join(p for p in parts if p)


_LEADING_TOKEN = re.compile(r"^\s*([A-Za-z0-9]+)")


def leading_token(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _LEADING_TOKEN.match(stem)
    return m.group(1) if m else ""


class StudentIndex:
    def __init__(self, students: list[Student]):
        self.students = students
        self.by_key = {s.key: s for s in students}
        self.by_tag = {make_tag(s): s for s in students}
        counts: dict[str, int] = {}
        for s in students:
            counts[s.display.lower()] = counts.get(s.display.lower(), 0) + 1
        self.duplicate_names = {k for k, v in counts.items() if v > 1}

    def find_by_number(self, n: str) -> Student | None:
        return self.by_key.get(normalise_number(n)) if n else None

    def match_file(self, filename: str) -> Student | None:
        return self.find_by_number(leading_token(filename))

    def match_photo(self, barcode: str, filename: str) -> Student | None:
        if barcode:
            s = self.by_tag.get(barcode) or self.match_file(barcode)
            if s:
                return s
        return self.match_file(filename)

    def has_duplicate_name(self, s: Student) -> bool:
        return s.display.lower() in self.duplicate_names


# --------------------------------------------------------------------------- scanning

@dataclass
class PhotoFile:
    path: str
    student: Student | None
    status: str          # "Matched", "Not on list", "No student number"
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
            s = index.find_by_number(tok)
            if s:
                status = "Matched"
            elif tok and any(ch.isdigit() for ch in tok):
                status = "Not on list"
            else:
                status = "No student number"
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            out.append(PhotoFile(path, s, status, mtime))
    return out


# --------------------------------------------------------------------------- export planning

@dataclass
class ExportOptions:
    by_name: bool = True
    by_id: bool = True
    which: str = "newest"          # "all", "newest", "first"
    name_format: str = "first_last"  # "first_last" or "last_first"


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


def plan_export(photos: list[PhotoFile], index: StudentIndex, opts: ExportOptions) -> ExportPlan:
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
        shots: dict[str, list[PhotoFile]] = {}
        for p in files:
            stem = os.path.splitext(os.path.basename(p.path))[0].lower()
            shots.setdefault(stem, []).append(p)
        ordered = sorted(shots.values(),
                         key=lambda g: (max(p.mtime for p in g), os.path.basename(g[0].path).lower()))
        if opts.which == "newest":
            ordered = ordered[-1:]
        elif opts.which == "first":
            ordered = ordered[:1]

        targets = []
        if opts.by_id:
            targets.append(("By ID", safe_part(s.number)))
        if opts.by_name:
            targets.append(("By Name", name_base(s, opts, index)))
        for i, group in enumerate(ordered, start=1):
            suffix = "" if i == 1 else f"_{i}"
            for p in sorted(group, key=lambda p: p.path.lower()):
                ext = os.path.splitext(p.path)[1].lower()
                for folder, base in targets:
                    plan.copies.append(PlannedCopy(p.path, os.path.join(folder, base + suffix + ext), s))
    return plan


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
        w.writerow(["Status", "Student number", "First name", "Last name", "Original file", "Exported as"])
        for c in plan.copies:
            w.writerow(["Exported", c.student.number, c.student.first, c.student.last,
                        os.path.basename(c.src), c.dest])
        for p in plan.unmatched:
            w.writerow([p.status, "", "", "", os.path.basename(p.path), ""])
        for e in errors:
            w.writerow(["Error", "", "", "", e, ""])
    write_student_list(plan.students_without_photo, os.path.join(dest_root, "students_without_photo.csv"))


def write_student_list(students: list[Student], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Student number", "First name", "Last name", "Class"])
        for s in students:
            w.writerow([s.number, s.first, s.last, s.group])
