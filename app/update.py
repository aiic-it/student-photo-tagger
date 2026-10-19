"""Check GitHub for a newer release of the app."""
from __future__ import annotations

import json
import re
import ssl
import urllib.request

REPO = "aiic-it/student-photo-tagger"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def version_tuple(text: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:4])


def is_newer(latest: str, current: str) -> bool:
    return bool(latest) and version_tuple(latest) > version_tuple(current)


def latest_release(url: str = LATEST_API, timeout: float = 8) -> tuple[str, str] | None:
    """(tag, page url) of the newest release, or None if it can't be read (offline, private repo...)."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "StudentPhotoTagger"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    return (tag, data.get("html_url") or RELEASES_PAGE) if tag else None
