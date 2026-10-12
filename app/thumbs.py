"""Small photo previews, made on a background thread so the window never stalls.

JPEG/PNG/TIFF open directly. For RAW files (CR2, CR3, NEF, ARW...) we use a JPEG
with the same name if there is one, otherwise the preview JPEG cameras embed in RAW files.
"""
from __future__ import annotations

import io
import os
import queue
import threading
import time

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - reported as "no preview"
    Image = None

RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf", ".rw2", ".dng", ".pef"}
MAX_RAW_READ = 80 * 1024 * 1024


def _open_plain(path: str):
    img = Image.open(path)
    img.load()
    return img


def _embedded_jpeg(path: str):
    with open(path, "rb") as f:
        data = f.read(MAX_RAW_READ)
    best, best_area, start, tries = None, 0, 0, 0
    while tries < 25:
        i = data.find(b"\xff\xd8\xff", start)
        if i < 0:
            break
        start, tries = i + 3, tries + 1
        try:
            img = Image.open(io.BytesIO(data[i:]))
            area = img.size[0] * img.size[1]
            if area > best_area:
                img.load()
                best, best_area = img, area
        except Exception:
            continue
    return best


def load_preview(path: str, size: int):
    """Return a PIL image no larger than size x size, or None."""
    if Image is None:
        return None
    stem, ext = os.path.splitext(path)
    ext = ext.lower()
    img = None
    if ext in RAW_EXTS:
        for jpg in (stem + ".jpg", stem + ".JPG", stem + ".jpeg"):
            if os.path.isfile(jpg):
                try:
                    img = _open_plain(jpg)
                    break
                except Exception:
                    pass
        if img is None:
            try:
                img = _embedded_jpeg(path)
            except OSError:
                img = None
    else:
        # The file may still be being written when Smart Shooter announces it; retry briefly.
        for attempt in range(4):
            try:
                img = _open_plain(path)
                break
            except Exception:
                time.sleep(0.4)
    if img is None:
        return None
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    img = img.convert("RGB")
    img.thumbnail((size, size))
    return img


class Thumbnailer:
    """Requests go in with request(); finished images come out of results as (key, PIL image or None)."""

    def __init__(self):
        self._jobs: "queue.Queue[tuple | None]" = queue.Queue()
        self.results: "queue.Queue[tuple]" = queue.Queue()
        self._cache: dict[tuple, object] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request(self, key, path: str, size: int) -> None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        ck = (os.path.normcase(path), size, mtime)
        if ck in self._cache:
            self.results.put((key, self._cache[ck]))
        else:
            self._jobs.put((key, path, size, ck))

    def stop(self) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            key, path, size, ck = job
            try:
                img = load_preview(path, size)
            except Exception:
                img = None
            if img is not None:
                self._cache[ck] = img
                if len(self._cache) > 300:
                    self._cache.pop(next(iter(self._cache)))
            self.results.put((key, img))
