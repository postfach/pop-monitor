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
(filter with ?site=SITE_ID), and /roi?camera=NAME edits a camera's region
live. /api/* is forwarded to the gateway's live-settings API (GATEWAY_URL).
Set REVIEW_USER and REVIEW_PASSWORD to put a login on the pages.

This is a stand-in for the real LUMO backend (and a field-trial viewer),
not production code.
"""
import argparse
import base64
import urllib.error
import urllib.parse
import urllib.request
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
<h2>Cameras</h2><div class="wrap"><table><tr><th>Site</th><th>Connected</th><th>State</th><th>Plays / hour</th><th>Coverage</th><th>Last play (UTC)</th><th>Active faults</th><th>Region</th></tr>{cams}</table></div>
<h2>Latest PoP images</h2><div class="grid">{pops}</div>
<h2>Alerts</h2><div class="wrap"><table><tr><th>Time (UTC)</th><th>Site</th><th>Fault</th><th>State</th><th>Detail</th></tr>{alerts}</table></div>
</main></body></html>"""


ROI_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit region</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32;--accent:#d4237a;--panel:#ecece8}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f;--panel:#1f1f1d}}
*{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px} h1{font-size:20px;margin:0 0 2px} a{color:inherit}
.muted{color:var(--muted)} .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:14px 0 10px}
button{border:1px solid var(--line);background:transparent;color:var(--fg);padding:7px 12px;border-radius:8px;cursor:pointer;font:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff} button:disabled{opacity:.5;cursor:default}
input[type=number]{width:60px;font:inherit;padding:5px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--fg)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}
@media (max-width:860px){.layout{grid-template-columns:1fr}}
.stage{position:relative;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#000;min-height:180px}
.stage canvas{display:block;width:100%;height:auto;touch-action:none;cursor:crosshair}
.side{display:flex;flex-direction:column;gap:12px}
.panel{background:var(--panel);border-radius:8px;padding:12px}
.panel h2{font-size:13px;margin:0 0 8px;font-weight:600} #preview{width:100%;display:block;border-radius:4px;background:#000}
#msg{min-height:20px} .ok{color:var(--ok)} .bad{color:var(--bad)}
ol{margin:0;padding-left:18px} li{margin:2px 0} code{font-size:12px}
</style></head><body><main>
<p class="muted"><a href="/">&larr; Review</a></p>
<h1>Edit region: <span id="cam"></span></h1>
<p class="muted" id="sub">Loading&hellip;</p>
<div class="bar">
  <button id="snap">New snapshot</button>
  <button id="revert">Undo changes</button>
  <label>Inset <input id="inset" type="number" min="0" max="20" step="1"> %</label>
  <button class="primary" id="save" disabled>Save and apply</button>
  <span id="msg"></span>
</div>
<div class="layout">
  <div class="stage"><canvas id="c"></canvas></div>
  <div class="side">
    <div class="panel"><h2>What the gateway will analyse</h2><canvas id="preview"></canvas>
      <p class="muted" style="margin:6px 0 0">Straightened, with the inset trimmed. It should show only the screen.</p></div>
    <div class="panel"><h2>How to</h2><ol>
      <li>Drag the four handles onto the corners of the lit screen (TL, TR, BR, BL).</li>
      <li>Use the magnifier while dragging to land on the exact corner. Arrow keys nudge the last handle.</li>
      <li>Raise the inset if the preview still shows bezel or frame.</li>
      <li>Save. The gateway uses the new region straight away, from the next play.</li></ol></div>
  </div>
</div>
</main>
<script>
const name = new URLSearchParams(location.search).get('camera') || '';
const $ = id => document.getElementById(id);
const c = $('c'), ctx = c.getContext('2d'), pv = $('preview'), pctx = pv.getContext('2d');
const src = document.createElement('canvas'), sctx = src.getContext('2d', {willReadFrequently: true});
let img = null, pixels = null, saved = null, pts = null, drag = -1, last = 0, cursor = null, dirty = false;
const LABELS = ['TL', 'TR', 'BR', 'BL'];
$('cam').textContent = name;

function msg(t, cls) { $('msg').textContent = t; $('msg').className = cls || 'muted'; }
function setDirty(d) { dirty = d; $('save').disabled = !d; }
const api = p => '/api/cameras/' + encodeURIComponent(name) + p;

async function load() {
  try {
    const r = await fetch('/api/cameras');
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const cam = (await r.json()).find(x => x.name === name);
    if (!cam) throw new Error('camera "' + name + '" not found on the gateway');
    saved = {roi: cam.roi.map(p => p.slice()), inset: cam.inset_pct};
    pts = saved.roi.map(p => p.slice());
    $('inset').value = saved.inset;
    $('sub').textContent = cam.site_id + (cam.model ? ' · ' + cam.model : '') + (cam.connected ? ' · connected' : ' · video not connected');
    await snapshot();
  } catch (e) { $('sub').textContent = ''; msg(String(e.message || e), 'bad'); }
}

async function snapshot() {
  msg('Fetching a snapshot from the camera…');
  const r = await fetch(api('/snapshot') + '?t=' + Date.now());
  if (!r.ok) { msg('Snapshot failed: ' + ((await r.json().catch(() => ({}))).error || r.statusText), 'bad'); return; }
  const bmp = await createImageBitmap(await r.blob());
  img = bmp; c.width = src.width = bmp.width; c.height = src.height = bmp.height;
  sctx.drawImage(bmp, 0, 0); pixels = sctx.getImageData(0, 0, bmp.width, bmp.height);
  msg(dirty ? 'Unsaved changes' : '');
  draw();
}

// homography: unit square -> pixel quad (8x8 solve)
function homography(q) {
  const U = [[0,0],[1,0],[1,1],[0,1]], A = [], b = [];
  for (let i = 0; i < 4; i++) { const [u,v] = U[i], [X,Y] = q[i];
    A.push([u,v,1,0,0,0,-u*X,-v*X]); b.push(X); A.push([0,0,0,u,v,1,-u*Y,-v*Y]); b.push(Y); }
  for (let col = 0; col < 8; col++) { let p = col;
    for (let r = col+1; r < 8; r++) if (Math.abs(A[r][col]) > Math.abs(A[p][col])) p = r;
    [A[col],A[p]] = [A[p],A[col]]; [b[col],b[p]] = [b[p],b[col]];
    for (let r = col+1; r < 8; r++) { const f = A[r][col]/A[col][col];
      for (let k = col; k < 8; k++) A[r][k] -= f*A[col][k]; b[r] -= f*b[col]; } }
  const x = Array(8);
  for (let r = 7; r >= 0; r--) { let s = b[r]; for (let k = r+1; k < 8; k++) s -= A[r][k]*x[k]; x[r] = s/A[r][r]; }
  return [...x, 1];
}
const map = (h, u, v) => { const d = h[6]*u + h[7]*v + 1; return [(h[0]*u + h[1]*v + h[2])/d, (h[3]*u + h[4]*v + h[5])/d]; };

function preview() {
  if (!pixels) return;
  const W = img.width, H = img.height, q = pts.map(p => [p[0]*(W-1), p[1]*(H-1)]);
  const dist = (a, b) => Math.hypot(a[0]-b[0], a[1]-b[1]);
  const bw = (dist(q[0],q[1]) + dist(q[3],q[2]))/2, bh = (dist(q[0],q[3]) + dist(q[1],q[2]))/2;
  const ow = 320, oh = Math.max(40, Math.min(640, Math.round(ow * bh / Math.max(1, bw))));
  pv.width = ow; pv.height = oh;
  const h = homography(q), out = pctx.createImageData(ow, oh), inset = Math.min(0.4, (+$('inset').value || 0)/100);
  for (let j = 0; j < oh; j++) for (let i = 0; i < ow; i++) {
    const u = inset + (1-2*inset)*(i+0.5)/ow, v = inset + (1-2*inset)*(j+0.5)/oh;
    let [x, y] = map(h, u, v); x = Math.min(W-1, Math.max(0, x|0)); y = Math.min(H-1, Math.max(0, y|0));
    const s = (y*W + x)*4, d = (j*ow + i)*4;
    out.data[d] = pixels.data[s]; out.data[d+1] = pixels.data[s+1]; out.data[d+2] = pixels.data[s+2]; out.data[d+3] = 255;
  }
  pctx.putImageData(out, 0, 0);
}

function draw() {
  if (!img) return;
  const W = c.width, H = c.height, scale = W / c.getBoundingClientRect().width || 1;
  ctx.drawImage(img, 0, 0);
  const P = pts.map(p => [p[0]*W, p[1]*H]);
  ctx.lineWidth = 2*scale; ctx.strokeStyle = '#d4237a'; ctx.fillStyle = 'rgba(212,35,122,0.12)';
  ctx.beginPath(); P.forEach((p, i) => i ? ctx.lineTo(...p) : ctx.moveTo(...p)); ctx.closePath(); ctx.fill(); ctx.stroke();
  // inset outline
  const inset = Math.min(0.4, (+$('inset').value || 0)/100), h = homography(P);
  ctx.setLineDash([6*scale, 5*scale]); ctx.beginPath();
  [[inset,inset],[1-inset,inset],[1-inset,1-inset],[inset,1-inset]].forEach((uv, i) => { const p = map(h, ...uv); i ? ctx.lineTo(...p) : ctx.moveTo(...p); });
  ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
  const r = 9*scale; ctx.font = `600 ${Math.round(13*scale)}px system-ui`;
  P.forEach((p, i) => {
    ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, 7); ctx.fillStyle = i === last ? '#d4237a' : '#fff'; ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#fff'; ctx.strokeStyle = 'rgba(0,0,0,.6)'; ctx.lineWidth = 3*scale;
    ctx.strokeText(LABELS[i], p[0] + r*1.4, p[1] - r); ctx.fillText(LABELS[i], p[0] + r*1.4, p[1] - r);
    ctx.strokeStyle = '#d4237a'; ctx.lineWidth = 2*scale;
  });
  if (drag >= 0 && cursor) loupe(P[drag], scale);
  preview();
}

function loupe(p, scale) {   // 4x magnifier near the dragged corner
  const R = 70*scale, Z = 4, cx = p[0] + (p[0] > c.width*0.7 ? -R*1.6 : R*1.6), cy = p[1] + (p[1] > c.height*0.7 ? -R*1.6 : R*1.6);
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.clip();
  ctx.drawImage(img, p[0] - R/Z, p[1] - R/Z, 2*R/Z, 2*R/Z, cx - R, cy - R, 2*R, 2*R);
  ctx.strokeStyle = '#d4237a'; ctx.lineWidth = 1.5*scale;
  ctx.beginPath(); ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy); ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R); ctx.stroke();
  ctx.restore(); ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.lineWidth = 2*scale; ctx.strokeStyle = '#fff'; ctx.stroke();
}

function pos(e) { const b = c.getBoundingClientRect(); return [(e.clientX - b.left)/b.width, (e.clientY - b.top)/b.height]; }
c.addEventListener('pointerdown', e => {
  if (!img) return;
  const p = pos(e), b = c.getBoundingClientRect(); let best = -1, bd = 1e9;
  pts.forEach((q, i) => { const d = Math.hypot((q[0]-p[0])*b.width, (q[1]-p[1])*b.height); if (d < bd) { bd = d; best = i; } });
  if (bd < 36) { drag = last = best; cursor = p; c.setPointerCapture(e.pointerId); draw(); }
});
c.addEventListener('pointermove', e => {
  if (drag < 0) return;
  const p = pos(e); cursor = p;
  pts[drag] = [Math.min(1, Math.max(0, p[0])), Math.min(1, Math.max(0, p[1]))];
  setDirty(true); msg('Unsaved changes'); requestAnimationFrame(draw);
});
c.addEventListener('pointerup', () => { drag = -1; cursor = null; draw(); });
document.addEventListener('keydown', e => {
  if (!img || document.activeElement === $('inset')) return;
  const step = (e.shiftKey ? 10 : 1) / img.width, d = {ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1]}[e.key];
  if (!d) return; e.preventDefault();
  pts[last] = [Math.min(1, Math.max(0, pts[last][0] + d[0]*step)), Math.min(1, Math.max(0, pts[last][1] + d[1]*step*img.width/img.height))];
  setDirty(true); msg('Unsaved changes'); draw();
});
$('inset').addEventListener('input', () => { setDirty(true); msg('Unsaved changes'); draw(); });
$('snap').onclick = snapshot;
$('revert').onclick = () => { if (!saved) return; pts = saved.roi.map(p => p.slice()); $('inset').value = saved.inset; setDirty(false); msg(''); draw(); };
$('save').onclick = async () => {
  $('save').disabled = true; msg('Saving…');
  const body = {roi: pts.map(p => [Math.round(p[0]*1e4)/1e4, Math.round(p[1]*1e4)/1e4]), inset_pct: +$('inset').value || 0};
  const r = await fetch(api('/roi'), {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg('Not saved: ' + (j.error || r.statusText), 'bad'); $('save').disabled = false; return; }
  saved = {roi: body.roi.map(p => p.slice()), inset: body.inset_pct}; setDirty(false);
  msg('Saved and applied. The next play will use this region.', 'ok');
};
window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
window.addEventListener('resize', draw);
load();
</script></body></html>
'''


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
        f"<td class='bad'>{esc(', '.join(c.get('active_faults', [])))}</td>"
        f"<td><a href='/roi?camera={urllib.parse.quote(c.get('camera', ''))}'>Edit region</a></td></tr>"
        for k, c in sorted(hb.items()) if not site or k == site) or "<tr><td colspan=8 class='muted'>No heartbeat yet</td></tr>"
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

    def authorised(self):
        """Optional login for the review pages (REVIEW_USER / REVIEW_PASSWORD)."""
        user, pw = os.environ.get("REVIEW_USER"), os.environ.get("REVIEW_PASSWORD")
        if not user or not pw:
            return True
        want = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        if hmac.compare_digest(self.headers.get("Authorization", ""), want):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PoP Monitor"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def proxy(self, method):
        """Forward /api/* to the gateway's live-settings API with the API key."""
        url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + self.path
        data = None
        if method in ("PUT", "POST"):
            data = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536))
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {ARGS.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code, ctype, body = r.status, r.headers.get("Content-Type", "application/json"), r.read()
        except urllib.error.HTTPError as e:
            code, ctype, body = e.code, e.headers.get("Content-Type", "application/json"), e.read()
        except Exception as e:  # gateway not running / unreachable
            return self.reply(502, {"error": f"cannot reach the gateway at {url.split('/api')[0]}: {e}"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if self.path.startswith("/api/") and self.authorised():
            return self.proxy("PUT")
        if not self.path.startswith("/api/"):
            return self.reply(404)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if not self.authorised():
            return
        if path.startswith("/api/"):
            return self.proxy("GET")
        if path == "/roi":
            body = ROI_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/":
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
