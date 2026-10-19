"""A stand-in for the TASS REST API, following the TASS developer portal:
POST /api/users -> JWT; GET /api/{cmpy}/students with $select/$top (max 500)/$skip and Bearer auth."""
import datetime as dt
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY, SECRET, COMPANY = "demo-key", "demo-secret", "10"


def make_students(n=1203):
    rows = []
    for i in range(n):
        rows.append({
            "stud_code": f"{10000 + i:07d}", "first_name": f"First{i}", "preferred_name": None,
            "surname": f"Surname{i:04d}", "preferred_surname": None,
            "campus_code": "NTH" if i % 2 else "STH", "pctut_grp": f"{7 + i % 6}{'ABC'[i % 3]}",
            "form_cls": f"F{i % 4}", "year_grp": 7 + i % 6, "house": "Banksia", "doe": "2020-01-28",
            "dol": None, "privacy_flg": False, "gender": "string", "usi": None,
        })
    return rows


class FakeTass:
    def __init__(self, students=None):
        self.students = students if students is not None else make_students()
        self.tokens: set[str] = set()
        self.expire_next = False          # make the next authorised call return 401 once
        self.calls: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload=None):
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                fake.calls.append("POST " + self.path)
                if self.path != "/api/users":
                    return self._send(404, {"message": "Not Found"})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if body.get("ClientKey") != KEY or body.get("ClientSecret") != SECRET:
                    return self._send(401, {"message": "Unauthorized"})
                token = f"tok{len(fake.tokens)}"
                fake.tokens.add(token)
                expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                self._send(200, {"token": token, "token_expiry_date": expiry,
                                 "allowed_companies": [{"cmpy_code": COMPANY, "name_text": "Example School"}]})

            def do_GET(self):
                fake.calls.append("GET " + self.path)
                parsed = urllib.parse.urlparse(self.path)
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in fake.tokens:
                    return self._send(401, {"message": "Unauthorized"})
                if fake.expire_next:
                    fake.expire_next = False
                    fake.tokens.discard(auth[7:])
                    return self._send(401, {"message": "Unauthorized"})
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "api" or parts[2] != "students":
                    return self._send(404, {"message": "Not Found"})
                if parts[1] != COMPANY:
                    return self._send(403, {"message": "Forbidden"})
                q = urllib.parse.parse_qs(parsed.query)
                top = int(q.get("$top", ["500"])[0])
                if top > 500:
                    return self._send(400, {"errors": {"$top": ["Maximum is 500."]}})
                skip = int(q.get("$skip", ["0"])[0])
                fields = q.get("$select", [""])[0].split(",") if q.get("$select") else None
                rows = fake.students[skip:skip + top]
                if fields:
                    rows = [{k: r.get(k) for k in fields} for r in rows]
                self._send(200, rows)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        self.server.shutdown()
        self.server.server_close()
