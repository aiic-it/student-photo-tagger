"""Printable reports: contact sheets (photos per class) and retake lists, as A4 PDFs."""
from __future__ import annotations

import csv
import io
import time

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .core import Student, _natural
from .thumbs import load_preview

INK = HexColor("#1F2328")
MUTED = HexColor("#6B7280")
RULE = HexColor("#D0D4D9")
EMPTY = HexColor("#F1F2F4")
WARN = HexColor("#9A3412")
PAGE_W, PAGE_H = A4
MARGIN = 36          # 0.5 inch
NO_CLASS = "No class"


def group_by_class(students: list[Student]) -> list[tuple[str, list[Student]]]:
    """[(heading, students)] by campus then class, students by surname."""
    groups: dict[tuple, list[Student]] = {}
    for s in students:
        groups.setdefault((s.campus or "", s.group or ""), []).append(s)
    out = []
    for (campus, cls), members in sorted(groups.items(), key=lambda kv: (not kv[0][0], _natural(kv[0][0]),
                                                                          not kv[0][1], _natural(kv[0][1]))):
        members.sort(key=lambda s: (_natural(s.last), _natural(s.first), _natural(s.number)))
        heading = " — ".join(p for p in (cls or NO_CLASS, campus) if p)
        out.append((heading, members))
    return out


def _fit(c: canvas.Canvas, text: str, font: str, size: float, width: float) -> str:
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _header(c, title: str, heading: str, page: int, pages: int, school: str):
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(MARGIN, PAGE_H - MARGIN - 4, heading)
    c.setFont("Helvetica", 9)
    c.setFillColor(MUTED)
    right = f"{title}  ·  {time.strftime('%d %b %Y')}" + (f"  ·  page {page} of {pages}" if pages > 1 else "")
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 4, right)
    if school:
        c.drawString(MARGIN, PAGE_H - MARGIN - 18, school)
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(MARGIN, PAGE_H - MARGIN - 24, PAGE_W - MARGIN, PAGE_H - MARGIN - 24)


def _footer(c, text: str):
    c.setFont("Helvetica", 9)
    c.setFillColor(MUTED)
    c.drawString(MARGIN, MARGIN - 14, text)


def contact_sheets(students: list[Student], photo_for, out_path: str, school: str = "",
                   cols: int = 4, rows: int = 5, progress=None) -> int:
    """photo_for(student) -> image path or None. Returns the number of pages written."""
    c = canvas.Canvas(out_path, pagesize=A4)
    c.setTitle("Contact sheets")
    c.setAuthor("Student Photo Tagger")
    top = PAGE_H - MARGIN - 34
    bottom = MARGIN + 6
    cell_w = (PAGE_W - 2 * MARGIN) / cols
    cell_h = (top - bottom) / rows
    pic_h = cell_h - 34
    pic_w = min(cell_w - 12, pic_h * 0.8)
    per_page = cols * rows
    pages = done = 0
    total = len(students)
    for heading, members in group_by_class(students):
        n_pages = max(1, -(-len(members) // per_page))
        for p in range(n_pages):
            _header(c, "Contact sheet", heading, p + 1, n_pages, school)
            _footer(c, "Please check each photo matches the name. Mark any problems and return to the office.   "
                       "Checked by: ______________________")
            for i, s in enumerate(members[p * per_page:(p + 1) * per_page]):
                col, row = i % cols, i // cols
                x = MARGIN + col * cell_w + (cell_w - pic_w) / 2
                y = top - (row + 1) * cell_h + 30
                path = photo_for(s)
                img = None
                if path:
                    try:
                        img = load_preview(path, 600)
                    except Exception:
                        img = None
                if img is not None:
                    w, h = img.size
                    scale = min(pic_w / w, pic_h / h)
                    dw, dh = w * scale, h * scale
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=82)
                    buf.seek(0)
                    c.drawImage(ImageReader(buf), x + (pic_w - dw) / 2, y + (pic_h - dh) / 2, dw, dh)
                else:
                    c.setFillColor(EMPTY)
                    c.setStrokeColor(RULE)
                    c.rect(x, y, pic_w, pic_h, fill=1, stroke=1)
                    c.setFillColor(MUTED)
                    c.setFont("Helvetica", 9)
                    c.drawCentredString(x + pic_w / 2, y + pic_h / 2 - 3, "No photo")
                cx = MARGIN + col * cell_w + cell_w / 2
                c.setFillColor(WARN if s.privacy else INK)
                c.setFont("Helvetica-Bold", 9.5)
                name = ("! " if s.privacy else "") + s.display
                c.drawCentredString(cx, y - 12, _fit(c, name, "Helvetica-Bold", 9.5, cell_w - 8))
                c.setFillColor(MUTED)
                c.setFont("Helvetica", 8)
                c.drawCentredString(cx, y - 23, s.number + ("   privacy flag" if s.privacy else ""))
                done += 1
                if progress:
                    progress(done, total)
            c.showPage()
            pages += 1
    if pages == 0:
        _header(c, "Contact sheet", "No students", 1, 1, school)
        c.showPage()
        pages = 1
    c.save()
    return pages


def retake_rows(students: list[Student], photo_count, retakes: dict, include_unstarred: bool, best: dict):
    """[(student, reason)] for students who need a photo (re)taken."""
    out = []
    for s in students:
        if s.key in retakes:
            out.append((s, "Marked for retake" + (f" ({retakes[s.key]})" if retakes[s.key] else "")))
        elif photo_count(s) == 0:
            out.append((s, "No photo"))
        elif include_unstarred and s.key not in best:
            out.append((s, "No best photo chosen"))
    return out


def retake_list_pdf(rows: list[tuple[Student, str]], out_path: str, school: str = "") -> int:
    c = canvas.Canvas(out_path, pagesize=A4)
    c.setTitle("Retake list")
    c.setAuthor("Student Photo Tagger")
    by_class = group_by_class([s for s, _ in rows])
    reason = {s.key: r for s, r in rows}
    cols = [("", 18), ("Number", 70), ("Name", 190), ("Reason", 170), ("Done", 40)]
    line_h = 20
    pages = 0
    for heading, members in by_class:
        per_page = int((PAGE_H - 2 * MARGIN - 70) // line_h)
        n_pages = max(1, -(-len(members) // per_page))
        for p in range(n_pages):
            _header(c, f"Retake list · {len(members)} student{'s' if len(members) != 1 else ''}", heading,
                    p + 1, n_pages, school)
            y = PAGE_H - MARGIN - 46
            x = MARGIN
            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(MUTED)
            for label, w in cols:
                c.drawString(x, y, label)
                x += w
            y -= 6
            c.setStrokeColor(RULE)
            c.line(MARGIN, y, PAGE_W - MARGIN, y)
            for s in members[p * per_page:(p + 1) * per_page]:
                y -= line_h
                x = MARGIN
                c.setFillColor(WARN if s.privacy else INK)
                c.setFont("Helvetica", 10)
                values = ["!" if s.privacy else "", s.number, s.display, reason[s.key], ""]
                for (label, w), v in zip(cols, values):
                    if label == "Done":
                        c.setStrokeColor(INK)
                        c.rect(x + 8, y - 2, 11, 11, fill=0, stroke=1)
                    else:
                        c.drawString(x, y, _fit(c, v, "Helvetica", 10, w - 6))
                    x += w
                c.setStrokeColor(RULE)
                c.line(MARGIN, y - 6, PAGE_W - MARGIN, y - 6)
            _footer(c, "Tick each student once their photo has been retaken.")
            c.showPage()
            pages += 1
    if pages == 0:
        _header(c, "Retake list", "Nobody needs a retake", 1, 1, school)
        c.showPage()
        pages = 1
    c.save()
    return pages


def retake_list_csv(rows: list[tuple[Student, str]], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Student number", "First name", "Last name", "Class", "Campus", "Reason", "Privacy"])
        for heading, members in group_by_class([s for s, _ in rows]):
            for s in members:
                r = dict((x.key, y) for x, y in rows)[s.key]
                w.writerow([s.number, s.first, s.last, s.group, s.campus, r, "Yes" if s.privacy else ""])
