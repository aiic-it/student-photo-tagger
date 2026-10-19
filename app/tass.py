"""TASS REST API client (read-only): sign in with a ClientKey/ClientSecret and read students.

Docs: TASS developer portal. Sign-in is POST /api/users -> JWT (15 minutes); every other call is
GET /api/{cmpy_code}/... with "Authorization: Bearer <token>". Lists page with $top (max 500) / $skip.
Uses only the Python standard library.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from .core import Student

PAGE = 500
TIMEOUT = 30
STUDENT_FIELDS = ["stud_code", "first_name", "preferred_name", "surname", "preferred_surname", "campus_code",
                  "pctut_grp", "form_cls", "year_grp", "house", "doe", "dol", "privacy_flg"]
CLASS_SOURCES = {          # label -> TASS field
    "Tutor group": "pctut_grp",
    "Form class": "form_cls",
    "Year group": "year_grp",
    "House": "house",
}


class TassError(Exception):
    """A problem talking to TASS, with a message suitable for the person using the app."""


def api_root(url: str) -> str:
    """Accept 'school.edu.au', 'https://school.edu.au' or 'https://school.edu.au/api'."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise TassError("Enter the TASS web address.")
    if "://" not in url:
        url = "https://" + url
    if not url.lower().endswith("/api"):
        url += "/api"
    return url


class TassClient:
    def __init__(self, url: str, client_key: str, client_secret: str, company: str = ""):
        self.root = api_root(url)
        self.client_key = (client_key or "").strip()
        self.client_secret = client_secret or ""
        self.company = (company or "").strip()
        self.token: str | None = None
        self.token_expiry: dt.datetime | None = None
        self.companies: list[dict] = []
        self._ssl = ssl.create_default_context()   # uses the Windows certificate store

    # ------------------------------------------------------------------ http
    def _request(self, method: str, url: str, body: dict | None = None, auth: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self._ssl) as r:
                raw = r.read()
                return r.status, (json.loads(raw.decode("utf-8")) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8") or "null")
            except ValueError:
                payload = None
            return e.code, payload
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError):
                raise TassError(f"The TASS server's security certificate wasn't accepted ({reason}).") from e
            raise TassError(f"Can't reach TASS at {self.root} ({reason}). "
                            "Check the address and that this PC is on the school network.") from e
        except (TimeoutError, OSError) as e:
            raise TassError(f"TASS didn't respond ({e}).") from e
        except ValueError as e:
            raise TassError("TASS sent a reply the app couldn't read. Is the web address right?") from e

    @staticmethod
    def _message(payload, fallback: str) -> str:
        if isinstance(payload, dict):
            if payload.get("message"):
                return str(payload["message"])
            if isinstance(payload.get("errors"), dict):
                return "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in payload["errors"].items())
        return fallback

    # ------------------------------------------------------------------ sign-in
    def sign_in(self) -> list[dict]:
        if not self.client_key or not self.client_secret:
            raise TassError("Enter the ClientKey and ClientSecret from your TASS administrator.")
        status, payload = self._request("POST", f"{self.root}/users",
                                        {"ClientKey": self.client_key, "ClientSecret": self.client_secret},
                                        auth=False)
        if status == 401 or status == 403:
            raise TassError("TASS didn't accept the ClientKey/ClientSecret. Check them with your TASS administrator.")
        if status != 200 or not isinstance(payload, dict) or not payload.get("token"):
            raise TassError(self._message(payload, f"TASS sign-in failed (HTTP {status})."))
        self.token = payload["token"]
        self.token_expiry = _parse_time(payload.get("token_expiry_date"))
        self.companies = payload.get("allowed_companies") or []
        if not self.company and len(self.companies) == 1:
            self.company = str(self.companies[0].get("cmpy_code", ""))
        return self.companies

    def _ensure_token(self) -> None:
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)
        if not self.token or (self.token_expiry and self.token_expiry <= soon):
            self.sign_in()

    def get(self, path: str, params: dict | None = None):
        self._ensure_token()
        if not self.company:
            raise TassError("Choose the TASS company code.")
        query = "?" + urllib.parse.urlencode(params, safe="$,", quote_via=urllib.parse.quote) if params else ""
        url = f"{self.root}/{urllib.parse.quote(self.company)}/{path}{query}"
        status, payload = self._request("GET", url)
        if status == 401:                       # token expired early: sign in again once
            self.sign_in()
            status, payload = self._request("GET", url)
        if status == 403:
            raise TassError("TASS says this API user doesn't have permission for that "
                            f"(company {self.company}). Ask your TASS administrator for Students > Information > Student: Read.")
        if status == 404:
            raise TassError(f"TASS couldn't find {path} (HTTP 404). Is the company code right?")
        if status != 200:
            raise TassError(self._message(payload, f"TASS returned an error (HTTP {status})."))
        return payload

    # ------------------------------------------------------------------ students
    def students(self, progress=None) -> list[dict]:
        rows: list[dict] = []
        skip = 0
        while True:
            page = self.get("students", {"$select": ",".join(STUDENT_FIELDS),
                                         "$orderby": "surname asc,first_name asc",
                                         "$top": PAGE, "$skip": skip})
            if not isinstance(page, list):
                raise TassError("TASS sent an unexpected reply for the student list.")
            rows.extend(page)
            if progress:
                progress(len(rows))
            if len(page) < PAGE:
                return rows
            skip += PAGE


def _parse_time(text):
    if not text:
        return None
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _parse_date(text):
    if not text:
        return None
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def is_current(row: dict, today: dt.date | None = None, include_future: bool = False) -> bool:
    today = today or dt.date.today()
    left = _parse_date(row.get("dol"))
    if left and left < today:
        return False
    entry = _parse_date(row.get("doe"))
    if entry and entry > today and not include_future:
        return False
    return True


def to_students(rows: list[dict], class_source: str = "Tutor group", preferred_names: bool = True,
                include_future: bool = False, campuses: list[str] | None = None,
                years: list[str] | None = None, today: dt.date | None = None) -> list[Student]:
    field = CLASS_SOURCES.get(class_source, "pctut_grp")
    wanted_campus = {c.strip().lower() for c in campuses or [] if c.strip()}
    wanted_year = {str(y).strip().lower() for y in years or [] if str(y).strip()}
    out: list[Student] = []
    for r in rows:
        code = str(r.get("stud_code") or "").strip()
        if not code or not is_current(r, today, include_future):
            continue
        campus = str(r.get("campus_code") or "").strip()
        year = "" if r.get("year_grp") is None else str(r.get("year_grp")).strip()
        if wanted_campus and campus.lower() not in wanted_campus:
            continue
        if wanted_year and year.lower() not in wanted_year:
            continue
        first = (r.get("preferred_name") if preferred_names else None) or r.get("first_name") or ""
        last = (r.get("preferred_surname") if preferred_names else None) or r.get("surname") or ""
        group = r.get(field)
        s = Student(code, str(first).strip(), str(last).strip(), "" if group is None else str(group).strip(), campus,
                    privacy=bool(r.get("privacy_flg")))
        out.append(s)
    return out


# --------------------------------------------------------------------------- secret storage

def protect(text: str) -> str:
    """Encrypt a secret for this Windows user (DPAPI). Elsewhere it is only obscured (base64)."""
    if not text:
        return ""
    if sys.platform.startswith("win"):
        try:
            return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), protect=True)).decode()
        except OSError:
            pass
    return "b64:" + base64.b64encode(text.encode("utf-8")).decode()


def unprotect(stored: str) -> str:
    if not stored:
        return ""
    kind, _, data = stored.partition(":")
    try:
        if kind == "dpapi":
            return _dpapi(base64.b64decode(data), protect=False).decode("utf-8")
        if kind == "b64":
            return base64.b64decode(data).decode("utf-8")
    except (OSError, ValueError):
        return ""
    return ""


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("Windows couldn't encrypt/decrypt the TASS secret")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def cache_path(app_folder: str) -> str:
    return os.path.join(app_folder, "tass_students.csv")
