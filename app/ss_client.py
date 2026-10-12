"""Talks to Smart Shooter's External API (ZeroMQ + JSON).

Smart Shooter publishes events on port 54543 and answers requests on port 54544.
Everything runs on background threads; results and events are placed on
`self.inbox` for the GUI to pick up, so the window never freezes.
"""
from __future__ import annotations

import json
import queue
import threading
import time

try:
    import zmq
except ImportError:  # pragma: no cover - reported in the GUI
    zmq = None

REQUEST_TIMEOUT_MS = 3000


class SmartShooterClient:
    def __init__(self, host: str = "127.0.0.1", req_port: int = 54544, pub_port: int = 54543):
        self.host = host
        self.req_endpoint = f"tcp://{host}:{req_port}"
        self.pub_endpoint = f"tcp://{host}:{pub_port}"
        self.inbox: "queue.Queue[tuple]" = queue.Queue()
        self._requests: "queue.Queue[tuple | None]" = queue.Queue()
        self._stop = threading.Event()
        self._seq = 0
        self.last_contact = 0.0
        self._threads: list[threading.Thread] = []
        # One shared ZeroMQ context for the whole app. A private context that gets garbage-collected
        # while its sockets are still open in background threads can freeze the program.
        self._ctx = zmq.Context.instance() if zmq else None

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not zmq:
            self.inbox.put(("error", "The ZeroMQ library is missing, so Smart Shooter can't be reached."))
            return
        for target in (self._listen_loop, self._request_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._requests.put(None)
        for t in self._threads:
            t.join(timeout=2)       # each thread closes its own socket when it sees the stop flag

    @property
    def connected(self) -> bool:
        return time.time() - self.last_contact < 12

    # ----------------------------------------------------------------- requests
    def request(self, msg_id: str, fields: dict | None = None, tag: str | None = None) -> None:
        """Queue a request. The reply arrives in inbox as ("reply", tag, msg_or_None)."""
        self._requests.put((msg_id, fields or {}, tag or msg_id))

    def set_tag(self, text: str, tag: str = "SetTag") -> None:
        self.request("SetOptions", {"Barcode": text}, tag)

    def apply_naming(self, expression: str = "[Z]", tag: str = "ApplyNaming") -> None:
        self.request("SetOptions", {"FilenameExpression": expression,
                                    "ResetSequenceNumOnEdit": True}, tag)

    def synchronise(self) -> None:
        self.request("Synchronise", {}, "Synchronise")

    def _new_req_socket(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, REQUEST_TIMEOUT_MS)
        s.setsockopt(zmq.SNDTIMEO, REQUEST_TIMEOUT_MS)
        s.connect(self.req_endpoint)
        return s

    def _request_loop(self) -> None:
        sock = self._new_req_socket()
        while not self._stop.is_set():
            item = self._requests.get()
            if item is None:
                break
            msg_id, fields, tag = item
            self._seq += 1
            msg = {"msg_type": "Request", "msg_id": msg_id, "msg_seq_num": self._seq}
            msg.update(fields)
            reply = None
            try:
                sock.send_string(json.dumps(msg))
                reply = json.loads(sock.recv_string())
                self.last_contact = time.time()
            except Exception:
                self.last_contact = 0.0
                # A REQ socket is stuck after a timeout; start a fresh one.
                sock.close(linger=0)
                sock = self._new_req_socket()
            self.inbox.put(("reply", tag, reply))
        sock.close(linger=0)

    # ----------------------------------------------------------------- events
    def _listen_loop(self) -> None:
        sub = self._ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(self.pub_endpoint)
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        while not self._stop.is_set():
            try:
                if not dict(poller.poll(500)):
                    continue
                raw = sub.recv()
            except Exception:
                break
            self.last_contact = time.time()
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("msg_id") in ("PhotoUpdated", "OptionsUpdated"):
                self.inbox.put(("event", msg.get("msg_id"), msg))
        sub.close(linger=0)
