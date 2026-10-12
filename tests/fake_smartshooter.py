"""A stand-in for Smart Shooter's External API, used by the automated tests.

It answers SetOptions / Synchronise like Smart Shooter does, and `shoot()` saves a
photo named with the current [Z]_[S] pattern and announces it with a PhotoUpdated event.
Run it on its own to try the app without a camera:  python tests/fake_smartshooter.py <photo folder>
"""
import json
import os
import sys
import threading
import time
import uuid

import zmq


class FakeSmartShooter:
    def __init__(self, download_path, req_port=54544, pub_port=54543):
        self.download_path = download_path
        os.makedirs(download_path, exist_ok=True)
        self.req_port, self.pub_port = req_port, pub_port
        self.options = {"Barcode": "", "FilenameExpression": "[Z]_[S]", "ResetSequenceNumOnEdit": True,
                        "DownloadPath": download_path}
        self.seq = 0
        self.photos = []
        self.requests = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.ctx = zmq.Context()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.bind(f"tcp://127.0.0.1:{pub_port}")
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=3)
        self.pub.close(linger=0)
        self.ctx.term()

    def publish(self, msg_id, fields):
        msg = {"msg_type": "Event", "msg_id": msg_id, "msg_seq_num": 0}
        msg.update(fields)
        with self._lock:
            self.pub.send_string(json.dumps(msg))

    def _serve(self):
        rep = self.ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.req_port}")
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if not dict(poller.poll(200)):
                continue
            req = json.loads(rep.recv_string())
            self.requests.append(req)
            reply = {"msg_type": "Response", "msg_id": req["msg_id"], "msg_seq_num": req["msg_seq_num"],
                     "msg_result": True}
            if req["msg_id"] == "SetOptions":
                changed = {k: v for k, v in req.items() if not k.startswith("msg_")}
                if changed.get("Barcode") != self.options.get("Barcode") and self.options.get("ResetSequenceNumOnEdit"):
                    self.seq = 0
                self.options.update(changed)
                rep.send_string(json.dumps(reply))
                self.publish("OptionsUpdated", dict(self.options))
                continue
            if req["msg_id"] == "Synchronise":
                reply["OptionsInfo"] = dict(self.options)
                reply["PhotoInfo"] = list(self.photos)
            rep.send_string(json.dumps(reply))
        rep.close(linger=0)

    def shoot(self, ext=".jpg"):
        self.seq += 1
        z = self.options.get("Barcode", "")
        base = self.options["FilenameExpression"].replace("[Z]", z).replace("[S]", f"{self.seq:03d}")
        name, n = base + ext, 1
        while os.path.exists(os.path.join(self.download_path, name)):  # filename collision check
            name, n = f"{base}-{n}{ext}", n + 1
        path = os.path.join(self.download_path, name)
        try:  # a real small JPEG so previews can be tested
            from PIL import Image
            Image.new("RGB", (300, 400), (40 + (self.seq * 50) % 200, 120, 160)).save(path, "JPEG")
        except Exception:
            with open(path, "wb") as f:
                f.write(b"fake photo")
        info = {"PhotoKey": uuid.uuid4().hex, "PhotoName": name, "PhotoFilename": path,
                "PhotoOriginalName": f"IMG_{1000 + self.seq}{ext.upper()}", "PhotoBarcode": z,
                "PhotoSequenceNum": self.seq}
        self.publish("PhotoUpdated", dict(info, PhotoLocation="Camera"))
        info["PhotoLocation"] = "Local Disk"
        self.photos.append(info)
        self.publish("PhotoUpdated", info)
        return path


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "fake_photos")
    ss = FakeSmartShooter(folder).start()
    print(f"Fake Smart Shooter running. Photos go to {folder}")
    print("Press Enter to 'take a photo', or type q then Enter to quit.")
    try:
        while input().strip().lower() != "q":
            print("Saved", os.path.basename(ss.shoot()))
    except (EOFError, KeyboardInterrupt):
        pass
    ss.stop()
