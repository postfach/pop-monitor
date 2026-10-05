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
(filter with ?site=SITE_ID), and /roi?site=SITE_ID edits a camera's region
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
import time
import uuid
from datetime import datetime, timezone
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


def rewrite_records(fn):
    """Rewrite records.jsonl through fn(record) -> record, or None to drop it
    (its images are deleted too). Returns how many records were dropped."""
    path = os.path.join(ARGS.out, "records.jsonl")
    dropped = 0
    with LOCK:
        try:
            with open(path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return 0
        tmp = path + ".tmp"
        with open(tmp, "w") as out:
            for line in lines:
                try:
                    r = json.loads(line)
                except ValueError:
                    out.write(line)
                    continue
                new = fn(r)
                if new is None:
                    dropped += 1
                    SEEN.discard(r.get("event_id"))
                    for kind in ("frame", "roi"):
                        try:
                            os.remove(os.path.join(ARGS.out, "images", f"{r.get('event_id')}.{kind}.jpg"))
                        except (FileNotFoundError, TypeError):
                            pass
                    continue
                out.write(json.dumps(new) + "\n")
        os.replace(tmp, path)
    return dropped


def recorded_sites():
    """Every site ID that has records here, with counts and the newest time."""
    sites = {}
    for r in tail_jsonl(os.path.join(ARGS.out, "records.jsonl"), 1000000):
        sid = r.get("site_id")
        if not sid:                      # gateway-level alerts (gateway_offline) belong to no site
            continue
        e = sites.setdefault(sid, {"site_id": sid, "name": "", "pops": 0, "alerts": 0, "last": ""})
        e["name"] = disp(r) or e["name"]
        e["pops" if r.get("type") == "creative_change" else "alerts"] += 1
        t = r.get("play_started_at") or r.get("at") or ""
        if t > e["last"]:
            e["last"] = t
    for e in sites.values():
        e["last_local"] = local_time(e["last"], True)
    return sites


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PoP Monitor review</title><style>
:root{{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f}}}}
body{{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:1200px;margin:0 auto;padding:20px 16px 40px}} h1{{font-size:20px;margin:0}}
h2{{font-size:16px;margin:28px 0 8px}} .muted{{color:var(--muted)}} a{{color:inherit}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.card{{border:1px solid var(--line);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}}
.thumb{{position:relative;aspect-ratio:16/9;background:#000;overflow:hidden;display:flex;align-items:center;justify-content:center}}
.thumb img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block}} .thumb span{{color:#888;font-size:12px}}
.card div{{padding:8px 10px}} .bad{{color:var(--bad)}} .ok{{color:var(--ok)}}
.wrap{{overflow-x:auto}}
.alerts{{max-height:330px;overflow-y:auto;border:1px solid var(--line);border-radius:8px}}
.alerts td:nth-child(-n+3){{white-space:nowrap}} .alerts th{{position:sticky;top:0;background:var(--bg);box-shadow:inset 0 -1px var(--line)}} .alerts tr:last-child td{{border-bottom:0}}
.toolbar{{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}}
.refresh{{display:flex;gap:8px;align-items:center}} .refresh select,.refresh button{{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg)}}
.refresh button{{cursor:pointer}} #next{{min-width:110px;font-size:12px}}</style></head><body><main>
<nav style="display:flex;gap:16px;margin-bottom:14px"><a href="/" style="font-weight:600;text-decoration:none">Review</a><a href="/sites" style="color:var(--muted);text-decoration:none">Sites</a><a href="/health" style="color:var(--muted);text-decoration:none">Health</a></nav>
<div class="toolbar"><div><h1>PoP Monitor review</h1><p class="muted" style="margin:2px 0 0">{subtitle}</p></div>
<div class="refresh"><label for="every" class="muted">Refresh</label>
<select id="every"><option value="0">Off</option><option value="5">Every 5 s</option><option value="10">Every 10 s</option>
<option value="15">Every 15 s</option><option value="30">Every 30 s</option><option value="60">Every minute</option>
<option value="120">Every 2 min</option><option value="300">Every 5 min</option><option value="600">Every 10 min</option>
<option value="900">Every 15 min</option><option value="1800">Every 30 min</option><option value="3600">Every hour</option></select>
<button id="now" type="button">Refresh now</button><span id="next" class="muted"></span></div></div>
{banner}<h2>Sites</h2><div class="wrap"><table><tr><th>Site</th><th>Connected</th><th>State</th><th>Plays / hour</th><th>Coverage</th><th>Last play ({zone})</th><th>Active faults</th><th></th></tr>{cams}</table></div>
<h2>{pops_title}</h2><div class="grid">{pops}</div>
<h2>Alerts <span class="muted" style="font-size:13px;font-weight:400">{alerts_note}</span></h2><div class="wrap alerts"><table><tr><th>Time ({zone})</th><th>Site</th><th>Fault</th><th>State</th><th>Detail</th></tr>{alerts}</table></div>
</main>{refresh_js}</body></html>"""


SITES_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sites</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32;--accent:#d4237a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f}}
*{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px} h1{font-size:20px;margin:0} a{color:inherit}
nav{display:flex;gap:16px;margin-bottom:14px} nav a{color:var(--muted);text-decoration:none} nav a.on{color:var(--fg);font-weight:600}
.muted{color:var(--muted)} .bad{color:var(--bad)} .ok{color:var(--ok)}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 12px}
.btn{display:inline-block;border:1px solid var(--line);background:transparent;color:var(--fg);padding:7px 12px;border-radius:8px;cursor:pointer;font:inherit;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.wrap{overflow-x:auto} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top;white-space:nowrap}
td.actions a{margin-right:12px} .note{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-top:16px}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites" class="on">Sites</a><a href="/health">Health</a></nav>
<div class="head"><div><h1>Sites</h1><p class="muted" id="sub" style="margin:2px 0 0">Loading&hellip;</p></div>
<div style="display:flex;gap:8px"><a class="btn" href="/sites/import">Import from CSV</a><a class="btn primary" href="/site">Add site</a></div></div>
<div class="wrap"><table><thead><tr><th>Site</th><th>Model</th><th>Camera</th><th>Video</th><th>Rotation</th><th>Plays / hour</th><th>Faults</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="8" class="muted">Loading&hellip;</td></tr></tbody></table></div>
<p class="note muted" id="note" hidden></p>
<section id="old" hidden><h2 style="font-size:16px;margin:28px 0 4px">Records from sites that no longer exist</h2>
<p class="muted" style="margin:0 0 10px">PoP images and alerts stored under a site ID the gateway no longer has (sites that were removed, or renamed before this version). They're already hidden from the review page; delete them to free the space.</p>
<div class="wrap"><table><thead><tr><th>Site ID</th><th>Last name</th><th>PoP images</th><th>Alerts</th><th>Newest</th><th></th></tr></thead>
<tbody id="oldrows"></tbody></table></div></section>
</main><script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function rotation(c) {
  if (c.takeover) return 'Takeover';
  if (!c.slot_count && !c.slot_ms) return '<span class="muted">not set</span>';
  return `${c.slot_count || '?'} × ${c.slot_ms ? (c.slot_ms / 1000) + ' s' : '?'}` + (c.dayparts ? ` <span class="muted">+${c.dayparts} daypart</span>` : '');
}
function hostText(c) {
  if (c.rtsp_url_override) return '<span class="muted">custom URL</span>';
  const ports = [c.http_port ? 'web ' + c.http_port : '', c.rtsp_transport === 'http' ? 'video over web' : (c.rtsp_port ? 'RTSP ' + c.rtsp_port : '')].filter(Boolean).join(', ');
  return esc(c.host) + (ports ? ` <span class="muted">(${esc(ports)})</span>` : '');
}
async function load() {
  try {
    const [r, s] = await Promise.all([fetch('/api/cameras'), fetch('/api/settings')]);
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const cams = await r.json(), st = s.ok ? await s.json() : {};
    document.getElementById('sub').textContent = `${cams.length} site(s) on gateway ${st.gateway_id || ''}`;
    document.getElementById('rows').innerHTML = cams.map(c => {
      const q = encodeURIComponent(c.id);
      const exp = c.expected_per_hour ? ` <span class="muted">of ${c.expected_per_hour}</span>` : '';
      return `<tr><td><b>${esc(c.name)}</b>${c.name !== c.id ? `<br><span class="muted">${esc(c.id)}</span>` : ''}</td>
        <td>${esc(c.model)}</td><td>${hostText(c)}</td>
        <td class="${c.connected ? 'ok' : 'bad'}">${c.connected ? 'connected' : 'not connected'}</td>
        <td>${rotation(c)}</td><td>${c.plays_last_hour}${exp}</td>
        <td class="bad">${esc(c.active_faults.join(', '))}</td>
        <td class="actions"><a href="/site?site=${q}">Settings</a><a href="/roi?site=${q}">Region</a><a href="/?site=${q}">PoP</a></td></tr>`;
    }).join('') || '<tr><td colspan="8" class="muted">No sites yet. Click Add site.</td></tr>';
    loadOld(new Set(cams.map(c => c.id)));
    if (st.cameras_from_state) {
      const n = document.getElementById('note'); n.hidden = false;
      n.textContent = 'Sites are managed here and saved by the gateway. The cameras list in config.json is no longer used.';
    }
  } catch (e) {
    document.getElementById('sub').innerHTML = '<span class="bad">' + esc(e.message || e) + '</span>';
    document.getElementById('rows').innerHTML = '';
  }
}
async function loadOld(known) {
  const r = await fetch('/records/sites'); if (!r.ok) return;
  const old = (await r.json()).filter(e => !known.has(e.site_id));
  const sec = document.getElementById('old'), rows = document.getElementById('oldrows');
  if (rows.querySelector('[data-armed]')) return;   // don't redraw while a delete is being confirmed
  sec.hidden = !old.length;
  rows.innerHTML = old.map(e => `<tr><td><b>${esc(e.site_id)}</b></td><td>${esc(e.name)}</td><td>${e.pops}</td><td>${e.alerts}</td>
    <td>${esc(e.last_local)}</td>
    <td><button class="btn" data-site="${esc(e.site_id)}">Delete</button></td></tr>`).join('');
}
document.getElementById('oldrows').addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-site]'); if (!b) return;
  if (!b.dataset.armed) {                          // first click arms, second click deletes
    b.dataset.armed = 1; b.textContent = 'Click again to delete'; b.style.borderColor = 'var(--bad)'; b.style.color = 'var(--bad)';
    setTimeout(() => { if (b.isConnected && b.dataset.armed) { delete b.dataset.armed; b.textContent = 'Delete'; b.style.cssText = ''; } }, 5000);
    return;
  }
  b.disabled = true; b.textContent = 'Deleting…';
  const r = await fetch('/records/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({site_id: b.dataset.site})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { b.textContent = j.error || 'Failed'; return; }
  delete b.dataset.armed; load();
});
load(); setInterval(load, 15000);
</script></body></html>
'''

SITE_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Site settings</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32;--accent:#d4237a;--panel:#ecece8}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f;--panel:#1f1f1d}}
*{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px} h1{font-size:20px;margin:0 0 2px} a{color:inherit}
nav{display:flex;gap:16px;margin-bottom:14px} nav a{color:var(--muted);text-decoration:none} nav a.on{color:var(--fg);font-weight:600}
.muted{color:var(--muted)} .bad{color:var(--bad)} .ok{color:var(--ok)}
fieldset{border:0;background:var(--panel);border-radius:10px;padding:14px 16px 6px;margin:14px 0}
legend{font-weight:600;padding:0;float:left;width:100%;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px 16px;clear:both}
label.f{display:flex;flex-direction:column;gap:4px;margin-bottom:8px} label.f span{font-size:12px;color:var(--muted)}
input[type=text],input[type=number],input[type=password],input[type=time]{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);width:100%}
input:disabled{opacity:.5} .check{display:flex;gap:8px;align-items:center;margin:4px 0 10px;clear:both}
[hidden]{display:none!important}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden} .seg label{padding:6px 12px;cursor:pointer}
.seg input{display:none} .seg input:checked+span{font-weight:600;color:var(--accent)}
details{clear:both;margin:4px 0 10px} summary{cursor:pointer;color:var(--muted)}
.hint{font-size:12px;color:var(--muted);margin:-4px 0 10px;clear:both}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:18px}
button{border:1px solid var(--line);background:transparent;color:var(--fg);padding:8px 14px;border-radius:8px;cursor:pointer;font:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff} button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:default} #msg{min-height:20px}
#test img{max-width:100%;border-radius:8px;margin-top:10px;display:block}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites" class="on">Sites</a><a href="/health">Health</a></nav>
<h1 id="title">Add site</h1><p class="muted" id="sub">New camera</p>
<form id="f" autocomplete="off">
<fieldset><legend>Site</legend><div class="grid">
  <label class="f"><span>Site ID (required, unique)</span><input type="text" name="site_id" required placeholder="AKL-BRITOMART-01"></label>
  <label class="f"><span>Display name (optional)</span><input type="text" name="display_name" placeholder="Britomart north face"></label>
  <label class="f"><span>Camera model</span><input type="text" name="model" list="models" placeholder="M5525-E"></label>
</div>
<datalist id="models"><option>M5525-E</option><option>M5526-E</option><option>P3225-LVE Mk II</option><option>P3227-LVE</option><option>P3267-LV</option></datalist>
</fieldset>

<fieldset><legend>Camera connection</legend><div class="grid">
  <label class="f"><span>Host (IP or DNS name, no port)</span><input type="text" name="host" placeholder="203.0.113.10"></label>
  <label class="f"><span>Web port (blank = 80, or 443 with HTTPS)</span><input type="number" name="http_port" min="1" max="65535" placeholder="80"></label>
  <label class="f"><span>RTSP port (blank = 554)</span><input type="number" name="rtsp_port" min="1" max="65535" placeholder="554"></label>
</div>
<div class="check"><span class="muted" style="min-width:110px">Video transport</span>
  <div class="seg"><label><input type="radio" name="rtsp_transport" value="tcp" checked><span>RTSP</span></label><label><input type="radio" name="rtsp_transport" value="http"><span>Over web port</span></label></div></div>
<p class="hint">Use "Over web port" when only the web port is forwarded, or if the video connects but no picture arrives.</p>
<label class="check"><input type="checkbox" name="use_https"> Use HTTPS for snapshots</label>
<label class="check"><input type="checkbox" name="camera_tls_insecure"> Accept the camera's self-signed certificate</label>
<div class="grid">
  <label class="f"><span id="userhint">Camera user (blank = default)</span><input type="text" name="username"></label>
  <label class="f"><span id="pwhint">Password (blank = default)</span><input type="password" name="password" autocomplete="new-password"></label>
</div>
<label class="check" id="clearpw" hidden><input type="checkbox" name="clear_password"> Remove this site's password and use the default</label>
<details><summary>Advanced: custom URLs</summary><div class="grid" style="margin-top:10px">
  <label class="f"><span>Video URL override (rtsp://&hellip;)</span><input type="text" name="rtsp_url_override" placeholder="built from the fields above"></label>
  <label class="f"><span>Snapshot base URL override</span><input type="text" name="vapix_url_override" placeholder="built from the fields above"></label>
</div><p class="hint">Only for unusual setups. These replace the host, ports and transport above. <span id="effective"></span></p></details>
</fieldset>

<fieldset><legend>Rotation</legend>
<label class="check"><input type="checkbox" name="takeover"> Takeover (one creative only, so no frozen or loop alerts)</label>
<div class="grid">
  <label class="f"><span>Slots in the rotation</span><input type="number" name="slot_count" min="1" max="200" placeholder="6"></label>
  <label class="f"><span>Slot length (seconds)</span><input type="number" name="slot_s" min="1" max="3600" step="0.5" placeholder="10"></label>
</div><p class="hint" id="rothint">Leave blank if unknown. Setting these makes detection and fault alerts fit the loop.</p>
</fieldset>

<fieldset><legend>Schedule and checks</legend>
<label class="check"><input type="checkbox" name="allday" checked> Operates 24 hours</label>
<div class="grid">
  <label class="f"><span>Screen on from</span><input type="time" name="from" value="05:00"></label>
  <label class="f"><span>Screen off at</span><input type="time" name="to" value="01:00"></label>
</div><p class="hint">Black screens outside these hours don't raise alerts.</p>
<label class="check"><input type="checkbox" name="ptz_check" checked> Alert and pause if a PTZ camera leaves its preset</label>
<div class="grid">
  <label class="f"><span id="covhint">Coverage alert below (%, 0 = off)</span><input type="number" name="coverage_min_pct" min="0" max="100" placeholder="90"></label>
  <label class="f"><span>Measured over (minutes)</span><input type="number" name="coverage_window_min" min="5" max="240" placeholder="30"></label>
</div><p class="hint">Alerts when fewer plays are logged than the rotation should produce. Needs a slot length. Not checked outside operating hours or until a full window of data has been collected. Blank = the gateway default.</p>
</fieldset>

<div class="bar">
  <button type="submit" class="primary" id="save">Add site</button>
  <button type="button" id="test" hidden>Test camera</button>
  <button type="button" id="region" hidden>Edit region</button>
  <button type="button" id="dup" hidden>Duplicate site</button>
  <span style="flex:1"></span>
  <button type="button" class="danger" id="del" hidden>Delete site</button>
</div>
<p id="msg"></p><div id="testout"></div>
</form>
</main><script>
const params = new URLSearchParams(location.search), editing = params.get('site') || params.get('camera');
const copying = !editing && params.get('copy');
const f = document.getElementById('f'), $ = id => document.getElementById(id);
const msg = (t, cls) => { $('msg').textContent = t; $('msg').className = cls || 'muted'; };
let current = null;

function setVal(n, v) { const el = f.elements[n]; if (!el) return; if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? ''; }
function sync() {
  const take = f.elements.takeover.checked;
  f.elements.slot_count.disabled = f.elements.slot_s.disabled = take;
  const all = f.elements.allday.checked;
  f.elements.from.disabled = f.elements.to.disabled = all;
  f.elements.camera_tls_insecure.disabled = !f.elements.use_https.checked;
}
f.addEventListener('change', sync);

async function init() {
  const s = await fetch('/api/settings').then(r => r.ok ? r.json() : {}).catch(() => ({}));
  if (s.default_coverage_min_pct !== undefined) {
    f.elements.coverage_min_pct.placeholder = s.default_coverage_min_pct;
    f.elements.coverage_window_min.placeholder = s.default_coverage_window_min;
    $('covhint').textContent = `Coverage alert below (%, 0 = off, blank = ${s.default_coverage_min_pct})`;
  }
  if (s.default_username) $('userhint').textContent = `Camera user (blank = default "${s.default_username}")`;
  if (!s.default_password_set) $('pwhint').textContent = 'Password (no default set: enter one)';
  if (!editing && !copying) { sync(); return; }
  const r = await fetch('/api/cameras/' + encodeURIComponent(editing || copying));
  if (!r.ok) { msg('Could not load this site: ' + ((await r.json().catch(() => ({}))).error || r.statusText), 'bad'); return; }
  current = await r.json();
  if (copying) { fillFrom(current, true); return; }
  fillFrom(current, false);
}

function fillFrom(c, copy) {
  if (copy) {
    $('title').textContent = 'Add site'; $('sub').textContent = `Settings copied from ${c.name}. Enter the new site's ID, name and camera address.`;
  } else {
    $('title').textContent = c.name;
    document.title = c.name + ' – settings';
  }
  const current = c;
  if (!copy)
    $('sub').innerHTML = (current.connected ? '<span class="ok">Video connected</span>' : '<span class="bad">Video not connected</span>') +
      (current.active_faults.length ? ' &middot; <span class="bad">' + current.active_faults.join(', ') + '</span>' : '');
  for (const k of copy ? ['model','username'] : ['site_id','display_name','model','host','username','rtsp_url_override','vapix_url_override'])
    setVal(k, current[k]);
  if (current.coverage_custom) { setVal('coverage_min_pct', current.coverage_min_pct); setVal('coverage_window_min', current.coverage_window_min); }
  setVal('http_port', current.http_port || ''); setVal('rtsp_port', current.rtsp_port || '');
  f.querySelector(`input[name=rtsp_transport][value=${current.rtsp_transport}]`).checked = true;
  for (const k of ['use_https','camera_tls_insecure','takeover','ptz_check']) setVal(k, current[k]);
  setVal('slot_count', current.slot_count || ''); setVal('slot_s', current.slot_ms ? current.slot_ms / 1000 : '');
  const hrs = current.operating_hours || '00:00-24:00', all = hrs === '00:00-24:00' || hrs.split('-')[0] === hrs.split('-')[1];
  setVal('allday', all); if (!all) { setVal('from', hrs.split('-')[0]); setVal('to', hrs.split('-')[1]); }
  if (copy) { sync(); f.elements.site_id.focus(); return; }
  if (current.password_set) { $('pwhint').textContent = 'Password (blank = keep this site’s password)'; $('clearpw').hidden = false; }
  if (current.dayparts) $('rothint').textContent = `This site also has ${current.dayparts} time-of-day profile(s) from the config file; these fields set the default profile.`;
  $('effective').textContent = current.snapshot_url ? 'Snapshots currently use ' + current.snapshot_url + '.' : '';
  $('save').textContent = 'Save changes'; $('test').hidden = $('region').hidden = $('del').hidden = $('dup').hidden = false;
  sync();
}

function formData() {
  const e = f.elements, num = v => v === '' ? 0 : Number(v);
  const d = {
    site_id: e.site_id.value.trim(), display_name: e.display_name.value.trim(), model: e.model.value.trim(),
    host: e.host.value.trim(), http_port: num(e.http_port.value), rtsp_port: num(e.rtsp_port.value),
    rtsp_transport: f.querySelector('input[name=rtsp_transport]:checked').value,
    use_https: e.use_https.checked, camera_tls_insecure: e.use_https.checked && e.camera_tls_insecure.checked,
    username: e.username.value.trim(), rtsp_url_override: e.rtsp_url_override.value.trim(),
    vapix_url_override: e.vapix_url_override.value.trim(),
    takeover: e.takeover.checked, slot_count: e.takeover.checked ? 0 : num(e.slot_count.value),
    slot_ms: e.takeover.checked ? 0 : Math.round(num(e.slot_s.value) * 1000),
    operating_hours: e.allday.checked ? '00:00-24:00' : `${e.from.value}-${e.to.value}`,
    ptz_check: e.ptz_check.checked,
    coverage_min_pct: e.coverage_min_pct.value === '' ? null : Number(e.coverage_min_pct.value),
    coverage_window_min: e.coverage_window_min.value === '' ? null : Number(e.coverage_window_min.value),
  };
  if (e.password.value) d.password = e.password.value;
  if (e.clear_password.checked) d.clear_password = true;
  return d;
}

f.addEventListener('submit', async ev => {
  ev.preventDefault();
  const d = formData();
  if (!d.site_id) { msg('Site ID is required.', 'bad'); return; }
  if (!d.host && !d.rtsp_url_override) { msg('Enter the camera host (or a custom video URL).', 'bad'); return; }
  $('save').disabled = true; msg(editing ? 'Saving and restarting this camera…' : 'Adding site…');
  const r = await fetch(editing ? '/api/cameras/' + encodeURIComponent(editing) : '/api/cameras',
                        {method: editing ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(d)});
  const j = await r.json().catch(() => ({}));
  $('save').disabled = false;
  if (!r.ok) { msg('Not saved: ' + (j.error || r.statusText), 'bad'); return; }
  if (!editing) { location.href = '/roi?site=' + encodeURIComponent(j.id) + '&new=1'; return; }
  if (j.id !== editing) { location.href = '/site?site=' + encodeURIComponent(j.id); return; }
  location.reload();
});

$('test').onclick = async () => {
  const name = current.id; $('testout').innerHTML = ''; msg('Asking the camera for a snapshot (saved settings)…');
  const r = await fetch('/api/cameras/' + encodeURIComponent(name) + '/snapshot?t=' + Date.now());
  if (!r.ok) { msg((await r.json().catch(() => ({}))).error || 'Snapshot failed', 'bad'); return; }
  const url = URL.createObjectURL(await r.blob());
  msg('Snapshot OK. Video status is shown at the top of the page (refresh after a few seconds).', 'ok');
  $('testout').innerHTML = `<img src="${url}" alt="Snapshot from the camera">`;
};
$('region').onclick = () => { location.href = '/roi?site=' + encodeURIComponent(current.id); };
$('dup').onclick = () => { location.href = '/site?copy=' + encodeURIComponent(current.id); };
$('del').onclick = async () => {
  if (!confirm(`Delete site ${current.name}? PoP records already received are kept.`)) return;
  const r = await fetch('/api/cameras/' + encodeURIComponent(current.id), {method: 'DELETE'});
  if (r.ok) location.href = '/sites'; else msg('Not deleted: ' + ((await r.json().catch(() => ({}))).error || r.statusText), 'bad');
};
init();
</script></body></html>
'''

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
<nav style="display:flex;gap:16px;margin-bottom:14px"><a href="/" style="color:var(--muted);text-decoration:none">Review</a><a href="/sites" style="font-weight:600;text-decoration:none">Sites</a><a href="/health" style="color:var(--muted);text-decoration:none">Health</a></nav>
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
const qs = new URLSearchParams(location.search), name = qs.get('site') || qs.get('camera') || '';   /* site ID */
const $ = id => document.getElementById(id);
const c = $('c'), ctx = c.getContext('2d'), pv = $('preview'), pctx = pv.getContext('2d');
const src = document.createElement('canvas'), sctx = src.getContext('2d', {willReadFrequently: true});
let img = null, pixels = null, saved = null, pts = null, drag = -1, last = 0, cursor = null, dirty = false;
const LABELS = ['TL', 'TR', 'BR', 'BL'];
$('cam').textContent = name;
if (new URLSearchParams(location.search).get('new')) setTimeout(() => msg('Site added. Now drag the corners onto the screen and save.', 'ok'), 1500);

function msg(t, cls) { $('msg').textContent = t; $('msg').className = cls || 'muted'; }
function setDirty(d) { dirty = d; $('save').disabled = !d; }
const api = p => '/api/cameras/' + encodeURIComponent(name) + p;

async function load() {
  try {
    const r = await fetch('/api/cameras');
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const cam = (await r.json()).find(x => x.id === name);
    if (!cam) throw new Error('site "' + name + '" not found on the gateway');
    $('cam').textContent = cam.name; document.title = cam.name + ' – region';
    saved = {roi: cam.roi.map(p => p.slice()), inset: cam.inset_pct};
    pts = saved.roi.map(p => p.slice());
    $('inset').value = saved.inset;
    $('sub').textContent = (cam.name !== cam.id ? cam.id + ' · ' : '') + (cam.model ? cam.model : 'Camera') + (cam.connected ? ' · connected' : ' · video not connected');
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


# Review-page auto refresh: interval chosen from the dropdown, remembered per
# browser (and overridable with ?refresh=SECONDS). Pauses while the tab is hidden.
IMPORT_TEMPLATE = (
    "site_id,display_name,model,host,http_port,rtsp_port,video_transport,use_https,accept_self_signed,"
    "username,password,slot_count,slot_seconds,takeover,hours,ptz_check,coverage_min_pct,coverage_window_min\r\n"
    "AKL-EXAMPLE-01,Example Rd North,M5525-E,203.0.113.10,8080,8554,rtsp,no,no,,,6,10,no,05:00-01:00,yes,,\r\n"
)

IMPORT_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import sites</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32;--warn:#a15c00;--accent:#d4237a;--panel:#ececea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f;--panel:#22221f;--warn:#e0a040}}
*{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px} h1{font-size:20px;margin:0} h2{font-size:16px;margin:22px 0 6px} a{color:inherit}
nav{display:flex;gap:16px;margin-bottom:14px} nav a{color:var(--muted);text-decoration:none} nav a.on{color:var(--fg);font-weight:600}
.muted{color:var(--muted)} .bad{color:var(--bad)} .ok{color:var(--ok)} .warn{color:var(--warn)}
.btn{display:inline-block;border:1px solid var(--line);background:transparent;color:var(--fg);padding:7px 12px;border-radius:8px;cursor:pointer;font:inherit;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff} .btn:disabled{opacity:.5;cursor:default}
.panel{background:var(--panel);border-radius:10px;padding:14px 16px;margin:12px 0}
.panel ol{margin:4px 0 0;padding-left:20px} .panel li{margin:3px 0}
.wrap{overflow-x:auto} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}
th{white-space:nowrap} td.st{min-width:220px} code{font-size:12px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}
.drop{display:block;border:2px dashed var(--line);border-radius:10px;padding:22px;text-align:center;cursor:pointer;margin-top:14px}
.drop.over{border-color:var(--accent)}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites" class="on">Sites</a><a href="/health">Health</a></nav>
<h1>Import sites from CSV</h1>
<p class="muted" style="margin:2px 0 0">Add many sites at once. Nothing changes until you click Import.</p>
<div class="panel"><ol>
<li><a href="/sites/template.csv">Download the template</a> and open it in Excel. Replace the example row with one row per site, then save as CSV.</li>
<li>Only <b>site_id</b> and <b>host</b> are required. Blank cells use the gateway defaults (ports 80/554, camera login from <code>.env</code>, 24-hour operation, coverage alert at the default).</li>
<li>Upload it below and check the preview. Each site still needs its region set afterwards.</li>
</ol>
<details style="margin-top:8px"><summary>Column reference</summary><div class="wrap"><table style="margin-top:6px">
<tr><td><code>site_id</code></td><td>Required, unique. Used in URLs and reports, e.g. AKL-BRITOMART-01</td></tr>
<tr><td><code>display_name</code></td><td>What people see, e.g. Britomart North</td></tr>
<tr><td><code>model</code></td><td>Camera model, e.g. M5525-E</td></tr>
<tr><td><code>host</code></td><td>Camera IP or DNS name, no port</td></tr>
<tr><td><code>http_port</code>, <code>rtsp_port</code></td><td>Blank = 80 (443 with HTTPS) and 554</td></tr>
<tr><td><code>video_transport</code></td><td><code>rtsp</code> (default) or <code>web</code> (video over the web port)</td></tr>
<tr><td><code>use_https</code>, <code>accept_self_signed</code>, <code>takeover</code>, <code>ptz_check</code></td><td>yes / no (blank = no, except ptz_check = yes)</td></tr>
<tr><td><code>username</code>, <code>password</code></td><td>Blank = the default login. Passwords typed here are stored in the gateway's site list, so prefer the default where cameras share one.</td></tr>
<tr><td><code>slot_count</code>, <code>slot_seconds</code></td><td>The rotation, e.g. 6 and 10</td></tr>
<tr><td><code>hours</code></td><td>Screen on-off, e.g. <code>05:00-01:00</code>. Blank = 24 hours</td></tr>
<tr><td><code>coverage_min_pct</code>, <code>coverage_window_min</code></td><td>Coverage alert below this %, over this many minutes. 0 = off, blank = default</td></tr>
</table></div></details></div>

<label class="drop" id="drop"><input type="file" id="file" accept=".csv,text/csv" hidden>
<b>Choose a CSV file</b> <span class="muted">or drop it here</span></label>
<label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="update">
Update sites that already exist (only the filled-in cells are changed)</label>

<section id="prev" hidden><h2>Preview</h2><p class="muted" id="summary"></p>
<div class="wrap"><table><thead><tr><th>Row</th><th>Site ID</th><th>Name</th><th>Camera</th><th>Rotation</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="bar"><button class="btn primary" id="go" disabled>Import</button><span id="msg" class="muted"></span></div></section>
</main><script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let plan = [], existing = new Set();

function parseCSV(text) {
  text = text.replace(/^﻿/, '');
  const first = text.split(/\r?\n/, 1)[0];
  const sep = (first.match(/;/g) || []).length > (first.match(/,/g) || []).length ? ';' : ',';   // Excel in some locales
  const rows = []; let row = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') q = false;
      else cell += ch;
    } else if (ch === '"') q = true;
    else if (ch === sep) { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += ch;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.filter(r => r.some(c => c.trim() !== ''));
}
const yes = v => /^(y|yes|true|1|x|on)$/i.test(String(v).trim());
const no = v => /^(n|no|false|0|off)$/i.test(String(v).trim());

// One CSV row -> the JSON the gateway's site API takes. For updates only filled cells are sent.
function toSite(r, forUpdate) {
  const v = k => (r[k] ?? '').trim(), d = {}, problems = [];
  const put = (k, val) => { d[k] = val; };
  const int = (k, col, lo, hi) => {
    const t = v(col); if (t === '') return;
    const n = Number(t);
    if (!Number.isFinite(n) || n < lo || n > hi) problems.push(`${col} must be ${lo}-${hi}`); else put(k, Math.round(n));
  };
  const bool = (k, col) => { const t = v(col); if (t === '') return; if (yes(t)) put(k, true); else if (no(t)) put(k, false); else problems.push(`${col}: use yes or no`); };
  for (const [k, col] of [['site_id','site_id'],['display_name','display_name'],['model','model'],['host','host'],['username','username']])
    if (v(col) !== '' || (!forUpdate && k !== 'site_id')) put(k, v(col));
  if (v('password')) put('password', v('password'));
  int('http_port', 'http_port', 1, 65535); int('rtsp_port', 'rtsp_port', 1, 65535);
  const tr = v('video_transport').toLowerCase();
  if (tr) { if (tr === 'rtsp' || tr === 'tcp') put('rtsp_transport', 'tcp'); else if (tr === 'web' || tr === 'http') put('rtsp_transport', 'http'); else problems.push('video_transport: use rtsp or web'); }
  else if (!forUpdate) put('rtsp_transport', 'tcp');
  bool('use_https', 'use_https'); bool('camera_tls_insecure', 'accept_self_signed'); bool('takeover', 'takeover'); bool('ptz_check', 'ptz_check');
  int('slot_count', 'slot_count', 1, 200);
  const ss = v('slot_seconds');
  if (ss !== '') { const n = Number(ss); if (!(n > 0 && n <= 3600)) problems.push('slot_seconds must be 1-3600'); else put('slot_ms', Math.round(n * 1000)); }
  const h = v('hours');
  if (h) { if (/^\d{1,2}:\d\d-\d{1,2}:\d\d$/.test(h)) put('operating_hours', h.split('-').map(x => x.padStart(5, '0')).join('-')); else problems.push('hours: use HH:MM-HH:MM, e.g. 05:00-01:00'); }
  else if (!forUpdate) put('operating_hours', '00:00-24:00');
  int('coverage_min_pct', 'coverage_min_pct', 0, 100); int('coverage_window_min', 'coverage_window_min', 5, 240);
  if (d.takeover) { d.slot_count = 0; d.slot_ms = 0; }
  return {d, problems};
}

async function preview(text) {
  const rows = parseCSV(text);
  if (rows.length < 2) { show('The file has no rows under the header.', 'bad'); return; }
  const head = rows[0].map(h => h.trim().toLowerCase().replace(/\s+/g, '_'));
  if (!head.includes('site_id')) { show('No site_id column. Start from the template.', 'bad'); return; }
  const cams = await fetch('/api/cameras').then(r => r.ok ? r.json() : []).catch(() => []);
  existing = new Set(cams.map(c => c.id));
  const update = $('update').checked, seen = new Map();
  plan = rows.slice(1).map((cells, i) => {
    const r = Object.fromEntries(head.map((h, k) => [h, cells[k] ?? '']));
    const id = (r.site_id || '').trim(), isNew = !existing.has(id);
    const {d, problems} = toSite(r, !isNew);
    const p = {row: i + 2, id, name: (r.display_name || '').trim(), host: (r.host || '').trim(), d, isNew, problems, rot: d.takeover ? 'Takeover' : (d.slot_count || d.slot_ms ? `${d.slot_count || '?'} × ${d.slot_ms ? d.slot_ms / 1000 + ' s' : '?'}` : '')};
    if (!id) p.problems.push('site_id is empty');
    if (id && seen.has(id)) p.problems.push(`same site_id as row ${seen.get(id)}`); else if (id) seen.set(id, p.row);
    if (isNew && !p.host) p.problems.push('host is empty');
    p.action = p.problems.length ? 'error' : isNew ? 'create' : update ? 'update' : 'skip';
    return p;
  });
  render();
  // ask the gateway to validate new sites without creating them
  for (const p of plan.filter(p => p.action === 'create')) {
    const r = await fetch('/api/cameras?dry_run=1', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(p.d)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { p.action = 'error'; p.problems.push(j.error || r.statusText); }
    render();
  }
}

function statusCell(p) {
  if (p.done) return p.done;
  if (p.action === 'error') return `<span class="bad">${esc(p.problems.join('; '))}</span>`;
  if (p.action === 'create') return '<span class="ok">New site</span>';
  if (p.action === 'update') return '<span class="warn">Exists: will be updated</span>';
  return '<span class="muted">Exists: skipped</span>';
}
function render() {
  $('prev').hidden = false;
  $('rows').innerHTML = plan.map(p => `<tr><td>${p.row}</td><td><b>${esc(p.id)}</b></td><td>${esc(p.name)}</td>
    <td>${esc(p.host)}</td><td>${esc(p.rot)}</td><td class="st">${statusCell(p)}</td></tr>`).join('');
  const n = a => plan.filter(p => p.action === a).length;
  $('summary').textContent = `${plan.length} row(s): ${n('create')} new, ${n('update')} to update, ${n('skip')} skipped, ${n('error')} with problems.`;
  $('go').disabled = !(n('create') + n('update')) || plan.some(p => p.busy);
  $('go').textContent = `Import ${n('create') + n('update')} site(s)`;
}
function show(t, cls) { $('prev').hidden = false; $('rows').innerHTML = ''; $('summary').innerHTML = `<span class="${cls}">${esc(t)}</span>`; $('go').disabled = true; }

async function load(file) {
  if (!file) return;
  $('drop').querySelector('b').textContent = file.name;
  preview(await file.text());
}
$('file').onchange = e => load(e.target.files[0]);
$('update').onchange = () => { const f = $('file').files[0]; if (f) load(f); };
const drop = $('drop');
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); load(e.dataTransfer.files[0]); };

$('go').onclick = async () => {
  const todo = plan.filter(p => p.action === 'create' || p.action === 'update');
  $('go').disabled = true; let ok = 0, bad = 0;
  for (const [i, p] of todo.entries()) {
    $('msg').textContent = `Importing ${i + 1} of ${todo.length}…`;
    const url = p.action === 'create' ? '/api/cameras' : '/api/cameras/' + encodeURIComponent(p.id);
    const r = await fetch(url, {method: p.action === 'create' ? 'POST' : 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(p.d)});
    const j = await r.json().catch(() => ({}));
    if (r.ok) { ok++; p.done = `<span class="ok">${p.action === 'create' ? 'Added' : 'Updated'}</span> · <a href="/roi?site=${encodeURIComponent(j.id)}">Set region</a> · <a href="/site?site=${encodeURIComponent(j.id)}">Settings</a>`; }
    else { bad++; p.done = `<span class="bad">Failed: ${esc(j.error || r.statusText)}</span>`; }
    p.action = 'done'; render();
  }
  $('msg').innerHTML = `${ok} imported${bad ? `, <span class="bad">${bad} failed</span>` : ''}. New sites start straight away; set each one's region next. <a href="/sites">Back to Sites</a>`;
  $('go').disabled = true;
};
</script></body></html>'''


HEALTH_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gateway health</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32;--warn:#a15c00;--panel:#ececea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f;--panel:#22221f;--warn:#e0a040}}
*{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px} h1{font-size:20px;margin:0} h2{font-size:17px;margin:0} a{color:inherit}
nav{display:flex;gap:16px;margin-bottom:14px} nav a{color:var(--muted);text-decoration:none} nav a.on{color:var(--fg);font-weight:600}
.muted{color:var(--muted)} .bad{color:var(--bad)} .ok{color:var(--ok)} .warn{color:var(--warn)}
.gw{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
.gw.down{border-color:var(--bad)} .top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:12px 0}
.tile{background:var(--panel);border-radius:8px;padding:9px 11px} .tile .k{font-size:12px;color:var(--muted)} .tile .v{font-size:18px;font-weight:600}
.tile .s{font-size:12px;color:var(--muted)}
ul.w{margin:6px 0 0;padding-left:18px} ul.w li{color:var(--warn)} ul.w li.bad{color:var(--bad)}
.wrap{overflow-x:auto} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top;white-space:nowrap}
td.wr{white-space:normal}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites">Sites</a><a href="/health" class="on">Health</a></nav>
<h1>Gateway health</h1><p class="muted" id="sub" style="margin:2px 0 0">Loading&hellip;</p>
<div id="gws"></div>
</main><script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const age = s => s == null ? '—' : s < 90 ? Math.round(s) + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : s < 172800 ? (s / 3600).toFixed(1) + ' h' : (s / 86400).toFixed(1) + ' days';
const gb = b => b == null ? '—' : b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + ' GB' : (b / (1 << 20)).toFixed(0) + ' MB';
const tile = (k, v, s, cls) => `<div class="tile"><div class="k">${k}</div><div class="v ${cls || ''}">${v}</div><div class="s">${s || ''}</div></div>`;
function gateway(g) {
  const h = g.health || {}, q = g.queue || {}, u = g.uploads || {}, d = g.disk || {};
  const status = g.online ? '<span class="ok">Online</span>' : '<span class="bad">Offline</span>';
  const cams = g.cameras.map(c => `<tr><td><a href="/?site=${encodeURIComponent(c.site_id)}">${esc(c.site_name || c.site_id)}</a></td>
    <td class="${!g.online ? 'muted' : c.connected ? 'ok' : 'bad'}">${!g.online ? 'unknown' : c.connected ? 'connected' : 'not connected'}</td>
    <td>${c.uptime_pct != null ? c.uptime_pct + '%' : '—'}</td><td>${c.reconnects ?? '—'}</td>
    <td>${c.fps != null ? c.fps.toFixed(1) : '—'}</td>
    <td>${c.coverage == null ? '—' : c.coverage_data_min != null && c.coverage_data_min < 60 ? '<span class="muted">warming up</span>' : Math.round(c.coverage * 100) + '%'}</td>
    <td class="wr ${c.warnings.length ? 'warn' : 'muted'}">${esc(c.warnings.join(', ')) || 'OK'}</td></tr>`).join('');
  const qcls = (q.oldest_age_s || 0) > 600 ? 'warn' : '';
  const dcls = d.total_bytes && d.free_bytes < Math.min(d.total_bytes * 0.1, 2 * (1 << 30)) ? 'bad' : '';
  return `<div class="gw ${g.online ? '' : 'down'}"><div class="top"><h2>${esc(g.gateway_id)} · ${status}</h2>
    <span class="muted">last heartbeat ${age(g.last_heartbeat_age_s)} ago${g.last_heartbeat_at ? ' (' + esc(g.last_heartbeat_at) + ')' : ''}</span></div>
    ${g.warnings.length ? '<ul class="w">' + g.warnings.map(w => `<li class="${/offline|low disk/.test(w) ? 'bad' : ''}">${esc(w)}</li>`).join('') + '</ul>'
                        : '<p class="ok" style="margin:6px 0 0">No problems.</p>'}
    <div class="tiles">
      ${tile('Uptime', age(h.uptime_s), 'version ' + esc(h.app_version || '?'))}
      ${tile('Cameras', `${h.cameras_connected ?? '?'} / ${h.cameras ?? '?'}`, 'connected', h.cameras_connected < h.cameras ? 'warn' : '')}
      ${tile('Waiting to upload', q.records ?? '—', q.records ? `oldest ${age(q.oldest_age_s)} · ${gb(q.bytes)}` : 'queue empty', qcls)}
      ${tile('Uploaded', u.sent ?? '—', g.uploads_last_ok ? 'last ' + esc(g.uploads_last_ok) : 'since start')}
      ${tile('Upload problems', (u.retries || 0) + (u.rejected || 0), u.fail_streak ? esc(u.last_error) : (g.uploads_last_fail ? 'last ' + esc(g.uploads_last_fail) : 'none'), u.fail_streak ? 'bad' : '')}
      ${tile('Disk free', gb(d.free_bytes), d.total_bytes ? 'of ' + gb(d.total_bytes) : '', dcls)}
      ${tile('Clock', h.ntp_synced === false ? 'not synced' : h.ntp_synced ? 'NTP synced' : '—', '', h.ntp_synced === false ? 'warn' : '')}
    </div>
    <div class="wrap"><table><thead><tr><th>Site</th><th>Video</th><th>Video uptime</th><th>Reconnects</th><th>FPS</th><th>Coverage</th><th>Problems</th></tr></thead>
    <tbody>${cams || '<tr><td colspan="7" class="muted">No sites</td></tr>'}</tbody></table></div>
    <p class="muted" style="font-size:12px;margin:8px 0 0">${g.online ? '' : 'The gateway is offline, so these are its last reported figures. '}Video uptime and reconnects count from when each camera last started. Figures come from the gateway's heartbeat every ${h.heartbeat_s || '?'} s.</p></div>`;
}
async function load() {
  try {
    const r = await fetch('/health.json'); const gws = await r.json();
    $sub = document.getElementById('sub');
    const down = gws.filter(g => !g.online).length, warn = gws.filter(g => g.online && g.warnings.length).length;
    $sub.innerHTML = gws.length ? `${gws.length} gateway(s)` + (down ? ` · <span class="bad">${down} offline</span>` : '') + (warn ? ` · <span class="warn">${warn} with warnings</span>` : '') + ' · updates every 15 s'
                                : 'No heartbeats received yet.';
    document.getElementById('gws').innerHTML = gws.map(gateway).join('');
  } catch (e) { document.getElementById('sub').innerHTML = '<span class="bad">' + esc(e.message || e) + '</span>'; }
}
let $sub; load(); setInterval(load, 15000);
</script></body></html>'''


REFRESH_JS = r'''<script>
(() => {
  const sel = document.getElementById('every'), next = document.getElementById('next');
  const fromUrl = new URLSearchParams(location.search).get('refresh');
  let saved = null;
  try { saved = localStorage.getItem('pop-review-refresh'); } catch (e) {}
  let every = Number(fromUrl ?? saved ?? 30);
  if (![...sel.options].some(o => Number(o.value) === every)) every = 30;
  sel.value = String(every);
  let due = every ? Date.now() + every * 1000 : 0;
  const reload = () => { try { sessionStorage.setItem('pop-review-y', String(scrollY)); } catch (e) {} location.reload(); };
  try { const y = sessionStorage.getItem('pop-review-y'); if (y) { scrollTo(0, Number(y)); sessionStorage.removeItem('pop-review-y'); } } catch (e) {}
  const fmt = s => s >= 60 ? Math.floor(s / 60) + ' min ' + String(s % 60).padStart(2, '0') + ' s' : s + ' s';
  function tick() {
    if (!every) { next.textContent = 'Auto refresh off'; return; }
    if (document.hidden) { next.textContent = 'Paused (tab hidden)'; return; }
    const left = Math.max(0, Math.ceil((due - Date.now()) / 1000));
    if (left <= 0) { reload(); return; }
    next.textContent = 'Next in ' + fmt(left);
  }
  sel.addEventListener('change', () => {
    every = Number(sel.value);
    try { localStorage.setItem('pop-review-refresh', String(every)); } catch (e) {}
    due = every ? Date.now() + every * 1000 : 0;
    tick();
  });
  document.getElementById('now').addEventListener('click', reload);
  document.addEventListener('visibilitychange', tick);
  setInterval(tick, 1000); tick();
})();
</script>'''


def gateway_cameras():
    """Live camera status from the gateway (None if it can't be reached)."""
    url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + "/api/cameras"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ARGS.api_key}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def disp(r):
    """What people see for a site: its display name, else its site ID."""
    return r.get("site_name") or r.get("camera") or r.get("site_id") or ""


def local_zone():
    """The time zone from TZ (as set in .env), else the container's, else UTC."""
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


LOCAL_TZ = local_zone()


def local_time(iso, with_zone=False):
    """'2026-10-05T06:41:10.200Z' -> '2026-10-05 19:41:10' in the local zone."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t = t.astimezone(LOCAL_TZ)
        return t.strftime("%Y-%m-%d %H:%M:%S" + (" %Z" if with_zone else ""))
    except ValueError:
        return str(iso)[:19]


def zone_label():
    return datetime.now(LOCAL_TZ).strftime("%Z") or "local"


def esc(v):
    return html.escape(str(v if v is not None else ""))


# ---- gateway health and the quiet-gateway watchdog -------------------------

GATEWAYS = {}          # gateway_id -> {"hb": last heartbeat, "at": receive time (epoch s), "quiet": since or None}
GW_LOCK = threading.Lock()


def note_heartbeat(hb, received=None):
    """Remember a gateway's latest heartbeat; clears its offline alert."""
    gid = hb.get("gateway_id") or "gateway"
    now = received or time.time()
    with GW_LOCK:
        g = GATEWAYS.setdefault(gid, {"hb": None, "at": 0, "quiet": None})
        was_quiet = g["quiet"]
        g["hb"], g["at"], g["quiet"] = hb, now, None
    if was_quiet and received is None:
        gateway_alert(gid, "cleared", was_quiet,
                      f"heartbeats resumed after {fmt_age(now - was_quiet)} without contact")


def quiet_after(g):
    """Seconds without a heartbeat before a gateway counts as offline."""
    hs = ((g.get("hb") or {}).get("health") or {}).get("heartbeat_s") or 60
    return max(180, 3 * hs)


def fmt_age(sec):
    sec = int(max(0, sec))
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    if sec < 172800:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 86400:.1f} days"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def post_webhook(url, payload):
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"webhook failed: {e}", flush=True)


def gateway_alert(gid, state, since, detail):
    """Record a gateway_offline alert and tell Teams/Slack (receiver's own webhooks)."""
    now = time.time()
    rec = {"event_id": str(uuid.uuid4()), "type": "fault", "fault": "gateway_offline", "state": state,
           "severity": "critical", "site_id": "", "site_name": f"Gateway {gid}", "gateway_id": gid,
           "since": iso(since), "at": iso(now), "detail": detail, "_received": {"endpoint": "watchdog"}}
    with LOCK, open(os.path.join(ARGS.out, "records.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"ALERT gateway {gid} gateway_offline {state}: {detail}", flush=True)
    when = local_time(rec["at"], True)
    title = f"{'Recovered: ' if state == 'cleared' else 'Critical: '}gateway {gid} {'back online' if state == 'cleared' else 'offline'}"
    teams = os.environ.get("POP_TEAMS_WEBHOOK", "").strip()
    slack = os.environ.get("POP_SLACK_WEBHOOK", "").strip()
    if teams:
        post_webhook(teams, {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                        "body": [{"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title, "wrap": True},
                                 {"type": "FactSet", "facts": [{"title": "Gateway", "value": gid}, {"title": "State", "value": state},
                                                               {"title": "Time", "value": when}, {"title": "Detail", "value": detail}]}]}}]})
    if slack:
        icon = ":white_check_mark:" if state == "cleared" else ":red_circle:"
        text = f"{icon} *{title}*\n{detail}\n_{when}_"
        post_webhook(slack, {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]})


def watchdog():
    while True:
        time.sleep(15)
        now = time.time()
        fire = []
        with GW_LOCK:
            for gid, g in GATEWAYS.items():
                if g["quiet"] is None and g["at"] and now - g["at"] > quiet_after(g):
                    g["quiet"] = g["at"]
                    fire.append((gid, g["at"]))
        for gid, last in fire:
            gateway_alert(gid, "raised", last, f"no heartbeat for {fmt_age(now - last)}: the gateway, its network "
                          f"or its connection to this server is down. PoP records are kept on the gateway until it reconnects.")


def offline_gateways():
    with GW_LOCK:
        return [(gid, g["at"]) for gid, g in GATEWAYS.items() if g["quiet"]]


def offline_banner():
    off = offline_gateways()
    if not off:
        return ""
    items = ", ".join(f"<b>{esc(gid)}</b> (last heard {esc(fmt_age(time.time() - at))} ago)" for gid, at in off)
    return (f"<div style='border:1px solid var(--bad);color:var(--bad);border-radius:8px;padding:10px 12px;margin:10px 0'>"
            f"Gateway offline: {items}. <a href='/health'>Health</a></div>")


def health_data():
    """Latest heartbeat per gateway, with derived warnings, for the Health page."""
    now = time.time()
    out = []
    with GW_LOCK:
        items = [(gid, dict(g)) for gid, g in GATEWAYS.items()]
    for gid, g in sorted(items):
        hb = g["hb"] or {}
        h = hb.get("health") or {}
        q, u, d = h.get("queue") or {}, h.get("uploads") or {}, h.get("disk") or {}
        warn = []
        age = now - g["at"] if g["at"] else None
        if g["quiet"]:
            warn.append(f"offline: no heartbeat for {fmt_age(age)}")
        if q.get("oldest_age_s", 0) > 600:
            warn.append(f"uploads behind: oldest waiting record is {fmt_age(q['oldest_age_s'])} old")
        if u.get("fail_streak"):
            warn.append(f"{u['fail_streak']} upload(s) failing in a row: {u.get('last_error', '')}")
        if q.get("dead"):
            warn.append(f"{q['dead']} record(s) rejected by the server (in queue/dead)")
        if q.get("max_bytes") and q.get("bytes", 0) > 0.8 * q["max_bytes"]:
            warn.append("queue above 80% of its size limit; oldest full frames will be dropped")
        if d.get("total_bytes") and d.get("free_bytes", 0) < min(0.1 * d["total_bytes"], 2 << 30):
            warn.append(f"low disk: {d['free_bytes'] / (1 << 30):.1f} GB free")
        if h and h.get("ntp_synced") is False:
            warn.append("clock not NTP-synced: PoP times may be off")
        cams = []
        for c in hb.get("cameras", []):
            cw = []
            if not c.get("connected"):
                cw.append("video not connected")
            if c.get("uptime_pct") is not None and c["uptime_pct"] < 95 and (h.get("uptime_s") or 0) > 3600:
                cw.append(f"video up {c['uptime_pct']}% of the time")
            if c.get("reconnects", 0) >= 5:
                cw.append(f"{c['reconnects']} reconnects")
            cams.append(dict(c, warnings=cw + c.get("active_faults", [])))
        out.append({"gateway_id": gid, "online": not g["quiet"], "last_heartbeat_age_s": age,
                    "last_heartbeat_at": local_time(iso(g["at"]), True) if g["at"] else "",
                    "health": h, "queue": q, "uploads": u, "disk": d, "warnings": warn, "cameras": cams,
                    "uploads_last_ok": local_time(u.get("last_ok_at"), True),
                    "uploads_last_fail": local_time(u.get("last_fail_at"), True)})
    return out


def connected_cell(c):
    if c.get("stale"):
        return "<td class='muted' title='the gateway is offline; this is its last report'>unknown</td>"
    return f"<td class='{'ok' if c.get('connected') else 'bad'}'>{'yes' if c.get('connected') else 'no'}</td>"


def coverage_cell(c):
    """Hourly coverage, or how far through its first hour of counting a site is."""
    mins = c.get("coverage_data_min")
    if "coverage" not in c:
        return ""
    if mins is not None and 0 <= mins < 60:
        return f"<span class='muted' title='coverage counts plays over a full hour'>warming up ({int(mins)} of 60 min)</span>"
    return esc(round(c["coverage"] * 100)) + "%"


def render_page(site):
    recs = tail_jsonl(os.path.join(ARGS.out, "records.jsonl"), 2000)
    if site:
        recs = [r for r in recs if r.get("site_id") == site]
    live = gateway_cameras()                     # live from the gateway when it's reachable
    if live is not None:                         # hide records from sites that no longer exist
        known = {c.get("id") or c.get("site_id") for c in live}
        recs = [r for r in recs if r.get("site_id") in known or r.get("fault") == "gateway_offline"]
    pops = [r for r in recs if r.get("type") == "creative_change"]
    if site:
        pops = pops[-60:][::-1]
    else:                                        # all sites: only the newest PoP from each
        newest = {}
        for r in pops:
            newest[r.get("site_id")] = r
        pops = sorted(newest.values(), key=lambda r: disp(r).lower())
    alerts = [r for r in recs if r.get("type") == "fault"][-50:][::-1]
    hb = {}
    if live is not None:
        for c in live:
            c = dict(c, site_name=c.get("name"))
            if c.get("expected_per_hour"):
                c["coverage"] = c.get("plays_last_hour", 0) / c["expected_per_hour"]
            hb[c.get("site_id")] = c
    else:                                        # otherwise the latest heartbeat
        last = tail_jsonl(os.path.join(ARGS.out, "heartbeats.jsonl"), 1)
        stale = bool(offline_gateways())
        for c in (last[0].get("cameras", []) if last else []):
            hb[c.get("site_id")] = dict(c, stale=stale)
    cams = "".join(
        f"<tr><td><a href='?site={urllib.parse.quote(k or '')}'>{esc(c.get('site_name') or c.get('camera') or k)}</a></td>"
        f"{connected_cell(c)}"
        f"<td>{esc(c.get('state'))}</td><td>{esc(c.get('plays_last_hour'))}</td>"
        f"<td>{coverage_cell(c)}</td>"
        f"<td>{esc(local_time(c.get('last_play_at')))}</td>"
        f"<td class='bad'>{esc(', '.join(c.get('active_faults', [])))}</td>"
        f"<td><a href='/roi?site={urllib.parse.quote(k or '')}'>Edit region</a> &middot; "
        f"<a href='/site?site={urllib.parse.quote(k or '')}'>Settings</a></td></tr>"
        for k, c in sorted(hb.items()) if not site or k == site) or "<tr><td colspan=8 class='muted'>No heartbeat yet</td></tr>"
    cards = []
    for r in pops:
        q = r.get("quality", {})
        img = (f"<div class='thumb'><img loading='lazy' src='/images/{esc(r['event_id'])}.roi.jpg' alt=''></div>"
               if "roi_sha256" in r else "<div class='thumb'><span>no image</span></div>")
        full = f" &middot; <a href='/images/{esc(r['event_id'])}.frame.jpg' target='_blank'>full frame</a>" if "frame_sha256" in r else ""
        flags = " [blank]" if r.get("is_blank") else ""
        flags += " [video]" if r.get("video_creative") else ""
        verdict = "" if q.get("usable", True) else f"<br><span class='bad'>{esc(q.get('reason'))}</span>"
        more = "" if site else f"<br><a href='?site={urllib.parse.quote(r.get('site_id') or '')}'>All PoP from this site</a>"
        cards.append(f"<div class='card'>{img}<div><b>{esc(disp(r))}</b> {esc(r.get('local_creative_id'))}{flags}<br>"
                     f"<span class='muted'>{esc(local_time(r.get('play_started_at'), True))} &middot; after "
                     f"{esc(round(r.get('previous_creative_ms', 0) / 1000, 1))} s{full}</span>{verdict}{more}</div></div>")
    rows = "".join(f"<tr><td>{esc(local_time(a.get('at')))}</td><td>{esc(disp(a))}</td><td>{esc(a.get('fault'))}</td>"
                   f"<td class='{'ok' if a.get('state') == 'cleared' else 'bad'}'>{esc(a.get('state'))}</td><td>{esc(a.get('detail'))}</td></tr>"
                   for a in alerts) or "<tr><td colspan=5 class='muted'>No alerts</td></tr>"
    site_label = disp(hb.get(site) or next((r for r in recs if r.get("site_id") == site), {"site_id": site}))
    sub = f"{esc(site_label)} &middot; <a href='/'>all sites</a>" if site else f"{len(hb)} site(s)"
    return PAGE.format(subtitle=sub, refresh_js=REFRESH_JS, zone=esc(zone_label()), banner=offline_banner(),
                       alerts_note=f"newest {len(alerts)}, scroll for more" if len(alerts) > 8 else "",
                       pops_title="Latest PoP images" if site else "Latest PoP from each site", cams=cams, pops="".join(cards) or "<p class='muted'>No PoP records yet</p>",
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
            base = url.split('/api')[0]
            hint = ("the gateway container is not running (it may be restarting after an error). "
                    "Check it with: docker compose ps   and   docker compose logs gateway --tail 30"
                    if "Name or service not known" in str(e) or "not known" in str(e)
                    else "check that the gateway is running and control_port is 8090")
            return self.reply(502, {"error": f"cannot reach the gateway at {base}: {hint} ({e})"})
        m = re.fullmatch(r"/api/cameras/([^/?]+)", self.path)
        if method == "PUT" and m and 200 <= code < 300:
            old_id = urllib.parse.unquote(m.group(1))
            try:
                cam = json.loads(body)
                new_id, new_name = cam.get("id") or cam.get("site_id"), cam.get("name")
            except ValueError:
                new_id = None
            if new_id and new_id != old_id:    # keep this site's history under its new ID
                def move(r):
                    if r.get("site_id") == old_id:
                        r["site_id"] = new_id
                        if new_name:
                            r["site_name"] = r["camera"] = new_name
                    return r
                rewrite_records(move)
                print(f"site ID {old_id} -> {new_id}: records moved", flush=True)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self.path.startswith("/api/"):
            return self.reply(404)
        if self.authorised():
            return self.proxy("PUT")

    def do_DELETE(self):
        if not self.path.startswith("/api/"):
            return self.reply(404)
        if self.authorised():
            return self.proxy("DELETE")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if not self.authorised():
            return
        if path.startswith("/api/"):
            return self.proxy("GET")
        if path == "/records/sites":
            return self.reply(200, sorted(recorded_sites().values(), key=lambda e: e["site_id"] or ""))
        if path == "/health.json":
            return self.reply(200, health_data())
        if path in ("/roi", "/sites", "/site", "/sites/import", "/health"):
            body = {"/roi": ROI_PAGE, "/sites": SITES_PAGE, "/site": SITE_PAGE, "/sites/import": IMPORT_PAGE,
                    "/health": HEALTH_PAGE}[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/sites/template.csv":
            body = IMPORT_TEMPLATE.encode("utf-8-sig")       # BOM so Excel reads UTF-8
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="pop-sites-template.csv"')
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
        if self.path.startswith("/api/"):          # review page -> gateway
            if self.authorised():
                self.proxy("POST")
            return
        if self.path == "/records/delete":         # review page: remove a site's stored records
            if not self.authorised():
                return
            try:
                sid = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))["site_id"]
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "site_id required"})
            live = gateway_cameras()
            if live is not None and sid in {c.get("id") for c in live}:
                return self.reply(409, {"error": "that site still exists; remove it on the Sites page first"})
            n = rewrite_records(lambda r: None if r.get("site_id") == sid else r)
            print(f"deleted {n} record(s) for site {sid}", flush=True)
            return self.reply(200, {"deleted": n})
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
            try:
                note_heartbeat(json.loads(body))
            except ValueError:
                pass
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
    for hb in tail_jsonl(os.path.join(ARGS.out, "heartbeats.jsonl"), 500):     # last known state per gateway
        try:
            t = datetime.fromisoformat(hb.get("sent_at", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        note_heartbeat(hb, received=t)
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"receiver on {ARGS.bind}:{ARGS.port}, storing in {ARGS.out}", flush=True)
    ThreadingHTTPServer((ARGS.bind, ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
