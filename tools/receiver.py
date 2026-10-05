#!/usr/bin/env python3
"""Reference receiver for the gateway's backend API (testing and integration).

Implements POST /v1/pop, /v1/alerts (multipart: metadata, frame, roi) and
POST /v1/heartbeat (JSON). Checks the bearer token, the HMAC signature and
idempotency, then stores everything under --out:

  out/records.jsonl         one line per accepted PoP record or alert
  out/heartbeats.jsonl
  out/images/<event_id>.frame.jpg / .roi.jpg

  python3 tools/receiver.py --port 8080 --api-key KEY --hmac-key SECRET --out ./received

GET / shows a review page: latest PoP images, alerts and camera status
(filter with ?site=SITE_ID). Keep it on an internal network: the page has
no login.

This is a stand-in for the real LUMO backend (and a field-trial viewer),
not production code.
"""
import argparse
import html
import re
import hashlib
import hmac
import json
import os
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None
SEEN = set()
LOCK = threading.Lock()


def parse_multipart(ctype, body):
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = part.get_payload(decode=True)
    return parts


def tail_jsonl(path, n):
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
<title>PoP Monitor review</title><style>
:root{{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f}}}}
body{{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:1200px;margin:0 auto;padding:20px 16px 40px}} h1{{font-size:20px;margin:0}}
h2{{font-size:16px;margin:28px 0 8px}} .muted{{color:var(--muted)}} a{{color:inherit}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.card{{border:1px solid var(--line);border-radius:8px;overflow:hidden}} .card img{{width:100%;display:block;background:#000}}
.card div{{padding:8px 10px}} .bad{{color:var(--bad)}} .ok{{color:var(--ok)}}
.wrap{{overflow-x:auto}}</style></head><body><main>
<h1>PoP Monitor review</h1><p class="muted">{subtitle} &middot; refreshes every 30 s</p>
<h2>Cameras</h2><div class="wrap"><table><tr><th>Site</th><th>Connected</th><th>State</th><th>Plays / hour</th><th>Coverage</th><th>Last play (UTC)</th><th>Active faults</th></tr>{cams}</table></div>
<h2>Latest PoP images</h2><div class="grid">{pops}</div>
<h2>Alerts</h2><div class="wrap"><table><tr><th>Time (UTC)</th><th>Site</th><th>Fault</th><th>State</th><th>Detail</th></tr>{alerts}</table></div>
</main></body></html>"""


def esc(v):
    return html.escape(str(v if v is not None else ""))


def render_page(site):
    recs = tail_jsonl(os.path.join(ARGS.out, "records.jsonl"), 2000)
    if site:
        recs = [r for r in recs if r.get("site_id") == site]
    pops = [r for r in recs if r.get("type") == "creative_change"][-60:][::-1]
    alerts = [r for r in recs if r.get("type") == "fault"][-50:][::-1]
    hb = {}
    for h in tail_jsonl(os.path.join(ARGS.out, "heartbeats.jsonl"), 500):
        for c in h.get("cameras", []):
            hb[c.get("site_id")] = c
    cams = "".join(
        f"<tr><td><a href='?site={esc(k)}'>{esc(k)}</a></td>"
        f"<td class='{'ok' if c.get('connected') else 'bad'}'>{'yes' if c.get('connected') else 'no'}</td>"
        f"<td>{esc(c.get('state'))}</td><td>{esc(c.get('plays_last_hour'))}</td>"
        f"<td>{esc(round(c['coverage'] * 100)) + '%' if 'coverage' in c else ''}</td>"
        f"<td>{esc((c.get('last_play_at') or '')[:19])}</td>"
        f"<td class='bad'>{esc(', '.join(c.get('active_faults', [])))}</td></tr>"
        for k, c in sorted(hb.items()) if not site or k == site) or "<tr><td colspan=7 class='muted'>No heartbeat yet</td></tr>"
    cards = []
    for r in pops:
        q = r.get("quality", {})
        img = f"<img loading='lazy' src='/images/{esc(r['event_id'])}.roi.jpg' alt=''>" if "roi_sha256" in r else ""
        full = f" &middot; <a href='/images/{esc(r['event_id'])}.frame.jpg' target='_blank'>full frame</a>" if "frame_sha256" in r else ""
        flags = " [blank]" if r.get("is_blank") else ""
        flags += " [video]" if r.get("video_creative") else ""
        verdict = "" if q.get("usable", True) else f"<br><span class='bad'>{esc(q.get('reason'))}</span>"
        cards.append(f"<div class='card'>{img}<div><b>{esc(r.get('site_id'))}</b> {esc(r.get('local_creative_id'))}{flags}<br>"
                     f"<span class='muted'>{esc(r.get('play_started_at', '')[:19])} UTC &middot; after "
                     f"{esc(round(r.get('previous_creative_ms', 0) / 1000, 1))} s{full}</span>{verdict}</div></div>")
    rows = "".join(f"<tr><td>{esc(a.get('at', '')[:19])}</td><td>{esc(a.get('site_id'))}</td><td>{esc(a.get('fault'))}</td>"
                   f"<td class='{'ok' if a.get('state') == 'cleared' else 'bad'}'>{esc(a.get('state'))}</td><td>{esc(a.get('detail'))}</td></tr>"
                   for a in alerts) or "<tr><td colspan=5 class='muted'>No alerts</td></tr>"
    sub = f"Site {esc(site)} &middot; <a href='/'>all sites</a>" if site else f"{len(hb)} camera(s)"
    return PAGE.format(subtitle=sub, cams=cams, pops="".join(cards) or "<p class='muted'>No PoP records yet</p>",
                       alerts=rows)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            m = re.search(r"(?:^|&)site=([^&]+)", query)
            body = render_page(m.group(1) if m else "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif re.fullmatch(r"/images/[0-9a-f-]{36}\.(frame|roi)\.jpg", path):
            try:
                with open(os.path.join(ARGS.out, "images", path.rsplit("/", 1)[1]), "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                return self.reply(404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=86400")
        elif path == "/healthz":
            return self.reply(200, {"status": "ok"})
        else:
            return self.reply(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/hooks/"):      # stand-in for Teams/Slack webhooks
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with LOCK, open(os.path.join(ARGS.out, "hooks.jsonl"), "a") as f:
                f.write(json.dumps({"hook": self.path, "body": json.loads(body)}) + "\n")
            print(f"HOOK  {self.path}", flush=True)
            return self.reply(200)
        if self.headers.get("Authorization") != f"Bearer {ARGS.api_key}":
            return self.reply(401, {"error": "bad token"})
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        os.makedirs(os.path.join(ARGS.out, "images"), exist_ok=True)

        if self.path == "/v1/heartbeat":
            with LOCK, open(os.path.join(ARGS.out, "heartbeats.jsonl"), "a") as f:
                f.write(body.decode() + "\n")
            return self.reply(200)

        if self.path not in ("/v1/pop", "/v1/alerts"):
            return self.reply(404)
        parts = parse_multipart(self.headers["Content-Type"], body)
        meta_raw = parts.get("metadata")
        if not meta_raw:
            return self.reply(400, {"error": "no metadata"})
        if ARGS.hmac_key:
            sig = self.headers.get("X-PoP-Signature", "")
            want = "sha256=" + hmac.new(ARGS.hmac_key.encode(), meta_raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, want):
                return self.reply(401, {"error": "bad signature"})
        meta = json.loads(meta_raw)
        eid = meta["event_id"]
        if self.headers.get("Idempotency-Key") != eid:
            return self.reply(400, {"error": "idempotency key mismatch"})
        with LOCK:
            if eid in SEEN:
                return self.reply(409, {"status": "duplicate"})
            SEEN.add(eid)
        checks = {}
        for name in ("frame", "roi"):
            data = parts.get(name)
            if data:
                ok = hashlib.sha256(data).hexdigest() == meta.get(f"{name}_sha256")
                checks[name] = "sha256 ok" if ok else "SHA256 MISMATCH"
                with open(os.path.join(ARGS.out, "images", f"{eid}.{name}.jpg"), "wb") as f:
                    f.write(data)
        meta["_received"] = {"endpoint": self.path, "images": checks}
        with LOCK, open(os.path.join(ARGS.out, "records.jsonl"), "a") as f:
            f.write(json.dumps(meta) + "\n")
        if meta.get("type") == "fault":
            print(f"ALERT {meta['site_id']} {meta['fault']} {meta['state']}: {meta['detail']}", flush=True)
        else:
            q = meta.get("quality", {})
            print(f"PoP   {meta['site_id']} #{meta['sequence']} {meta['local_creative_id']} "
                  f"start={meta['play_started_at']} blank={meta['is_blank']} video={meta['video_creative']} "
                  f"usable={q.get('usable')} {checks}", flush=True)
        return self.reply(201, {"status": "stored"})


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1", help="0.0.0.0 in a container")
    ap.add_argument("--api-key", default=os.environ.get("POP_API_KEY"))
    ap.add_argument("--hmac-key", default=os.environ.get("POP_HMAC_KEY", ""))
    ap.add_argument("--out", default="received")
    ARGS = ap.parse_args()
    if not ARGS.api_key:
        ap.error("--api-key or POP_API_KEY is required")
    os.makedirs(ARGS.out, exist_ok=True)
    for r in tail_jsonl(os.path.join(ARGS.out, "records.jsonl"), 1000000):   # idempotency across restarts
        SEEN.add(r.get("event_id"))
    print(f"receiver on {ARGS.bind}:{ARGS.port}, storing in {ARGS.out}", flush=True)
    ThreadingHTTPServer((ARGS.bind, ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
