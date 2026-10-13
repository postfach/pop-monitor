#!/usr/bin/env python3
"""A stand-in for Microsoft Graph (and the gateway's camera list) for
end-to-end tests of creatives-sync without a tenant.

  python3 sync/tests/mock_graph.py --library LIBDIR --port 8099 [--gateway-port 8098]

LIBDIR is a folder laid out like the SharePoint library: its tree is served as
the Sales drive, with image sizes in the `image` facet like Graph gives them.
Children are paged (3 per page) to exercise @odata.nextLink. /api/cameras on
the gateway port lists the sites in --sites (site_id=Vault screen,...).
"""
import argparse
import hashlib
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

ARGS = None
IDS = {}            # id -> path relative to the library root


def item_id(rel):
    i = "I" + hashlib.sha1(rel.encode()).hexdigest()[:16].upper()
    IDS[i] = rel
    return i


def item(rel):
    full = os.path.join(ARGS.library, rel)
    st = os.stat(full)
    name = os.path.basename(rel) if rel else "root"
    d = {"id": item_id(rel), "name": name, "eTag": f'"{int(st.st_mtime)}-{st.st_size}"',
         "lastModifiedDateTime": "2026-10-01T00:00:00Z", "size": st.st_size,
         "webUrl": "https://lumodigital.sharepoint.com/sites/LUMO/Sales/" + urllib.parse.quote(rel),
         "parentReference": {"path": "/drives/drive1/root:/" + os.path.dirname(rel)}}
    if os.path.isdir(full):
        d["folder"] = {"childCount": len(os.listdir(full))}
    else:
        d["file"] = {}
        if rel.lower().endswith((".jpg", ".jpeg", ".png")):
            try:
                with Image.open(full) as im:
                    d["image"] = {"width": im.width, "height": im.height}
            except Exception:
                pass
    return d


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        if ARGS.verbose:
            print("mock:", self.path, flush=True)

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        b = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        qs = urllib.parse.parse_qs(query)
        if path == "/api/cameras":
            cams = []
            for pair in ARGS.sites.split(","):
                sid, _, scr = pair.partition("=")
                cams.append({"id": sid, "name": sid, "vault_screen": scr, "slot_count": 8, "slot_ms": 8000})
            return self.send(200, cams)
        if self.headers.get("Authorization") != "Bearer test-token":
            return self.send(401, {"error": {"code": "InvalidAuthenticationToken", "message": "bad token"}})
        p = urllib.parse.unquote(path)
        p = p[len("/v1.0"):] if p.startswith("/v1.0") else p
        if re.fullmatch(r"/sites/[^/]+:/sites/LUMO", p):
            return self.send(200, {"id": "site1", "webUrl": "https://lumodigital.sharepoint.com/sites/LUMO"})
        if p == "/sites/site1/drives":
            return self.send(200, {"value": [
                {"id": "drive0", "name": "Documents", "webUrl": "https://lumodigital.sharepoint.com/sites/LUMO/Shared%20Documents"},
                {"id": "drive1", "name": "Sales Library", "webUrl": "https://lumodigital.sharepoint.com/sites/LUMO/Sales"}]})
        m = re.fullmatch(r"/drives/drive1/root:/(.+)", p)
        if m:
            rel = m.group(1).strip("/")
            if not os.path.exists(os.path.join(ARGS.library, rel)):
                return self.send(404, {"error": {"code": "itemNotFound", "message": "not found"}})
            return self.send(200, item(rel))
        m = re.fullmatch(r"/drives/drive1/items/([^/]+)/children", p)
        if m:
            rel = IDS.get(m.group(1))
            if rel is None:
                return self.send(404, {"error": {"code": "itemNotFound", "message": "no item"}})
            names = sorted(os.listdir(os.path.join(ARGS.library, rel)))
            skip = int(qs.get("skip", ["0"])[0])
            page = names[skip: skip + 3]
            out = {"value": [item(f"{rel}/{n}".strip("/")) for n in page]}
            if skip + 3 < len(names):
                out["@odata.nextLink"] = f"http://127.0.0.1:{ARGS.port}/v1.0/drives/drive1/items/{m.group(1)}/children?skip={skip + 3}"
            return self.send(200, out)
        m = re.fullmatch(r"/drives/drive1/items/([^/]+)/content", p)
        if m:
            rel = IDS.get(m.group(1))
            if rel is None:
                return self.send(404, {"error": {"code": "itemNotFound", "message": "no item"}})
            with open(os.path.join(ARGS.library, rel), "rb") as f:
                return self.send(200, raw=f.read(), ctype="application/octet-stream")
        m = re.fullmatch(r"/drives/drive1/root/search\(q='(.+)'\)", p)
        if m:
            want = m.group(1).lower()
            hits = []
            for root, dirs, files in os.walk(ARGS.library):
                for n in dirs + files:
                    if want in n.lower():
                        hits.append(item(os.path.relpath(os.path.join(root, n), ARGS.library)))
            return self.send(200, {"value": hits})
        self.send(404, {"error": {"code": "notMocked", "message": p}})

    def _authed_path(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send(401, {"error": {"code": "InvalidAuthenticationToken", "message": "bad token"}})
            return None
        p = urllib.parse.unquote(self.path.partition("?")[0])
        return p[len("/v1.0"):] if p.startswith("/v1.0") else p

    def do_PUT(self):
        """Simple upload by path (PoP photos): creates missing folders, like Graph."""
        p = self._authed_path()
        if p is None:
            return
        m = re.fullmatch(r"/drives/drive1/root:/(.+):/content", p)
        if not m:
            return self.send(404, {"error": {"code": "notMocked", "message": p}})
        if ARGS.readonly:
            return self.send(403, {"error": {"code": "accessDenied", "message": "read only"}})
        rel = m.group(1).strip("/")
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        os.makedirs(os.path.dirname(os.path.join(ARGS.library, rel)), exist_ok=True)
        with open(os.path.join(ARGS.library, rel), "wb") as f:
            f.write(data)
        self.send(201, item(rel))

    def do_DELETE(self):
        p = self._authed_path()
        if p is None:
            return
        m = re.fullmatch(r"/drives/drive1/items/([^/]+)", p)
        rel = IDS.get(m.group(1)) if m else None
        if rel is None or not os.path.exists(os.path.join(ARGS.library, rel)):
            return self.send(404, {"error": {"code": "itemNotFound", "message": "no item"}})
        os.remove(os.path.join(ARGS.library, rel))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--gateway-port", type=int, default=0)
    ap.add_argument("--sites", default="BEACH-01=Beach,CURLETTS-IN=Curletts In,ALBANY-IN=Albany Expressway In")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--readonly", action="store_true", help="refuse uploads (an app with read access only)")
    ARGS = ap.parse_args()
    item("")
    if ARGS.gateway_port:
        threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", ARGS.gateway_port), H).serve_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
