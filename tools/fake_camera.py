#!/usr/bin/env python3
"""Fake Axis VAPIX endpoint for testing the gateway without a camera.

Serves /axis-cgi/jpg/image.cgi (full-resolution JPEG of the synthetic scene
at the current scene time) and /axis-cgi/com/ptz.cgi?query=position.
Scene time = now - start, so start it together with the gateway.

  python3 tools/fake_camera.py --port 8081 [--size 1920x1080] [--moved-after 0]
"""
import argparse
import io
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

import sim_scene

START = time.time()
ARGS = None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/axis-cgi/jpg/image.cgi":
            w, h = map(int, (q.get("resolution", [ARGS.size])[0]).split("x"))
            comp = int(q.get("compression", ["30"])[0])
            t = time.time() - START
            img = Image.fromarray(sim_scene.render(t, w, h))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=max(5, min(98, 100 - comp)))
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/axis-cgi/com/ptz.cgi":
            moved = ARGS.moved_after and time.time() - START > ARGS.moved_after
            pan = 12.5 if moved else 10.0
            body = f"pan={pan:.4f}\ntilt=-5.0000\nzoom=2500\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    global ARGS, START
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--moved-after", type=float, default=0, help="report a PTZ move after N s")
    ARGS = ap.parse_args()
    sim_scene.render(0, *map(int, ARGS.size.split("x")))   # warm the maps
    START = time.time()
    print(f"fake camera on :{ARGS.port}", flush=True)
    ThreadingHTTPServer((ARGS.bind, ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
