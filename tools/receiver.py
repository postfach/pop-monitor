#!/usr/bin/env python3
"""Reference receiver for the gateway's backend API (testing and integration).

Implements POST /v1/pop, /v1/alerts (multipart: metadata, frame, roi) and
POST /v1/heartbeat (JSON). Checks the bearer token, the HMAC signature and
idempotency, then stores everything under --out:

  out/pop.db                PoP records, alerts and heartbeats (SQLite, tools/store.py)
  out/images/<event_id>.frame.jpg / .roi.jpg
                            photos, removed after POP_IMAGE_DAYS (3) days

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
import shutil
import zipfile
import io
from datetime import datetime, timezone

import store
import backup as bk
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None
STORE = None          # store.Store: records, alerts and heartbeats (set in main)
LOCK = threading.Lock()


def parse_multipart(ctype, body):
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = part.get_payload(decode=True)
    return parts


def load_resets():
    try:
        with open(os.path.join(ARGS.out, "site-resets.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def note_reset(site_id, ts):
    """Remember when a site's creatives were reset with its history deleted, so
    plays from before it (still queued on the gateway) aren't stored under IDs
    that are now being reused."""
    with LOCK:
        r = load_resets()
        r[site_id] = ts
        tmp = os.path.join(ARGS.out, "site-resets.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(r, f)
        os.replace(tmp, os.path.join(ARGS.out, "site-resets.json"))


def recorded_sites():
    """Every site ID that has records here, with counts and the newest time."""
    sites = {}
    for e in STORE.sites():
        e["last"] = iso(e["last_t"]) if e.get("last_t") else ""
        e["last_local"] = local_time(e["last"], True)
        sites[e["site_id"]] = e
    return sites


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review</title><style>
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
<nav style="display:flex;gap:16px;margin-bottom:14px"><a href="/" style="font-weight:600;text-decoration:none">Review</a><a href="/sites" style="color:var(--muted);text-decoration:none">Sites</a><a href="/bookings" style="color:var(--muted);text-decoration:none">Bookings</a><a href="/health" style="color:var(--muted);text-decoration:none">Health</a></nav>
<div class="toolbar"><div><h1>Review</h1><p class="muted" style="margin:2px 0 0">{subtitle}</p></div>
<div class="refresh"><label for="every" class="muted">Refresh</label>
<select id="every"><option value="0">Off</option><option value="5">Every 5 s</option><option value="10">Every 10 s</option>
<option value="15">Every 15 s</option><option value="30">Every 30 s</option><option value="60">Every minute</option>
<option value="120">Every 2 min</option><option value="300">Every 5 min</option><option value="600">Every 10 min</option>
<option value="900">Every 15 min</option><option value="1800">Every 30 min</option><option value="3600">Every hour</option></select>
<button id="now" type="button">Refresh now</button><span id="next" class="muted"></span></div></div>
{banner}<h2>{pops_title}</h2><div class="grid">{pops}</div>
<h2>Sites</h2><div class="wrap"><table><tr><th>Site</th><th>Connected</th><th>State</th><th>Plays / hour</th><th>Coverage</th><th>Last play ({zone})</th><th>Active faults</th><th></th></tr>{cams}</table></div>
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
.pill{font-size:11px;border:1px solid var(--accent,#d4237a);border-radius:999px;padding:1px 7px;color:var(--accent,#d4237a);text-decoration:none;white-space:nowrap} .muted{color:var(--muted)} .bad{color:var(--bad)} .ok{color:var(--ok)}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 12px}
.btn{display:inline-block;border:1px solid var(--line);background:transparent;color:var(--fg);padding:7px 12px;border-radius:8px;cursor:pointer;font:inherit;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.wrap{overflow-x:auto} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top;white-space:nowrap}
td.actions a{margin-right:12px} .note{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-top:16px}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites" class="on">Sites</a><a href="/bookings">Bookings</a><a href="/health">Health</a></nav>
<div class="head"><div><h1>Sites</h1><p class="muted" id="sub" style="margin:2px 0 0">Loading&hellip;</p></div>
<div style="display:flex;gap:8px"><a class="btn" href="/sites/import">Import from CSV</a><a class="btn primary" href="/site">Add site</a></div></div>
<div class="wrap"><table><thead><tr><th>Site</th><th>Model</th><th>Camera</th><th>Video</th><th>Rotation</th><th>Plays / hour</th><th>Faults</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="8" class="muted">Loading&hellip;</td></tr></tbody></table></div>
<p class="note muted" id="note" hidden></p>
<section id="dsec" hidden><div class="head" style="margin-top:28px"><div><h2 style="font-size:16px;margin:0">Screens in Doohly</h2>
<p class="muted" id="dsub" style="margin:2px 0 0"></p></div>
<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><input type="search" id="dq" placeholder="Filter screens" style="padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit">
<label class="muted"><input type="checkbox" id="dall"> show active ones too</label></div></div>
<div class="wrap"><table><thead><tr><th>Active</th><th>Screen</th><th>Address</th><th>Screen size</th><th>Loop</th><th>Player</th></tr></thead>
<tbody id="drows"></tbody></table></div></section>
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
    const [r, s, ro] = await Promise.all([fetch('/api/cameras'), fetch('/api/settings'), fetch('/sites/rotation').catch(() => null)]);
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const cams = await r.json(), st = s.ok ? await s.json() : {}, rots = ro && ro.ok ? await ro.json() : {};
    document.getElementById('sub').textContent = `${cams.length} site(s) on gateway ${st.gateway_id || ''}`;
    document.getElementById('rows').innerHTML = cams.map(c => {
      const q = encodeURIComponent(c.id);
      const exp = c.expected_per_hour ? ` <span class="muted">of ${c.expected_per_hour}</span>` : '';
      return `<tr><td><b>${esc(c.name)}</b>${c.setup ? ` <a class="pill" href="/roi?site=${q}" title="Being set up: its plays aren't counted until it goes live (Region page)">Setup</a>` : ''}${c.name !== c.id ? `<br><span class="muted">${esc(c.id)}</span>` : ''}</td>
        <td>${esc(c.model)}</td><td>${hostText(c)}</td>
        <td class="${c.connected ? 'ok' : 'bad'}">${c.connected ? 'connected' : 'not connected'}</td>
        <td>${rotation(c)}${rots[c.id] && rots[c.id].auto ? ' <span class="muted" title="kept in step with the Doohly player\'s loop">· Doohly</span>' : ''}</td><td>${c.plays_last_hour}${exp}</td>
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
let players = [];
async function loadPlayers() {
  const r = await fetch('/doohly/players').catch(() => null);
  if (!r || !r.ok) return;
  const d = await r.json(); players = d.players || [];
  document.getElementById('dsec').hidden = !players.length;
  const n = players.filter(p => !p.site_id).length;
  document.getElementById('dsub').textContent = `${players.length} player(s) in Doohly, ${players.length - n} set up as sites, ${n} not yet` + (d.at ? ` · read ${d.at}` : '') +
    '. Tick a screen to set it up: its name, Vault screen and rotation are filled in from Doohly; you add the camera, then its region.';
  drawPlayers();
}
function drawPlayers() {
  const q = document.getElementById('dq').value.trim().toLowerCase(), all = document.getElementById('dall').checked;
  const rows = players.filter(p => (all || !p.site_id) && (!q || (p.name + ' ' + p.address).toLowerCase().includes(q)));
  document.getElementById('drows').innerHTML = rows.map(p => `<tr>
    <td>${p.site_id ? `<input type="checkbox" checked disabled title="Set up as ${esc(p.site_name || p.site_id)}. To stop monitoring it, delete the site from its Settings.">`
      : `<input type="checkbox" data-player="${esc(p.id)}" title="Set up this screen as a site">`}</td>
    <td><b>${esc(p.screen)}</b>${p.site_id ? `<br><a class="muted" href="/site?site=${encodeURIComponent(p.site_id)}">${esc(p.site_name || p.site_id)}</a>` : `<br><span class="muted">${esc(p.name)}</span>`}</td>
    <td>${esc(p.address)}</td><td>${p.frame ? `${p.frame.width} × ${p.frame.height}` : '<span class="muted">?</span>'}</td>
    <td>${p.loop ? `${p.loop.slot_count} × ${p.loop.slot_s} s` : '<span class="muted">?</span>'}</td>
    <td class="${p.online ? 'ok' : 'bad'}">${p.online ? 'online' : 'offline'}</td></tr>`).join('') ||
    '<tr><td colspan="6" class="muted">No screens match.</td></tr>';
}
document.getElementById('dq').oninput = drawPlayers;
document.getElementById('dall').onchange = drawPlayers;
document.getElementById('drows').addEventListener('change', ev => {
  const c = ev.target.closest('input[data-player]'); if (!c || !c.checked) return;
  location.href = '/site?doohly=' + encodeURIComponent(c.dataset.player);
});
load(); setInterval(load, 15000); loadPlayers();
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
dialog{border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--fg);max-width:520px;width:calc(100% - 32px);padding:18px 20px}
dialog::backdrop{background:rgba(0,0,0,.45)} dialog h2{font-size:17px;margin:0 0 8px} dialog ul{margin:6px 0 10px;padding-left:18px}
dialog code{background:var(--panel);padding:1px 5px;border-radius:4px}
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
<nav><a href="/">Review</a><a href="/sites" class="on">Sites</a><a href="/bookings">Bookings</a><a href="/health">Health</a></nav>
<h1 id="title">Add site</h1><p class="muted" id="sub">New camera</p>
<form id="f" autocomplete="off">
<fieldset><legend>Site</legend><div class="grid">
  <label class="f"><span>Site ID (required, unique)</span><input type="text" name="site_id" required placeholder="AKL-BRITOMART-01"></label>
  <label class="f"><span>Display name (optional)</span><input type="text" name="display_name" placeholder="Britomart north face"></label>
  <label class="f"><span>Camera model</span><input type="text" name="model" list="models" placeholder="M5525-E"></label>
  <label class="f"><span>Vault screen name (for bookings)</span><input type="text" name="vault_screen" list="vscreens" placeholder="as in Vault, e.g. Parnell"></label>
</div>
<datalist id="vscreens"></datalist>
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
<div class="grid">
  <label class="f"><span>Video quality (for a weak link)</span><select name="video_quality">
    <option value="">Standard: 1280&times;720 at 10 fps, full-size photos</option>
    <option value="reduced">Reduced: 1280&times;720 at 5 fps, more compressed, 1920&times;1080 photos</option>
    <option value="low">Low: 800&times;450 at 5 fps, 1280&times;720 photos</option>
    <option value="minimal">Minimal: 640&times;360 at 5 fps, 1280&times;720 photos, one per play</option>
  </select></label>
</div>
<p class="hint">For a camera on a poor wireless or low-bandwidth link that keeps dropping out. The gateway watches the video all the time, so a lighter stream is what helps most; detection still works at the lower settings (it looks at 5 frames a second, and the board is a small part of the view). The PoP photos get smaller too. Try one step down at a time. Saving restarts this camera. <span id="qnow"></span></p>
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
  <label class="f"><span>Video stream settings (replace the quality's)</span><input type="text" name="stream_params" placeholder="e.g. resolution=1920x1080&amp;fps=5&amp;videocodec=h264"></label>
</div><p class="hint">Only for unusual setups. These replace the host, ports and transport above. <span id="effective"></span></p></details>
</fieldset>

<fieldset><legend>Rotation</legend>
<label class="check"><input type="checkbox" name="takeover"> Takeover (one creative only, so no frozen or loop alerts)</label>
<div class="grid">
  <label class="f"><span>Slots in the rotation</span><input type="number" name="slot_count" min="1" max="200" placeholder="6"></label>
  <label class="f"><span>Slot length (seconds)</span><input type="number" name="slot_s" min="1" max="3600" step="0.5" placeholder="10"></label>
</div><p class="hint" id="rothint">Leave blank if unknown. Setting these makes detection and fault alerts fit the loop.</p>
<label class="check" id="rotauto" hidden><input type="checkbox" name="rot_doohly"> Keep in step with Doohly's loop <span class="muted" id="rotdoohly"></span></label>
<p class="hint" id="rotnote" hidden></p>
</fieldset>

<fieldset><legend>Screen</legend>
<div class="grid">
  <label class="f"><span>Screen width (pixels)</span><input type="number" name="board_width" min="16" max="20000" placeholder="576"></label>
  <label class="f"><span>Screen height (pixels)</span><input type="number" name="board_height" min="16" max="20000" placeholder="396"></label>
</div><p class="hint" id="boardhint">The screen's resolution. The straightened PoP image is given the screen's shape, so a board seen at an angle isn't squeezed (used for artwork matching, scores and the photos on these pages). Blank = the shape the camera sees. Filled in from Doohly when the screen has a Doohly player.</p>
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
  <button type="button" class="danger" id="reset" hidden>Reset creatives&hellip;</button>
  <button type="button" class="danger" id="del" hidden>Delete site</button>
</div>
<p id="msg"></p><div id="testout"></div>
</form>
<dialog id="resetdlg"><form method="dialog" id="resetf">
<h2>Reset creatives for <span id="rsname"></span>?</h2>
<p>The gateway forgets every creative it has seen at this site and starts learning them again from the next play.</p>
<ul><li>Detection alerts for this site (loop changed, missing creative, frozen, low coverage) are cleared and checked again from now.</li>
<li>New creatives get new IDs after the highest one used, so they can't be mixed up with earlier records.</li></ul>
<label class="check"><input type="checkbox" id="rshist"> Also delete this site's stored PoP records, images and alerts, its creative links on the Bookings page, and start IDs again at c-0001</label>
<label class="f"><span>Type the site ID <code id="rsid"></code> to confirm</span><input type="text" id="rsconfirm" autocomplete="off" spellcheck="false"></label>
<p id="rsmsg" class="muted" style="min-height:20px;margin:4px 0"></p>
<div class="bar"><button type="button" class="danger" id="rsgo" disabled>Reset creatives</button><button value="cancel" formnovalidate>Cancel</button></div>
</form></dialog>
</main><script>
const params = new URLSearchParams(location.search), editing = params.get('site') || params.get('camera');
const copying = !editing && params.get('copy');
const fromDoohly = !editing && !copying && params.get('doohly');
const f = document.getElementById('f'), $ = id => document.getElementById(id);
const msg = (t, cls) => { $('msg').textContent = t; $('msg').className = cls || 'muted'; };
let current = null;

function setVal(n, v) { const el = f.elements[n]; if (!el) return; if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? ''; }
let rot = null;                          // this site's Doohly rotation state (/sites/rotation)
function sync() {
  const take = f.elements.takeover.checked;
  const follow = rot && rot.doohly && f.elements.rot_doohly.checked && !take;
  f.elements.slot_count.disabled = f.elements.slot_s.disabled = take || follow;
  const all = f.elements.allday.checked;
  f.elements.from.disabled = f.elements.to.disabled = all;
  f.elements.camera_tls_insecure.disabled = !f.elements.use_https.checked;
}
f.addEventListener('change', sync);

async function init() {
  fetch('/bookings/screens').then(r => r.ok ? r.json() : []).then(list => {   // screen names from the latest Vault pull
    $('vscreens').innerHTML = list.map(n => `<option value="${n.replace(/"/g, '&quot;')}">`).join('');
  }).catch(() => {});
  const s = await fetch('/api/settings').then(r => r.ok ? r.json() : {}).catch(() => ({}));
  if (s.default_coverage_min_pct !== undefined) {
    f.elements.coverage_min_pct.placeholder = s.default_coverage_min_pct;
    f.elements.coverage_window_min.placeholder = s.default_coverage_window_min;
    $('covhint').textContent = `Coverage alert below (%, 0 = off, blank = ${s.default_coverage_min_pct})`;
  }
  if (s.default_username) $('userhint').textContent = `Camera user (blank = default "${s.default_username}")`;
  if (!s.default_password_set) $('pwhint').textContent = 'Password (no default set: enter one)';
  if (fromDoohly) {                       // set up a screen from Doohly: everything but the camera
    const d = await fetch('/doohly/players').then(r => r.ok ? r.json() : {}).catch(() => ({}));
    const p = (d.players || []).find(x => x.id === fromDoohly);
    if (p) {
      const g = p.suggested;
      for (const k of ['site_id', 'display_name', 'vault_screen']) setVal(k, g[k]);
      if (g.slot_count) setVal('slot_count', g.slot_count);
      if (g.slot_s) setVal('slot_s', g.slot_s);
      if (g.board_width) { setVal('board_width', g.board_width); setVal('board_height', g.board_height); }
      $('title').textContent = 'Add ' + p.screen;
      $('sub').innerHTML = `From Doohly player <b>${p.name.replace(/</g, '&lt;')}</b>${p.address ? ' · ' + p.address.replace(/</g, '&lt;') : ''}` +
        (p.loop ? ` · loop ${p.loop.slot_count} × ${p.loop.slot_s} s` : '') + '. Enter the camera’s address and login, save, then set its region.';
      if (p.site_id) msg(`This screen is already set up as ${p.site_name || p.site_id}.`, 'bad');
      sync(); f.elements.host.focus(); return;
    }
    msg('That Doohly player was not found (the list is refreshed every 30 minutes).', 'bad');
  }
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
  for (const k of copy ? ['model','username','video_quality'] : ['site_id','display_name','model','host','username','rtsp_url_override','vapix_url_override','vault_screen','video_quality','stream_params'])
    setVal(k, current[k]);
  if (current.coverage_custom) { setVal('coverage_min_pct', current.coverage_min_pct); setVal('coverage_window_min', current.coverage_window_min); }
  setVal('http_port', current.http_port || ''); setVal('rtsp_port', current.rtsp_port || '');
  f.querySelector(`input[name=rtsp_transport][value=${current.rtsp_transport}]`).checked = true;
  for (const k of ['use_https','camera_tls_insecure','takeover','ptz_check']) setVal(k, current[k]);
  setVal('slot_count', current.slot_count || ''); setVal('slot_s', current.slot_ms ? current.slot_ms / 1000 : '');
  setVal('board_width', current.board_width || ''); setVal('board_height', current.board_height || '');
  const hrs = current.operating_hours || '00:00-24:00', all = hrs === '00:00-24:00' || hrs.split('-')[0] === hrs.split('-')[1];
  setVal('allday', all); if (!all) { setVal('from', hrs.split('-')[0]); setVal('to', hrs.split('-')[1]); }
  if (copy) { sync(); f.elements.site_id.focus(); return; }
  if (current.password_set) { $('pwhint').textContent = 'Password (blank = keep this site’s password)'; $('clearpw').hidden = false; }
  if (current.dayparts) $('rothint').textContent = `This site also has ${current.dayparts} time-of-day profile(s) from the config file; these fields set the default profile.`;
  $('effective').textContent = current.snapshot_url ? 'Snapshots currently use ' + current.snapshot_url + '.' : '';
  $('qnow').textContent = current.stream_effective ? `Now: video ${current.stream_effective.replace(/&/g, ', ')}; photos ${current.photo_resolution || 'the camera\'s full size'}.` : '';
  loadRotation(c.id);
  $('save').textContent = 'Save changes'; $('test').hidden = $('region').hidden = $('del').hidden = $('dup').hidden = $('reset').hidden = false;
  sync();
}

async function loadRotation(id) {
  const all = await fetch('/sites/rotation').then(r => r.ok ? r.json() : {}).catch(() => ({}));
  rot = all[id] || null;
  const fr = rot && rot.frame;                 // Doohly's resolution for this screen
  if (fr && fr.width) {
    const same = +f.elements.board_width.value === fr.width && +f.elements.board_height.value === fr.height;
    $('boardhint').textContent = `Doohly: ${fr.width} × ${fr.height}` + (same ? '.' : f.elements.board_width.value ?
      ' (different from what is set here, which wins).' : ' (it fills this in within 5 minutes, or enter it now).') +
      ' The straightened PoP image gets the screen\'s shape, so a board seen at an angle isn\'t squeezed.';
  }
  if (!rot || !rot.doohly) {                   // no Doohly loop for this site: nothing to follow
    if (rot && rot.why) { $('rotnote').hidden = false; $('rotnote').textContent = `Rotation set by hand (${rot.why}).`; }
    return;
  }
  $('rotauto').hidden = false;
  f.elements.rot_doohly.checked = !rot.manual;
  $('rotdoohly').textContent = `(${rot.doohly.slot_count} × ${rot.doohly.slot_s} s, "${rot.doohly.name || ''}")`;
  const ch = rot.last_change;
  $('rotnote').hidden = !(ch || rot.why);
  $('rotnote').textContent = rot.why && !rot.manual ? `Not following Doohly: ${rot.why}.` :
    ch ? `Last set from Doohly ${new Date(ch.at).toLocaleString()}: ${ch.from} → ${ch.to}.` : '';
  sync();
}
f.addEventListener('change', async ev => {
  if (ev.target.name !== 'rot_doohly' || !current) return;
  const manual = !ev.target.checked;
  const r = await fetch('/sites/rotation', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({site_id: current.id, manual})});
  if (!r.ok) { msg('Could not change this: ' + ((await r.json().catch(() => ({}))).error || r.statusText), 'bad'); return; }
  rot = await r.json();
  if (!manual && rot.doohly) { setVal('slot_count', rot.doohly.slot_count); setVal('slot_s', rot.doohly.slot_s); }
  msg(manual ? 'The rotation is now set by hand: Doohly won\'t change it.' : 'The rotation now follows Doohly (applied within a few seconds).', 'ok');
  sync();
});

function formData() {
  const e = f.elements, num = v => v === '' ? 0 : Number(v);
  const d = {
    site_id: e.site_id.value.trim(), display_name: e.display_name.value.trim(), model: e.model.value.trim(),
    vault_screen: e.vault_screen.value.trim(),
    host: e.host.value.trim(), http_port: num(e.http_port.value), rtsp_port: num(e.rtsp_port.value),
    rtsp_transport: f.querySelector('input[name=rtsp_transport]:checked').value,
    use_https: e.use_https.checked, camera_tls_insecure: e.use_https.checked && e.camera_tls_insecure.checked,
    username: e.username.value.trim(), rtsp_url_override: e.rtsp_url_override.value.trim(),
    vapix_url_override: e.vapix_url_override.value.trim(),
    video_quality: e.video_quality.value, stream_params: e.stream_params.value.trim().replace(/^\?/, ''),
    takeover: e.takeover.checked, slot_count: e.takeover.checked ? 0 : num(e.slot_count.value),
    slot_ms: e.takeover.checked ? 0 : Math.round(num(e.slot_s.value) * 1000),
    operating_hours: e.allday.checked ? '00:00-24:00' : `${e.from.value}-${e.to.value}`,
    ptz_check: e.ptz_check.checked,
    board_width: num(e.board_width.value), board_height: num(e.board_height.value),
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
  if (!d.board_width !== !d.board_height) { msg('Enter both the screen width and height, or neither.', 'bad'); return; }
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
$('reset').onclick = () => {
  $('rsname').textContent = current.name; $('rsid').textContent = current.id;
  $('rsconfirm').value = ''; $('rshist').checked = false; $('rsgo').disabled = true; $('rsmsg').textContent = '';
  $('resetdlg').showModal(); $('rsconfirm').focus();
};
$('rsconfirm').oninput = () => { $('rsgo').disabled = $('rsconfirm').value.trim() !== current.id; };
$('rsgo').onclick = async () => {
  if ($('rsconfirm').value.trim() !== current.id) return;
  $('rsgo').disabled = true; $('rsmsg').textContent = 'Resetting…'; $('rsmsg').className = 'muted';
  const r = await fetch('/sites/reset-creatives', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({site_id: current.id, confirm: $('rsconfirm').value.trim(), history: $('rshist').checked})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { $('rsmsg').textContent = j.error || 'Reset failed'; $('rsmsg').className = 'bad'; $('rsgo').disabled = false; return; }
  $('resetdlg').close();
  msg('Creatives reset.' + (j.history ? ` Deleted ${j.deleted} stored record(s); IDs start again at c-0001.` : ' New creatives continue the ID numbering.'), 'ok');
};
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
select{font:inherit;padding:5px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
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
#setup.insetup{border:2px solid var(--accent)} #setup ul{list-style:none;margin:6px 0;padding:0} #setup li{display:flex;gap:6px;margin:4px 0}
#setup .tick{width:16px;flex:none;text-align:center;font-weight:700} #setup .d{font-size:12px} .linkbtn{border:0;padding:0;background:none;text-decoration:underline;color:var(--muted)}
</style></head><body><main>
<nav style="display:flex;gap:16px;margin-bottom:14px"><a href="/" style="color:var(--muted);text-decoration:none">Review</a><a href="/sites" style="font-weight:600;text-decoration:none">Sites</a><a href="/bookings" style="color:var(--muted);text-decoration:none">Bookings</a><a href="/health" style="color:var(--muted);text-decoration:none">Health</a></nav>
<h1>Edit region: <span id="cam"></span></h1>
<p class="muted" id="sub">Loading&hellip;</p>
<div class="bar">
  <button id="snap">New snapshot</button>
  <button id="revert">Undo changes</button>
  <label>Screen <select id="shape"><option value="standard">Standard (flat)</option><option value="curve">Curved</option><option value="fold">Fold (building corner)</option></select></label>
  <button id="fit" title="Move the handles until recent photos line up with the booked artwork they matched">Fit to artwork</button>
  <label id="foldl" hidden title="How much of the screen's width is on the left face (left of the fold)">Left face <input id="foldpct" type="number" min="5" max="95" step="1"> %</label>
  <label>Inset <input id="inset" type="number" min="0" max="20" step="1"> %</label>
  <button class="primary" id="save" disabled>Save and apply</button>
  <span id="msg"></span>
</div>
<div class="layout">
  <div class="stage"><canvas id="c"></canvas></div>
  <div class="side">
    <div class="panel" id="setup" hidden></div>
    <div class="panel"><h2>What the gateway will analyse</h2><canvas id="preview"></canvas>
      <p class="muted" style="margin:6px 0 0">Straightened, with the inset trimmed. It should show only the screen.</p></div>
    <div class="panel"><h2>How to</h2><ol id="howto"></ol></div>
  </div>
</div>
</main>
<script>
const qs = new URLSearchParams(location.search), name = qs.get('site') || qs.get('camera') || '';   /* site ID */
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
const c = $('c'), ctx = c.getContext('2d'), pv = $('preview'), pctx = pv.getContext('2d');
const src = document.createElement('canvas'), sctx = src.getContext('2d', {willReadFrequently: true});
let img = null, pixels = null, saved = null, pts = null, drag = -1, last = 0, cursor = null, dirty = false, board = 0;
const LABELS = ['TL', 'TR', 'BR', 'BL'], CLABELS = ['T1', 'T2', 'R1', 'R2', 'B1', 'B2', 'L1', 'L2'], FLABELS = ['fold top', 'fold bottom'];
// curve: two handles per edge (top and bottom left to right, right and left top to bottom); fold: top and bottom of the fold
let shape = 'standard', curve = null, fold = null;
const HOWTO = {
  standard: ['Drag the four handles onto the corners of the lit screen (TL, TR, BR, BL).',
             'Use the magnifier while dragging to land on the exact corner. Arrow keys nudge the last handle.',
             'Raise the inset if the preview still shows bezel or frame.',
             'Save. The gateway uses the new region straight away, from the next play.'],
  curve: ['Drag the four corner handles onto the corners of the lit screen (TL, TR, BR, BL).',
          'Drag the middle handles along each edge, so the outline follows the curve. A handle left on the straight line means that edge is straight.',
          'Or press Fit to artwork: it moves the handles until a recent photo lines up with the booked artwork it matched.',
          'Check the preview: text and edges should come out straight, not bowed.',
          'Raise the inset if the preview still shows bezel or frame, then save.'],
  fold: ['Drag the four corner handles onto the outer corners of the screen (TL, TR, BR, BL).',
         'Drag the two fold handles onto the top and bottom of the corner where the two faces meet.',
         'Set Left face to how much of the screen\'s width is left of the fold (from the screen\'s layout, e.g. 40% for a 2 m + 3 m wrap).',
         'Check the preview: the artwork should run straight across the fold, or press Fit to artwork to line the fold up with a recent photo. Then save.'],
};

// homography: unit square -> quad (8x8 solve)
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
const CURVE_AT = [[1/3,0],[2/3,0],[1,1/3],[1,2/3],[1/3,1],[2/3,1],[0,1/3],[0,2/3]];
function straightCurve(P) { const h = homography(P); return CURVE_AT.map(([u,v]) => map(h, u, v)); }
function thirds(s) { return [13.5*s*(s - 2/3)*(s - 1), -13.5*s*(s - 1/3)*(s - 1)]; }

// the same mapping as the gateway (core/src/warp.c): (u, v) on the flat screen -> point in the picture
function mapper(scaleX, scaleY) {
  const P = pts.map(p => [p[0]*scaleX, p[1]*scaleY]), h = homography(P);
  if (shape === 'fold' && fold) {
    const F = fold.map(p => [p[0]*scaleX, p[1]*scaleY]), fu = Math.min(0.95, Math.max(0.05, (+$('foldpct').value || 50) / 100));
    const hl = homography([P[0], F[0], F[1], P[3]]), hr = homography([F[0], P[1], P[2], F[1]]);
    return (u, v) => u < fu ? map(hl, u / fu, v) : map(hr, (u - fu) / (1 - fu), v);
  }
  if (shape === 'curve' && curve) {
    const bend = CURVE_AT.map(([u,v], k) => { const s = map(h, u, v); return [curve[k][0]*scaleX - s[0], curve[k][1]*scaleY - s[1]]; });
    return (u, v) => {
      const p = map(h, u, v), [a1, a2] = thirds(u), [b1, b2] = thirds(v);
      const w = [a1*(1-v), a2*(1-v), b1*u, b2*u, a1*v, a2*v, b1*(1-u), b2*(1-u)];
      for (let k = 0; k < 8; k++) { p[0] += w[k]*bend[k][0]; p[1] += w[k]*bend[k][1]; }
      return p;
    };
  }
  return (u, v) => map(h, u, v);
}
function outline(m, a) {             // boundary of the screen (or its inset) as a path
  if (shape === 'fold') return outlineFold(m, a, Math.min(0.95, Math.max(0.05, (+$('foldpct').value || 50) / 100)));
  const N = shape === 'standard' ? 1 : 24, P = [], b = 1 - a, step = t => a + (b - a) * t;
  for (let k = 0; k < N; k++) P.push(m(step(k/N), a));
  for (let k = 0; k < N; k++) P.push(m(b, step(k/N)));
  for (let k = 0; k < N; k++) P.push(m(step(1 - k/N), b));
  for (let k = 0; k < N; k++) P.push(m(a, step(1 - k/N)));
  return P;
}
function outlineFold(m, a, fu) {    // two flat faces: straight edges, a corner at the fold
  const b = 1 - a, us = [a, fu, b].filter((x, i, arr) => i === 0 || x > arr[i-1]);
  return [...us.map(u => m(u, a)), ...us.slice().reverse().map(u => m(u, b))];
}
// every draggable handle: corners, then the curve's midpoints or the fold's ends
function handles() {
  const H = pts.map((p, i) => ({p, label: LABELS[i], set: q => moveCorner(i, q)}));
  if (shape === 'curve' && curve) curve.forEach((p, i) => H.push({p, label: CLABELS[i], small: true, set: q => { curve[i] = q; }}));
  if (shape === 'fold' && fold) fold.forEach((p, i) => H.push({p, label: FLABELS[i], small: true, set: q => { fold[i] = q; }}));
  return H;
}
// how much of each corner's move each curve handle follows (the nearer one 2/3, the farther 1/3), keeping the bend
const FOLLOW = [[[0, 2/3], [1, 1/3], [6, 2/3], [7, 1/3]], [[0, 1/3], [1, 2/3], [2, 2/3], [3, 1/3]],
                [[2, 1/3], [3, 2/3], [4, 1/3], [5, 2/3]], [[4, 2/3], [5, 1/3], [6, 1/3], [7, 2/3]]];
function moveCorner(i, q) {
  const d = [q[0] - pts[i][0], q[1] - pts[i][1]];
  pts[i] = q;
  if (shape === 'curve' && curve) for (const [k, f] of FOLLOW[i]) curve[k] = [curve[k][0] + d[0]*f, curve[k][1] + d[1]*f];
}
function setShape(s, fresh) {
  shape = s; $('shape').value = s; $('foldl').hidden = s !== 'fold';
  if (s === 'curve' && (!curve || fresh)) curve = straightCurve(pts);
  if (s === 'fold' && (!fold || fresh)) { const h = homography(pts); fold = [map(h, 0.5, 0), map(h, 0.5, 1)]; if (!$('foldpct').value) $('foldpct').value = 50; }
  $('howto').innerHTML = HOWTO[s].map(t => `<li>${t}</li>`).join('') + (s !== 'standard' && !board
    ? `<li>Set the screen's resolution on its <a href="/site?site=${encodeURIComponent(name)}">Settings</a> page: a ${s === 'fold' ? 'folded' : 'curved'} screen's shape can't be judged from the camera's view.</li>` : '');
  if (last >= handles().length) last = 0;
}
const clamp01 = p => [Math.min(1, Math.max(0, p[0])), Math.min(1, Math.max(0, p[1]))];
const round4 = p => [Math.round(p[0]*1e4)/1e4, Math.round(p[1]*1e4)/1e4];
const copyPts = a => a ? a.map(p => p.slice()) : null;
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
    saved = {roi: cam.roi.map(p => p.slice()), inset: cam.inset_pct, shape: cam.roi_shape || 'standard',
             curve: copyPts(cam.roi_curve), fold: copyPts(cam.roi_fold), fold_pct: cam.fold_pct || 50};
    board = cam.board_width > 0 && cam.board_height > 0 ? cam.board_width / cam.board_height : 0;
    restore();
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

function restore() {
  pts = saved.roi.map(p => p.slice()); curve = copyPts(saved.curve); fold = copyPts(saved.fold);
  $('inset').value = saved.inset; $('foldpct').value = saved.fold_pct;
  setShape(saved.shape, false);
}

function preview() {
  if (!pixels) return;
  const W = img.width, H = img.height, m = mapper(W - 1, H - 1);
  const len = (a, b) => Math.hypot(a[0]-b[0], a[1]-b[1]);
  // apparent size along the (curved or folded) edges, as the gateway measures it
  let top = 0, bot = 0, left = 0, right = 0; const N = 16;
  for (let k = 0; k < N; k++) { const a = k/N, b = (k+1)/N;
    top += len(m(a,0), m(b,0)); bot += len(m(a,1), m(b,1)); left += len(m(0,a), m(0,b)); right += len(m(1,a), m(1,b)); }
  const bw = (top + bot)/2, bh = (left + right)/2;
  const ow = 320, oh = Math.max(40, Math.min(640, Math.round(board ? ow / board : ow * bh / Math.max(1, bw))));
  pv.width = ow; pv.height = oh;
  const out = pctx.createImageData(ow, oh), inset = Math.min(0.4, (+$('inset').value || 0)/100);
  for (let j = 0; j < oh; j++) for (let i = 0; i < ow; i++) {
    const u = inset + (1-2*inset)*(i+0.5)/ow, v = inset + (1-2*inset)*(j+0.5)/oh;
    let [x, y] = m(u, v); x = Math.min(W-1, Math.max(0, x|0)); y = Math.min(H-1, Math.max(0, y|0));
    const s = (y*W + x)*4, d = (j*ow + i)*4;
    out.data[d] = pixels.data[s]; out.data[d+1] = pixels.data[s+1]; out.data[d+2] = pixels.data[s+2]; out.data[d+3] = 255;
  }
  pctx.putImageData(out, 0, 0);
}

function draw() {
  if (!img) return;
  const W = c.width, H = c.height, scale = W / c.getBoundingClientRect().width || 1, m = mapper(W, H);
  ctx.drawImage(img, 0, 0);
  const path = (P, close) => { ctx.beginPath(); P.forEach((p, i) => i ? ctx.lineTo(...p) : ctx.moveTo(...p)); if (close) ctx.closePath(); };
  ctx.lineWidth = 2*scale; ctx.strokeStyle = '#d4237a'; ctx.fillStyle = 'rgba(212,35,122,0.12)';
  path(outline(m, 0), true); ctx.fill(); ctx.stroke();
  const inset = Math.min(0.4, (+$('inset').value || 0)/100);
  ctx.setLineDash([6*scale, 5*scale]); path(outline(m, inset), true); ctx.stroke();
  if (shape === 'fold' && fold) { path(fold.map(p => [p[0]*W, p[1]*H]), false); ctx.stroke(); }
  ctx.setLineDash([]);
  const r = 9*scale; ctx.font = `600 ${Math.round(13*scale)}px system-ui`;
  handles().forEach((hd, i) => {
    const p = [hd.p[0]*W, hd.p[1]*H], rr = hd.small ? r*0.75 : r;
    ctx.beginPath(); ctx.arc(p[0], p[1], rr, 0, 7); ctx.fillStyle = i === last ? '#d4237a' : hd.small ? '#ffd2e8' : '#fff'; ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#fff'; ctx.strokeStyle = 'rgba(0,0,0,.6)'; ctx.lineWidth = 3*scale;
    ctx.strokeText(hd.label, p[0] + rr*1.4, p[1] - rr); ctx.fillText(hd.label, p[0] + rr*1.4, p[1] - rr);
    ctx.strokeStyle = '#d4237a'; ctx.lineWidth = 2*scale;
  });
  if (drag >= 0 && cursor) { const hp = handles()[drag].p; loupe([hp[0]*W, hp[1]*H], scale); }
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
  handles().forEach((hd, i) => { const q = hd.p, d = Math.hypot((q[0]-p[0])*b.width, (q[1]-p[1])*b.height); if (d < bd) { bd = d; best = i; } });
  if (bd < 36) { drag = last = best; cursor = p; c.setPointerCapture(e.pointerId); draw(); }
});
c.addEventListener('pointermove', e => {
  if (drag < 0) return;
  const p = pos(e); cursor = p;
  handles()[drag].set(clamp01(p));
  setDirty(true); msg('Unsaved changes'); requestAnimationFrame(draw);
});
c.addEventListener('pointerup', () => { drag = -1; cursor = null; draw(); });
document.addEventListener('keydown', e => {
  if (!img || ['inset', 'foldpct', 'shape'].includes(document.activeElement && document.activeElement.id)) return;
  const step = (e.shiftKey ? 10 : 1) / img.width, d = {ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1]}[e.key];
  if (!d) return; e.preventDefault();
  const hd = handles()[last];
  hd.set(clamp01([hd.p[0] + d[0]*step, hd.p[1] + d[1]*step*img.width/img.height]));
  setDirty(true); msg('Unsaved changes'); draw();
});
$('inset').addEventListener('input', () => { setDirty(true); msg('Unsaved changes'); draw(); });
$('foldpct').addEventListener('input', () => { setDirty(true); msg('Unsaved changes'); draw(); });
$('shape').addEventListener('change', e => {
  const s = e.target.value, fresh = !(saved && saved.shape === s);   // a new shape starts from the straight outline
  setShape(s, fresh && !(s === 'curve' ? curve : fold)); setDirty(true); msg('Unsaved changes'); draw();
});
$('snap').onclick = snapshot;
function regionBody() {
  const body = {roi: pts.map(round4), inset_pct: +$('inset').value || 0, roi_shape: shape};
  if (shape === 'curve') body.roi_curve = curve.map(round4);
  if (shape === 'fold') { body.roi_fold = fold.map(round4); body.fold_pct = Math.min(95, Math.max(5, +$('foldpct').value || 50)); }
  return body;
}
$('fit').onclick = async () => {
  $('fit').disabled = true; msg('Fitting to recent photos of matched creatives (up to half a minute)…');
  try {
    const r = await fetch('/roi/fit', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({site_id: name, region: regionBody(), inset_pct: +$('inset').value || 0})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.error) { msg('Can\'t fit: ' + (j.error || r.statusText), 'bad'); return; }
    const n = j.photos.length, pct = x => Math.round(x * 100) + '%';
    if (!j.better) { msg(`Already as good as it gets with ${n} photo(s) (match ${pct(j.before)}). Nothing changed.`, 'ok'); return; }
    const g = j.region;
    pts = g.roi.map(p => p.slice());
    if (g.roi_curve) curve = g.roi_curve.map(p => p.slice());
    if (g.roi_fold) fold = g.roi_fold.map(p => p.slice());
    if (g.fold_pct) $('foldpct').value = g.fold_pct;
    setDirty(true); draw();
    msg(`Fitted to ${n} photo(s): match ${pct(j.before)} → ${pct(j.after)}${j.timed_out ? ' (stopped at the time limit; press again to go on)' : ''}. Check the preview, then save.`, 'ok');
  } finally { $('fit').disabled = false; }
};
$('revert').onclick = () => { if (!saved) return; restore(); setDirty(false); msg(''); draw(); };
$('save').onclick = async () => {
  $('save').disabled = true; msg('Saving…');
  const body = regionBody();
  const r = await fetch(api('/roi'), {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg('Not saved: ' + (j.error || r.statusText), 'bad'); $('save').disabled = false; return; }
  saved = {roi: copyPts(body.roi), inset: body.inset_pct, shape, curve: copyPts(curve), fold: copyPts(fold), fold_pct: body.fold_pct || +$('foldpct').value || 50};
  setDirty(false);
  msg('Saved and applied. The next play will use this region.', 'ok');
  loadSetup();
};

// ---- setting the site up: its plays count only once it goes live
async function loadSetup() {
  const r = await fetch('/sites/setup?site=' + encodeURIComponent(name)).catch(() => null);
  const st = r && r.ok ? await r.json() : null, el = $('setup');
  if (!st) { el.hidden = true; return; }
  el.hidden = false;
  el.className = 'panel' + (st.in_setup ? ' insetup' : '');
  if (!st.in_setup) {
    el.innerHTML = `<h2>Live</h2><p class="muted" style="margin:0 0 8px">Its plays are counted${st.live_since ? ' since ' + esc(st.live_since) : ''}.
      If the camera was moved or re-aimed, put it back into setup while you fix the region: its plays stop counting and the creatives are learned again once it goes live.</p>
      <button id="tosetup">Back to setup</button>`;
    $('tosetup').onclick = () => goLive(false);
    return;
  }
  el.innerHTML = `<h2>In setup: plays not counted yet</h2>
    <p class="muted d" style="margin:0">Since ${esc(st.since || '?')}. Place the region, then leave it while plays come in. Its plays aren't counted on the Bookings page or used for PoP photos, and detection alerts aren't sent to Teams, until it goes live.</p>
    <ul>${st.checks.map(c => `<li><span class="tick ${c.ok ? 'ok' : 'bad'}">${c.ok ? '✓' : '✗'}</span><span><b>${esc(c.label)}</b><br><span class="muted d">${esc(c.detail)}</span></span></li>`).join('')}</ul>
    <button class="primary" id="golive" ${st.ready ? '' : 'disabled'}>Go live</button>
    ${st.ready ? '' : ' <button class="linkbtn" id="force">Go live anyway</button>'}
    <p class="muted d" style="margin:8px 0 0">Going live makes the gateway learn the creatives again from that moment, so each artwork gets one ID with the final region.</p>`;
  $('golive').onclick = () => goLive(true, false);
  if ($('force')) $('force').onclick = () => {
    if (confirm('Some checks haven\'t passed. Go live anyway? Its plays will count from now.')) goLive(true, true);
  };
}
async function goLive(live, force) {
  if (!live && !confirm('Put this site back into setup? Its plays stop counting until it goes live again.')) return;
  if (live && dirty && !confirm('The region has unsaved changes. Go live with the saved region?')) return;
  const r = await fetch('/sites/setup', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify({site_id: name, live, force: !!force})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg(j.error || r.statusText, 'bad'); }
  else msg(live ? 'Live: its plays count from now, and the creatives are being learned again.' : 'Back in setup.', 'ok');
  loadSetup();
}
setInterval(() => { if (!document.hidden) loadSetup(); }, 60000);
window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
window.addEventListener('resize', draw);
load(); loadSetup();
</script></body></html>
'''


# Review-page auto refresh: interval chosen from the dropdown, remembered per
# browser (and overridable with ?refresh=SECONDS). Pauses while the tab is hidden.
IMPORT_TEMPLATE = (
    "site_id,display_name,vault_screen,model,host,http_port,rtsp_port,video_transport,use_https,accept_self_signed,"
    "username,password,slot_count,slot_seconds,takeover,hours,ptz_check,coverage_min_pct,coverage_window_min\r\n"
    "AKL-EXAMPLE-01,Example Rd North,Example Rd North,M5525-E,203.0.113.10,8080,8554,rtsp,no,no,,,6,10,no,05:00-01:00,yes,,\r\n"
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
<nav><a href="/">Review</a><a href="/sites" class="on">Sites</a><a href="/bookings">Bookings</a><a href="/health">Health</a></nav>
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
<tr><td><code>vault_screen</code></td><td>The screen's name in Vault, so bookings can be compared with what plays</td></tr>
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
  for (const [k, col] of [['site_id','site_id'],['display_name','display_name'],['vault_screen','vault_screen'],['model','model'],['host','host'],['username','username']])
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
<title>Gateway Health</title><style>
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
.btn{border:1px solid var(--line);background:transparent;color:var(--fg);padding:6px 11px;border-radius:8px;font:inherit;text-decoration:none;cursor:pointer;display:inline-block}
.btn.danger{border-color:var(--bad);color:var(--bad)} input.cf{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--fg);width:120px}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites">Sites</a><a href="/bookings">Bookings</a><a href="/health" class="on">Health</a></nav>
<h1>Gateway Health</h1>
<div id="gws"><p class="muted">Loading&hellip;</p></div>
<div class="gw" id="bk"><div class="top"><h2>Backup and restore</h2></div>
  <p class="muted" style="margin:6px 0 10px">A backup is one file with everything but the photos: the sites and their regions,
    camera logins, learned creatives, links and settings, every play and alert record, and which PoP photos are in SharePoint.
    The photos (the last few days; the best are in SharePoint) and the artwork (synced again) aren't in it. Keep it safe: it has the camera logins.
    To move to another machine: set it up with the same <code>.env</code> and <code>config.json</code>, then restore a backup here.</p>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <a class="btn" id="dl" href="/backup">Download a backup</a>
    <button class="btn" id="spnow">Back up to SharePoint now</button>
    <label class="btn" style="cursor:pointer">Restore from a file&hellip;<input type="file" id="rf" accept=".gz,.tgz,application/gzip" hidden></label>
    <span id="bkmsg" class="muted"></span>
  </div>
  <div id="bknight" class="muted" style="margin-top:10px;font-size:13px"></div>
  <div class="wrap"><table id="bklist" style="margin-top:6px"></table></div>
  <div id="rprev" hidden style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px"></div>
</div>
<p class="muted" id="sub" style="margin:24px 0 0;padding-top:12px;border-top:1px solid var(--line);font-size:13px">Loading&hellip;</p>
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
      ${h.host ? tile('Machine CPU', h.host.cpu_pct != null ? Math.round(h.host.cpu_pct) + '%' : '—', `${h.host.cpus} core${h.host.cpus === 1 ? '' : 's'}` + (h.host.load1 != null ? ` · load ${h.host.load1}` : ''), h.host.cpu_pct >= 85 ? 'warn' : '') : ''}
      ${h.host && h.host.mem_total_bytes ? tile('Machine memory', gb(h.host.mem_available_bytes) + ' free', 'of ' + gb(h.host.mem_total_bytes), h.host.mem_available_bytes < 0.1 * h.host.mem_total_bytes ? 'bad' : '') : ''}
      ${h.process ? tile('Gateway', h.process.cpu_pct != null ? Math.round(h.process.cpu_pct) + '% CPU' : '—', h.process.rss_bytes ? gb(h.process.rss_bytes) + ' memory' : '') : ''}
      ${tile('Clock', h.ntp_synced === false ? 'not synced' : h.ntp_synced ? 'NTP synced' : '—', '', h.ntp_synced === false ? 'warn' : '')}
    </div>
    <div class="wrap"><table><thead><tr><th>Site</th><th>Video</th><th>Video uptime</th><th>Reconnects</th><th>FPS</th><th>Coverage</th><th>Problems</th></tr></thead>
    <tbody>${cams || '<tr><td colspan="7" class="muted">No sites</td></tr>'}</tbody></table></div>
    ${g.online ? '' : `<p style="margin:10px 0 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><button type="button" class="btn danger" data-forget="${esc(g.gateway_id)}">Remove this gateway</button>
      <span class="muted" style="font-size:12px">If it was renamed, retired or replaced (e.g. moved to another machine): takes it off this page and the offline warnings. Its PoP records and sites stay. If it sends a heartbeat again it comes back.</span></p>`}
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
    const st = await (await fetch('/health/storage')).json().catch(() => null);
    if (st) $sub.innerHTML += `<br>This server: ${st.records.toLocaleString()} PoP and alert record(s) since ${esc(st.oldest || '–')} (${st.db_mb} MB), ` +
      `${st.photo_files.toLocaleString()} photo file(s) (${st.photo_mb} MB) · photos kept ${st.image_days} day(s), their full camera frames until the day and night best-photo runs have been through them (the ${st.pinned || 0} best kept as long as they are in SharePoint), alert photos ${st.alert_image_days}, records ${st.record_days} · ` +
      `<span class="${st.free_gb < 5 ? 'bad' : ''}">${st.free_gb} GB free</span> of ${st.disk_gb} GB`;
  } catch (e) { document.getElementById('sub').innerHTML = '<span class="bad">' + esc(e.message || e) + '</span>'; }
}
let $sub; load(); setInterval(load, 15000);
document.getElementById('gws').addEventListener('click', async e => {     // remove an offline gateway (asks once)
  const b = e.target.closest('button[data-forget]'); if (!b) return;
  if (!b.dataset.sure) { b.dataset.sure = '1'; b.textContent = `Remove ${b.dataset.forget}? Click again`; return; }
  b.disabled = true;
  const r = await fetch('/health/forget', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({gateway_id: b.dataset.forget})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { b.disabled = false; b.textContent = 'Not removed: ' + (j.error || r.status); return; }
  load();
});

// ---- backup and restore
const $b = id => document.getElementById(id);
const when = iso => iso ? new Date(iso).toLocaleString(undefined, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : '';
async function loadBackups() {
  const r = await fetch('/backup/list').then(r => r.json()).catch(e => ({error: String(e)}));
  const st = r.status || {};
  $b('bknight').innerHTML = r.configured === false ? 'Nightly backups to SharePoint need the SharePoint app set up (creatives-sync).' :
    (st.enabled === false ? 'Nightly backups to SharePoint are off (BACKUP_TO_SHAREPOINT).' :
     `Every night at ${esc(st.at || '?')} a backup goes to SharePoint (${esc(st.where || '')}); the newest ${st.keep || '?'} are kept. ` +
     (st.last ? `Last: <span class="${st.ok === false ? 'bad' : 'ok'}">${esc(when(st.last))}</span>${st.size ? ' (' + gb(st.size) + ')' : ''}` : 'None yet.') +
     (st.ok === false ? ` <span class="bad">Last try failed: ${esc(st.error)}</span>` : '') + (st.running ? ' <span class="warn">Running now&hellip;</span>' : '')) +
    (r.error ? ` <span class="bad">${esc(r.error)}</span>` : '');
  const items = r.items || [];
  $b('bklist').innerHTML = items.length ? '<thead><tr><th>Backup in SharePoint</th><th>Size</th><th></th></tr></thead><tbody>' + items.map(i =>
    `<tr><td>${i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.name)}</a>` : esc(i.name)}</td><td>${gb(i.size)}</td>
     <td><button class="btn" data-sp="${esc(i.id)}">Restore&hellip;</button></td></tr>`).join('') + '</tbody>' : '';
}
function preview(p) {
  const m = p.manifest || {}, el = $b('rprev');
  el.hidden = false;
  el.innerHTML = `<b>Backup from ${esc(when(m.created_at))}</b> <span class="muted">(${esc(m.host || '?')}, ${gb(p.size)})</span>
    <div style="margin:6px 0">${(m.sites || []).length} site(s): ${esc((m.sites || []).map(s => s.name || s.id).join(', ')) || 'none'}<br>
    ${(m.plays || 0).toLocaleString()} play record(s), ${(m.alerts || 0).toLocaleString()} alert(s), learned creatives for ${m.learned_sites || 0} site(s)${p.creatives_sync ? ', the PoP photos in SharePoint' : ''}.
    ${(m.warnings || []).length ? '<br><span class="warn">' + esc(m.warnings.join('; ')) + '</span>' : ''}</div>
    <p class="bad" style="margin:6px 0">Restoring replaces this machine's sites, settings and records with the backup's (what they replace is kept
      in a before-restore folder). The gateway, receiver and artwork sync restart; this page comes back in about half a minute.</p>
    Type <b>RESTORE</b> to confirm: <input class="cf" id="rcf" autocomplete="off"> <button class="btn danger" id="rgo">Restore now</button>
    <button class="btn" id="rno">Cancel</button> <span id="rmsg" class="muted"></span>`;
  $b('rno').onclick = () => { el.hidden = true; el.innerHTML = ''; };
  $b('rgo').onclick = async () => {
    if ($b('rcf').value !== 'RESTORE') { $b('rmsg').textContent = 'Type RESTORE first.'; return; }
    $b('rgo').disabled = true; $b('rmsg').textContent = 'Restoring…';
    const r = await fetch('/restore/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: p.token, confirm: 'RESTORE'})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { $b('rmsg').innerHTML = '<span class="bad">' + esc(j.error || r.status) + '</span>'; $b('rgo').disabled = false; return; }
    $b('rmsg').innerHTML = '<span class="ok">Restored. Restarting…</span>' + ((j.warnings || []).length ? ' <span class="warn">' + esc(j.warnings.join('; ')) + '</span>' : '');
    setTimeout(function wait() { fetch('/healthz').then(r => r.ok ? location.reload() : setTimeout(wait, 2000)).catch(() => setTimeout(wait, 2000)); }, 4000);
  };
}
$b('rf').onchange = () => {
  const f = $b('rf').files[0]; if (!f) return;
  $b('bkmsg').textContent = `Uploading ${f.name}…`;
  const x = new XMLHttpRequest();
  x.open('POST', '/restore/upload');
  x.upload.onprogress = e => { if (e.lengthComputable) $b('bkmsg').textContent = `Uploading ${f.name}: ${Math.round(e.loaded / e.total * 100)}%`; };
  x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch (e) {}
    if (x.status !== 200) { $b('bkmsg').innerHTML = '<span class="bad">' + esc(j.error || x.status) + '</span>'; return; }
    $b('bkmsg').textContent = ''; preview(j); };
  x.onerror = () => { $b('bkmsg').innerHTML = '<span class="bad">upload failed</span>'; };
  x.send(f); $b('rf').value = '';
};
$b('bklist').addEventListener('click', async e => {
  const b = e.target.closest('[data-sp]'); if (!b) return;
  b.disabled = true; $b('bkmsg').textContent = 'Fetching the backup from SharePoint…';
  const r = await fetch('/restore/sharepoint?id=' + encodeURIComponent(b.dataset.sp), {method: 'POST'});
  const j = await r.json().catch(() => ({})); b.disabled = false;
  if (!r.ok) { $b('bkmsg').innerHTML = '<span class="bad">' + esc(j.error || r.status) + '</span>'; return; }
  $b('bkmsg').textContent = ''; preview(j);
});
$b('spnow').onclick = async () => {
  const r = await fetch('/backup/sharepoint', {method: 'POST'}); const j = await r.json().catch(() => ({}));
  $b('bkmsg').innerHTML = r.ok ? 'Backing up to SharePoint: it shows below when done.' : '<span class="bad">' + esc(j.error || r.status) + '</span>';
  setTimeout(loadBackups, 3000);
};
$b('dl').onclick = () => { $b('bkmsg').textContent = 'Making the backup: the download starts in a minute or so.'; };
loadBackups(); setInterval(loadBackups, 60000);
</script></body></html>'''


BOOKINGS_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bookings</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--line:#d8d8d2;--bad:#c62828;--ok:#2e7d32;--warn:#a15c00;--panel:#ececea;--accent:#d4237a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--line:#33332f;--panel:#22221f;--warn:#e0a040}}
*{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px} h1{font-size:20px;margin:0} h2{font-size:17px;margin:0} h3{font-size:14px;margin:14px 0 6px} a{color:inherit}
nav{display:flex;gap:16px;margin-bottom:14px} nav a{color:var(--muted);text-decoration:none} nav a.on{color:var(--fg);font-weight:600}
.muted{color:var(--muted)} .bad{color:var(--bad)} .ok{color:var(--ok)} .warn{color:var(--warn)}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}
.btn{display:inline-block;border:1px solid var(--line);background:transparent;color:var(--fg);padding:6px 11px;border-radius:8px;cursor:pointer;font:inherit;text-decoration:none}
.site{border:1px solid var(--line);border-radius:10px;padding:0;margin:10px 0}
.site > summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:10px minmax(0,1fr) auto;gap:6px 12px;align-items:center;padding:11px 16px}
.site > summary .sl{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.site > summary .sl .muted{margin-left:8px}
.site > summary::-webkit-details-marker{display:none}
.site > summary::before{content:'\25B8';color:var(--muted);width:10px} .site[open] > summary::before{content:'\25BE'}
.site[open] > summary{border-bottom:1px solid var(--line)} details.site > :not(summary){margin-left:16px;margin-right:16px}
.site[open]{padding-bottom:12px} .site.plain{padding:11px 16px;display:flex;gap:14px;flex-wrap:wrap}
.sname{font-weight:600;font-size:15px} .sname a{text-decoration:none}
.chips{display:grid;grid-template-columns:94px 112px 134px 94px 108px 80px 84px;gap:6px;align-items:center}
.chips .cell,.chips .cell > span:not(.chip){display:flex} .chips .chip{text-decoration:none}
@media (max-width:1100px){.site > summary{grid-template-columns:10px minmax(0,1fr)} .site > summary .chips{grid-column:2;display:flex;flex-wrap:wrap} .chips .cell:empty{display:none}}
.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:2px 9px;white-space:nowrap}
.chip.warn{border-color:var(--warn);color:var(--warn)} .chip.bad{border-color:var(--bad);color:var(--bad)} .chip.ok{border-color:var(--ok);color:var(--ok)}
.chip.warn .muted,.chip.bad .muted,.chip.ok .muted{color:inherit;opacity:.8}
button.link{background:none;border:0;padding:0 0 0 4px;color:var(--muted);cursor:pointer;font:inherit;font-size:12px}
button.link:hover{color:var(--fg);text-decoration:underline}
tr.sub td{background:var(--panel);padding-top:3px;padding-bottom:3px} tr.sub .thumb{width:54px;height:30px}
.nophoto{display:inline-block;font-size:11px;line-height:1.25;color:var(--muted);text-align:center;width:72px}
tr.minor td{background:var(--panel)} tr.minor .muted{font-size:12px;margin-left:6px}
.top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:10px;margin:12px 0}
.tile{background:var(--panel);border-radius:8px;padding:9px 11px} .tile .k{font-size:12px;color:var(--muted)} .tile .v{font-size:18px;font-weight:600} .tile .s{font-size:12px;color:var(--muted)}
ul.w{margin:6px 0 0;padding-left:18px} ul.w li{color:var(--warn)}
.wrap{overflow-x:auto} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:middle}
th{white-space:nowrap;font-weight:600} td.n{text-align:right;white-space:nowrap} th.n{text-align:right}
.meter{display:inline-block;width:70px;height:6px;border-radius:3px;background:var(--line);vertical-align:middle;margin-right:6px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--ok)} .meter i.under{background:var(--warn)} .meter i.none{background:var(--bad)}
.thumb{width:72px;height:40px;object-fit:contain;background:#000;border-radius:4px;display:block}
.thumb.sm{width:44px;height:30px;display:inline-block;margin-right:4px;vertical-align:middle}
.pill.ok{border-color:var(--ok);color:var(--ok)}
select{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);max-width:260px}
.pill{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 7px;color:var(--muted);white-space:nowrap}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.note{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:12px 0}
button.warnlink{color:var(--warn);font-size:inherit;padding:0;text-decoration:underline dotted}
dialog#missdlg{max-width:min(1100px,95vw);width:100%;max-height:88vh;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
dialog#missdlg::backdrop{background:rgba(0,0,0,.5)}
.mcamp{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}
.mcamp h3{margin:0 0 2px;font-size:15px} .msite{border-top:1px solid var(--line);margin-top:8px;padding-top:8px}
.msite ul{margin:4px 0;padding-left:18px} .msite li{margin:2px 0} .mup{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px}
.mstat{font-size:12px} code.f{font-size:12px;word-break:break-all}
.frow{display:flex;align-items:center;gap:8px;width:100%;text-align:left;background:none;border:0;border-radius:6px;padding:7px 9px;color:inherit;font:inherit;cursor:pointer}
.frow:hover{background:var(--panel,rgba(127,127,127,.12))} .ficon{font-size:15px}
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites">Sites</a><a href="/bookings" class="on">Bookings</a><a href="/health">Health</a></nav>
<h1>Bookings vs Delivery</h1>
<div class="bar"><button class="btn" id="prev">&larr; Previous week</button><b id="wk"></b><button class="btn" id="next">Next week &rarr;</button>
<button class="btn" id="all">Expand / collapse all</button><span class="muted" id="built" style="font-size:12px"></span>
<span style="flex:1"></span><label class="btn" style="cursor:pointer">Upload Vault pull or creatives<input type="file" id="up" accept=".json,.zip,application/json,application/zip" hidden></label></div>
<div id="out"></div>
<div class="foot" style="margin-top:24px;padding-top:12px;border-top:1px solid var(--line);font-size:13px">
<p class="muted" id="sub" style="margin:0">Loading&hellip;</p>
<p class="muted" id="sync" style="margin:6px 0 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap" hidden><span id="synctxt"></span><button class="btn" id="syncnow" style="padding:2px 9px">Sync now</button></p>
<div class="muted" id="popup" style="margin:6px 0 0" hidden><span id="poptxt"></span>
 <button class="btn" data-phase="day" style="padding:2px 9px" title="Upload the best daytime photo of each campaign since the last run, now">Upload day photos now</button>
 <button class="btn" data-phase="night" style="padding:2px 9px" title="Upload the best night photo of each campaign since the last run, now">Upload night photos now</button>
 <details id="poprecent" style="margin-top:4px" hidden></details></div></div>
<dialog id="missdlg"><div class="top"><h2 style="margin:0">Bookings without artwork</h2><button class="btn" id="missclose">Close</button></div>
<p class="muted" id="misssub" style="margin:4px 0 10px"></p><div id="misslist"></div></dialog>
<dialog id="fdlg" style="width:min(640px,94vw)"><div class="top"><h2 style="margin:0">Choose the campaign's folder</h2></div>
<p class="muted" id="fsub" style="margin:4px 0 8px"></p>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><button type="button" class="btn" id="fup" title="Up one folder">&uarr; Up</button>
<code class="f" id="fpath" style="flex:1"></code></div>
<div id="flist" style="max-height:50vh;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:4px"></div>
<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px"><button type="button" class="btn" id="fcancel">Cancel</button><button type="button" class="btn primary" id="fuse">Use this folder</button></div></dialog>
<details class="note muted"><summary>How this is worked out</summary>
<p>Vault lists, for each booked campaign, the screens it books with its weeks and plays per loop. For each site linked to a Vault screen, this page adds up that week's bookings and compares them with what the gateway saw.</p>
<p><b>Per loop</b> figures are slots held per loop of the site's rotation. A creative on screen for two slots back to back counts as two; a longer run than that creative usually holds (a missed change, hold-over or freeze) only counts its usual length.</p>
<p>Vault gives each booking's first and last week and how many weeks it books, but not which ones. A booking with gaps is only <b>certain</b> in its first and last week; in between it shows as <i>may be booked this week</i> and isn't counted in true coverage (it can still be matched to a creative). Week-by-week data from a Vault API would remove this.</p>
<p><b>Artwork matching.</b> The booked creatives for each screen are pulled from SharePoint (Booked Campaigns / Creative+MIs, using each campaign's Material Instructions where they name the file for the screen). Each creative the gateway sees is compared with that artwork using its PoP images: fine detail (text, edges, layout) with the camera's glare and exposure removed. A clear match (at least 40% and well ahead of every other campaign) assigns it to that campaign as <span class="pill ok">artwork match</span>; a weaker one shows <span class="pill">looks like …?</span> and stays unassigned; <span class="pill">matches no booked artwork</span> usually means programmatic, house, or a creative that isn't in SharePoint. Artwork that played outside the dates it is booked for is flagged <span class="pill warn">off schedule</span>. Video creatives are matched by frames taken from the video.</p>
<p>Anything can be set by hand (a campaign, <b>Programmatic</b> or <b>House</b>), which always wins and is kept for later weeks. A campaign with no artwork in SharePoint and no creative linked shows as <i>not linked</i> and isn't judged.</p>
<p><b>Delivery</b> is slots delivered per loop ÷ slots booked per loop, over the time the gateway was watching. <b>True coverage</b> is booked slots delivered ÷ booked slots, over the certain bookings that have creatives linked. <b>Monitored</b> is the share of the week so far with verified plays.</p></details>
</main><script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = v => v == null ? '—' : Math.round(v * 100) + '%';
const one = v => v == null ? '—' : (Math.round(v * 10) / 10).toString();
const hrs = s => s == null ? '—' : s < 3600 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h';
const params = new URLSearchParams(location.search);
let week = params.get('week') || '';
const tile = (k, v, s, cls) => `<div class="tile"><div class="k">${k}</div><div class="v ${cls || ''}">${v}</div><div class="s">${s || ''}</div></div>`;
function meter(c) {
  if (c.status === 'not linked') return '<span class="muted" title="Link this campaign\'s creative(s) under Creatives seen to measure it">not linked</span>';
  const d = c.delivery ?? 0, cls = c.status === 'not seen' ? 'none' : c.status === 'under' ? 'under' : '';
  if (c.status === 'maybe' && c.source === 'doohly') return '<span class="muted" title="Non-guaranteed bonus: plays only when the loop has room, so it isn\'t counted in true coverage">bonus if room</span>';
  if (c.status === 'maybe') return '<span class="muted" title="Vault doesn\'t say which weeks of this booking are live">may be booked this week</span>';
  if (c.status === 'seen') return `<span class="muted">seen, ${one(c.delivered)} / loop</span>`;
  if (c.status === 'not started') return `<span class="muted" title="Booked in Doohly on a schedule (some days or hours of the week) that hasn't come round yet this week">${c.starts ? 'starts ' + esc(c.starts) : 'not on yet'}</span>`;
  return `<span class="meter"><i class="${cls}" style="width:${Math.min(100, Math.round(d * 100))}%"></i></span>${c.status === 'not seen' ? '<span class="bad">not seen</span>' : pct(d)}`;
}
const artUrl = f => '/creatives/file?f=' + encodeURIComponent(f);
const fmtd = d => d ? new Date(d + 'T00:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short'}) : '';
function artCell(c, s) {
  const files = c.artwork.filter(a => a.file);
  if (!files.length) {
    const miss = (s.artwork.missing || []).find(m => m.campaign_id === c.campaign_id);
    const vids = c.artwork.filter(a => a.kind === 'video').length;
    return `<span class="muted" title="${esc(miss ? miss.reason : '')}">${vids ? vids + ' video(s), not matched' : s.artwork.library ? 'none in SharePoint' : '—'}</span>` +
      (miss && miss.folder_url ? ` <a class="muted" href="${esc(miss.folder_url)}" target="_blank" title="Open the campaign folder in SharePoint">folder</a>` : '');
  }
  const dco = files.filter(a => a.dco);
  if (dco.length) {      // a dynamic creative: one picture and the count, not 30 near-identical thumbnails
    const rest = files.filter(a => !a.dco);
    return `<span class="pill" title="Dynamic creative (HTML in Doohly). PoP photos are matched against its ${dco.length} rendered variants from LUMO Labs (weather and time of day); the best variant wins.">DCO</span> ` +
      `<a href="${artUrl(dco[0].file)}" target="_blank" title="${esc(dco[0].variant || dco[0].name)}"><img class="thumb sm" loading="lazy" src="${artUrl(dco[0].file)}" alt=""></a>` +
      ` <span class="muted">${dco.length} variant${dco.length === 1 ? '' : 's'}</span>` +
      rest.slice(0, 2).map(a => `<a href="${artUrl(a.file)}" target="_blank" title="${esc(a.name)}"><img class="thumb sm" loading="lazy" src="${artUrl(a.file)}" alt=""></a>`).join('');
  }
  const hand = files.find(a => a.via === 'picked');
  return files.slice(0, 4).map(a => `<a href="${artUrl(a.file)}" target="_blank" title="${esc(a.name)}${a.via === 'picked' ? ' (chosen by hand)' : ''}${a.from || a.to ? ' · ' + fmtd(a.from) + '–' + fmtd(a.to) : ''}${a.rotation ? ' · ' + a.rotation + '%' : ''}"><img class="thumb sm" loading="lazy" src="${artUrl(a.file)}" alt=""></a>`).join('') +
     (files.length > 4 ? ` <span class="muted">+${files.length - 4}</span>` : '') +
     (hand ? ` <span class="pill" title="Matched against ${esc(files.filter(a => a.via === 'picked').map(a => a.name).join(', '))}, chosen from the campaign folder on this page">chosen</span> <button type="button" class="link" style="font-size:12px" data-unpick="${esc(c.campaign_id)}" data-screen="${esc(hand.screen || s.vault_screen)}" title="Stop matching against the file(s) chosen by hand">undo</button>` : '');
}
function refThumb(cr) {
  const a = cr.art; if (!a || !a.file || a.status === 'none') return '';
  const f = a.frame || a.file;          // a video: the frame it looked like
  return `<a href="${artUrl(f)}" target="_blank" title="${esc(a.variant ? 'DCO variant: ' + a.variant : a.name)}${a.kind === 'video' ? ' (video frame)' : ''}"><img class="thumb" loading="lazy" src="${artUrl(f)}" alt="booked artwork" style="${a.status === 'possible' ? 'opacity:.55' : ''}"></a>`;
}
function artPill(cr, s, cname) {
  const a = cr.art;
  if (cr.link) return '<span class="pill">set by hand</span>';
  if (!a) return '';
  const sc = `match ${Math.round(a.score * 100)}%, next best ${Math.round(a.next * 100)}%, over ${a.samples} PoP image(s)${a.washed ? ` (${a.washed} washed out)` : ''} and ${a.candidates} booked artwork(s)`;
  if (a.status === 'matched') return `<span class="pill ok" title="${esc(sc)}">artwork match</span>` +
     (a.off_schedule ? ` <span class="pill warn" title="${esc(a.name)} runs ${fmtd(a.from)}–${fmtd(a.to)}">off schedule</span>` : '');
  if (a.status === 'possible') return `<span class="pill" title="${esc(sc)}">looks like ${esc(cname(a.campaign_id))}?</span>`;
  return `<span class="pill" title="${esc(sc)}">matches no booked artwork</span>`;
}
const remember = (k, set) => { try { localStorage.setItem(k, JSON.stringify([...set])); } catch (e) {} };
const recall = k => { try { return new Set(JSON.parse(localStorage.getItem(k) || '[]')); } catch (e) { return new Set(); } };
const OPEN_SITES = recall('pop-bookings-open'), OPEN_GROUPS = recall('pop-bookings-groups');
function site(s, rep) {
  if (!s.vault_screen) return `<div class="site plain"><span class="sname">${esc(s.name)}</span>
     <span class="muted">Not linked to a Vault screen. <a href="/site?site=${encodeURIComponent(s.site_id)}">Set its Vault screen name</a> to compare bookings.</span></div>`;
  const camp = s.campaigns.map(c => `<tr${c.certain ? '' : ' style="opacity:.7"'}><td>${c.url ? `<a href="${esc(c.url)}" target="_blank">${esc(c.name)}</a>` : esc(c.name)}<br><span class="muted">${esc(c.advertiser)} · ${esc(c.campaign_id)}</span></td>
     <td class="n">${c.plays_per_loop}</td><td>${c.slot_types.map(t => `<span class="pill">${esc(t)}</span>`).join(' ')}${(c.doohly || []).map((b, i) => ` <a class="pill" href="${esc(b.url)}" target="_blank" rel="noopener" title="${esc(b.name)} · ${esc(b.purchase_type || '')} · ${b.creatives} creative(s)">Doohly${c.doohly.length > 1 ? ' ' + (i + 1) : ''}</a>`).join('')}${c.gaps ? ` <span class="pill" title="booked ${c.weeks} of the weeks between its first and last week; Vault doesn't say which">${c.weeks} wks in range</span>` : ''}</td>
     <td>${artCell(c, s)}</td>
     <td>${c.creatives.length ? `<span title="${esc(c.creatives.join(', '))}">${c.creatives.length === 1 ? esc(c.creatives[0]) : c.creatives.length + ' IDs'}</span>` : '<span class="muted">none</span>'}</td><td class="n">${one(c.delivered)}</td><td>${meter(c)}</td></tr>`).join('');
  const opt = (v, label, sel) => `<option value="${esc(v)}" ${sel === v ? 'selected' : ''}>${esc(label)}</option>`;
  const cname = id => (s.campaigns.find(c => c.campaign_id === id) || {}).name || id;
  const opts = (sel, cr) => (sel === '\u0000' ? opt('\u0000', 'Mixed: pick one to set them all', sel) : '') + opt('', cr.by === 'inferred' ? 'Auto: Programmatic (inferred)' : cr.art && cr.art.status === 'matched' && !cr.link ? 'Auto: ' + cname(cr.art.campaign_id) : 'Unassigned', sel) +
     opt('programmatic', 'Programmatic', sel) + opt('house', 'House', sel) +
     `<optgroup label="Booked this week">` + s.campaigns.map(c => opt(c.campaign_id, c.name, sel)).join('') + `</optgroup>` +
     (sel && sel !== 'programmatic' && sel !== 'house' && !s.campaigns.some(c => c.campaign_id === sel) ? opt(sel, sel + ' (not booked this week)', sel) : '');
  const days = rep.image_days || 3;
  const pop = cr => cr.image ? `<a href="/images/${cr.image}.roi.jpg" target="_blank"><img class="thumb" loading="lazy" src="/images/${cr.image}.roi.jpg" alt="PoP image"></a>`
     : cr.blank ? '' : cr.last_play && Date.now() / 1000 - cr.last_play > days * 86400
     ? `<span class="nophoto" title="Last seen ${new Date(cr.last_play * 1000).toLocaleString()}; photos are kept ${days} days (the best ones are in SharePoint)">photo removed<br>after ${days} days</span>`
     : '<span class="nophoto" title="No usable photo was taken of its plays">no photo</span>';
  const pick = (cr, ids) => cr.blank ? '<span class="muted">blank screen</span>'
     : `<select data-site="${esc(s.site_id)}" data-ids="${esc(ids.join(','))}">${opts(cr.mixed ? '\u0000' : cr.link, cr)}</select>
       ${cr.mixed ? '<span class="pill">mixed</span>' : artPill(cr, s, cname)}${cr.assigned === 'unassigned' ? ' <span class="pill warn">unassigned</span>' : ''}${cr.off_schedule && !(cr.art || {}).off_schedule ? ' <span class="pill warn">off schedule</span>' : ''}`;
  const groups = s.groups || [];
  // seen once or twice, nothing linked or matched: mostly moments caught mid-change
  const minor = g => !g.blank && g.plays <= (rep.minor_plays ?? 2) && !g.link && !g.mixed && g.assigned === 'unassigned' &&
     !(g.art && g.art.status === 'matched') && g.share < 0.05;
  const row = g => {
    const ids = g.members.map(m => m.creative_id), many = ids.length > 1, open = OPEN_GROUPS.has(s.site_id + '|' + g.key);
    const nm = (g.merged || []).length, mkeyG = s.site_id + '|merged|' + g.key, mopenG = nm && OPEN_GROUPS.has(mkeyG);
    const mergedBtn = nm ? ` <button type="button" class="link" style="font-size:12px" data-group="${esc(mkeyG)}" title="IDs the gateway has merged into ${esc(ids.join(', '))} as the same artwork: ${esc(g.merged.join(', '))}. Open to check them, and separate one that was merged by mistake.">${mopenG ? '&#9662;' : '&#9656;'} +${nm} merged</button>` : '';
    const mergedRows = g => (g.merged_info || []).map(m => `<tr class="sub merged"><td>${m.image ? `<a href="/images/${m.image}.roi.jpg" target="_blank"><img class="thumb" loading="lazy" src="/images/${m.image}.roi.jpg" alt="PoP image"></a>` : '<span class="nophoto">no photo kept</span>'}</td><td></td>
     <td><span class="muted">${esc(m.creative_id)}</span> <span class="pill">merged into ${esc(m.canon)}</span><br><span class="muted" style="font-size:12px">${esc(m.why || '')}${m.at ? ' · ' + new Date(m.at * 1000).toLocaleString() : ''}</span></td>
     <td class="n" title="plays reported as ${esc(m.creative_id)} (before the merge; since then they count as ${esc(m.canon)})">${m.plays}</td><td></td>
     <td><button type="button" class="btn" data-separate="${esc(m.creative_id)}" data-site="${esc(s.site_id)}" title="Not the same artwork: give ${esc(m.creative_id)} its own ID again, here and on the gateway. Plays since the merge whose photos look like it go back to it, and it is never merged with ${esc(m.canon)} again. Then pick its campaign in its own row.">Separate</button> <span class="sepmsg muted" style="font-size:12px"></span></td></tr>`).join('');
    const label = many ? `<b>${esc(g.creative_id)}</b> <button type="button" class="link" data-group="${esc(s.site_id + '|' + g.key)}" title="${esc(ids.join(', '))}">${open ? '&#9662;' : '&#9656;'} ${ids.length} IDs</button>` : `<b>${esc(g.creative_id)}</b>`;
    const head = `<tr class="grp${open ? ' open' : ''}"><td>${pop(g)}</td><td>${refThumb(g)}</td>
     <td>${label}${mergedBtn}${g.video ? ' <span class="pill">video</span>' : ''}${g.blank ? ' <span class="pill">blank</span>' : ''}</td>
     <td class="n">${g.plays}</td><td class="n">${one(g.share)}</td><td>${pick(g, ids)}</td></tr>`;
    const subs = (many && open ? g.members.map(m => `<tr class="sub"><td>${pop(m)}</td><td></td><td class="muted">${esc(m.creative_id)}</td>
     <td class="n">${m.plays}</td><td class="n">${one(m.share)}</td><td>${pick(m, [m.creative_id])}</td></tr>`).join('') : '');
    return head + subs + (mopenG ? mergedRows(g) : '');
  };
  const few = groups.filter(minor), mkey = s.site_id + '|minor', mopen = OPEN_GROUPS.has(mkey);
  const crs = groups.filter(g => !minor(g)).map(row).join('') + (few.length ? `<tr class="grp minor${mopen ? ' open' : ''}"><td colspan="6">
     <button type="button" class="link" data-group="${esc(mkey)}">${mopen ? '&#9662;' : '&#9656;'} ${few.length} creative(s) seen only ${(rep.minor_plays ?? 2) > 1 ? 'once or twice' : 'once'} this week</button>
     <span class="muted">${few.reduce((n, g) => n + g.plays, 0)} play(s) in all, nothing linked or matched: mostly moments caught mid-change, or a creative that has since stopped.</span></td></tr>` +
     (mopen ? few.map(row).join('') : '') : '');
  const nIds = groups.reduce((n, g) => n + g.members.length, 0);
  const tc = s.true_coverage, tcls = tc == null ? '' : tc >= 0.9 ? 'ok' : tc >= 0.75 ? 'warn' : 'bad';
  const due = s.booked_due_per_loop ?? s.booked_per_loop;
  const tsub = s.booked_linked_per_loop ? (s.booked_linked_per_loop < due - 0.05
       ? `of ${one(s.booked_linked_per_loop)} linked booked slot(s) so far; ${one(due - s.booked_linked_per_loop)} not linked yet` : 'booked slots delivered')
       : 'link creatives to campaigns to measure';
  const notSeen = s.campaigns.filter(c => c.status === 'not seen').length;
  const chip = (k, v, cls) => `<span class="chip ${cls || ''}"><span class="muted">${k}</span> ${v}</span>`;
  const same = (a, b) => (a || '').toLowerCase().replace(/^lumo[\s_-]*/, '').trim() === (b || '').toLowerCase().trim();
  const day = v => { const m = /^(\d{4}-\d{2}-\d{2})/.exec(v || ''); return m ? new Date(m[1] + 'T00:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short'}) : (v || ''); };
  return `<details class="site" data-site="${esc(s.site_id)}"${OPEN_SITES.has(s.site_id) ? ' open' : ''}><summary>
     <span class="sl"><span class="sname"><a href="/?site=${encodeURIComponent(s.site_id)}">${esc(s.name)}</a></span>
       <span class="muted">${same(s.name, s.vault_screen) ? '' : esc(s.vault_screen) + ' · '}${s.slot_count} × ${s.slot_s} s</span></span>
     <span class="chips">
       <span class="cell">${s.doohly ? `<span class="chip ${s.doohly.online ? 'ok' : 'bad'}" title="Doohly player ${esc(s.doohly.player)}, last seen ${esc(s.doohly.alive_at || '?')}; bookings read ${esc(s.doohly.bookings_at || '?')}">Player ${s.doohly.online ? 'online' : 'offline'}</span>` : ''}</span>
       <span class="cell">${s.setup ? (s.setup.now ? `<a class="chip warn" href="/roi?site=${encodeURIComponent(s.site_id)}" title="Being set up since ${esc(s.setup.from)}: its plays aren't counted until it goes live on the Region page">In setup</a>`
         : `<span class="chip" title="Was being set up ${esc(s.setup.from)} to ${esc(s.setup.to)}: plays in that time aren't counted">Live from ${esc(day(s.setup.to))}</span>`) : ''}</span>
       <span class="cell">${chip('True coverage', pct(tc), tcls)}</span>
       <span class="cell"><span title="${one(s.booked_per_loop ?? 0)} of ${s.slot_count} slots per loop booked this week">${chip('Booked', s.slot_count ? pct((s.booked_per_loop || 0) / s.slot_count) : '—', (s.booked_per_loop || 0) > s.slot_count ? 'warn' : '')}</span></span>
       <span class="cell">${s.unassigned_per_loop > 0.05 ? `<span title="${one(s.unassigned_per_loop)} slot(s) per loop not assigned to a campaign, programmatic or house">${chip('Unassigned', one(s.unassigned_per_loop), 'warn')}</span>` : ''}</span>
       <span class="cell">${notSeen ? chip('Not seen', notSeen, 'bad') : ''}</span>
       <span class="cell">${s.warnings.length ? chip('', s.warnings.length + ' warning' + (s.warnings.length === 1 ? '' : 's'), 'warn') : ''}</span></span></summary>
     ${s.setup && s.setup.now ? `<p class="muted">Being set up since ${esc(s.setup.from)}: its plays aren't counted here (or used for PoP photos) until it goes live. <a href="/roi?site=${encodeURIComponent(s.site_id)}">Region page</a></p>` : ''}
     ${s.warnings.length ? '<ul class="w">' + s.warnings.map(w => `<li>${esc(w)}</li>`).join('') + '</ul>' : ''}
     <div class="tiles">
       ${tile('True coverage', pct(tc), tsub, tcls)}
       ${tile('Booked per loop', `${one(s.booked_per_loop ?? 0)} / ${s.slot_count}`, `${s.campaigns.filter(c => c.certain).length} campaign(s)` + (s.campaigns.some(c => !c.certain) ? ` + ${s.campaigns.filter(c => !c.certain).length} ${s.doohly ? 'bonus if room' : 'maybe'}` : '') + (s.unsold_per_loop ? `, ${one(s.unsold_per_loop)} unsold` : ''))}
       ${tile('Programmatic', one(s.programmatic_per_loop), 'slots per loop' + (s.programmatic_room != null ? ` · ${one(s.programmatic_room)} unbooked in Doohly` : '') +
         (s.programmatic_inference && s.programmatic_inference.on ? ` · ${s.programmatic_inference.creatives.length} creative(s) inferred` :
          s.programmatic_inference && s.programmatic_inference.why_not ? ` · <span title="${esc(s.programmatic_inference.why_not)}" style="text-decoration:underline dotted">not inferred</span>` : ''))}
       ${tile('House', one(s.house_per_loop), 'slots per loop' + (s.unbooked_per_loop > 0.05 ? ` · plus ${one(s.unbooked_per_loop)} from campaigns not booked this week` : ''))}
       ${tile('Unassigned', one(s.unassigned_per_loop), 'slots per loop, not yet marked', s.unassigned_per_loop > 0.05 ? 'warn' : '')}
       ${tile('Blank', one(s.blank_per_loop), 'slots per loop', s.blank_per_loop > 0.2 ? 'warn' : '')}
       ${tile('Monitored', hrs(s.seen_s), `of ${hrs(s.elapsed_s)} this week so far · ${Math.floor(s.loops_seen || 0)} loops`)}
     </div>
     <h3>Booked campaigns</h3>
     <div class="wrap"><table><thead><tr><th>Campaign</th><th class="n">Booked / loop</th><th>Slot type</th><th>Artwork</th><th>Creatives</th><th class="n">Seen / loop</th><th>Delivery</th></tr></thead>
     <tbody>${camp || '<tr><td colspan="7" class="muted">Nothing booked on this screen this week ' + (s.doohly ? 'in Doohly' : 'in the Vault pull') + '.</td></tr>'}</tbody></table></div>
     <h3>Creatives seen <span class="muted" style="font-weight:400">${groups.length} artwork(s)${nIds > groups.length ? `, from ${nIds} gateway IDs` : ''}</span></h3>
     <div class="wrap"><table><thead><tr><th>PoP</th><th>Booked artwork</th><th>Creative</th><th class="n">Plays</th><th class="n">Slots / loop</th><th>Campaign</th></tr></thead>
     <tbody>${crs || '<tr><td colspan="6" class="muted">No plays recorded this week.</td></tr>'}</tbody></table></div></details>`;
}
async function load() {
  const r = await fetch('/bookings.json' + (week ? '?week=' + week : ''));
  const rep = await r.json();
  week = rep.week;
  document.getElementById('wk').textContent = 'Week of ' + new Date(rep.week + 'T00:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'});
  const dh = rep.doohly ? (rep.doohly.ok === false ? `<span class="bad">Doohly: ${esc(rep.doohly.error || 'failed')}</span>`
      : `Bookings from Doohly (${esc(rep.doohly.at)}, ${rep.doohly.bookings} booking(s) on ${rep.doohly.sites} screen(s))` +
        (rep.doohly.unmatched.length ? ` · <span class="warn" title="${esc(rep.doohly.unmatched.map(u => u.site_id + ': ' + u.looked_for + ' (' + u.why + ')').join('; '))}">${rep.doohly.unmatched.length} site(s) not found in Doohly</span>` : '')) : '';
  // the Vault pull is only mentioned when a screen without a Doohly player uses it
  const vault = !rep.vault_used ? '' : rep.pulled_at
    ? `${rep.doohly ? 'screens without a Doohly player from the ' : ''}Vault pull of ${esc(rep.pulled_at)} · ${rep.campaigns} booked campaign(s)` + (rep.source ? ` · ${esc(rep.source)}` : '')
    : '<span class="warn">No Vault pull yet for the screens without a Doohly player. The weekly scheduled task writes one, or upload a file.</span>';
  const lib = rep.creatives ? `artwork library of ${esc(rep.creatives.pulled_at)}, ${rep.creatives.count} file(s)` : '<span class="warn">no artwork library yet</span>';
  document.getElementById('sub').innerHTML = [dh, vault, lib,
    rep.artwork_enabled === false ? '<span class="warn">artwork matching unavailable (needs numpy and Pillow)</span>' : ''].filter(Boolean).join(' · ');
  last = rep;
  render();
}
let last = null;
function render() {
  const rep = last; if (!rep) return;
  document.getElementById('out').innerHTML = !rep.gateway ? '<p class="bad">Cannot reach the gateway, so the sites are unknown.</p>'
    : rep.sites.map(s => site(s, rep)).join('') || '<p class="muted">No sites.</p>';
  document.getElementById('built').textContent = rep.built_at ? `Worked out ${rep.built_at.split(' ')[1] || rep.built_at}` : '';
}
function shift(days) {   // in UTC: a local midnight is the previous day in UTC east of Greenwich (NZ)
  const d = new Date(week + 'T00:00Z'); d.setUTCDate(d.getUTCDate() + days); week = d.toISOString().slice(0, 10);
  history.replaceState(null, '', '?week=' + week); load(); }
document.getElementById('prev').onclick = () => shift(-7);
document.getElementById('next').onclick = () => shift(7);
document.getElementById('out').addEventListener('change', async e => {
  const s = e.target.closest('select[data-ids]'); if (!s || s.value === '\u0000') return;
  s.disabled = true;
  await fetch('/bookings/link', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({site_id: s.dataset.site, creative_ids: s.dataset.ids.split(','), campaign_id: s.value})});
  load();
});
document.getElementById('out').addEventListener('toggle', e => {      // remember which sites are open
  const d = e.target; if (!d.matches || !d.matches('details.site')) return;
  if (d.open) OPEN_SITES.add(d.dataset.site); else OPEN_SITES.delete(d.dataset.site);
  remember('pop-bookings-open', OPEN_SITES);
}, true);
document.getElementById('out').addEventListener('click', async e => {   // separate a merged ID (asks once)
  const b = e.target.closest('button[data-separate]'); if (!b) return;
  const msg = b.parentElement.querySelector('.sepmsg');
  if (!b.dataset.sure) {
    b.dataset.sure = '1'; b.textContent = 'Separate ' + b.dataset.separate + '?'; b.classList.add('danger');
    msg.textContent = 'click again to confirm';
    setTimeout(() => { if (b.isConnected && !b.disabled) { delete b.dataset.sure; b.textContent = 'Separate'; b.classList.remove('danger'); msg.textContent = ''; } }, 5000);
    return;
  }
  b.disabled = true; msg.textContent = 'separating…';
  const r = await fetch('/bookings/link', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({site_id: b.dataset.site, separate: b.dataset.separate})}).then(r => r.json()).catch(e => ({error: String(e)}));
  if (r.error) { b.disabled = false; msg.textContent = r.error; return; }
  msg.textContent = `separated; ${r.plays_moved} play(s) given back`;
  load();
});
document.getElementById('out').addEventListener('click', e => {       // show / hide a row's gateway IDs
  const b = e.target.closest('button[data-group]'); if (!b) return;
  const k = b.dataset.group;
  if (OPEN_GROUPS.has(k)) OPEN_GROUPS.delete(k); else OPEN_GROUPS.add(k);
  remember('pop-bookings-groups', OPEN_GROUPS);
  render();
});
document.getElementById('all').onclick = () => {
  const ds = [...document.querySelectorAll('details.site')], open = !ds.every(d => d.open);
  ds.forEach(d => { d.open = open; });
};
// ---- bookings without artwork: what should be there, where, and uploading it
let missing = [], missWatch = false;
async function openMissing() {
  const d = document.getElementById('missdlg');
  document.getElementById('misslist').innerHTML = '<p class="muted">Loading&hellip;</p>';
  if (!d.open) d.showModal();
  const r = await fetch('/bookings/missing').then(r => r.json()).catch(e => ({error: String(e)}));
  missing = r.items || [];
  const full = missing.filter(m => !m.partial).length, part = missing.length - full;
  document.getElementById('misssub').textContent = r.error ? r.error :
    `${full} booking(s) with no artwork and ${part} with some missing, on the monitored screens, from the artwork sync of ${r.pulled_at || '?'}. ` +
    'A file uploaded here goes into the campaign\'s folder in SharePoint (a new one if it has none), then the artwork sync runs again.';
  const by = {};
  missing.forEach((m, i) => (by[m.campaign_id] = by[m.campaign_id] || []).push([m, i]));
  const sz = m => m.size ? `${m.size[0]} × ${m.size[1]} px` : 'size not known';
  const dates = m => m.from ? `${fmtd(m.from)}–${fmtd(m.to)}` : '';
  document.getElementById('misslist').innerHTML = Object.values(by).map(list => {
    const m0 = list[0][0];
    return `<div class="mcamp"><h3>${esc(m0.campaign_name || m0.campaign_id)}</h3>
      <div class="muted">${esc([m0.advertiser, m0.campaign_id.startsWith('D:') ? 'booked in Doohly without a LUMO number' : m0.campaign_id].filter(Boolean).join(' · '))}</div>` +
      list.map(([m, i]) => {
        const want = ((m.not_found && m.not_found.length ? m.not_found : m.partial ? [] : m.expected) || []).filter(n => !/\.zip$/i.test(n));
        const zips = m.dco_missing || m.dco || [];
        const where = m.folder ? `in <a href="${esc(m.folder.url)}" target="_blank" rel="noopener"><code class="f">${esc(m.folder.path)}</code></a>`
          : m.new_folder ? `in a new campaign folder, made when you upload: <code class="f">${esc(m.new_folder.path)}</code>`
          : m.campaign_id.startsWith('D:') ? '<span class="muted">nowhere until its folder is chosen below (with no LUMO number it gets no folder of its own)</span>'
          : '<span class="bad">nowhere yet: the campaign has no folder and this month\'s folder wasn\'t found to make one in</span>';
        const up = (m.folder || m.new_folder) && r.can_upload;
        return `<div class="msite"><b>${esc(m.site_name || m.screen)}</b>
          <span class="pill ${m.partial ? '' : 'warn'}">${m.partial ? 'some missing' : 'no artwork'}</span>
          <span class="muted">${esc([dates(m), 'screen ' + sz(m)].filter(Boolean).join(' · '))}</span>
          <div class="muted" style="font-size:12px">${esc(m.reason || '')}</div>
          ${m.dco && m.dco.length ? `<div style="font-size:13px;margin-top:4px"><span class="pill">DCO</span> Doohly plays ${m.dco.map(n => `<code class="f">${esc(n)}</code>`).join(', ')} (HTML). Its renders are looked for in LUMO Labs (a folder with the campaign's LUMO_ number or MI in it, or named for the advertiser, with a subfolder named for the screen). Or download the zip from Doohly and upload it here: the video in it (an animated ad) or the picture (a static one) goes into the campaign folder under the zip's name and is matched from then on. A live ad with neither: upload a render or screenshot with the zip's button instead.</div>` : ''}
          ${want.length ? `<div style="font-size:13px;margin-top:4px">Doohly plays (not in SharePoint):</div><ul>${want.map(n => `<li><code class="f">${esc(n)}</code></li>`).join('')}</ul>` :
            `<div style="font-size:13px;margin-top:4px">Should be: the campaign's artwork for this screen, ${esc(sz(m))}. Name the file with the screen's name (e.g. <i>${esc(m.screen)}</i>), or make it exactly that size, or name it in the Material Instructions.</div>`}
          <div style="font-size:13px">Goes ${where}</div>
          ${m.folder_guessed ? `<div class="muted" style="font-size:12px">No folder has its number; this one was found by the advertiser's name: <code class="f">${esc(m.folder_guessed)}</code> (only files named as Doohly plays them are used from it).</div>` : ''}
          ${m.folder_linked ? `<div class="muted" style="font-size:12px">Folder set by hand: <code class="f">${esc(m.folder_linked)}</code> <button type="button" class="link" data-unfolder="${i}">undo</button></div>` : ''}
          ${m.pick_folder && r.can_upload ? `<div class="mfold" style="margin-top:6px;font-size:13px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">${m.folder_guessed ? 'Not the right folder?' : m.campaign_id.startsWith('D:') ? 'Where is its artwork?' : 'Its folder is under another number or name?'}
            <button type="button" class="btn" data-browse="${i}">Choose its folder&hellip;</button>
            ${(m.folder_choices || []).length ? '<span class="muted">or one that looks right:</span>' : ''}${(m.folder_choices || []).map(f => `<button type="button" class="btn" data-folder="${i}" data-id="${esc(f.id)}" title="Use ${esc(f.path)}">${esc(f.name)}</button>`).join('')}
            <span class="fstat muted" style="font-size:12px"></span></div>` : ''}
          ${(m.folder_files || []).length && r.can_upload ? `<div class="mpick" style="margin-top:6px;font-size:13px">Or match against a file already in the campaign folder (it isn't renamed):
            <select data-i="${i}" style="max-width:100%"><option value="">Choose a file&hellip;</option>${m.folder_files.map(f => `<option value="${esc(f.name)}">${esc((f.path ? f.path + '/' : '') + f.name)}${f.size ? ` (${f.size[0]}×${f.size[1]})` : ''}</option>`).join('')}</select>
            <button type="button" class="btn" data-pick="${i}">Use this file</button></div>` : ''}
          ${up ? `<div class="mup">` +
            zips.map(n => `<label class="btn" style="cursor:pointer" title="The zip from Doohly, or a picture of it">Upload ${esc(n)}<input type="file" hidden data-i="${i}" data-name="${esc(n)}" data-zip="1" accept=".zip,application/zip,image/*,video/*"></label>`).join('') +
            want.map(n => `<label class="btn" style="cursor:pointer">Upload as ${esc(n)}<input type="file" hidden data-i="${i}" data-name="${esc(n)}" accept="image/*,video/*"></label>`).join('') +
            `<label class="btn" style="cursor:pointer">${want.length ? 'Upload another file' : 'Upload artwork'}<input type="file" hidden data-i="${i}" accept="image/*,video/*"></label>
             <span class="mstat muted"></span></div>` : ''}
        </div>`;
      }).join('') + '</div>';
  }).join('') || '<p class="muted">Nothing missing on the monitored screens.</p>';
}
document.getElementById('missclose').onclick = () => document.getElementById('missdlg').close();
document.addEventListener('click', e => { if (e.target.closest('[data-missing]')) { e.preventDefault(); openMissing(); } });
document.getElementById('misslist').addEventListener('click', async e => {   // match against a file in the folder
  const b = e.target.closest('button[data-pick]'); if (!b) return;
  const box = b.closest('.msite'), sel = box.querySelector('.mpick select'), stat = box.querySelector('.mstat') || b;
  const m = missing[+b.dataset.pick];
  if (!sel.value) { sel.focus(); return; }
  b.disabled = true;
  const r = await fetch('/bookings/artwork-pick', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({campaign_id: m.campaign_id, screen: m.screen, names: [sel.value]})});
  const j = await r.json().catch(() => ({}));
  b.disabled = false;
  const out = box.querySelector('.mstat');
  if (!r.ok) { if (out) { out.className = 'mstat bad'; out.textContent = 'Not saved: ' + (j.error || r.status); } return; }
  if (out) { out.className = 'mstat ok'; out.textContent = `${sel.value} will be matched against for ${m.site_name || m.screen}. The artwork sync for this screen is running; this list and the page update when it finishes.`; }
  missWatch = true; loadSync();
});
document.getElementById('misslist').addEventListener('click', async e => {   // a campaign's folder, by hand
  const b = e.target.closest('button[data-folder],button[data-unfolder]'); if (!b) return;
  const box = b.closest('.msite'), m = missing[+(b.dataset.folder ?? b.dataset.unfolder)];
  const id = b.dataset.folder !== undefined ? b.dataset.id : '';
  if (b.dataset.folder !== undefined && !id) return;
  await useFolder(m, id, box, b);
});
async function useFolder(m, id, box, b) {
  if (b) b.disabled = true;
  const r = await fetch('/bookings/artwork-folder', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({campaign_id: m.campaign_id, folder_id: id})});
  const j = await r.json().catch(() => ({}));
  b.disabled = false;
  const st = box.querySelector('.fstat') || box.querySelector('.mstat');
  if (st) { st.className = r.ok ? 'fstat ok' : 'fstat bad'; st.textContent = r.ok ? (id ? `Using ${j.folder.path} for ${m.campaign_id}. ` : 'Undone. ') + 'The artwork sync is running; this list updates when it finishes.' : 'Not saved: ' + (j.error || r.status); }
  if (r.ok) { missWatch = true; loadSync(); }
  return r.ok;
}
// ---- choosing a campaign's folder, like choosing where a download goes
let fpick = null;                         // {m, box, cur}
async function fopen(id) {
  const list = document.getElementById('flist'), path = document.getElementById('fpath');
  list.innerHTML = '<p class="muted">Loading&hellip;</p>';
  const r = await fetch('/bookings/artwork-browse' + (id ? '?id=' + encodeURIComponent(id) : '')).then(r => r.json()).catch(e => ({error: String(e)}));
  if (r.error) { list.innerHTML = `<p class="bad">${esc(r.error)}</p>`; return; }
  fpick.cur = r;
  path.textContent = r.root + (r.folder.path ? '/' + r.folder.path : '');
  document.getElementById('fup').disabled = !r.up;
  document.getElementById('fuse').disabled = r.is_root;
  document.getElementById('fuse').textContent = r.is_root ? 'Use this folder' : `Use "${r.folder.name}"`;
  list.innerHTML = (r.folders.length ? r.folders.map(f => `<button type="button" class="frow" data-open="${esc(f.id)}"><span class="ficon">&#128193;</span> ${esc(f.name)}${f.count != null ? ` <span class="muted">${f.count}</span>` : ''}</button>`).join('') : '<p class="muted">No folders in here.</p>') +
    (r.files.length ? `<p class="muted" style="margin:8px 0 0;font-size:12px">${r.files.length} image(s) or video(s) here: ${r.files.slice(0, 6).map(esc).join(', ')}${r.files.length > 6 ? '&hellip;' : ''}</p>` : '');
}
document.getElementById('misslist').addEventListener('click', e => {
  const b = e.target.closest('button[data-browse]'); if (!b) return;
  fpick = {m: missing[+b.dataset.browse], box: b.closest('.msite'), cur: null};
  document.getElementById('fsub').textContent = `For ${fpick.m.campaign_name || fpick.m.name || fpick.m.campaign_id}: open folders until you're in the one with its artwork, then use it.`;
  document.getElementById('fdlg').showModal();
  fopen('');
});
document.getElementById('flist').addEventListener('click', e => { const b = e.target.closest('[data-open]'); if (b) fopen(b.dataset.open); });
document.getElementById('fup').onclick = () => fpick && fpick.cur && fpick.cur.up && fopen(fpick.cur.up);
document.getElementById('fcancel').onclick = () => document.getElementById('fdlg').close();
document.getElementById('fuse').onclick = async () => {
  if (!fpick || !fpick.cur || fpick.cur.is_root) return;
  if (await useFolder(fpick.m, fpick.cur.folder.id, fpick.box, document.getElementById('fuse'))) document.getElementById('fdlg').close();
  document.getElementById('fuse').disabled = false;
};
document.getElementById('out').addEventListener('click', async e => {    // undo a file chosen by hand
  const b = e.target.closest('button[data-unpick]'); if (!b) return;
  b.disabled = true;
  const r = await fetch('/bookings/artwork-pick', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({campaign_id: b.dataset.unpick, screen: b.dataset.screen, names: []})});
  const j = await r.json().catch(() => ({}));
  b.textContent = r.ok ? 'undone; syncing…' : 'failed: ' + (j.error || r.status);
  if (r.ok) loadSync();
});
document.getElementById('misslist').addEventListener('change', async e => {
  const inp = e.target.closest('input[type=file]'); if (!inp || !inp.files.length) return;
  const m = missing[+inp.dataset.i], f = inp.files[0], stat = inp.closest('.msite').querySelector('.mstat');
  let name = inp.dataset.name || f.name;
  // .jpeg and .jpg (or .tiff and .tif) are the same kind of file: it is uploaded under Doohly's name anyway
  const ext = s => ({'.jpeg': '.jpg', '.jpe': '.jpg', '.tiff': '.tif', '.mpeg': '.mpg'})[(s.match(/\.[^.]+$/) || [''])[0].toLowerCase()]
                   || (s.match(/\.[^.]+$/) || [''])[0].toLowerCase();
  if (inp.dataset.zip && ext(f.name) !== '.zip') {      // a picture of the DCO zip: named as the zip
    if (!/^\.(jpg|png|mp4|mov|m4v|webm|avi|mpg)$/.test(ext(f.name))) { stat.className = 'mstat bad'; stat.textContent = 'Upload the zip from Doohly, or a picture or video of it.'; inp.value = ''; return; }
    name = name.replace(/\.zip$/i, '') + ext(f.name);
  } else if (inp.dataset.name && ext(f.name) !== ext(name)) {
    stat.className = 'mstat bad'; stat.textContent = `Doohly plays a ${ext(name)} file; that one is ${ext(f.name) || 'of an unknown type'}.`; inp.value = ''; return;
  }
  stat.className = 'mstat muted'; stat.textContent = `Uploading ${name} (${(f.size / 1048576).toFixed(1)} MB)…`;
  const q = new URLSearchParams({campaign: m.campaign_id, screen: m.screen, name});
  let r, j = {};
  try { r = await fetch('/bookings/artwork?' + q, {method: 'POST', body: f}); j = await r.json().catch(() => ({})); }
  catch (err) { r = {ok: false}; j = {error: String(err)}; }
  inp.value = '';
  if (!r.ok) { stat.className = 'mstat bad'; stat.textContent = 'Not uploaded: ' + (j.error || r.status); return; }
  stat.className = 'mstat ok';
  stat.innerHTML = (j.from_zip ? `The ${j.kind === 'video' ? 'video' : 'picture'} in the zip (${esc(j.from_zip)}${j.size ? `, ${j.size[0]}×${j.size[1]}` : ''}) was uploaded` : 'Uploaded') +
    ` to <a href="${esc(j.url || '#')}" target="_blank" rel="noopener">${esc(j.path)}</a>. The artwork sync is running; this list and the page update when it finishes.`;
  missWatch = true; loadSync();
});
const fmtT = iso => iso ? new Date(iso).toLocaleString(undefined, {weekday: 'short', hour: 'numeric', minute: '2-digit'}) : '';
let syncTimer = null, wasRunning = false;
async function loadSync() {
  const box = document.getElementById('sync'), txt = document.getElementById('synctxt'), btn = document.getElementById('syncnow');
  let st = {};
  try { st = await (await fetch('/bookings/sync')).json(); } catch (e) { st = {error: String(e)}; }
  if (!st.version) {                     // no creatives-sync service in this stack (or it is down)
    box.hidden = !(st.error && /cannot reach/.test(st.error));
    document.getElementById('synctxt').innerHTML = `<span class="warn">${esc(st.error || '')}</span>`;
    btn.hidden = true; return;
  }
  box.hidden = false;
  btn.hidden = st.configured === false;
  btn.disabled = !!st.running;
  let h;
  if (st.configured === false) h = '<span class="warn">Artwork sync from SharePoint isn\'t set up yet</span> (AZ_TENANT_ID, AZ_CLIENT_ID and AZ_CLIENT_SECRET; see docs/sharepoint-setup.md). Upload a creatives zip instead.';
  else if (st.running) h = `Syncing artwork from SharePoint&hellip; (started ${esc(fmtT(st.started))})`;
  else if (st.last_success) h = `Artwork synced from SharePoint ${esc(fmtT(st.last_success))}: ${st.files ?? 0} file(s) for ` +
    `${st.pairs_ok ?? 0} of ${st.pairs ?? 0} bookings on ${(st.screens || []).length} screen(s)` +
    (st.missing ? ` · <button class="link warnlink" data-missing>${st.missing} without artwork</button>` : '') +
    (st.partial ? ` · <button class="link warnlink" data-missing>${st.partial} with some artwork missing</button>` : '') +
    (st.next_refresh ? ` · next check ${esc(fmtT(st.next_refresh))}` : '');
  else h = st.configured === false ? '' : 'Artwork sync has not run yet.';
  if (st.secret_days_left != null && st.secret_days_left <= 30)
    h += ` <span class="${st.secret_days_left <= 7 ? 'bad' : 'warn'}">SharePoint app secret expires in ${st.secret_days_left} day(s): run setup-sharepoint-app.ps1 again.</span>`;
  if (st.configured === false) {}
  else if (st.ok === false && st.error) h += ` <span class="bad">Last sync failed ${esc(fmtT(st.last_run))}: ${esc(st.error)}</span>`;
  else if (st.error && !st.last_run) h += ` <span class="bad">${esc(st.error)}</span>`;
  const pu = st.pop_upload;               // best PoP photos into SharePoint
  const pbox = document.getElementById('popup');
  pbox.hidden = !(pu && pu.enabled && st.configured !== false);
  if (!pbox.hidden) {
    const last = r => r ? ` (${esc(r)})` : '';
    document.getElementById('poptxt').innerHTML = 'PoP photos to SharePoint: ' +
      (pu.running ? `<b>uploading the best ${esc(pu.running)} photos&hellip;</b>` :
        (pu.photos ? `${pu.photos} up` : 'none yet') +
        (pu.last_day ? ` · day ${esc(fmtT(pu.last_day))}${last(pu.last_day_result)}` : '') +
        (pu.last_night ? ` · night ${esc(fmtT(pu.last_night))}${last(pu.last_night_result)}` : '') +
        ` · next day ${esc(fmtT(pu.next_day))}, night ${esc(fmtT(pu.next_night))}`) +
      (!pu.running && pu.ok === false && pu.error ? ` <span class="bad">Last upload failed: ${esc(pu.error)}</span>` : '');
    for (const b of pbox.querySelectorAll('button')) b.disabled = !!pu.running;
    const rec = pu.recent || [];
    document.getElementById('poprecent').hidden = !rec.length;
    document.getElementById('poprecent').innerHTML = `<summary>Latest uploads (${rec.length})</summary>` +
      rec.map(r => `<div><a href="${esc(r.url || '#')}" target="_blank" rel="noopener">${esc(r.name)}</a>` +
        ` <span class="muted">${esc(fmtT(r.at))}${r.was != null ? ` · replaced a ${Math.round(r.was)}` + (r.was_now != null && Math.round(r.was_now) !== Math.round(r.was) ? `, which scored ${Math.round(r.was_now)} against today's photos` : '') : ''}</span></div>`).join('');
  }
  txt.innerHTML = h;
  clearTimeout(syncTimer);
  if (st.running || (pu && pu.running)) syncTimer = setTimeout(loadSync, 4000);
  else if (wasRunning) { load(); if (missWatch && document.getElementById('missdlg').open) { missWatch = false; openMissing(); } }
  wasRunning = !!st.running;
}
for (const b of document.querySelectorAll('#popup button')) b.onclick = async () => {
  const r = await fetch('/bookings/pop-run?phase=' + b.dataset.phase, {method: 'POST'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) alert(j.error || 'Could not start the upload');
  setTimeout(loadSync, 800);
};
document.getElementById('syncnow').onclick = async () => {
  const r = await fetch('/bookings/sync', {method: 'POST'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) alert(j.error || 'Could not start the sync');
  wasRunning = true; setTimeout(loadSync, 800);
};
loadSync();
document.getElementById('up').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const zip = /\.zip$/i.test(f.name);
  const r = await fetch(zip ? '/bookings/creatives' : '/bookings/upload', {method: 'POST',
    headers: {'Content-Type': zip ? 'application/zip' : 'application/json'}, body: zip ? await f.arrayBuffer() : await f.text()});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) alert(j.error || 'Upload failed'); else load();
};
load();
</script></body></html>'''


# Every page: a photo or artwork that fails to load (the receiver was busy, a
# connection dropped) is asked for again a few times instead of staying black.
# ---------------------------------------------------------------- the LUMO look
# Every page gets the same header (the LUMO wordmark, from Vault's logo, as a lockup like
# Vault's) and tools/static/lumo.css, which re-colours the pages' own styles.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = {"lumo.css": "text/css; charset=utf-8", "Geist-Variable.woff2": "font/woff2",
          "favicon.svg": "image/svg+xml", "Geist-OFL.txt": "text/plain; charset=utf-8"}
LUMO_LOGO = ('<svg viewBox="0 25 469 95" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="LUMO" fill="currentColor">'
             '<polygon points="0 25.6 23.1 25.6 23.1 95.7 53.8 95.7 53.8 116 0 116 0 25.6"/>'
             '<path d="M126.4,25.6l.1,48.5c.1,13.4-7.2,23.1-19.9,23.1-12.7.1-19.9-9.2-20-23l-.1-48.7h-22.3l.1,47.9'
             'c.1,32.4,23,45.9,42.5,45.9,19.5-.1,42.2-14.1,42.1-46.4l-.1-47.3h-22.4Z"/>'
             '<polygon points="270.1 25.6 244 25.6 217.7 86.8 191.3 25.6 166 25.6 166 116 187.6 116 187.6 68.8 187.6 68.3 '
             '208.1 116 227.2 116 248.4 66.7 248.5 67.9 248.5 116 270.1 116 270.1 25.6"/>'
             '<path d="M329.5,96.8c-11.7,0-21.7-10.3-21.7-24.3s9.9-24.3,21.7-24.3h92.9c11.7,0,21.7,10.3,21.7,24.3'
             's-9.9,24.3-21.7,24.3h-92.9ZM468.8,72.5c0-21.6-15.4-46.8-47.3-46.9h-91.1c-32.1,0-47.2,25.3-47.2,46.9'
             's15.4,46.8,47.4,46.9h91c31.8-.2,47.2-25.3,47.2-46.9Z"/></svg>')
LUMO_SECTIONS = (("/", "Review"), ("/sites", "Sites"), ("/bookings", "Bookings"), ("/health", "Health"))


def lumo_page(body, path):
    """Put the LUMO header, styles and favicon on a page (bytes)."""
    on = "/sites" if path in ("/sites", "/site", "/roi", "/sites/import") else path
    links = "".join(f'<a href="{h}"{" class=\"lumo-on\"" if h == on else ""}>{t}</a>' for h, t in LUMO_SECTIONS)
    header = (f'<header class="lumo-bar"><div class="lumo-in"><a class="lumo-brand" href="/" aria-label="LUMO PoP Monitor">'
              f'{LUMO_LOGO}<i class="lumo-div"></i><span class="lumo-prod">PoP Monitor</span></a>'
              f'<nav class="lumo-nav">{links}</nav></div></header>').encode()
    body = re.sub(rb"<nav\b[^>]*>.*?</nav>", b"", body, count=1, flags=re.S)
    body = re.sub(rb"(<body[^>]*>)", lambda m: m.group(1) + header, body, count=1)
    head = (b'<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">'
            b'<link rel="preload" href="/static/Geist-Variable.woff2" as="font" type="font/woff2" crossorigin>'
            b'<link rel="stylesheet" href="/static/lumo.css">')
    body = body.replace(b"</head>", head + b"</head>", 1)
    return re.sub(rb"<title>(.*?)</title>", lambda m: b"<title>" + m.group(1) + b" \xc2\xb7 LUMO PoP Monitor</title>",
                  body, count=1, flags=re.S)


IMG_RETRY = b"""<script>addEventListener('error', e => {
  const i = e.target; if (!(i instanceof HTMLImageElement)) return;
  const n = +(i.dataset.retry || 0); if (n >= 4) return; i.dataset.retry = n + 1;
  const src = i.getAttribute('src').replace(/[?&]retry=\\d+$/, '');
  setTimeout(() => { i.src = src + (src.includes('?') ? '&' : '?') + 'retry=' + (n + 1); }, 800 * (n + 1));
}, true);</script>"""

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


def name_order(name):
    """Sort key for names people read: A to Z ignoring case, numbers in number
    order ("Site 2" before "Site 10")."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", str(name or "").casefold()) if t]


def site_order(cam):
    """Sites A to Z by the name people see (display name, else site ID)."""
    return name_order(cam.get("name") or cam.get("site_name") or cam.get("id") or cam.get("site_id") or "")


def gateway_cameras():
    """Live camera status from the gateway, A to Z (None if it can't be reached)."""
    url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + "/api/cameras"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ARGS.api_key}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            cams = json.loads(r.read())
    except Exception:
        return None
    return sorted(cams, key=site_order) if isinstance(cams, list) else cams


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
    STORE.add(rec)
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


def forget_gateway(gid):
    """Take a gateway that is offline (renamed, retired, or its machine replaced) off the
    Health page and the offline warnings. Returns (code, reply)."""
    with GW_LOCK:
        g = GATEWAYS.get(gid)
        if not g:
            return 404, {"error": f"no gateway {gid}"}
        if time.time() - (g["at"] or 0) <= quiet_after(g):
            return 409, {"error": f"{gid} is online: only an offline gateway can be removed"}
        GATEWAYS.pop(gid)
    n = STORE.forget_gateway(gid)
    print(f"health: removed gateway {gid} ({n} heartbeat(s) deleted; its PoP records stay)", flush=True)
    return 200, {"removed": gid, "heartbeats": n}


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


STORAGE = {"at": 0, "data": None}


def storage_data():
    """The receiver's own storage for the Health page (photo sizes counted every 5 minutes)."""
    if STORAGE["data"] and time.time() - STORAGE["at"] < 300:
        return STORAGE["data"]
    n = size = 0
    try:
        for ent in os.scandir(os.path.join(ARGS.out, "images")):
            try:
                size += ent.stat(follow_symlinks=False).st_size
                n += 1
            except OSError:
                pass
    except FileNotFoundError:
        pass
    du = shutil.disk_usage(ARGS.out)
    d = dict(STORE.counts(), photo_files=n, photo_mb=round(size / 1e6, 1), free_gb=round(du.free / 1e9, 1),
             disk_gb=round(du.total / 1e9, 1), image_days=store.IMAGE_DAYS, alert_image_days=store.ALERT_IMAGE_DAYS,
             record_days=store.RECORD_DAYS, pinned=len(STORE.pinned()))
    d["oldest"] = local_time(iso(d["oldest"]), True) if d.get("oldest") else None
    STORAGE.update(at=time.time(), data=d)
    return d


CPU_BUSY_PCT = float(os.environ.get("POP_CPU_WARN_PCT", "85"))    # the machine's CPU, over 15 minutes
MEM_LOW_PCT = 10                    # the machine's memory available
LEAK_GROWTH = 1.25                  # the gateway's memory up this much over a day (and by 100 MB)...


def system_warnings(gid, h, now):
    """CPU and memory warnings for a gateway: the machine busy for the last 15 minutes,
    short of memory, or the gateway's own memory growing steadily over a day (a leak)."""
    out = []
    try:
        series = STORE.system_series(gid, now - 26 * 3600)
    except Exception:
        series = []
    recent = [c for t, c, _, _ in series if t >= now - 900 and c is not None]
    if len(recent) >= 5 and sum(recent) / len(recent) >= CPU_BUSY_PCT:
        out.append(f"the machine's CPU has averaged {sum(recent) / len(recent):.0f}% for 15 minutes: "
                   "video may be analysed late or frames dropped (fewer FPS)")
    m = h.get("host") or {}
    if m.get("mem_total_bytes") and m.get("mem_available_bytes") is not None and \
            m["mem_available_bytes"] < MEM_LOW_PCT / 100 * m["mem_total_bytes"]:
        out.append(f"the machine is low on memory: {m['mem_available_bytes'] / (1 << 30):.1f} GB free of "
                   f"{m['mem_total_bytes'] / (1 << 30):.1f} GB")
    rss = (h.get("process") or {}).get("rss_bytes")
    up = h.get("uptime_s") or 0
    if rss and up >= 26 * 3600:
        # the same gateway run a day ago, once warmed up (2 h after its start)
        old = [r for t, _, r, u in series if r and u and u >= 7200 and now - 26 * 3600 <= t <= now - 22 * 3600]
        if old:
            base = min(old)
            if rss > LEAK_GROWTH * base and rss - base > 100 * (1 << 20):
                out.append(f"the gateway's memory grew from {base / (1 << 20):.0f} MB to {rss / (1 << 20):.0f} MB "
                           "over the last day: if it keeps growing, restart the gateway and report it")
    return out


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
        if h.get("host") and not g["quiet"]:
            warn += system_warnings(gid, h, now)
        cams = []
        for c in sorted(hb.get("cameras", []), key=site_order):
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


# ---- bookings: Vault schedule vs what actually played ----------------------
#
# The weekly Vault pull (vault-bookings.json) lists booked campaigns with the
# screens they book: weeks and plays per loop. For each site linked to a Vault
# screen we work out, per week, what the loop should hold and compare it with
# the creatives the gateway saw: how many slots per loop each one held
# (share-of-time aware), which booked campaign it most likely is, and how much
# of each booking was delivered.

from datetime import timedelta
import math

BOOKINGS_LOCK = threading.Lock()
BOOKINGS = {"mtime": None, "data": None, "path": None}


def bookings_paths():
    paths = [os.path.join(ARGS.out, "vault-bookings.json")]
    extra = os.environ.get("BOOKINGS_FILE", "/bookings/vault-bookings.json")
    if extra:
        paths.insert(0, extra)
    return paths


def load_bookings():
    """Newest Vault pull among the mounted folder and uploads (cached by mtime)."""
    best = None
    for p in bookings_paths():
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if best is None or m > best[1]:
            best = (p, m)
    with BOOKINGS_LOCK:
        if best is None:
            BOOKINGS.update(mtime=None, data=None, path=None)
            return None
        if BOOKINGS["path"] == best[0] and BOOKINGS["mtime"] == best[1]:
            return BOOKINGS["data"]
        try:
            with open(best[0], encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("proposals"), list):
                raise ValueError("no proposals list")
        except (OSError, ValueError) as e:
            print(f"bookings: cannot read {best[0]}: {e}", flush=True)
            return BOOKINGS["data"]
        BOOKINGS.update(mtime=best[1], data=data, path=best[0])
        print(f"bookings: loaded {len(data['proposals'])} campaign(s) from {best[0]} "
              f"(pulled {data.get('generated_at', '?')})", flush=True)
        return data


def screen_key(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def monday(d):
    return d - timedelta(days=d.weekday())


def week_of(iso_date):
    return monday(datetime.fromisoformat(iso_date[:10]).date())


DOOHLY = {"mtime": None, "path": None, "data": None}
DOOHLY_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def load_doohly():
    """doohly.json from creatives-sync (what the Doohly players have booked), newest copy."""
    best = None
    for d in [x.strip() for x in os.environ.get("CREATIVES_DIR", "/bookings/creatives").split(",") if x.strip()]:
        p = os.path.join(d, "doohly.json")
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if best is None or m > best[1]:
            best = (p, m)
    if best is None:
        DOOHLY.update(mtime=None, path=None, data=None)
        return None
    if DOOHLY["path"] != best[0] or DOOHLY["mtime"] != best[1]:
        try:
            with open(best[0], encoding="utf-8") as f:
                DOOHLY.update(data=json.load(f), path=best[0], mtime=best[1])
        except (OSError, ValueError) as e:
            print(f"doohly: cannot read {best[0]}: {e}", flush=True)
    return DOOHLY["data"]


def doohly_site(site_id=None, screen=None):
    d = load_doohly() or {}
    sites = d.get("sites") or {}
    if site_id and site_id in sites:
        return sites[site_id]
    if screen:
        want = screen_key(screen)
        for s in sites.values():
            if screen_key(s.get("screen")) == want:
                return s
    return None


def site_id_for(name):
    """A site ID from a screen name: "Albany Expressway IN" -> "ALBANY-EXPRESSWAY-IN"."""
    return re.sub(r"[^A-Z0-9]+", "-", (name or "").upper()).strip("-")[:40] or "SITE"


def doohly_players():
    """Every Doohly player, for setting up sites: which ones already have a site
    (by the site's Vault screen name, or as matched by creatives-sync) and the
    settings a new site would start with."""
    d = load_doohly() or {}
    cams = gateway_cameras() or []
    by_screen = {screen_key(c.get("vault_screen")): c for c in cams if c.get("vault_screen")}
    ids = {c.get("id") for c in cams}
    out = []
    for p in d.get("players") or []:
        cam = by_screen.get(screen_key(p.get("screen")))
        sid = (cam or {}).get("id") or (p.get("site_id") if p.get("site_id") in ids else None)
        lp = p.get("loop") or {}
        out.append(dict(p, site_id=sid, site_name=(cam or {}).get("name"),
                        suggested={"site_id": site_id_for(p.get("screen")), "display_name": p.get("screen"),
                                   "vault_screen": p.get("screen"), "slot_count": lp.get("slot_count"),
                                   "slot_s": lp.get("slot_s"),
                                   "board_width": (p.get("frame") or {}).get("width"),
                                   "board_height": (p.get("frame") or {}).get("height")}))
    out.sort(key=lambda p: name_order(p.get("screen") or p.get("name")))
    return {"at": local_time(d.get("generated_at"), True), "players": out}


def _doohly_plays(b, week):
    """Plays per loop a Doohly booking adds in a week, averaged over the days it is on."""
    try:
        d0 = max(datetime.fromisoformat(b["start"]).date(), week)
        d1 = min(datetime.fromisoformat(b["end"]).date(), week + timedelta(days=6))
    except (KeyError, TypeError, ValueError):
        return 0.0
    if d1 < d0:
        return 0.0
    days = b.get("days") or DOOHLY_DAYS
    n = sum(1 for i in range((d1 - d0).days + 1) if DOOHLY_DAYS[(d0 + timedelta(days=i)).weekday()] in days)
    return (b.get("plays_per_loop") or 0) * n / 7.0


def _doohly_windows(b, week):
    """When a Doohly booking plays in a week: [(start, end)] in unix seconds, one per day it
    is on (its dates, days of the week and hours)."""
    try:
        d0 = max(datetime.fromisoformat(b["start"]).date(), week)
        d1 = min(datetime.fromisoformat(b["end"]).date(), week + timedelta(days=6))
    except (KeyError, TypeError, ValueError):
        return []
    days = b.get("days") or DOOHLY_DAYS

    def hm(v, dflt):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", (v or "").strip())
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 if m else dflt
    h0, h1 = hm(b.get("start_time"), 0), hm(b.get("end_time"), 86400)
    if h1 >= 23 * 3600 + 59 * 60:
        h1 = 86400                                    # "23:59": to the end of the day
    out = []
    for i in range((d1 - d0).days + 1):
        d = d0 + timedelta(days=i)
        if DOOHLY_DAYS[d.weekday()] not in days:
            continue
        t = datetime.combine(d, datetime.min.time(), LOCAL_TZ).timestamp()
        if h1 > h0:
            out.append((t + h0, t + h1))
        else:                                         # overnight (e.g. 18:00-02:00)
            out += [(t + h0, t + 86400), (t, t + h1)]
    return out


def doohly_booked(site, week):
    """Campaigns booked on a Doohly player in a week. The weeks are exact; a
    campaign booked only as a non-guaranteed bonus isn't certain (it plays when
    the loop has room), so it is left out of true coverage, as Vault's maybes are."""
    vault = {p.get("campaign_id"): p for p in (load_bookings() or {}).get("proposals", []) if p.get("campaign_id")}
    camps = {}
    for b in site.get("bookings") or []:
        ppl = _doohly_plays(b, week)
        if ppl <= 0:
            continue
        cid = b.get("campaign_id") or f"D:{b.get('name')}"
        v = vault.get(cid) or {}
        name = re.sub(r"^LUMO_\d+\s*-\s*", "", b.get("campaign_name") or b.get("name") or "")
        # Doohly first (the stack reads it itself); a Vault pull, if one was imported, only fills gaps
        c = camps.setdefault(cid, {"certain": False, "campaign_id": cid, "name": name or v.get("name") or cid,
                                   "advertiser": b.get("advertiser") or (v.get("advertiser") or {}).get("name", "") or "",
                                   "url": v.get("url") or b.get("url", ""), "doohly": [], "plays_per_loop": 0.0,
                                   "slot_types": [], "weeks": None, "gaps": False, "source": "doohly"})
        c["plays_per_loop"] += ppl
        c.setdefault("windows", []).extend((a, z, b.get("plays_per_loop") or 0) for a, z in _doohly_windows(b, week))
        if b.get("purchase_type") != "Non-Guaranteed Bonus":
            c["certain"] = True              # sold or guaranteed: it should play; a non-guaranteed bonus only fills room
        c["doohly"].append({"url": b.get("url"), "name": b.get("name"), "purchase_type": b.get("purchase_type"),
                            "creatives": len(b.get("creatives") or [])})
        if b.get("slot_type") and b["slot_type"] not in c["slot_types"]:
            c["slot_types"].append(b["slot_type"])
    out = list(camps.values())
    for c in out:
        c["plays_per_loop"] = round(c["plays_per_loop"], 1)
        if c["plays_per_loop"] == int(c["plays_per_loop"]):
            c["plays_per_loop"] = int(c["plays_per_loop"])
    out.sort(key=lambda c: (not c["certain"], -c["plays_per_loop"], c["name"].lower()))
    return out


def booked_for(screen, week):
    """Campaigns booked on a Vault screen in the week starting `week` (a date):
    from the screen's Doohly player when there is one, else from the Vault pull."""
    ds = doohly_site(screen=screen) if screen else None
    if ds is not None:
        return doohly_booked(ds, week)
    data = load_bookings()
    if not data or not screen:
        return []
    want = screen_key(screen)
    out = []
    for p in data["proposals"]:
        if p.get("status") not in (None, "Booked"):
            continue
        for sc in p.get("screens") or []:
            if screen_key(sc.get("name")) != want:
                continue
            try:
                a, b = week_of(sc["first_week"]), week_of(sc["last_week"])
            except (KeyError, ValueError):
                continue
            if not (a <= week <= b):
                continue
            span = (b - a).days // 7 + 1
            # Vault gives first week, last week and a count of weeks, not which
            # weeks. A booking is certain this week if it has no gaps or this is
            # its first or last week; otherwise it only may be on this week.
            weeks = sc.get("weeks") or span
            certain = weeks >= span or week in (a, b)
            out.append({"certain": certain, "campaign_id": p.get("campaign_id") or f"P{p.get('id')}",
                        "name": p.get("name", ""), "advertiser": (p.get("advertiser") or {}).get("name", ""),
                        "url": p.get("url", ""), "plays_per_loop": int(sc.get("plays_per_loop") or 0),
                        "slot_types": sc.get("slot_types") or [], "weeks": sc.get("weeks"),
                        "gaps": bool(sc.get("weeks") and sc["weeks"] < span)})
    out.sort(key=lambda c: (not c["certain"], -c["plays_per_loop"], c["name"].lower()))
    return out


def vault_screens():
    data = load_bookings()
    names = {}
    for p in (data or {}).get("proposals", []):
        for sc in p.get("screens") or []:
            if sc.get("name"):
                names.setdefault(screen_key(sc["name"]), sc["name"])
    return sorted(names.values(), key=name_order)


def iso_to_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def runs_typical(plays, S, cap):
    """Usual run length (slots) of each creative: mode of rounded durations."""
    hist = {}
    for i in range(len(plays) - 1):
        r = min(cap, max(1, math.floor((plays[i + 1][0] - plays[i][0]) / S + 0.5)))
        h = hist.setdefault(plays[i][1], {})
        h[r] = h.get(r, 0) + 1
    return {cid: min(h, key=lambda r: (-h[r], r)) for cid, h in hist.items()}


# ---------------------------------------------------------------- creative merges
# The gateway can give one artwork several creative IDs (it looks different by
# day and by night). When the receiver can tell they are the same artwork (both
# match the same booked image, or their PoP images look alike) it merges them:
# the receiver reads the older ID wherever the newer one appears, and the
# gateway is told, so the newer ID's look becomes part of the older one and
# later plays report one ID.
MERGES = {"loaded": False, "sites": {}, "apart": {}, "mtime": None}   # apart: site -> pairs never to merge
MERGE_MIN_PLAYS = 3                # each ID needs this many plays this week before it is merged
MERGE_LOOKALIKE = 0.75             # unassigned IDs: PoP images this alike (median of several) to merge


def _merges_path():
    return os.path.join(ARGS.out, "creative-merges.json")


def _changed_on_disk(state, path, every=2.0):
    """True if the file was written (by the other process: the web process and the
    report worker each keep a copy) since this process last read or wrote it.
    Checked at most every `every` seconds."""
    now = time.monotonic()
    if now - state.get("checked", 0) < every:
        return False
    state["checked"] = now
    try:
        m = os.path.getmtime(path)
    except OSError:
        return False
    return state.get("disk_mtime") is not None and m != state["disk_mtime"]


def _note_disk(state, path):
    try:
        state["disk_mtime"] = os.path.getmtime(path)
    except OSError:
        state["disk_mtime"] = 0


def merges():
    if MERGES["loaded"] and _changed_on_disk(MERGES, _merges_path()):
        MERGES["loaded"] = False
    if not MERGES["loaded"]:
        _note_disk(MERGES, _merges_path())
        try:
            with open(_merges_path(), encoding="utf-8") as f:
                d = json.load(f)
            MERGES["sites"], MERGES["apart"] = d.get("sites", {}), d.get("apart", {})
            MERGES["mtime"] = os.path.getmtime(_merges_path())
        except (OSError, ValueError):
            MERGES["sites"], MERGES["apart"] = {}, {}
        MERGES["loaded"] = True
    return MERGES["sites"]


def save_merges():
    tmp = _merges_path() + ".tmp"
    with LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sites": merges(), "apart": apart_all()}, f, indent=1)
        os.replace(tmp, _merges_path())
        _note_disk(MERGES, _merges_path())
    MERGES["mtime"] = time.time()


def apart_all():
    merges()
    return MERGES.setdefault("apart", {})


def kept_apart(site_id, a, b):
    """Two IDs separated by hand (a wrong merge undone): never merged again."""
    return sorted((a, b)) in apart_all().get(site_id, [])


def canon(site_id, cid):
    """The ID a creative ID was merged into (following chains), or itself."""
    m = merges().get(site_id)
    seen = 0
    while m and cid in m and seen < 20:
        cid = m[cid]["into"]
        seen += 1
    return cid


def setup_spans(site_id):
    """The times a site was being set up: [(start, end)], end = inf while it still is."""
    return [(a, b if b is not None else float("inf")) for a, b, _ in STORE.setup_periods().get(site_id, [])]


def in_setup(site_id):
    return any(b is None for _, b, _ in STORE.setup_periods().get(site_id, []))


def site_plays(site_id, t0, t1, setup=False):
    """A site's plays in [t0, t1), with merged IDs read as the ID they were merged into.
    Plays while the site was being set up are left out (setup=True keeps them: the
    region page's fitting and go-live checks)."""
    ps = STORE.plays(site_id, t0, t1)
    spans = [] if setup else [(a, b) for a, b in setup_spans(site_id) if b > t0 and a < t1]
    if spans:
        ps = [p for p in ps if not any(a <= p[0] < b for a, b in spans)]
    if not merges().get(site_id):
        return ps
    return [(p[0], canon(site_id, p[1])) + tuple(p[2:]) for p in ps]


def _cnum(cid):
    m = re.fullmatch(r"c-(\d+)", cid or "")
    return int(m.group(1)) if m else None


def _usable_samples(site_id, cid, t0, t1, n=3):
    ps = [p for p in site_plays(site_id, t0, t1) if p[1] == cid and not p[2] and p[5] and (len(p) < 7 or p[6])]
    return [f for f in (_pop_fp(p[4]) for p in _spread(ps, n)) if f is not None]


def _surely_alike(site_id, a, b, t0, t1):
    """Stricter than bundling for display: several good PoP images of each,
    compared pairwise, must agree (one odd image can't merge two creatives)."""
    fa, fb = _usable_samples(site_id, a, t0, t1), _usable_samples(site_id, b, t0, t1)
    if len(fa) < 2 or len(fb) < 2:
        return False
    vals = sorted(float(v) for f in fa for v in _scores(f, _np.stack(fb)))
    return vals[len(vals) // 2] >= MERGE_LOOKALIKE


def plan_merges(report):
    """Find bundles that are certainly one artwork and record them as merges:
    IDs matched to the same booked image, or unassigned lookalikes. Returns how
    many were added."""
    if os.environ.get("AUTO_MERGE", "1").lower() in ("0", "no", "false", "off") or not ART_OK:
        return 0
    links = load_links()
    added = 0
    for site in report.get("sites", []):
        sid = site["site_id"]
        for g in site.get("groups") or []:
            kind, _, rest = g["key"].partition("|")
            same_art = kind == "a" and rest.partition("|")[2]       # matched to one booked image
            if not (same_art or kind == "u") or len(g["members"]) < 2:
                continue
            ms = [m for m in g["members"] if (m.get("plays") or 0) >= MERGE_MIN_PLAYS and _cnum(m["creative_id"])]
            if len(ms) < 2:
                continue
            hand = {links.get(sid, {}).get(m["creative_id"]) for m in ms} - {None, ""}
            if len(hand) > 1:            # set by hand to different things: not ours to merge
                continue
            into = min(ms, key=lambda m: _cnum(m["creative_id"]))["creative_id"]
            ms = [m for m in ms if m["creative_id"] == into or not kept_apart(sid, canon(sid, m["creative_id"]), into)]
            if kind == "u":                  # lookalikes: check properly before merging
                ms = [m for m in ms if m["creative_id"] == into or
                      _surely_alike(sid, into, m["creative_id"], site.get("from_ts", 0), site.get("to_ts", time.time()))]
            why = (f"same booked artwork: {(g.get('art') or {}).get('name')}" if same_art
                   else "PoP images look alike")
            site_m = merges().setdefault(sid, {})
            for m in ms:
                cid = m["creative_id"]
                if cid == into or cid in site_m:
                    continue
                site_m[cid] = {"into": into, "at": time.time(), "pushed": None, "why": why}
                link = links.get(sid, {}).get(cid)
                if link:
                    site_m[cid]["link"] = link            # put back if the merge is undone
                if link and not links.get(sid, {}).get(into):
                    save_link(sid, into, link)            # keep a hand-set link on the surviving ID
                    site_m[cid]["link_moved"] = True
                if link:
                    save_link(sid, cid, "")
                st = art_state().get(sid, {})
                if st.pop(cid, None) is not None:
                    ART_STATE["dirty"] = True
                print(f"creatives: {sid} {cid} merged into {into} ({why})", flush=True)
                added += 1
    if added:
        save_merges()
        save_art_state()
        REPORTS["gen"] += 1
    return added


def push_merges():
    """Tell the gateway about merges it hasn't confirmed: new ones, and old ones
    whose merged ID still turns up in new plays (e.g. the gateway restarted
    before applying them)."""
    changed = False
    for sid, site_m in merges().items():
        plays = STORE.last_plays(sid, 500)
        recent = {p[1] for p in plays[-500:]}
        todo = [(cid, m) for cid, m in site_m.items()
                if not m.get("pushed") or (cid in recent and any(p[1] == cid and p[0] > m["pushed"] + 600 for p in plays[-500:]))]
        if not todo:
            continue
        url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + \
            f"/api/cameras/{urllib.parse.quote(sid, safe='')}/merge-creatives"
        body = {"merges": [{"from": cid, "into": canon(sid, cid)} for cid, _ in todo]}
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {ARGS.api_key}",
                                                  "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            for _, m in todo:
                m["pushed"] = time.time()
            changed = True
            print(f"creatives: told the gateway about {len(todo)} merge(s) at {sid}", flush=True)
        except Exception as e:
            print(f"creatives: could not send merges for {sid}: {e}", flush=True)
    if changed:
        save_merges()


def forget_merges(site_id):
    if merges().pop(site_id, None) is not None or apart_all().pop(site_id, None) is not None:
        save_merges()


SEPARATE_MARGIN = 0.05             # a play goes back to the separated ID when its photo is this much closer to it


def _raw_refs(site_id, cid, before=None, n=5):
    """Fingerprints of a few good photos of plays reported as exactly this ID."""
    ps = [p for p in STORE.plays(site_id, 0, before or time.time() + 60)
          if p[1] == cid and not p[2] and p[5] and (p[6] or p[7])]
    return [f for f in (_pop_fp(p[4]) for p in _spread(ps, n)) if f is not None]


def separate(site_id, cid):
    """Undo a merge that was wrong: cid gets its own ID again, here and on the gateway,
    the plays the gateway has reported as the other ID since then whose photos look
    like cid go back to cid, and the two are never merged again. Returns (code, reply)."""
    site_m = merges().get(site_id, {})
    m = site_m.get(cid)
    if not m:
        return 404, {"error": f"{cid} isn't merged into anything at this site"}
    into = canon(site_id, cid)
    # IDs merged into cid before (their looks went with it): they come back with it
    also = []
    for a in site_m:
        x, seen = a, 0
        while x in site_m and seen < 20:
            x = site_m[x]["into"]
            seen += 1
            if x == cid:
                also.append(a)
                break
    if m.get("pushed"):                       # the gateway has it: its looks go back out
        code, body = gateway_call(f"/api/cameras/{urllib.parse.quote(site_id, safe='')}/split-creative",
                                  {"from": cid, "into": into, "with": also})
        if code >= 400:
            return code, {"error": f"gateway refused: {body.get('error') or code}"}
    moved = 0
    since = m.get("pushed") or m.get("at") or 0
    if ART_OK and since:
        mine, theirs = _raw_refs(site_id, cid), _raw_refs(site_id, into, before=m.get("at"))
        if mine and theirs:
            A, B = _np.stack(mine), _np.stack(theirs)
            back = []
            for p in STORE.plays(site_id, since - 600, time.time() + 60):
                if p[1] != into or p[2] or not p[5]:
                    continue
                f = _pop_fp(p[4])
                if f is None:
                    continue
                sa, sb = float(_np.median(_scores(f, A))), float(_np.median(_scores(f, B)))
                if sa >= ART_LOOKALIKE and sa > sb + SEPARATE_MARGIN:
                    back.append(p[4])
            moved = STORE.relabel(back, cid)
    del site_m[cid]
    pair = sorted((cid, into))
    ap = apart_all().setdefault(site_id, [])
    if pair not in ap:
        ap.append(pair)
    links = load_links().get(site_id, {})
    if m.get("link_moved") and links.get(into) == m.get("link"):
        save_link(site_id, into, "")
    if m.get("link"):
        save_link(site_id, cid, m["link"])
    st = art_state().get(site_id, {})
    for c in (cid, into):
        if st.pop(c, None) is not None:
            ART_STATE["dirty"] = True
    save_merges()
    save_art_state()
    REPORTS["gen"] += 1
    print(f"creatives: {site_id} {cid} separated from {into} by hand ({moved} play(s) given back)", flush=True)
    return 200, {"separated": cid, "from": into, "with": also, "plays_moved": moved}



def observed(site_id, t0, t1, S, N):
    """What the screen showed in [t0, t1): per creative slots credited, plays, latest image."""
    plays = site_plays(site_id, t0 - N * S, t1)
    cap = max(1, N - 1) if N > 1 else 1
    typ = runs_typical(plays, S, cap)
    out, seen_s = {}, 0.0
    now = time.time()
    for i, p in enumerate(plays):
        s, e = p[0], plays[i + 1][0] if i + 1 < len(plays) else min(t1, now)
        if e <= t0 or s >= t1 or e <= s:
            continue
        r = min(cap, max(1, math.floor((e - s) / S + 0.5)))
        r = min(r, typ.get(p[1], 1))
        frac = (min(e, t1) - max(s, t0)) / (e - s)
        c = out.setdefault(p[1], {"creative_id": p[1], "slots": 0.0, "plays": 0, "blank": p[2], "video": p[3],
                                  "image": None, "last": 0, "last_play": 0})
        c["slots"] += r * frac
        seen_s += r * frac * S
        if s >= t0:
            c["plays"] += 1
            c["last_play"] = max(c["last_play"], s)
        if p[5] and s > c["last"]:
            c["image"], c["last"] = p[4], s
    return list(out.values()), seen_s


def load_links():
    try:
        with open(os.path.join(ARGS.out, "creative-links.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_link(site_id, creative_ids, campaign_id):
    """Link one creative ID or a list of them (a bundled row) to a campaign,
    programmatic or house; an empty campaign_id removes the links."""
    if isinstance(creative_ids, str):
        creative_ids = [creative_ids]
    with LOCK:
        links = load_links()
        site = links.setdefault(site_id, {})
        for creative_id in creative_ids:
            if campaign_id:
                site[creative_id] = campaign_id
            else:
                site.pop(creative_id, None)
        REPORTS["gen"] += 1
        tmp = os.path.join(ARGS.out, "creative-links.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(links, f, indent=1)
        os.replace(tmp, os.path.join(ARGS.out, "creative-links.json"))


LINK_KINDS = ("programmatic", "house")


def match(creatives, campaigns, links, art=None):
    """Assign each creative: a link someone set on the Bookings page (a campaign,
    programmatic or house) wins; otherwise a confident match of its PoP images
    to a booked creative's artwork; otherwise it stays unassigned. Nothing is
    guessed from share of the loop. Returns {creative_id: assignment}, where an
    assignment is a booked campaign_id, "programmatic", "house", "unbooked"
    (linked to a campaign not booked this week) or "unassigned"."""
    booked = {c["campaign_id"] for c in campaigns}
    art = art or {}
    out = {}
    for cr in creatives:
        cid = links.get(cr["creative_id"])
        a = art.get(cr["creative_id"])
        if not cid and a and a["status"] == "matched":
            cid = a["campaign_id"]
        if not cid:
            out[cr["creative_id"]] = "unassigned"
        elif cid in LINK_KINDS or cid in booked:
            out[cr["creative_id"]] = cid
        else:
            out[cr["creative_id"]] = "unbooked"
    return out



PROG_MIN_SAMPLES = 3       # PoP images compared before a creative can be called programmatic
PROG_MIN_PLAYS = 3
# A creative that only faintly resembles a booked campaign ("looks like X?") is still
# programmatic when X is already fully delivered here by the creatives matched to it, or
# when X isn't booked here this week and the resemblance is clearly weak: programmatic
# ads often look a little like something (the MetService ad scores ~0.30 against unrelated
# artwork). A closer look-alike of a campaign not booked here (an overrun) stays unassigned.
PROG_WEAK_UNBOOKED = 0.35
PROG_DELIVERED = 0.95          # X counts as fully delivered at this share of its booked slots


def infer_programmatic(ds, week, crs, assigned, art, library, campaigns=None):
    """Creatives that are almost certainly programmatic: the screen's loop fills its
    unbooked slots with programmatic (Doohly), Doohly says exactly what is booked,
    every booked image has its artwork here (or a creative linked to it by hand), and
    the creative matches none of it (a faint look-alike of a campaign that is already
    delivered, or isn't booked here, counts as none). Returns (set of creative IDs,
    reason it can't be done or "")."""
    if not ART_OK:
        return set(), "artwork matching is unavailable"
    lp = ds.get("loop") or {}
    if lp and lp.get("programmatic") is False:
        return set(), "this screen's Doohly loop has no programmatic"
    w0, w1 = week.isoformat(), (week + timedelta(days=6)).isoformat()
    live = [b for b in ds.get("bookings") or [] if (b.get("start") or "9") <= w1 and (b.get("end") or "0") >= w0]
    have = {x.get("campaign_id") for x in library if x.get("file")}
    taken = set(assigned.values())
    video_block = False
    for b in live:
        # a booking with no LUMO number (made in Doohly only) has the ID the page gives it
        cid, kinds = b.get("campaign_id") or f"D:{b.get('name')}", {c.get("type") for c in b.get("creatives") or []}
        if "image" in kinds and cid not in have and cid not in taken:
            return set(), (f"the artwork for {b.get('campaign_name') or cid} isn't in the library, so its plays could be "
                           "mistaken for programmatic: upload it, or link its creative to the campaign below")
        if "html" in kinds and cid not in taken and not any(
                x.get("campaign_id") == cid and x.get("dco") and x.get("file") for x in library):
            # live web content with no renders of its variants: needs a creative linked first
            return set(), (f"{b.get('campaign_name') or cid} is booked with live web content (DCO) and no renders of it "
                           "were found in LUMO Labs: link its creative to the campaign below and the rest can be worked out")
        if "video" in kinds and cid not in taken and not any(
                x.get("campaign_id") == cid and x.get("kind") == "video" and x.get("frames") for x in library):
            video_block = True                       # a booked video with no frames to compare with
    booked = {c["campaign_id"]: c.get("plays_per_loop") or 0 for c in campaigns or []}
    delivered = {}
    for cr in crs:
        a = assigned.get(cr["creative_id"])
        if a in booked:
            delivered[a] = delivered.get(a, 0) + (cr.get("share") or 0)

    def faint(a):
        """A "looks like X?" that isn't X."""
        x, sc = a.get("campaign_id"), a.get("score") or 0
        if campaigns is None or not x:
            return False
        if x in booked:
            return booked[x] > 0 and delivered.get(x, 0) >= PROG_DELIVERED * booked[x]
        return sc < PROG_WEAK_UNBOOKED
    out = set()
    for cr in crs:
        a = art.get(cr["creative_id"]) or {}
        if cr["blank"] or assigned.get(cr["creative_id"]) != "unassigned":
            continue
        if not (a.get("status") == "none" or (a.get("status") == "possible" and faint(a))):
            continue
        if (a.get("samples") or 0) < PROG_MIN_SAMPLES or cr["plays"] < PROG_MIN_PLAYS:
            continue
        if cr.get("video") and video_block:
            continue
        out.add(cr["creative_id"])
    return out, ""


# ---------------------------------------------------------------- artwork matching
# The creatives booked on each screen (pulled from SharePoint's Creative+MIs folders
# into vault-creatives.json + images) are compared with the PoP images of each
# creative the gateway has seen. Needs numpy and Pillow; without them the Bookings
# page works as before, with links set by hand only.
try:
    import numpy as _np
    from PIL import Image as _Image, ImageFilter as _ImageFilter
    ART_OK = True
except ImportError:                                   # pragma: no cover
    ART_OK = False

ART_GW, ART_GH = 48, 32          # fingerprint grid
ART_MATCH = 0.40                 # fine-detail correlation that counts as a match
ART_MARGIN = 0.08                # ...and by this much over any other campaign
ART_POSSIBLE = 0.30              # shown as "looks like" below a match
# a video frame is compared with a photo of whatever moment played (motion blur, a
# frame between two references), so a video match needs less agreement
ART_MATCH_VIDEO = float(os.environ.get("ART_MATCH_VIDEO", "0.36"))
ART_MARGIN_VIDEO = float(os.environ.get("ART_MARGIN_VIDEO", "0.08"))
ART_SAMPLES = 5                  # PoP images per creative compared
# A photo the gateway called "blown out" (white artwork, low sun) is still compared when
# there aren't enough clean photos of the creative: the fine detail it keeps (text, logos,
# edges) still says which artwork it is, and the blown-out parts are mostly the
# artwork's own blank white. The thresholds and the margin over other campaigns are the
# same, so it only makes a match sooner, not a different one.
CRE = {"mtime": None, "path": None, "data": None, "root": None}
CRE_LOCK = threading.Lock()
ART_CACHE = {}                   # (path, mtime, inset) or event id -> prepared arrays
ART_RESULTS = {}                 # memo of per-creative results


def creatives_paths():
    """(manifest, image root) candidates: the folders in CREATIVES_DIR (comma
    separated: the creatives-sync library, a hand-made pull), then an upload.
    The newest manifest wins."""
    mounted = [d.strip() for d in os.environ.get("CREATIVES_DIR", "/bookings/creatives").split(",") if d.strip()]
    up = os.path.join(ARGS.out, "creatives")
    return [(os.path.join(d, "vault-creatives.json"), d) for d in mounted + [up]]


def load_creatives():
    best = None
    for path, root in creatives_paths():
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if best is None or m > best[2]:
            best = (path, root, m)
    with CRE_LOCK:
        if best is None:
            CRE.update(mtime=None, path=None, data=None, root=None)
            return None
        if CRE["path"] == best[0] and CRE["mtime"] == best[2]:
            return CRE["data"]
        try:
            with open(best[0], encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("creatives"), list):
                raise ValueError("no creatives list")
        except (OSError, ValueError) as e:
            print(f"creatives: cannot read {best[0]}: {e}", flush=True)
            return CRE["data"]
        CRE.update(mtime=best[2], path=best[0], data=data, root=best[1])
        ART_RESULTS.clear()
        print(f"creatives: loaded {len(data['creatives'])} creative file(s) from {best[0]}", flush=True)
        return data


def install_creatives_zip(raw):
    """Unpack an uploaded creatives library (vault-creatives.json + images) into
    the data folder, replacing the previous upload. Returns the creative count."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("not a zip file")
    names = [n for n in z.namelist() if not n.endswith("/")]
    man = [n for n in names if n.rsplit("/", 1)[-1] == "vault-creatives.json"]
    if not man:
        raise ValueError("the zip has no vault-creatives.json")
    base = man[0][: -len("vault-creatives.json")]          # the folder the manifest is in
    try:
        data = json.loads(z.read(man[0]))
    except ValueError:
        raise ValueError("vault-creatives.json is not valid JSON")
    if not isinstance(data.get("creatives"), list):
        raise ValueError("vault-creatives.json has no creatives list")
    dest = os.path.join(ARGS.out, "creatives")
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    ok_ext = (".json", ".jpg", ".jpeg", ".png")
    for n in names:
        if not n.startswith(base) or not n.lower().endswith(ok_ext):
            continue
        rel = n[len(base):]
        target = os.path.realpath(os.path.join(tmp, rel))
        if not target.startswith(os.path.realpath(tmp) + os.sep):
            raise ValueError(f"unsafe path in zip: {n}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(n) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(dest):
        os.replace(dest, old)
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)
    os.utime(os.path.join(dest, "vault-creatives.json"))  # newest library wins
    load_creatives()
    return len(data["creatives"])


def creative_file(rel):
    """Absolute path of a manifest image, refusing anything outside the library."""
    root = CRE.get("root")
    if not root or not rel:
        return None
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full):
        return None
    return full


def screen_creatives(screen):
    data = load_creatives()
    if not data or not screen:
        return []
    want = screen_key(screen)
    return [c for c in data["creatives"] if screen_key(c.get("screen")) == want]


def _hp(t, k=3):
    p = _np.pad(t, k, mode="edge")
    b = _np.zeros_like(t)
    for dy in range(2 * k + 1):
        for dx in range(2 * k + 1):
            b += p[dy:dy + t.shape[0], dx:dx + t.shape[1]]
    return t - b / (2 * k + 1) ** 2


def art_grid(aspect=None):
    """Fingerprint grid for a screen of this shape (width / height), about as many
    cells as the default 48 x 32, so a tall or very wide screen isn't squashed into a
    3:2 grid (losing detail along its long side)."""
    if not aspect or aspect <= 0 or abs(math.log(aspect * ART_GH / ART_GW)) < 0.15:
        return ART_GW, ART_GH                         # near 3:2 (most screens): the usual grid
    gw = max(16, min(96, round(math.sqrt(ART_GW * ART_GH * aspect))))
    gh = max(16, min(96, round(ART_GW * ART_GH / gw)))
    return gw, gh


def _prep(img, inset=0.0, grid=None):
    """Fine luma detail of an image on a fixed grid: low frequencies (glare,
    vignetting, exposure) removed, so it can be compared with a camera photo."""
    gw, gh = grid or (ART_GW, ART_GH)
    img = img.convert("RGB")
    if inset > 0:                         # the PoP image leaves out the board's edge
        w, h = img.size
        img = img.crop((w * inset, h * inset, w * (1 - inset), h * (1 - inset)))
    img = img.resize((gw * 4, gh * 4), _Image.BILINEAR).filter(_ImageFilter.GaussianBlur(1.2))
    img = img.resize((gw, gh), _Image.BOX).convert("L")
    return _hp(_np.asarray(img, dtype=float))


def _corr(a, b):
    a = a.ravel() - a.mean()
    b = b.ravel() - b.mean()
    d = math.sqrt(float(a @ a) * float(b @ b))
    return float(a @ b) / d if d > 1e-9 else 0.0


def _shift_corr(a, b, r=2):
    """Best correlation over small offsets (the region may be a little off)."""
    H, W = a.shape
    best = -1.0
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            A = a[max(0, dy):H + min(0, dy), max(0, dx):W + min(0, dx)]
            B = b[max(0, -dy):H + min(0, -dy), max(0, -dx):W + min(0, -dx)]
            best = max(best, _corr(A, B))
    return best


def _ref_fp(rel, inset, grid=None):
    path = creative_file(rel)
    if not path:
        return None
    key = (path, os.path.getmtime(path), round(inset, 3), grid)
    if key not in ART_CACHE:
        try:
            with _Image.open(path) as im:
                ART_CACHE[key] = _prep(im, inset, grid)
        except Exception as e:
            print(f"creatives: cannot read {path}: {e}", flush=True)
            ART_CACHE[key] = None
    return ART_CACHE[key]


def _pop_fp(eid, grid=None):
    key = ("pop", eid, grid)
    if key not in ART_CACHE:
        try:
            with _Image.open(os.path.join(ARGS.out, "images", f"{eid}.roi.jpg")) as im:
                ART_CACHE[key] = _prep(im, grid=grid)
        except Exception:
            ART_CACHE[key] = None
    return ART_CACHE[key]


def _scores(f, refs):
    """Fine-detail correlation of one fingerprint with a stack of them (n, H, W),
    best over small offsets; one numpy pass per offset instead of per pair."""
    H, W = f.shape
    best = _np.full(len(refs), -1.0)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            A = f[max(0, dy):H + min(0, dy), max(0, dx):W + min(0, dx)]
            B = refs[:, max(0, -dy):H + min(0, -dy), max(0, -dx):W + min(0, -dx)]
            A0 = A - A.mean()
            B0 = B - B.mean(axis=(1, 2), keepdims=True)
            den = _np.sqrt(float((A0 * A0).sum()) * (B0 * B0).sum(axis=(1, 2)))
            c = _np.where(den > 1e-9, (B0 * A0).sum(axis=(1, 2)) / _np.maximum(den, 1e-9), 0.0)
            best = _np.maximum(best, c)
    return best


ART_STATE = {"loaded": False, "dirty": False, "sites": {}}   # site -> creative -> cached match
ART_RECHECK_MATCHED = 6 * 3600     # a confident match is re-checked this often (new artwork, drift)
ART_RECHECK_OTHER = 1800           # an unmatched creative is retried this often


def _art_state_path():
    return os.path.join(ARGS.out, "art-matches.json")


def art_state():
    """Per-creative artwork matches, kept on disk so a restart doesn't redo them."""
    if ART_STATE["loaded"] and not ART_STATE["dirty"] and _changed_on_disk(ART_STATE, _art_state_path()):
        ART_STATE["loaded"] = False
    if not ART_STATE["loaded"]:
        _note_disk(ART_STATE, _art_state_path())
        try:
            with open(_art_state_path(), encoding="utf-8") as f:
                ART_STATE["sites"] = json.load(f).get("sites", {})
        except (OSError, ValueError):
            ART_STATE["sites"] = {}
        ART_STATE["loaded"] = True
    return ART_STATE["sites"]


def save_art_state():
    if not ART_STATE["dirty"]:
        return
    tmp = _art_state_path() + ".tmp"
    with LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sites": ART_STATE["sites"]}, f)
        os.replace(tmp, _art_state_path())
        _note_disk(ART_STATE, _art_state_path())
    ART_STATE["dirty"] = False


def forget_art(site_id):
    """Drop a site's cached matches (its creatives were reset)."""
    if art_state().pop(site_id, None) is not None:
        ART_STATE["dirty"] = True
        save_art_state()


def _spread(ps, n):
    """Up to n plays spread evenly over the list (day and night, old and new)."""
    if n <= 0:
        return []
    if len(ps) <= n:
        return ps
    if n == 1:                       # one more to fill a sample: the middle one
        return [ps[len(ps) // 2]]
    return [ps[round(i * (len(ps) - 1) / (n - 1))] for i in range(n)]


def artwork_matches(cam, t0, t1, setup=False):
    """For each creative the site showed in [t0, t1): the booked artwork it
    matches. A creative's match is worked out once and kept (on disk too); it
    is re-checked only when the artwork library changes or after a while, so
    new plays don't make every page load redo it. Returns {creative_id: {...}}."""
    if not ART_OK or not cam.get("vault_screen"):
        return {}
    cands = [c for c in screen_creatives(cam["vault_screen"]) if c.get("file") and (
        c.get("kind", "image") == "image" or (c.get("kind") == "video" and c.get("frames")))]
    if not cands:
        return {}
    inset = max(0.0, min(0.4, float(cam.get("inset_pct") or 0) / 100.0))
    # the grid takes the screen's shape: its resolution (site or Doohly), else the artwork's
    px = board_px(cam)
    if not px:
        sizes = sorted(c["width"] / c["height"] for c in cands if (c.get("width") or 0) > 0 and (c.get("height") or 0) > 0)
        px = (sizes[len(sizes) // 2], 1) if sizes else None
    grid = art_grid(px[0] / px[1]) if px else None
    # one reference per image, one per frame of a video (a PoP shows one moment of it);
    # a booked entry scores as its best reference, so the right frame wins
    refs, owner, frame_of = [], [], []
    for k, c in enumerate(cands):
        for rel in (c["frames"] if c.get("kind") == "video" else [c["file"]]):
            f = _ref_fp(rel, inset, grid)
            if f is not None:
                refs.append(f)
                owner.append(k)
                frame_of.append(rel)
    if not refs:
        return {}
    stack = _np.stack(refs)
    owner = _np.array(owner)
    used = sorted(set(owner.tolist()))
    lib = f"{CRE['mtime']}|{len(refs)}|{round(inset, 3)}|{grid}"
    sid = cam.get("id")
    state = art_state().setdefault(sid, {})
    plays = site_plays(sid, t0, t1, setup=setup)
    by_cid = {}
    for p in plays:                                  # usable (or washed-out) images in this period, oldest first
        if not p[2] and p[5] and (len(p) < 7 or p[6] or (len(p) > 7 and p[7])):
            by_cid.setdefault(p[1], []).append(p)
    now = time.time()
    out = {}
    for cid, ps in by_cid.items():
        st = state.get(cid)
        fresh = st and st.get("lib") == lib and now - st.get("at", 0) < (
            ART_RECHECK_MATCHED if st["res"]["status"] == "matched" else ART_RECHECK_OTHER)
        # not matched yet on only a few photos: look again as soon as there is a new one
        if fresh and st["res"]["status"] != "matched" and st["res"].get("samples", 0) < ART_SAMPLES \
                and st.get("last") and ps[-1][4] != st.get("last"):
            fresh = False
        if not fresh:
            clean = [p for p in ps if len(p) < 7 or p[6]]
            sample = _spread(clean, ART_SAMPLES)
            if len(sample) < ART_SAMPLES:            # washed-out photos fill in
                sample += _spread([p for p in ps if not (len(p) < 7 or p[6])], ART_SAMPLES - len(sample))
            got = [(p, f) for p, f in ((p, _pop_fp(p[4], grid)) for p in sample) if f is not None]
            fps = [f for _, f in got]
            n_washed = sum(1 for p, _ in got if not (len(p) < 7 or p[6]))
            if not fps:
                continue
            raw = [_scores(f, stack) for f in fps]
            # per sample, each booked entry's best reference (its best video frame)
            per_s = _np.stack([[float(r[owner == k].max()) for k in used] for r in raw])
            per = _np.median(per_s, axis=0)                                       # per booked entry
            order = _np.argsort(-per)
            bk = used[int(order[0])]
            best, bc = float(per[order[0]]), cands[bk]
            other = max([float(per[i]) for i, k in enumerate(used) if cands[k]["campaign_id"] != bc["campaign_id"]] or [0.0])
            video = bc.get("kind") == "video"
            need, margin = (ART_MATCH_VIDEO, ART_MARGIN_VIDEO) if video else (ART_MATCH, ART_MARGIN)
            status = ("matched" if best >= need and best - other >= margin
                      else "possible" if best >= min(ART_POSSIBLE, need - 0.04) else "none")
            frame = None
            if video:                                      # the frame most samples looked like
                idx = _np.flatnonzero(owner == bk)
                votes = [int(idx[int(_np.argmax(r[idx]))]) for r in raw]
                frame = frame_of[max(set(votes), key=votes.count)]
            st = {"lib": lib, "at": now, "eid": sample[-1][4], "last": ps[-1][4],
                  "res": {"status": status, "campaign_id": bc["campaign_id"], "name": bc.get("name"),
                          "file": bc.get("file"), "score": round(best, 2), "next": round(other, 2),
                          "kind": bc.get("kind", "image"), "frame": frame, "variant": bc.get("variant"),
                          "from": bc.get("from"), "to": bc.get("to"), "samples": len(fps), "candidates": len(used),
                          "washed": n_washed}}
            state[cid] = st
            ART_STATE["dirty"] = True
        res = dict(st["res"])
        # played outside the dates it is booked for (e.g. a teaser left on). The same file
        # can be booked several times (this week's booking and next week's): any of them counts.
        if res["status"] == "matched":
            periods = book_periods(cands, res["campaign_id"], res.get("name"))
            if periods:
                res["from"], res["to"] = periods[0][0], periods[-1][1]
                res["periods"] = periods
            days = sorted({datetime.fromtimestamp(p[0], LOCAL_TZ).date().isoformat() for p in ps})
            res["off_schedule"] = off_schedule(days, periods)
        res["eid"] = st.get("eid")
        out[cid] = res
    save_art_state()
    return out


# ---------------------------------------------------------------- region fitting
# "Fit to artwork" on the region page: move the region's handles until recent
# photos of creatives already matched to booked artwork line up with that artwork.
# The mapping is the gateway's (core/src/warp.c) and the region page's.
CURVE_AT = [(1 / 3, 0), (2 / 3, 0), (1, 1 / 3), (1, 2 / 3), (1 / 3, 1), (2 / 3, 1), (0, 1 / 3), (0, 2 / 3)]
FIT_SECONDS = 25


def _homog(q):
    A, b = [], []
    for (u, v), (X, Y) in zip(((0, 0), (1, 0), (1, 1), (0, 1)), q):
        A += [[u, v, 1, 0, 0, 0, -u * X, -v * X], [0, 0, 0, u, v, 1, -u * Y, -v * Y]]
        b += [X, Y]
    return _np.append(_np.linalg.solve(_np.array(A, float), _np.array(b, float)), 1.0)


def _hmap(h, u, v):
    d = h[6] * u + h[7] * v + 1
    return (h[0] * u + h[1] * v + h[2]) / d, (h[3] * u + h[4] * v + h[5]) / d


def _thirds(s):
    return 13.5 * s * (s - 2 / 3) * (s - 1), -13.5 * s * (s - 1 / 3) * (s - 1)


def region_mapper(reg):
    """(u, v) on the flat screen -> (x, y) in the picture (both normalised)."""
    c = reg["roi"]
    h = _homog(c)
    shape = reg.get("roi_shape") or "standard"
    if shape == "fold" and reg.get("roi_fold"):
        f = reg["roi_fold"]
        fu = min(0.95, max(0.05, float(reg.get("fold_pct") or 50) / 100))
        hl, hr = _homog([c[0], f[0], f[1], c[3]]), _homog([f[0], c[1], c[2], f[1]])

        def fold(u, v):
            xl, yl = _hmap(hl, u / fu, v)
            xr, yr = _hmap(hr, (u - fu) / (1 - fu), v)
            left = u < fu
            return _np.where(left, xl, xr), _np.where(left, yl, yr)
        return fold
    if shape == "curve" and reg.get("roi_curve"):
        bend = []
        for (u, v), p in zip(CURVE_AT, reg["roi_curve"]):
            x, y = _hmap(h, u, v)
            bend.append((p[0] - x, p[1] - y))

        def curve(u, v):
            x, y = _hmap(h, u, v)
            a1, a2 = _thirds(u)
            b1, b2 = _thirds(v)
            for w, (bx, by) in zip((a1 * (1 - v), a2 * (1 - v), b1 * u, b2 * u, a1 * v, a2 * v, b1 * (1 - u), b2 * (1 - u)), bend):
                x = x + w * bx
                y = y + w * by
            return x, y
        return curve
    return lambda u, v: _hmap(h, u, v)


def _straighten(gray, reg, inset, ow, oh):
    """The region of a greyscale frame, straightened to ow x oh (bilinear)."""
    H, W = gray.shape
    v, u = _np.mgrid[0:oh, 0:ow].astype(float)
    u = inset + (1 - 2 * inset) * (u + 0.5) / ow
    v = inset + (1 - 2 * inset) * (v + 0.5) / oh
    x, y = region_mapper(reg)(u, v)
    x = _np.clip(x * (W - 1), 0, W - 1.001)
    y = _np.clip(y * (H - 1), 0, H - 1.001)
    x0, y0 = x.astype(int), y.astype(int)
    ax, ay = x - x0, y - y0
    g = gray
    out = (g[y0, x0] * (1 - ax) * (1 - ay) + g[y0, x0 + 1] * ax * (1 - ay) +
           g[y0 + 1, x0] * (1 - ax) * ay + g[y0 + 1, x0 + 1] * ax * ay)
    return _Image.fromarray(out.clip(0, 255).astype(_np.uint8))


def fit_photos(cam, n=3):
    """Up to n recent full-frame photos of different creatives matched to booked
    artwork: [(event id, artwork file, creative id, campaign)]."""
    now = time.time()
    res = artwork_matches(cam, now - POP_IMAGE_DAYS * 86400, now, setup=True)
    out = []
    plays = site_plays(cam.get("id"), now - POP_IMAGE_DAYS * 86400, now, setup=True)
    for cid, r in sorted(res.items(), key=lambda kv: -kv[1].get("score", 0)):
        if r.get("status") != "matched":
            continue
        art = r.get("frame") or r.get("file")
        if not art:
            continue
        for p in reversed(plays):
            if p[1] == cid and not p[2] and (len(p) < 7 or p[6]) and \
                    os.path.exists(os.path.join(ARGS.out, "images", f"{p[4]}.frame.jpg")):
                out.append((p[4], art, cid, r.get("campaign_id")))
                break
        if len(out) >= n:
            break
    return out


def _hp_img(a, k=6):
    """Detail of a greyscale array: lightly smoothed (sensor noise, the camera's
    blur) minus a box blur (lighting, haze and glare)."""
    im = _Image.fromarray(a.clip(0, 255).astype(_np.uint8))
    sm = _np.asarray(im.filter(_ImageFilter.GaussianBlur(1.5)), dtype=float)
    return sm - _np.asarray(im.filter(_ImageFilter.BoxBlur(k)), dtype=float)


def _shift_of(tpl, area, r):
    """Where template tpl best matches inside area (tpl's size + r on every side):
    (dy, dx) from the centred position, and the normalised correlation there."""
    t = tpl - tpl.mean()
    tn = _np.sqrt((t * t).sum())
    if tn < 1e-6 or t.std() < 2:
        return None
    win = _np.lib.stride_tricks.sliding_window_view(area, tpl.shape)       # (2r+1, 2r+1, th, tw)
    wm = win.mean(axis=(2, 3), keepdims=True)
    num = _np.einsum("ijkl,kl->ij", win - wm, t)
    den = _np.sqrt(((win - wm) ** 2).sum(axis=(2, 3))) * tn + 1e-9
    c = num / den
    k = _np.unravel_index(int(_np.argmax(c)), c.shape)
    return (k[0] - r, k[1] - r, float(c[k]))


def _matches(gray, reg, art, inset, ow, oh, r):
    """Correspondences between the artwork and the camera picture: for tiles of
    the artwork, where they appear once straightened with `reg` ->
    [(u, v, x, y, weight)] with (u, v) on the flat screen and (x, y) in the picture."""
    st = _np.asarray(_straighten(gray, reg, inset, ow, oh), dtype=float)
    hs = _np.pad(_hp_img(st), r, mode="edge")
    ha = _hp_img(art)
    m = region_mapper(reg)
    out = []
    nx = 12
    ny = max(3, round(oh / (ow / nx)))
    tw, th = ow // nx, oh // ny
    to_uv = lambda f: inset + (1 - 2 * inset) * f
    for ty in range(ny):
        for tx in range(nx):
            y0, x0 = ty * th, tx * tw
            res = _shift_of(ha[y0:y0 + th, x0:x0 + tw], hs[y0:y0 + th + 2 * r, x0:x0 + tw + 2 * r], r)
            if not res or res[2] < 0.4:
                continue
            dy, dx, conf = res
            # the artwork at this tile's centre shows at (centre + shift) in the straightened image
            fu, fv = (x0 + tw / 2) / ow, (y0 + th / 2) / oh
            su, sv = (x0 + tw / 2 + dx) / ow, (y0 + th / 2 + dy) / oh
            x, y = m(_np.array(to_uv(su)), _np.array(to_uv(sv)))
            out.append((to_uv(fu), to_uv(fv), float(x), float(y), conf))
    return out


def _robust(solve, reg, pts, resid):
    """Solve, drop the correspondences that disagree most (a tile matched to the
    wrong place), solve again."""
    out = solve(reg, pts)
    for _ in range(2):
        if len(pts) < 16:
            break
        r = _np.array([resid(out, p) for p in pts])
        keep = r <= max(_np.median(r) * 2.5, 1e-4)
        if keep.all():
            break
        pts = [p for p, k in zip(pts, keep) if k]
        out = solve(reg, pts)
    return out


def _resid(reg, p):
    x, y = region_mapper(reg)(_np.array(p[0]), _np.array(p[1]))
    return float(_np.hypot(x - p[2], y - p[3]))


def _solve_curve(reg, pts):
    """Least-squares curve handles (corners fixed) for correspondences."""
    h = _homog(reg["roi"])
    rows, bx, by, wts = [], [], [], []
    for u, v, x, y, w in pts:
        a1, a2 = _thirds(u)
        b1, b2 = _thirds(v)
        rows.append([a1 * (1 - v), a2 * (1 - v), b1 * u, b2 * u, a1 * v, a2 * v, b1 * (1 - u), b2 * (1 - u)])
        hx, hy = _hmap(h, u, v)
        bx.append(x - hx)
        by.append(y - hy)
        wts.append(w)
    A, W = _np.array(rows), _np.sqrt(_np.array(wts))[:, None]
    lam = 0.02 * _np.eye(8)                  # a little pull towards no bend where nothing was seen
    sol = []
    for b in (_np.array(bx), _np.array(by)):
        Aw, bw = A * W, b * W[:, 0]
        sol.append(_np.linalg.solve(Aw.T @ Aw + lam, Aw.T @ bw))
    reg = json.loads(json.dumps(reg))
    reg["roi_curve"] = [[min(1.0, max(0.0, float(_hmap(h, u, v)[0] + sol[0][k]))),
                         min(1.0, max(0.0, float(_hmap(h, u, v)[1] + sol[1][k])))] for k, (u, v) in enumerate(CURVE_AT)]
    return reg


def _solve_corners(reg, pts):
    """Least-squares homography (DLT) for correspondences; the corners it puts on the screen."""
    A = []
    for u, v, x, y, w in pts:
        A.append(_np.array([u, v, 1, 0, 0, 0, -u * x, -v * x, -x]) * w)
        A.append(_np.array([0, 0, 0, u, v, 1, -u * y, -v * y, -y]) * w)
    _, _, vt = _np.linalg.svd(_np.array(A))
    h = vt[-1] / vt[-1][-1]
    reg = json.loads(json.dumps(reg))
    reg["roi"] = [[float(c) for c in _hmap(h, u, v)] for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
    return reg


def fit_region(cam, reg, inset_pct):
    """Move the handles of `reg` (standard: the corners, a little; curve: the
    edge handles; fold: the fold line and the left face's share) so the photos
    line up with their artwork. Returns a result dict."""
    if not ART_OK:
        return {"error": "fitting needs numpy and Pillow"}
    photos = fit_photos(cam)
    if not photos:
        return {"error": f"no photo of a creative matched to booked artwork in the last {POP_IMAGE_DAYS:g} days: "
                         "fitting needs at least one (see the Bookings page)"}
    inset = max(0.0, min(0.4, float(inset_pct or 0) / 100))
    px = board_px(cam)
    data = []                                     # (grey frame, artwork array at the fit size, fingerprint grid, fingerprint)
    for eid, art, _, _ in photos:
        a = creative_file(art)
        if not a:
            continue
        with _Image.open(os.path.join(ARGS.out, "images", f"{eid}.frame.jpg")) as im:
            g = _np.asarray(im.convert("L"), dtype=float)
        with _Image.open(a) as im:
            aspect = (px[0] / px[1]) if px else im.size[0] / im.size[1]
            ow = 480
            oh = max(60, min(960, round(ow / aspect)))
            w, h = im.size
            crop = im.convert("L").crop((w * inset, h * inset, w * (1 - inset), h * (1 - inset)))
            arr = _np.asarray(crop.resize((ow, oh), _Image.BILINEAR), dtype=float)
        grid = art_grid(aspect)
        ref = _ref_fp(art, inset, grid)
        if ref is not None:
            data.append((g, arr, grid, ref, ow, oh))
    if not data:
        return {"error": "the matched artwork files couldn't be read"}

    def score(r):                                 # the matching score, averaged over the photos
        tot = 0.0
        for g, _, grid, ref, _, _ in data:
            im = _straighten(g, r, inset, grid[0] * 4, grid[1] * 4)
            tot += float(_scores(_prep(im, 0.0, grid), ref[None])[0])
        return tot / len(data)

    shape = reg.get("roi_shape") or "standard"
    reg = json.loads(json.dumps(reg))
    before = score(reg)
    best, best_reg = before, reg
    t0 = time.time()
    if shape in ("curve", "standard"):
        # find where tiles of the artwork are in each photo, then solve for the handles; repeat with a narrower search
        cur = reg
        # coarse first (half size, so a 24 px search reaches 10% of the width), then finer
        schedule = [(True, 24), (True, 24), (False, 16), (False, 8), (False, 4)]
        last_best = best
        for _ in range(4):                        # again while it keeps getting better
            for half, radius in schedule:
                if time.time() - t0 > FIT_SECONDS:
                    break
                pts = []
                for g, arr, _, _, ow, oh in data:
                    if half:
                        a2 = _np.asarray(_Image.fromarray(arr.astype(_np.uint8)).resize((ow // 2, oh // 2), _Image.BILINEAR),
                                         dtype=float)
                        pts += _matches(g, cur, a2, inset, ow // 2, oh // 2, radius)
                    else:
                        pts += _matches(g, cur, arr, inset, ow, oh, radius)
                if len(pts) < 12:
                    break
                try:
                    cand = _robust(_solve_curve if shape == "curve" else _solve_corners, cur, pts, _resid)
                    sc = score(cand)
                except (_np.linalg.LinAlgError, ValueError):
                    break
                cur = cand                        # a step can look worse on the way (a coarse pass); the best is kept
                if sc > best:
                    best, best_reg = sc, cand
            if best < last_best + 0.005 or time.time() - t0 > FIT_SECONDS:
                break
            last_best, cur = best, best_reg
        if shape == "standard":                   # corners: a nudge only, never a new region
            moved = max(abs(a - b) for p, q in zip(best_reg["roi"], reg["roi"]) for a, b in zip(p, q))
            if moved > 0.03:
                best, best_reg = before, reg
    else:
        # fold: few parameters, so search them directly
        params = [("roi_fold", i, ax) for i in range(2) for ax in (0, 1)] + [("fold_pct", None, None)]
        H, W = data[0][0].shape
        cur = json.loads(json.dumps(reg))

        def get(r, p):
            return float(r[p[0]]) if p[1] is None else r[p[0]][p[1]][p[2]]

        def put(r, p, val):
            if p[1] is None:
                r[p[0]] = min(95.0, max(5.0, val))
            else:
                r[p[0]][p[1]][p[2]] = min(1.0, max(0.0, val))
        for px_step in (32, 16, 8, 4, 2, 1):
            improved = True
            while improved and time.time() - t0 < FIT_SECONDS:
                improved = False
                for p in params:
                    step = px_step * 0.5 if p[1] is None else px_step / (W - 1 if p[2] == 0 else H - 1)
                    v0 = get(cur, p)
                    for d in (step, -step):
                        put(cur, p, v0 + d)
                        try:
                            sc = score(cur)
                        except _np.linalg.LinAlgError:
                            sc = -1
                        if sc > best + 0.002:
                            best, v0, improved = sc, v0 + d, True
                            break
                        put(cur, p, v0)
        best_reg = cur
    rounded = lambda pts: [[round(x, 4), round(y, 4)] for x, y in pts]
    for k in ("roi", "roi_curve", "roi_fold"):
        if best_reg.get(k):
            best_reg[k] = rounded(best_reg[k])
    if "fold_pct" in best_reg:
        best_reg["fold_pct"] = round(float(best_reg["fold_pct"]), 1)
    return {"region": best_reg, "before": round(before, 3), "after": round(best, 3), "better": best > before + 0.01,
            "seconds": round(time.time() - t0, 1), "timed_out": time.time() - t0 >= FIT_SECONDS,
            "photos": [{"event_id": e, "creative_id": c, "campaign_id": camp} for e, _, c, camp in photos]}


def book_periods(entries, campaign_id, name):
    """The dates a campaign's file is booked for, from every library entry of it
    (one per booking or Material Instructions row), joined where they meet:
    [(from, to)] with open ends as "" / "9999-12-31"."""
    raw = sorted((x.get("from") or "", x.get("to") or "9999-12-31") for x in entries
                 if x.get("campaign_id") == campaign_id and x.get("name") == name)
    def next_day(d):
        try:
            return (datetime.fromisoformat(d).date() + timedelta(days=1)).isoformat()
        except ValueError:
            return d
    out = []
    for a, z in raw:
        if out and a <= next_day(out[-1][1]):           # overlaps or follows on: one period
            out[-1] = (out[-1][0], max(out[-1][1], z))
        else:
            out.append((a, z))
    return out


def off_schedule(days, periods):
    """"early", "late" or "between" when any of these play dates is outside every
    booked period, else ""."""
    if not periods or not days:
        return ""
    for d in days:
        if not any(a <= d <= z for a, z in periods):
            return "early" if d < periods[0][0] else "late" if d > periods[-1][1] else "between"
    return ""


def merged_info(site_id, cid):
    """A merged ID, for showing it: what it was merged into and why, and its own plays
    (before the merge; after it the gateway reported them as the other ID)."""
    m = merges().get(site_id, {}).get(cid, {})
    n, last, photo = STORE.creative_summary(site_id, cid)
    return {"creative_id": cid, "into": m.get("into"), "canon": canon(site_id, cid), "why": m.get("why"),
            "at": m.get("at"), "plays": n, "last_play": last, "image": photo}


def lookalikes(site_id, cids, eids):
    """Group creatives whose PoP images look like the same artwork (the gateway
    can give one artwork several IDs, e.g. by day and by night). Returns a list
    of groups (lists of creative IDs)."""
    parent = {c: c for c in cids}
    if not ART_OK or len(cids) < 2:
        return [[c] for c in cids]
    fps = [(c, _pop_fp(eids[c])) for c in cids if eids.get(c)]
    fps = [(c, f) for c, f in fps if f is not None]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    if len(fps) >= 2:
        stack = _np.stack([f for _, f in fps])
        for i, (c, f) in enumerate(fps[:-1]):
            sc = _scores(f, stack[i + 1:])
            for j, v in enumerate(sc):
                if v >= ART_LOOKALIKE:
                    parent[find(fps[i + 1 + j][0])] = find(c)
    groups = {}
    for c in cids:
        groups.setdefault(find(c), []).append(c)
    return list(groups.values())


ART_LOOKALIKE = 0.55               # two PoP images this alike show the same artwork


def group_creatives(site_id, crs):
    """Bundle the creative IDs that are the same thing: matched to the same booked
    artwork, linked to the same campaign, or (unassigned) lookalike PoP images.
    One row per group on the Bookings page instead of one per gateway ID."""
    keyed, loose = {}, []
    for cr in crs:
        a = cr.get("assigned")
        art = cr.get("art") or {}
        if cr.get("blank"):
            keyed.setdefault(("blank",), []).append(cr)
        elif a and a != "unassigned":
            keyed.setdefault(("a", a, art.get("file") if art.get("status") == "matched" else ""), []).append(cr)
        else:
            loose.append(cr)
    by_id = {c["creative_id"]: c for c in loose}
    for g in lookalikes(site_id, [c["creative_id"] for c in loose],
                        {c["creative_id"]: c.get("image") for c in loose}):
        keyed[("u", min(g))] = [by_id[c] for c in g]
    out = []
    for key, ms in keyed.items():
        ms = sorted(ms, key=lambda c: (-c["plays"], c["creative_id"]))
        head = ms[0]
        links = {c.get("link") or "" for c in ms}
        out.append({"key": "|".join(str(k) for k in key), "creative_id": head["creative_id"],
                    "members": [{k: c.get(k) for k in ("creative_id", "plays", "share", "image", "link", "by",
                                                       "assigned", "art", "video", "blank", "last_play")} for c in ms],
                    "last_play": max((c.get("last_play") or 0) for c in ms),
                    "plays": sum(c["plays"] for c in ms), "share": sum(c["share"] for c in ms),
                    "image": next((c["image"] for c in ms if c.get("image")), None),
                    "blank": key[0] == "blank", "video": all(c.get("video") for c in ms),
                    "assigned": head.get("assigned"), "art": head.get("art"),
                    "by": head.get("by") if len({c.get("by") for c in ms}) == 1 else "mixed",
                    "link": links.pop() if len(links) == 1 else "", "mixed": len(links) > 1,
                    "off_schedule": any((c.get("art") or {}).get("off_schedule") for c in ms),
                    "merged": sorted(a for a, m in merges().get(site_id, {}).items()
                                     if canon(site_id, a) in {c["creative_id"] for c in ms})})
        out[-1]["merged_info"] = [merged_info(site_id, a) for a in out[-1]["merged"]]
    out.sort(key=lambda g: -g["share"])
    return out


def site_bookings(cam, week, links_all):
    """Booked vs delivered for one site in the week starting `week` (date)."""
    sid, S, N = cam.get("id"), (cam.get("slot_ms") or 0) / 1000.0, cam.get("slot_count") or 0
    res = {"site_id": sid, "name": cam.get("name"), "vault_screen": cam.get("vault_screen") or "",
           "slot_count": N, "slot_s": S, "campaigns": [], "creatives": [], "groups": [], "warnings": []}
    if not res["vault_screen"]:
        res["warnings"].append("No Vault screen set for this site (Settings).")
    wk0 = datetime.combine(week, datetime.min.time(), LOCAL_TZ).timestamp()
    spans = [(a, b) for a, b in setup_spans(sid) if b > wk0 and a < wk0 + 7 * 86400]
    if spans:            # being set up for some or all of the week: those plays aren't counted
        res["setup"] = {"now": in_setup(sid), "from": local_time(iso(max(spans[0][0], wk0)), True),
                        "to": None if spans[-1][1] == float("inf") else local_time(iso(spans[-1][1]), True)}
    ds = doohly_site(sid, res["vault_screen"])
    if ds:
        dev, lp = ds.get("device") or {}, ds.get("loop") or {}
        res["doohly"] = {"player": dev.get("name"), "online": dev.get("online"), "alive_at": local_time(dev.get("alive_at"), True),
                         "frame": ds.get("frame"), "loop": lp, "bookings_at": local_time(ds.get("bookings_at"), True)}
        if dev.get("online") is False:
            res["warnings"].append(f"Doohly says the player {dev.get('name')} is offline (last seen "
                                   f"{local_time(dev.get('alive_at'), True) or 'unknown'}).")
        if lp.get("slot_count") and (lp["slot_count"] != N or abs((lp.get("slot_s") or 0) - S) > 0.01) \
                and not (rotation_info([cam]).get(sid) or {}).get("auto"):     # auto: it is being set now
            res["warnings"].append(f"Doohly's loop for this screen is {lp['slot_count']} slots of {lp.get('slot_s')} s "
                                   f"({lp.get('name')}), but this site is set to {N} x {S:g} s by hand: change the rotation "
                                   "in the site's Settings (or let it follow Doohly) so plays are counted in the right slots.")
    if not S or N < 1:
        res["warnings"].append("No rotation (slot count and length) set, so plays can't be measured in slots.")
        return res
    start = datetime.combine(week, datetime.min.time(), LOCAL_TZ).timestamp()
    end = start + 7 * 86400
    now = time.time()
    t1 = min(end, now)
    campaigns = booked_for(res["vault_screen"], week)
    loop_s = N * S
    crs, seen_s = observed(sid, start, t1, S, N) if t1 > start else ([], 0)
    loops = seen_s / loop_s if loop_s else 0
    for c in crs:
        c["share"] = c["slots"] / loops if loops else 0
    for c in campaigns:
        c["per_loop"] = c["plays_per_loop"]
        # a Doohly booking on a schedule (some days or hours of the week) is due only for the
        # time it has been on so far: none before its first day, its full rate while it is on
        wins = c.pop("windows", None)
        if wins and t1 > start:
            on = sum(ppl * max(0.0, min(z, t1) - max(a, start)) for a, z, ppl in wins)
            c["due_per_loop"] = round(on / (t1 - start), 2)
            nxt = [a for a, z, _ in wins if a >= t1]
            if not on and nxt:
                c["starts"] = local_time(iso(min(nxt)), True)
    sure = [c for c in campaigns if c["certain"]]
    site_links = links_all.get(sid, {})
    art = artwork_matches(cam, start, t1) if t1 > start else {}
    library = screen_creatives(res["vault_screen"])
    m = match(crs, campaigns, site_links, art)
    by_c = {c["campaign_id"]: c for c in campaigns}
    for c in campaigns:
        mine = [x for x in library if x.get("campaign_id") == c["campaign_id"]]
        c.update(creatives=[], delivered=0.0,
                 linked=[k for k, v in site_links.items() if v == c["campaign_id"]],
                 artwork=[{"name": x.get("name"), "file": x.get("file"), "kind": x.get("kind", "image"),
                           "from": x.get("from"), "to": x.get("to"), "rotation": x.get("rotation"),
                           "dco": bool(x.get("dco")), "variant": x.get("variant"), "via": x.get("via"),
                           "screen": x.get("screen")} for x in mine])
    ds = doohly_site(sid, res["vault_screen"])
    inferred, why_not = (infer_programmatic(ds, week, crs, m, art, library, campaigns) if ds else (set(), ""))
    res["programmatic_inference"] = {"on": bool(ds) and not why_not, "why_not": why_not, "creatives": sorted(inferred)}
    tally = {"programmatic": 0.0, "house": 0.0, "unbooked": 0.0, "unassigned": 0.0}
    for cr in crs:
        a = "blank" if cr["blank"] else m.get(cr["creative_id"], "unassigned")
        if cr["creative_id"] in inferred:
            a = "programmatic"
        cr["assigned"] = a
        cr["link"] = site_links.get(cr["creative_id"], "")
        cr["art"] = art.get(cr["creative_id"])
        cr["by"] = ("manual" if cr["link"] else "inferred" if cr["creative_id"] in inferred else
                    "artwork" if a not in ("unassigned", "blank") else "")
        if cr["by"] == "artwork" and a in by_c:
            by_c[a]["linked"].append(cr["creative_id"])
        if a in by_c:
            by_c[a]["creatives"].append(cr["creative_id"])
            by_c[a]["delivered"] += cr["share"]
        elif a in tally:
            tally[a] += cr["share"]
    booked = sum(c["plays_per_loop"] for c in sure)
    booked_linked = 0
    delivered = 0.0
    for c in campaigns:
        due = c.get("due_per_loop", c["plays_per_loop"])
        c["delivery"] = c["delivered"] / due if due else None
        if not c["linked"] and not any(x["file"] for x in c["artwork"]):
            c["status"] = "not linked"      # no creative assigned and no artwork to look for: can't tell
            continue
        if not c["certain"]:
            c["status"] = "maybe" if not c["creatives"] else "seen"
            continue
        if not due:                          # on a schedule that hasn't started yet this week
            c["status"] = "seen" if c["creatives"] else "not started"
            continue
        booked_linked += due
        c["status"] = ("not seen" if not c["creatives"] else "ok" if (c["delivery"] or 0) >= 0.9
                       else "under")
        delivered += min(c["delivered"], due)
        if loops >= 1 and c["status"] == "not seen":
            res["warnings"].append(f"{c['name']} is booked for {due:g} play(s) per loop so far this week but "
                                   "none of its creatives has been seen.")
    offs = {}
    for cr in crs:
        a = cr.get("art") or {}
        if a.get("off_schedule"):
            offs.setdefault((a["campaign_id"], a.get("name"), a["off_schedule"], a.get("from"), a.get("to")), []).append(cr["creative_id"])
    for (camp, name, kind, frm, to), ids in offs.items():
        when = (f"after its end date ({to})" if kind == "late" else f"before its start date ({frm})" if kind == "early"
                else "between its booked periods")
        res["warnings"].append(f"{name} ({camp}) played {when}"
                               f" (seen as {', '.join(sorted(ids))}).")
    if ds and loops >= 20 and (ds.get("loop") or {}).get("programmatic") is not False:
        dbooked = sum(c["plays_per_loop"] for c in campaigns)          # sold, bonus and bonus-if-room
        room = max(0.0, N - dbooked)
        seen_p = tally["programmatic"]
        if abs(seen_p - room) > max(1.0, 0.25 * room) and not why_not:
            res["warnings"].append(f"Programmatic is taking {seen_p:.1f} slot(s) per loop, but Doohly leaves {room:.1f} "
                                   f"unbooked ({N} slots, {round(dbooked, 1):g} booked)" +
                                   (": some programmatic may still be unassigned" if seen_p < room else
                                    ": something marked programmatic may belong to a booking") + ".")
        res["programmatic_room"] = round(room, 1)
    if tally["unassigned"] > 0.05 and loops >= 1:
        res["warnings"].append(f"{tally['unassigned']:.1f} slot(s) per loop show creatives not assigned yet: "
                               "mark them as a campaign, programmatic or house below.")
    if booked > N:
        res["warnings"].append(f"{'Doohly' if res.get('doohly') else 'Vault'} has {round(booked, 1):g} plays per loop booked but the site's rotation has {N} slots: "
                               "check the slot count, or the screen runs a longer loop.")
    blank = sum(c["share"] for c in crs if c["blank"])
    elapsed = max(0.0, t1 - start)
    res["groups"] = group_creatives(sid, crs)
    res.update(campaigns=campaigns, creatives=sorted(crs, key=lambda c: -c["share"]),
               booked_per_loop=round(booked, 2), unsold_per_loop=round(max(0, N - booked), 2),
               programmatic_per_loop=tally["programmatic"], house_per_loop=tally["house"], unbooked_per_loop=tally["unbooked"],
               unassigned_per_loop=tally["unassigned"], blank_per_loop=blank,
               booked_linked_per_loop=round(booked_linked, 2),
               booked_due_per_loop=round(sum(c.get("due_per_loop", c["plays_per_loop"]) for c in sure), 2),
               artwork={"enabled": ART_OK, "library": len([x for x in library if x.get("file")]),
                        "videos": len([x for x in library if x.get("kind") == "video"]),
                        "missing": [x for x in ((load_creatives() or {}).get("missing") or [])
                                    if screen_key(x.get("screen")) == screen_key(res["vault_screen"])]}, loops_seen=loops, monitored=(seen_s / elapsed) if elapsed else None,
               seen_s=seen_s, elapsed_s=elapsed,
               # over the certain bookings that have creatives linked; the rest can't be judged yet
               true_coverage=(delivered / booked_linked) if booked_linked and loops else None,
               week=week.isoformat(), from_ts=start, to_ts=t1)
    return res


REPORTS = {"gen": 0, "weeks": {}}
REPORT_LOCK = threading.Lock()
REPORT_LOCKS = {}
REPORT_MAX_AGE = 60                # seconds a Bookings report is served from cache


def _mtime(path):
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


def report_version(cams):
    """What a cached report depends on besides new plays (which a minute's
    staleness covers): links, the Vault pull, the artwork library, site setup."""
    return (REPORTS["gen"], MERGES.get("mtime"), _mtime(os.path.join(ARGS.out, "creative-links.json")), BOOKINGS.get("mtime"),
            CRE.get("mtime"), _mtime(DOOHLY.get("path") or ""), None if cams is None else tuple(
                (c.get("id"), c.get("name"), c.get("vault_screen"), c.get("slot_count"), c.get("slot_ms"),
                 c.get("inset_pct")) for c in cams))


# ---------------------------------------------------------------- worker process
# The Bookings report and the best-photo runs are heavy Python (every play of the
# week, photo scoring). Done in this process they hold the interpreter and the web
# pages and photos wait (and can fail to load). They run in a separate worker
# process instead, which reads the same database and files.
IN_WORKER = False
_POOL = {"pool": None, "broken": False}
_POOL_LOCK = threading.Lock()


def _worker_init(out, api_key, hmac_key):
    global IN_WORKER, ARGS, STORE
    IN_WORKER = True
    ARGS = argparse.Namespace(out=out, api_key=api_key, hmac_key=hmac_key)
    STORE = store.Store(out, log=lambda m: print(m, flush=True), migrate=False)


def _worker_report(week_iso, cams):
    week = datetime.fromisoformat(week_iso).date()
    MERGES["checked"] = ART_STATE["checked"] = 0      # pick up merges and matches changed by the web process
    return _build_report(week, cams)


def _worker_best(t0, t1, phase, current):
    return best_pops(t0, t1, phase, current)


def in_worker(fn, *args, timeout=900):
    """Run fn(*args) in the worker process (in this one if it can't start)."""
    if IN_WORKER or _POOL["broken"] or os.environ.get("POP_WORKER", "1") == "0":
        return None
    with _POOL_LOCK:
        if _POOL["pool"] is None:
            try:
                import multiprocessing
                ctx = multiprocessing.get_context("spawn")
                _POOL["pool"] = ctx.Pool(2, initializer=_worker_init, initargs=(ARGS.out, ARGS.api_key, ARGS.hmac_key))
            except Exception as e:
                print(f"worker: can't start ({e}); working in the web process", flush=True)
                _POOL["broken"] = True
                return None
    try:
        return ("ok", _POOL["pool"].apply_async(fn, args).get(timeout))
    except Exception as e:
        print(f"worker: {fn.__name__} failed: {e}", flush=True)
        raise


def _build_report(week, cams):
    t = time.time()
    rep = bookings_report(week, cams)
    rep["built_at"] = local_time(iso(t), True)
    rep["image_days"] = POP_IMAGE_DAYS
    rep["minor_plays"] = MINOR_PLAYS
    rep["build_s"] = round(time.time() - t, 2)
    return rep


REPORT_BUILDING = set()


def cached_report(week, max_age=REPORT_MAX_AGE):
    """The Bookings report for a week, rebuilt at most once a minute (a background
    thread keeps this week's warm) or as soon as links or set-up change. A report
    only out of date by time is served at once while a fresh one is built."""
    load_bookings()
    load_creatives()
    cams = gateway_cameras()
    ver = report_version(cams)
    key = week.isoformat()
    hit = REPORTS["weeks"].get(key)
    if hit and hit[1] == ver and time.time() - hit[0] < max_age:
        return hit[2]
    if hit and hit[1] == ver and not IN_WORKER:   # just older than max_age: serve it, refresh behind
        with REPORT_LOCK:
            start = key not in REPORT_BUILDING
            REPORT_BUILDING.add(key)
        if start:
            threading.Thread(target=_refresh_report, args=(week, cams, ver), daemon=True).start()
        return hit[2]
    return _refresh_report(week, cams, ver)


def _refresh_report(week, cams, ver):
    key = week.isoformat()
    lock = REPORT_LOCKS.setdefault(key, threading.Lock())
    try:
        with lock:                                # one build per week at a time; others wait and reuse it
            hit = REPORTS["weeks"].get(key)
            if hit and hit[1] == ver and time.time() - hit[0] < 5:
                return hit[2]
            t = time.time()
            got = in_worker(_worker_report, key, cams)
            rep = got[1] if got else _build_report(week, cams)
            REPORTS["weeks"][key] = (t, ver, rep)
            for k in [k for k, v in REPORTS["weeks"].items() if time.time() - v[0] > 3600]:
                REPORTS["weeks"].pop(k, None)
            return rep
    finally:
        with REPORT_LOCK:
            REPORT_BUILDING.discard(key)


def report_warmer():
    """Keep this week's Bookings report fresh so the page and review cards load at once."""
    while True:
        try:
            wk = monday(datetime.now(LOCAL_TZ).date()).isoformat()
            last = (REPORTS["weeks"].get(wk) or (0, 0, {}))[2].get("build_s") or 0
            # with many sites a build takes a while: refresh less often, so it never runs non-stop
            rep = cached_report(monday(datetime.now(LOCAL_TZ).date()), max_age=max(REPORT_MAX_AGE - 15, 4 * last))
            if plan_merges(rep):
                cached_report(monday(datetime.now(LOCAL_TZ).date()))
            push_merges()
        except Exception as e:   # never let the helper thread die
            print(f"bookings: report refresh failed: {e}", flush=True)
        time.sleep(30)


def bookings_report(week, cams=None):
    cams = gateway_cameras() if cams is None else cams
    data = load_bookings()
    links = load_links()
    out = {"week": week.isoformat(), "pulled_at": local_time((data or {}).get("generated_at"), True),
           "source": (data or {}).get("source", ""), "file": BOOKINGS["path"],
           "campaigns": len((data or {}).get("proposals", [])), "gateway": cams is not None, "sites": [],
           "artwork_enabled": ART_OK}
    dd = load_doohly()
    if dd:
        out["doohly"] = {"at": local_time(dd.get("generated_at"), True), "ok": dd.get("ok"), "error": dd.get("error"),
                         "sites": len(dd.get("sites") or {}), "unmatched": dd.get("unmatched") or [],
                         "bookings": sum(len(x.get("bookings") or []) for x in (dd.get("sites") or {}).values())}
    lib = load_creatives()
    if lib:
        out["creatives"] = {"pulled_at": local_time(lib.get("generated_at"), True), "source": lib.get("source", ""),
                            "count": len([c for c in lib["creatives"] if c.get("file")])}
    if not IN_WORKER:            # the worker process only reads the database
        setup_reconcile(cams)
    for cam in cams or []:
        out["sites"].append(site_bookings(cam, week, links))
    # the Vault pull only matters for screens Doohly doesn't know
    out["vault_used"] = any(s.get("vault_screen") and not s.get("doohly") for s in out["sites"])
    return out


# ---------------------------------------------------------------- setting a site up
# A new site starts in setup: while its region is being placed every move gives the
# same artwork a new creative ID, so its plays would count twice. Its plays aren't
# counted (Bookings, coverage, PoP photos), the gateway holds back its detection
# alerts, and it goes live from the region page once the region fits: the creatives
# are learned again from then, once.
SETUP_SETTLE_S = float(os.environ.get("SETUP_SETTLE_MIN", "30")) * 60    # region left alone this long
SETUP_MIN_PLAYS = int(os.environ.get("SETUP_MIN_PLAYS", "20"))           # plays since then
SETUP_MATCH_SHARE = float(os.environ.get("SETUP_MATCH_SHARE", "0.6"))   # of them matching booked artwork
FULL_FRAME = [[0, 0], [1, 0], [1, 1], [0, 1]]


def setup_reconcile(cams):
    """The gateway says a site is in setup (it was added while the receiver was down,
    or in the config file): start its setup period now."""
    for c in cams or []:
        if c.get("setup") and c.get("id") and not in_setup(c["id"]):
            STORE.setup_start(c["id"], time.time())


def setup_status(cam, now=None):
    """Where a site is in setting up, and whether it can go live: the checks the
    region page shows."""
    sid, now = cam.get("id"), now or time.time()
    per = [p for p in STORE.setup_periods().get(sid, []) if p[1] is None]
    if not per:
        last = (STORE.setup_periods().get(sid) or [None])[-1]
        return {"site_id": sid, "in_setup": False, "live_since": local_time(iso(last[1]), True) if last else None}
    start, _, region_at = per[-1]
    since = max(start, region_at or 0)
    t0 = max(since, now - 86400)
    checks = []
    roi = [[round(float(x), 3) for x in p] for p in (cam.get("roi") or FULL_FRAME)]
    placed = roi != FULL_FRAME
    checks.append({"key": "region", "ok": placed, "label": "Region placed",
                   "detail": "the outline is on the screen" if placed else "the outline is still the whole picture: place it on the screen"})
    quiet = now - since
    checks.append({"key": "settled", "ok": quiet >= SETUP_SETTLE_S, "label": "Region left alone",
                   "detail": f"{int(quiet // 60)} min since it last changed (needs {int(SETUP_SETTLE_S // 60)})"})
    plays = [p for p in site_plays(sid, t0, now, setup=True) if not p[2]]
    checks.append({"key": "plays", "ok": len(plays) >= SETUP_MIN_PLAYS, "label": "Plays seen since",
                   "detail": f"{len(plays)} (needs {SETUP_MIN_PLAYS})"})
    art = artwork_matches(cam, t0, now, setup=True) if plays else {}
    booked = {c.get("campaign_id") for c in screen_creatives(cam.get("vault_screen") or "") if c.get("file")} \
        if cam.get("vault_screen") else set()
    if not booked:
        checks.append({"key": "artwork", "ok": False, "label": "Plays match booked artwork",
                       "detail": "no booked artwork for this screen to compare with" +
                       ("" if cam.get("vault_screen") else " (set its Vault screen in Settings)") +
                       ": check the region by eye and go live anyway"})
    else:
        matched = {cid for cid, a in art.items() if a.get("status") == "matched"}
        share = sum(1 for p in plays if p[1] in matched) / len(plays) if plays else 0.0
        camps = {art[c]["campaign_id"] for c in matched}
        need_c = min(2, len(booked))
        ok = bool(plays) and share >= SETUP_MATCH_SHARE and len(camps) >= need_c
        checks.append({"key": "artwork", "ok": ok, "label": "Plays match booked artwork",
                       "detail": f"{round(share * 100)}% of plays (needs {round(SETUP_MATCH_SHARE * 100)}%), "
                                 f"{len(camps)} campaign(s) recognised (needs {need_c})",
                       "share": round(share, 2), "campaigns": sorted(camps)})
    return {"site_id": sid, "in_setup": True, "since": local_time(iso(start), True),
            "region_at": local_time(iso(region_at), True) if region_at else None,
            "ready": all(c["ok"] for c in checks), "checks": checks}


def gateway_call(path, payload, timeout=10):
    """POST to the gateway's API; returns (status, reply)."""
    url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {ARGS.api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": e.reason}
    except Exception as e:
        return 502, {"error": f"cannot reach the gateway: {e}"}


def set_live(sid, live, force=False):
    """Go live (the creatives are learned again from now, and its plays count) or back
    into setup. Returns (http code, reply)."""
    cams = gateway_cameras()
    cam = next((c for c in cams or [] if c.get("id") == sid), None)
    if cam is None:
        return (502, {"error": "cannot reach the gateway"}) if cams is None else (404, {"error": f"no site {sid}"})
    q = urllib.parse.quote(sid, safe="")
    if not live:
        code, r = gateway_call(f"/api/cameras/{q}/setup", {"setup": True})
        if code != 200:
            return code, {"error": f"gateway refused: {r.get('error')}"}
        STORE.setup_start(sid, time.time())
        REPORTS["gen"] += 1
        print(f"site {sid} back in setup", flush=True)
        return 200, setup_status(cam)
    if not in_setup(sid):
        return 409, {"error": "this site is already live"}
    st = setup_status(cam)
    if not st["ready"] and not force:
        return 409, {"error": "not ready yet: " + "; ".join(c["label"].lower() + " (" + c["detail"] + ")"
                                                          for c in st["checks"] if not c["ok"]), "status": st}
    # what it learned while the region moved is thrown away: learned again, once, from now.
    # Reset first, while still in setup, so the faults it clears aren't sent to Teams.
    code, r = gateway_call(f"/api/cameras/{q}/reset-creatives", {"restart_numbering": False})
    if code != 200:
        return code, {"error": f"gateway refused the creative reset: {r.get('error')}"}
    code, r = gateway_call(f"/api/cameras/{q}/setup", {"setup": False})
    if code != 200:
        return code, {"error": f"creatives reset, but the gateway refused to leave setup: {r.get('error')}"}
    STORE.setup_end(sid, time.time())
    forget_art(sid)
    forget_merges(sid)
    REPORTS["gen"] += 1
    print(f"site {sid} live" + (" (checks overridden)" if not st["ready"] else ""), flush=True)
    return 200, setup_status(cam)


# ---------------------------------------------------------------- backup and restore
# One file with everything but the photos: the receiver's database and settings, the
# gateway's sites and learned creatives, and what creatives-sync has put in SharePoint
# (tools/backup.py). Restoring stages each part; the three services restart and put it in
# place before they load anything. creatives-sync also keeps one in SharePoint nightly.
BACKUP_LOCK = threading.Lock()


def _gateway_get(path, timeout=60):
    url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ARGS.api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _backup_dir(sub):
    d = os.path.join(ARGS.out, sub)
    os.makedirs(d, exist_ok=True)
    for n in os.listdir(d):                           # leftovers of an earlier one
        p = os.path.join(d, n)
        if time.time() - os.path.getmtime(p) > 6 * 3600:
            try:
                os.remove(p)
            except OSError:
                pass
    return d


def make_backup():
    """Build a backup; returns (file path, manifest). Raises if the gateway can't be reached
    (a backup without the sites would be no use)."""
    try:
        gw = _gateway_get("/api/backup")
    except Exception as e:
        raise RuntimeError(f"cannot reach the gateway for its sites ({e})")
    warnings = []
    sy = None
    if sync_url():
        code, sy = sync_call("GET", "/backup", timeout=30)
        if code != 200:
            warnings.append(f"creatives-sync's state is not in it: {sy.get('error') or code}")
            sy = None
    name = f"pop-monitor-backup-{datetime.now(LOCAL_TZ):%Y-%m-%d_%H%M}.tar.gz"
    dest = os.path.join(_backup_dir("backup-tmp"), name)
    with BACKUP_LOCK:
        manifest = bk.build(ARGS.out, dest, gw, sy, {"warnings": warnings, "gateway_id": gw.get("gateway_id")})
    return dest, manifest


def restore_preview(path):
    manifest, gw, sy = bk.read(path)
    return {"token": os.path.basename(path).rsplit(".tar.gz", 1)[0], "manifest": manifest,
            "gateway": bool(gw), "creatives_sync": bool(sy), "size": os.path.getsize(path)}


def restore_apply(token):
    """Restore the uploaded (or fetched) backup `token`. Returns (code, reply); on success
    the receiver restarts shortly after replying."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return 400, {"error": "unknown backup"}
    path = os.path.join(ARGS.out, "restore-incoming", token + ".tar.gz")
    if not os.path.exists(path):
        return 404, {"error": "that backup is no longer here: upload it again"}
    try:
        manifest, gw, sy = bk.read(path)
        bk.stage_receiver(path, ARGS.out)
    except ValueError as e:
        return 400, {"error": str(e)}
    warnings = []
    if gw:
        code, r = gateway_call("/api/restore", gw, timeout=120)
        if code not in (200, 202):
            bk.unstage(ARGS.out)
            return 502, {"error": f"the gateway refused the restore: {r.get('error') or code}. Nothing was changed."}
    else:
        warnings.append("the backup has no gateway part: the gateway's sites were left as they are")
    if sy and sync_url():
        code, r = sync_call("POST", "/restore", timeout=60, data=json.dumps(sy).encode())
        if code not in (200, 202):
            warnings.append(f"creatives-sync didn't take its part ({r.get('error') or code}): it will re-upload PoP photos")
    bk.mark_ready(ARGS.out)
    print(f"restore of a backup from {manifest.get('created_at')} ({manifest.get('host')}) staged: restarting", flush=True)

    def restart():
        time.sleep(1.5)
        os._exit(3)                                   # the container restarts; the restore goes in first
    threading.Thread(target=restart, daemon=True).start()
    return 202, {"restarting": True, "warnings": warnings, "manifest": manifest}


def missing_artwork():
    """The bookings on monitored screens with no artwork (or only some) from the last
    artwork sync: what should be there and where, for the Bookings page."""
    lib = load_creatives() or {}
    cams = gateway_cameras() or []
    sites = {screen_key(c.get("vault_screen")): c for c in cams if c.get("vault_screen")}
    names = {}
    for ds in ((load_doohly() or {}).get("sites") or {}).values():
        for b in ds.get("bookings") or []:
            if b.get("campaign_id"):
                names.setdefault(b["campaign_id"], {"name": b.get("campaign_name") or b.get("name"),
                                                    "advertiser": b.get("advertiser")})
    out = []
    for m in lib.get("missing") or []:
        cam = sites.get(screen_key(m.get("screen")))
        if not cam:
            continue
        n = names.get(m.get("campaign_id")) or {}
        out.append(dict(m, site_id=cam.get("id"), site_name=cam.get("name"),
                        campaign_name=n.get("name") or m.get("name"), advertiser=n.get("advertiser")))
    out.sort(key=lambda m: (bool(m.get("partial")), name_order(m.get("campaign_name") or m["campaign_id"]),
                            name_order(m.get("site_name"))))
    return {"pulled_at": local_time(lib.get("generated_at"), True), "items": out,
            "can_upload": bool(sync_url())}


def campaign_for_record(r, cache):
    """Campaign a PoP record's creative is linked to (for the review cards)."""
    sid, cid = r.get("site_id"), canon(r.get("site_id"), r.get("local_creative_id"))
    t = iso_to_ts(r.get("play_started_at"))
    if not sid or not cid or t is None or not load_bookings():
        return None
    week = monday(datetime.fromtimestamp(t, LOCAL_TZ).date())
    key = (sid, week)
    if key not in cache:
        cache[key] = {}
        if week not in cache:
            cache[week] = {s["site_id"]: s for s in cached_report(week)["sites"]}
        rep = cache[week].get(sid)
        if rep and rep.get("campaigns") is not None:
            names = {c["campaign_id"]: c for c in rep["campaigns"]}
            for cr in rep["creatives"]:
                a = cr.get("assigned")
                if a in names:
                    cache[key][cr["creative_id"]] = names[a]["name"] + (" (artwork match)" if cr.get("by") == "artwork" else "")
                elif a in ("programmatic", "house"):
                    cache[key][cr["creative_id"]] = ("House" if a == "house" else
                                                     "Programmatic (inferred)" if cr.get("by") == "inferred" else "Programmatic")
    return cache[key].get(cid)


def sync_expectations():
    """Tell the gateway how many distinct creatives each site's loop should hold
    this week (booked campaigns), so share-of-time bookings don't raise
    missing_creative. 0 = let the gateway work it out."""
    cams = gateway_cameras()
    if cams is None or not load_bookings():
        return
    week = monday(datetime.now(LOCAL_TZ).date())
    for cam in cams:
        n = len([c for c in booked_for(cam.get("vault_screen"), week) if c["certain"]]) if cam.get("vault_screen") else 0
        if n == (cam.get("expected_creatives") or 0):
            continue
        url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + \
            f"/api/cameras/{urllib.parse.quote(cam['id'], safe='')}/expectations"
        try:
            req = urllib.request.Request(url, data=json.dumps({"expected_creatives": n}).encode(), method="PUT",
                                         headers={"Authorization": f"Bearer {ARGS.api_key}",
                                                  "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            print(f"bookings: {cam['id']} expects {n} booked creative(s) this week", flush=True)
        except Exception as e:
            print(f"bookings: could not update {cam['id']}: {e}", flush=True)


def sync_url():
    return os.environ.get("SYNC_URL", "").rstrip("/")


def sync_call(method, path, timeout=5, data=None):
    """Talk to the creatives-sync service. Returns (status code, JSON)."""
    if not sync_url():
        return 404, {"configured": False, "error": "no artwork sync service (SYNC_URL is not set)"}
    req = urllib.request.Request(sync_url() + path, method=method,
                                 data=(data if data is not None else b"") if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {ARGS.api_key}", "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 502, {"error": f"cannot reach the artwork sync service at {sync_url()}: {e}"}


def nudge_sync(why):
    """Ask creatives-sync to check its inputs now rather than within 5 minutes:
    it re-syncs only if the Vault pull or the sites' Vault screens changed."""
    if sync_url():
        def go():
            code, _ = sync_call("POST", "/check")
            print(f"creatives-sync asked to refresh ({why}): {code}", flush=True)
        threading.Thread(target=go, daemon=True).start()


def sync_inputs():
    """What creatives-sync needs: the latest Vault pull and each site's Vault screen."""
    cams = gateway_cameras()
    return {"bookings": load_bookings(), "bookings_file": BOOKINGS["path"],
            "screens": None if cams is None else sorted({c["vault_screen"] for c in cams if c.get("vault_screen")}),
            "sites": None if cams is None else [{"site_id": c.get("id"), "name": c.get("name"),
                                                 "vault_screen": c.get("vault_screen", "")} for c in cams]}


# ---------------------------------------------------------------- best PoP images
# The clearest day and night PoP photo of each campaign's creative at each site,
# for creatives-sync to put in SharePoint (8pm: today's daytime photos; 8am: last
# night's). Scoring is in popscore.py; here we find each campaign's versions and
# their photos, sort them into day and night, and pick the best.
try:
    import popscore as _ps
except ImportError:                                   # pragma: no cover
    _ps = None
POP_IMAGE_DAYS = store.IMAGE_DAYS
# Creatives seen only this often in a week, with nothing linked or matched, are folded
# away on the Bookings page: mostly moments caught mid-change, not real artwork.
MINOR_PLAYS = int(os.environ.get("BOOKINGS_MINOR_PLAYS", "2"))
POP_SAMPLES = int(os.environ.get("POP_BEST_SAMPLES", "240"))   # photos scored per version and window
POP_DEFAULT_COORDS = os.environ.get("POP_DEFAULT_COORDS", "-36.85,174.76")   # Auckland
PREP_CACHE = {}                    # event id -> popscore.prep() of its region image
PREP_MAX = 6000


def site_coords(site_id):
    """Where a site is, for sunrise and sunset: POP_SITE_COORDS ("SITE=lat,lon;..."),
    else its Doohly player's location, else POP_DEFAULT_COORDS. A few tens of km out doesn't matter: twilight is skipped."""
    for part in os.environ.get("POP_SITE_COORDS", "").split(";"):
        k, _, v = part.partition("=")
        if k.strip() == site_id and v:
            try:
                lat, lon = (float(x) for x in v.split(","))
                return lat, lon
            except ValueError:
                pass
    ds = doohly_site(site_id)                          # the Doohly player's location
    if ds and ds["device"].get("lat") is not None:
        return float(ds["device"]["lat"]), float(ds["device"]["lon"])
    lat, lon = (float(x) for x in POP_DEFAULT_COORDS.split(","))
    return lat, lon


def _pop_prep(eid):
    if eid in PREP_CACHE:
        return PREP_CACHE[eid]
    try:
        with _Image.open(os.path.join(ARGS.out, "images", f"{eid}.roi.jpg")) as im:
            v = _ps.prep(im)
    except Exception:
        v = None
    if len(PREP_CACHE) >= PREP_MAX:
        for k in list(PREP_CACHE)[:PREP_MAX // 4]:
            PREP_CACHE.pop(k, None)
    PREP_CACHE[eid] = v
    return v


def _frame_luma(eid):
    path = os.path.join(ARGS.out, "images", f"{eid}.frame.jpg")
    try:
        with _Image.open(path) as im:
            im.draft("L", (160, 90))                  # decode at 1/8 size: fast
            return _ps.frame_luma(im)
    except Exception:
        return None


def _versions(site_id, group):
    """A campaign's creative versions within one Bookings group: the booked
    artwork file it matched, else creatives whose photos look alike."""
    members = [m for m in group["members"] if not m.get("blank")]
    key = group.get("key", "")
    file = key.split("|", 2)[2] if key.startswith("a|") and key.count("|") >= 2 else ""
    if file:
        return [{"key": "art:" + file, "label": os.path.basename(file), "file": file,
                 "ids": [m["creative_id"] for m in members], "video": group.get("video")}]
    out = []
    for g in lookalikes(site_id, [m["creative_id"] for m in members],
                        {m["creative_id"]: m.get("image") for m in members}):
        out.append({"key": "id:" + min(g), "label": min(g), "file": "", "ids": g,
                    "video": all(next((m.get("video") for m in members if m["creative_id"] == c), False) for c in g)})
    return out


def _play_by_eid(site_id, eid):
    p = STORE.play(eid)
    if not p or p[7] != site_id:
        return None
    return (p[0], canon(site_id, p[1]), bool(p[2]), bool(p[3]), p[4], bool(p[5]), p[6] == 1, p[6] == 2)


def best_pops(t0, t1, phase, current=None):
    """For every campaign version that played in [t0, t1): its best `phase`
    ('day' or 'night') photo. Unassigned, programmatic and house are left out.
    current: {"site|campaign|version": event id} of the photos already uploaded;
    each is scored again alongside the new ones, against the same reference, so
    "better" is decided fairly (a dull, rainy day can't inflate the scores)."""
    current = current or {}
    if not (ART_OK and _ps):
        return {"error": "numpy, Pillow and popscore.py are needed", "items": []}
    items = {}
    wk = monday(datetime.fromtimestamp(t0, LOCAL_TZ).date())
    last = monday(datetime.fromtimestamp(t1 - 1, LOCAL_TZ).date())
    weeks = []
    while wk <= last:
        weeks.append(wk)
        wk = wk + timedelta(days=7)
    for week in weeks:
        rep = cached_report(week)
        for site in rep.get("sites") or []:
            sid = site.get("site_id")
            names = {c["campaign_id"]: c.get("name") for c in site.get("campaigns") or []}
            advs = {c["campaign_id"]: c.get("advertiser") or "" for c in site.get("campaigns") or []}
            for g in site.get("groups") or []:
                camp = g.get("assigned") or ""
                if not re.fullmatch(r"LUMO_\d+", camp):
                    continue
                for v in _versions(sid, g):
                    k = (sid, camp, v["key"])
                    it = items.setdefault(k, {"site_id": sid, "site_name": site.get("name"),
                                              "screen": site.get("vault_screen") or site.get("name"),
                                              "campaign_id": camp, "campaign_name": names.get(camp, ""), "advertiser": advs.get(camp, ""),
                                              "version": v["key"], "version_label": v["label"],
                                              "art_file": v["file"], "creative_ids": set(), "video": v["video"]})
                    it["creative_ids"].update(v["ids"])
    out = []
    for (sid, camp, vkey), it in items.items():
        lat, lon = site_coords(sid)
        ids = it["creative_ids"]
        plays = [p for p in site_plays(sid, t0, t1)
                 if p[1] in ids and not p[2] and p[5] and (len(p) < 7 or p[6])]
        plays = [p for p in plays if _ps.phase(p[0], lat, lon) == phase]      # by the clock first: cheap
        if not plays:
            continue
        sample = _spread(plays, POP_SAMPLES)
        cands = []
        for p in sample:
            if _ps.phase(p[0], lat, lon, _frame_luma(p[4])) != phase:       # the picture agrees?
                continue
            pr = _pop_prep(p[4])
            if pr is not None and os.path.exists(os.path.join(ARGS.out, "images", f"{p[4]}.frame.jpg")):
                cands.append((p, pr))
        if not cands:
            continue
        norm = _ps.norms([pr for _, pr in cands])
        cur_eid = current.get(f"{sid}|{camp}|{vkey}")
        cur = None
        if cur_eid and cur_eid not in {p[4] for p, _ in cands}:
            cp, cpr = _play_by_eid(sid, cur_eid), _pop_prep(cur_eid)
            if cp and cpr is not None:
                cur = (cp, cpr)
        cons = _ps.consensus([pr for _, pr in _spread(cands, 25)]) if len(cands) >= 3 else None
        art_ref = None
        if it["art_file"]:
            path = creative_file(it["art_file"])
            if path:
                try:
                    with _Image.open(path) as im:
                        art_ref = _ps.prep(im)
                except Exception:
                    art_ref = None
        best, cur_score = None, None
        for p, pr in cands + ([cur] if cur else []):
            refs = [r for r in (art_ref, cons) if r is not None]
            if it["video"] or not refs:
                s = _ps.score(pr, None, norm, video=True)
            else:
                ss = [_ps.score(pr, r, norm) for r in refs]
                s = dict(ss[-1], score=round(sum(x["score"] for x in ss) / len(ss), 1),
                         by_ref={"artwork": ss[0]["score"]} if art_ref is not None and len(ss) == 2 else {})
            if p[4] == cur_eid:
                cur_score = s["score"]
            if best is None or s["score"] > best[1]["score"]:
                best = (p, s)
        raw = {a for a, m in merges().get(sid, {}).items() if canon(sid, a) in ids} | set(ids)
        first = STORE.first_play(sid, sorted(raw), t1) or best[0][0]
        p, s = best
        out.append(dict(it, creative_ids=sorted(ids), event_id=p[4], creative_id=p[1],
                        taken_at=datetime.fromtimestamp(p[0], LOCAL_TZ).isoformat(timespec="seconds"),
                        first_seen=datetime.fromtimestamp(first, LOCAL_TZ).date().isoformat(),
                        score=s["score"], parts=s["parts"], candidates=len(cands),
                        current_event_id=cur_eid, current_score=cur_score))
    out.sort(key=lambda x: (x["site_id"] or "", x["campaign_id"], x["version"]))
    return {"phase": phase, "from": iso(t0), "to": iso(t1), "items": out,
            "generated_at": iso(time.time())}


ROT_LOCK = threading.Lock()


def _rot_path():
    return os.path.join(ARGS.out, "rotation.json")


def rotation_state():
    """{"manual": {site: true}, "changes": {site: {...}}}: sites whose rotation is set
    by hand, and the last change made to follow Doohly."""
    try:
        with open(_rot_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("manual", {})
    d.setdefault("changes", {})
    return d


def save_rotation_state(d):
    tmp = _rot_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, _rot_path())


def rotation_info(cams=None):
    """Per site: whether its rotation follows Doohly, Doohly's loop, and why not."""
    cams = gateway_cameras() if cams is None else cams
    st = rotation_state()
    out = {}
    for cam in cams or []:
        sid = cam.get("id")
        ds = doohly_site(sid, cam.get("vault_screen"))
        lp = (ds or {}).get("loop") or {}
        why = ("set by hand" if st["manual"].get(sid) else "takeover" if cam.get("takeover") else
               "has time-of-day rotations" if cam.get("dayparts") else
               "no Doohly player" if not ds else "Doohly's loop not known yet" if not lp.get("slot_count") else "")
        fr = (ds or {}).get("frame") or {}
        out[sid] = {"auto": not why, "why": why, "manual": bool(st["manual"].get(sid)),
                    "frame": {"width": fr["width"], "height": fr["height"]} if fr.get("width") and fr.get("height") else None,
                    "doohly": {"slot_count": lp.get("slot_count"), "slot_s": lp.get("slot_s"), "name": lp.get("name")} if lp else None,
                    "last_change": st["changes"].get(sid)}
    return out


def sync_rotations():
    """Keep each site's rotation (slot count and length) the same as its Doohly
    player's loop, unless the site is set by hand, a takeover or day-parted."""
    cams = gateway_cameras()
    if cams is None or not load_doohly():
        return
    info = rotation_info(cams)
    for cam in cams:
        sid = cam.get("id")
        r = info.get(sid) or {}
        if not r.get("auto"):
            continue
        n, ms = int(r["doohly"]["slot_count"]), int(round(float(r["doohly"]["slot_s"]) * 1000))
        if n == (cam.get("slot_count") or 0) and ms == (cam.get("slot_ms") or 0):
            continue
        url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + f"/api/cameras/{urllib.parse.quote(sid, safe='')}"
        try:
            req = urllib.request.Request(url, data=json.dumps({"slot_count": n, "slot_ms": ms}).encode(), method="PUT",
                                         headers={"Authorization": f"Bearer {ARGS.api_key}", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"rotation: could not update {sid}: {e}", flush=True)
            continue
        was = f"{cam.get('slot_count') or '?'} x {(cam.get('slot_ms') or 0) / 1000:g} s"
        print(f"rotation: {sid} set to {n} x {ms / 1000:g} s from Doohly ({r['doohly'].get('name')}), was {was}", flush=True)
        with ROT_LOCK:
            st = rotation_state()
            st["changes"][sid] = {"at": iso(time.time()), "from": was, "to": f"{n} x {ms / 1000:g} s",
                                  "loop": r["doohly"].get("name")}
            save_rotation_state(st)
        REPORTS["gen"] += 1


def board_px(cam):
    """(width, height) of a site's screen: as set on the site, else Doohly's frame, else None."""
    if (cam.get("board_width") or 0) > 0 and (cam.get("board_height") or 0) > 0:
        return cam["board_width"], cam["board_height"]
    fr = (doohly_site(cam.get("id"), cam.get("vault_screen")) or {}).get("frame") or {}
    if (fr.get("width") or 0) > 0 and (fr.get("height") or 0) > 0:
        return fr["width"], fr["height"]
    return None


def sync_board_sizes():
    """Give each site with no screen resolution set its Doohly player's resolution, so
    the gateway straightens PoP images to the screen's shape. Once set (here or by
    hand) it is left alone."""
    cams = gateway_cameras()
    if cams is None or not load_doohly():
        return
    for cam in cams:
        sid = cam.get("id")
        if (cam.get("board_width") or 0) > 0:
            continue
        fr = (doohly_site(sid, cam.get("vault_screen")) or {}).get("frame") or {}
        w, h = fr.get("width") or 0, fr.get("height") or 0
        if not (16 <= w <= 20000 and 16 <= h <= 20000):
            continue
        url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + f"/api/cameras/{urllib.parse.quote(sid, safe='')}"
        try:
            req = urllib.request.Request(url, data=json.dumps({"board_width": w, "board_height": h}).encode(), method="PUT",
                                         headers={"Authorization": f"Bearer {ARGS.api_key}", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"screen size: could not update {sid}: {e}", flush=True)
            continue
        print(f"screen size: {sid} set to {w} x {h} from Doohly", flush=True)


def bookings_loop():
    while True:
        for job in (sync_expectations, sync_rotations, sync_board_sizes):
            try:
                job()
            except Exception as e:  # never let the helper thread die
                print(f"bookings: {job.__name__} failed: {e}", flush=True)
        time.sleep(300)



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
    if site:
        recs = STORE.recent(200, site)
        recs = [r for r in recs if r.get("site_id") == site and r.get("type") == "creative_change"] + STORE.alerts(50, site)
    else:
        recs = STORE.newest_per_site(photo=True) + STORE.alerts(50)
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
        pops = sorted(newest.values(), key=lambda r: name_order(disp(r)))
    alerts = [r for r in recs if r.get("type") == "fault"][-50:][::-1]
    hb = {}
    if live is not None:
        for c in live:
            c = dict(c, site_name=c.get("name"))
            if c.get("expected_per_hour"):
                c["coverage"] = c.get("plays_last_hour", 0) / c["expected_per_hour"]
            hb[c.get("site_id")] = c
    else:                                        # otherwise the latest heartbeat
        last = STORE.last_heartbeats(1)
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
        for k, c in sorted(hb.items(), key=lambda kc: site_order(dict(kc[1], id=kc[0]))) if not site or k == site) or "<tr><td colspan=8 class='muted'>No heartbeat yet</td></tr>"
    cards = []
    camp_cache = {}
    for r in pops:
        q = r.get("quality", {})
        gone = r.get("_images_removed")
        img = (f"<div class='thumb'><span>photo removed after {POP_IMAGE_DAYS:g} days</span></div>" if gone else
               f"<div class='thumb'><img loading='lazy' src='/images/{esc(r['event_id'])}.roi.jpg' alt=''></div>"
               if "roi_sha256" in r else "<div class='thumb'><span>" + (
                   "photographed every few minutes per creative, not this play" if r.get("images") == "skipped_by_policy"
                   else "no photo") + "</span></div>")
        full = (f" &middot; <a href='/images/{esc(r['event_id'])}.frame.jpg' target='_blank'>full frame</a>"
                if "frame_sha256" in r and not gone and not r.get("_frame_removed") else "")
        flags = " [blank]" if r.get("is_blank") else ""
        flags += " [video]" if r.get("video_creative") else ""
        verdict = "" if q.get("usable", True) else f"<br><span class='bad'>{esc(q.get('reason'))}</span>"
        more = "" if site else f"<br><a href='?site={urllib.parse.quote(r.get('site_id') or '')}'>All PoP from this site</a>"
        cm = campaign_for_record(r, camp_cache)
        camp = ""
        if cm:
            camp = f"<br><span>{esc(cm)}</span>"
        cid = canon(r.get("site_id"), r.get("local_creative_id"))
        shown = esc(cid) if cid == r.get("local_creative_id") else \
            f"<span title='reported as {esc(r.get('local_creative_id'))}, merged into {esc(cid)} (same artwork)'>{esc(cid)}</span>"
        cards.append(f"<div class='card'>{img}<div><b>{esc(disp(r))}</b> {shown}{flags}{camp}<br>"
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
        if method == "GET" and self.path.split("?")[0] == "/api/cameras" and code == 200:
            try:                                  # every page lists the sites A to Z
                body = json.dumps(sorted(json.loads(body), key=site_order)).encode()
            except (ValueError, TypeError):
                pass
        if method in ("PUT", "POST", "DELETE") and re.match(r"/api/cameras", self.path) and 200 <= code < 300:
            nudge_sync("sites changed")           # only re-syncs if a Vault screen changed
        if method == "POST" and self.path == "/api/cameras" and code == 201:
            try:                                  # a new site: in setup until its region fits
                cam = json.loads(body)
                if cam.get("setup") and cam.get("id"):
                    STORE.setup_start(cam["id"], time.time())
            except (ValueError, AttributeError):
                pass
        mr = re.fullmatch(r"/api/cameras/([^/?]+)/roi", self.path)
        if method in ("PUT", "POST") and mr and 200 <= code < 300:
            STORE.setup_region(urllib.parse.unquote(mr.group(1)), time.time())   # the setup checks start over
        m = re.fullmatch(r"/api/cameras/([^/?]+)", self.path)
        if method == "PUT" and m and 200 <= code < 300:
            old_id = urllib.parse.unquote(m.group(1))
            try:
                cam = json.loads(body)
                new_id, new_name = cam.get("id") or cam.get("site_id"), cam.get("name")
            except ValueError:
                new_id = None
            if new_id and new_id != old_id:    # keep this site's history under its new ID
                STORE.rename_site(old_id, new_id, new_name)
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
        if path == "/sync/inputs":                # creatives-sync (API key, not the review login)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {ARGS.api_key}"):
                return self.reply(401, {"error": "bad token"})
            return self.reply(200, sync_inputs())
        if path in ("/pop/best",) or re.fullmatch(r"/pop/frame/[0-9a-f-]{36}\.jpg", path):
            # creatives-sync's PoP uploads (API key, not the review login)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {ARGS.api_key}"):
                return self.reply(401, {"error": "bad token"})
            if path == "/pop/best":
                qs = urllib.parse.parse_qs(query)
                try:
                    t0, t1 = float(qs["since"][0]), float(qs.get("until", [time.time()])[0])
                    ph = qs.get("phase", ["day"])[0]
                    assert ph in ("day", "night") and t1 > t0 and t1 - t0 <= 15 * 86400
                except (KeyError, ValueError, AssertionError):
                    return self.reply(400, {"error": "since (unix time), until and phase=day|night; 15 days at most"})
                got = in_worker(_worker_best, t0, t1, ph, None)
                return self.reply(200, got[1] if got else best_pops(t0, t1, ph))
            try:
                with open(os.path.join(ARGS.out, "images", path.rsplit("/", 1)[1].replace(".jpg", ".frame.jpg")), "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                return self.reply(404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authorised():
            return
        if path.startswith("/api/"):
            return self.proxy("GET")
        if path == "/backup":                     # Health page download, or creatives-sync's nightly one
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {ARGS.api_key}") \
                    and not self.authorised():
                return
            try:
                dest, manifest = make_backup()
            except Exception as e:
                return self.reply(502, {"error": str(e)})
            try:
                size = os.path.getsize(dest)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(dest)}"')
                self.send_header("X-Backup-Manifest", json.dumps({k: manifest.get(k) for k in ("created_at", "plays", "warnings")}))
                self.end_headers()
                with open(dest, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
            finally:
                try:
                    os.remove(dest)
                except OSError:
                    pass
            return
        if path == "/backup/list":                # Health page: the nightly backups in SharePoint
            if not self.authorised():
                return
            code, body = sync_call("GET", "/backups", timeout=30)
            return self.reply(code if code != 404 else 200, body if code != 404 else {"items": [], "configured": False})
        if path == "/sites/setup":                # region page: setting the site up, and the go-live checks
            sid = (urllib.parse.parse_qs(query).get("site") or [""])[0]
            cams = gateway_cameras()
            if cams is None:
                return self.reply(502, {"error": "cannot reach the gateway"})
            setup_reconcile(cams)
            cam = next((c for c in cams if c.get("id") == sid), None)
            if not cam:
                return self.reply(404, {"error": f"no site {sid}"})
            return self.reply(200, setup_status(cam))
        if path == "/records/sites":
            return self.reply(200, sorted(recorded_sites().values(), key=lambda e: name_order(e.get("name") or e["site_id"])))
        if path == "/health.json":
            return self.reply(200, health_data())
        if path == "/health/storage":
            return self.reply(200, storage_data())
        if path == "/bookings.json":
            m = re.search(r"(?:^|&)week=(\d{4}-\d\d-\d\d)", query)
            wk = monday(datetime.fromisoformat(m.group(1)).date()) if m else monday(datetime.now(LOCAL_TZ).date())
            return self.reply(200, cached_report(wk))
        if path == "/bookings/screens":
            return self.reply(200, vault_screens())
        if path == "/doohly/players":
            return self.reply(200, doohly_players())
        if path == "/sites/rotation":
            return self.reply(200, rotation_info())
        if path == "/bookings/artwork-browse":     # choosing a campaign's folder in SharePoint
            if not self.authorised():
                return
            fid = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id") or [""])[0]
            code, body = sync_call("GET", "/artwork/browse" + ("?id=" + urllib.parse.quote(fid, safe="") if fid else ""), timeout=30)
            return self.reply(code, body)
        if path == "/bookings/missing":           # Bookings page: bookings without artwork
            return self.reply(200, missing_artwork())
        if path == "/bookings/sync":              # artwork sync status for the Bookings page
            code, st = sync_call("GET", "/status")
            st.setdefault("configured", code != 404 and bool(sync_url()))
            return self.reply(200, st)
        if path in ("/roi", "/sites", "/site", "/sites/import", "/health", "/bookings"):
            body = {"/roi": ROI_PAGE, "/sites": SITES_PAGE, "/site": SITE_PAGE, "/sites/import": IMPORT_PAGE,
                    "/health": HEALTH_PAGE, "/bookings": BOOKINGS_PAGE}[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/sites/template.csv":
            body = IMPORT_TEMPLATE.encode("utf-8-sig")       # BOM so Excel reads UTF-8
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="pop-sites-template.csv"')
        elif path.startswith("/static/") and path[8:] in STATIC:     # the LUMO look: styles, font, icon
            try:
                with open(os.path.join(STATIC_DIR, path[8:]), "rb") as f:
                    body = f.read()
            except OSError:
                return self.reply(404)
            self.send_response(200)
            self.send_header("Content-Type", STATIC[path[8:]])
            self.send_header("Cache-Control", "max-age=3600")
        elif path == "/":
            m = re.search(r"(?:^|&)site=([^&]+)", query)
            body = render_page(m.group(1) if m else "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/creatives/file":           # a booked creative's artwork, from the library
            load_creatives()
            m = re.search(r"(?:^|&)f=([^&]+)", query)
            full = creative_file(urllib.parse.unquote(m.group(1))) if m else None
            if not full:
                return self.reply(404)
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if full.lower().endswith(".png") else "image/jpeg")
            self.send_header("Cache-Control", "max-age=3600")
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
        if body[:15].lower() == b"<!doctype html>":
            body = lumo_page(body, path).replace(b"</body>", IMG_RETRY + b"</body>", 1)
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
        if self.path == "/pop/best":              # creatives-sync, with the photos already uploaded
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {ARGS.api_key}"):
                return self.reply(401, {"error": "bad token"})
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                t0, t1, ph = float(d["since"]), float(d.get("until") or time.time()), d.get("phase", "day")
                assert ph in ("day", "night") and t1 > t0 and t1 - t0 <= 15 * 86400
                cur = {str(k): str(v) for k, v in (d.get("current") or {}).items()}
            except (KeyError, ValueError, TypeError, AssertionError):
                return self.reply(400, {"error": "since (unix time), until, phase=day|night, current; 15 days at most"})
            # the photos up in SharePoint are kept here (record and photo) past the usual 3 days,
            # so each run scores them again alongside the new ones; one replaced is released
            STORE.set_pins(f"best-{ph}", cur.values())
            got = in_worker(_worker_best, t0, t1, ph, cur)
            res = got[1] if got else best_pops(t0, t1, ph, cur)
            if not res.get("error"):
                # the best photos offered are kept until the next run (one of them may be
                # uploaded now); every other full frame up to t1 is no longer needed for this phase
                STORE.set_pins(f"offered-{ph}", [it.get("event_id") for it in res.get("items", [])])
                STORE.note_run(ph, t1)
            return self.reply(200, res)
        if self.path.startswith("/api/"):          # review page -> gateway
            if self.authorised():
                self.proxy("POST")
            return
        if self.path == "/bookings/sync":          # Bookings page: "Sync now"
            if not self.authorised():
                return
            code, body = sync_call("POST", "/sync", timeout=10)
            return self.reply(code if code >= 400 else 202, body)
        m = re.fullmatch(r"/bookings/pop-run\?phase=(day|night)", self.path)
        if m:                                       # Bookings page: upload the best PoP photos now
            if not self.authorised():
                return
            code, body = sync_call("POST", f"/pop/run?phase={m.group(1)}", timeout=10)
            return self.reply(code if code >= 400 else 202, body)
        if self.path == "/bookings/artwork-folder":   # a campaign's folder under another number, or undo that
            if not self.authorised():
                return
            data = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
            code, body = sync_call("POST", "/artwork/folder", timeout=15, data=data)
            return self.reply(code, body)
        if self.path == "/health/forget":         # Health page: remove an offline gateway
            if not self.authorised():
                return
            try:
                gid = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)))["gateway_id"]
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "gateway_id required"})
            code, body = forget_gateway(str(gid))
            return self.reply(code, body)
        if self.path == "/bookings/artwork-pick":   # match a booking against a file in its folder, or undo that
            if not self.authorised():
                return
            data = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
            code, body = sync_call("POST", "/artwork/pick", timeout=15, data=data)
            return self.reply(code, body)
        if self.path.startswith("/bookings/artwork?"):   # artwork for a booking with none: to SharePoint
            if not self.authorised():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > 300 << 20:
                return self.reply(413, {"error": "files up to 300 MB"})
            data = self.rfile.read(n)
            code, body = sync_call("POST", "/artwork/upload?" + self.path.split("?", 1)[1], timeout=600, data=data)
            return self.reply(code, body)
        if self.path == "/bookings/creatives":     # upload a creatives library (zip)
            if not self.authorised():
                return
            raw = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 512 << 20))
            try:
                n = install_creatives_zip(raw)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            return self.reply(200, {"status": "loaded", "creatives": n})
        if self.path in ("/bookings/upload", "/bookings/link"):
            if not self.authorised():
                return
            raw = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 64 << 20))
            try:
                body = json.loads(raw)
            except ValueError:
                return self.reply(400, {"error": "not a JSON file"})
            if self.path == "/bookings/link" and body.get("separate"):   # undo a wrong merge
                sid, cid = body.get("site_id"), body.get("separate")
                if not isinstance(sid, str) or not isinstance(cid, str):
                    return self.reply(400, {"error": "site_id and separate (a creative ID) required"})
                code, res = separate(sid, cid)
                return self.reply(code, res)
            if self.path == "/bookings/link":
                sid = body.get("site_id")
                ids = body.get("creative_ids") or ([body["creative_id"]] if body.get("creative_id") else [])
                if not sid or not ids or not all(isinstance(i, str) for i in ids):
                    return self.reply(400, {"error": "site_id and creative_id(s) required"})
                save_link(sid, ids, body.get("campaign_id") or "")
                return self.reply(200, {"status": "saved", "creatives": len(ids)})
            if not isinstance(body, dict) or not isinstance(body.get("proposals"), list):
                return self.reply(400, {"error": "not a Vault pull: expected an object with a proposals list"})
            body.setdefault("generated_at", iso(time.time()))
            body.setdefault("source", "uploaded")
            tmp = os.path.join(ARGS.out, "vault-bookings.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f)
            os.replace(tmp, os.path.join(ARGS.out, "vault-bookings.json"))
            load_bookings()
            threading.Thread(target=sync_expectations, daemon=True).start()
            nudge_sync("new Vault pull uploaded")
            return self.reply(200, {"status": "loaded", "campaigns": len(body["proposals"])})
        if self.path == "/sites/reset-creatives":  # site settings: forget a site's creatives
            if not self.authorised():
                return
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
                sid, confirm, history = body["site_id"], body.get("confirm"), bool(body.get("history"))
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "site_id required"})
            if not sid or confirm != sid:
                return self.reply(400, {"error": "type the site ID exactly to confirm"})
            asked = time.time()
            url = os.environ.get("GATEWAY_URL", "http://gateway:8090").rstrip("/") + \
                f"/api/cameras/{urllib.parse.quote(sid, safe='')}/reset-creatives"
            try:
                req = urllib.request.Request(url, data=json.dumps({"restart_numbering": history}).encode(),
                                             method="POST", headers={"Authorization": f"Bearer {ARGS.api_key}",
                                                                     "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read()).get("error")
                except ValueError:
                    err = None
                return self.reply(e.code, {"error": f"gateway refused: {err or e.reason}"})
            except Exception as e:
                return self.reply(502, {"error": f"cannot reach the gateway: {e}"})
            deleted = 0
            forget_art(sid)
            forget_merges(sid)
            REPORTS["gen"] += 1
            if history:
                note_reset(sid, asked)
                deleted = STORE.delete_site(sid)
                with LOCK:
                    links = load_links()
                    if links.pop(sid, None) is not None:
                        tmp = os.path.join(ARGS.out, "creative-links.json.tmp")
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(links, f, indent=1)
                        os.replace(tmp, os.path.join(ARGS.out, "creative-links.json"))
            print(f"creatives reset for site {sid}" + (f"; deleted {deleted} record(s) and its links" if history else ""), flush=True)
            return self.reply(200, {"reset": True, "history": history, "deleted": deleted})
        if self.path in ("/restore/upload", "/restore/sharepoint") or self.path.startswith("/restore/sharepoint?"):
            if not self.authorised():                 # Health page: a backup to restore, checked and previewed
                return
            dest = os.path.join(_backup_dir("restore-incoming"), uuid.uuid4().hex + ".tar.gz")
            try:
                if self.path == "/restore/upload":
                    left = int(self.headers.get("Content-Length") or 0)
                    if left <= 0:
                        return self.reply(400, {"error": "no file"})
                    with open(dest, "wb") as f:
                        while left > 0:
                            chunk = self.rfile.read(min(left, 1 << 20))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                    if left:
                        raise ValueError("the upload didn't arrive complete")
                else:
                    item = (urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").get("id") or [""])[0]
                    if not re.fullmatch(r"[A-Za-z0-9!._-]{6,200}", item):
                        return self.reply(400, {"error": "which backup?"})
                    req = urllib.request.Request(f"{sync_url()}/backups/{urllib.parse.quote(item)}",
                                                 headers={"Authorization": f"Bearer {ARGS.api_key}"})
                    with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
                return self.reply(200, restore_preview(dest))
            except (ValueError, OSError, urllib.error.URLError) as e:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                return self.reply(400, {"error": str(e)})
        if self.path == "/backup/sharepoint":         # Health page: back up to SharePoint now
            if not self.authorised():
                return
            code, body = sync_call("POST", "/backup/run", timeout=10)
            return self.reply(code, body)
        if self.path == "/restore/apply":             # Health page: restore it (everything restarts)
            if not self.authorised():
                return
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
            except ValueError:
                body = {}
            if body.get("confirm") != "RESTORE":
                return self.reply(400, {"error": "type RESTORE to confirm"})
            code, reply = restore_apply(body.get("token"))
            return self.reply(code, reply)
        if self.path == "/sites/setup":            # region page: go live, or back into setup
            if not self.authorised():
                return
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
                sid, live, force = body["site_id"], bool(body["live"]), bool(body.get("force"))
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "site_id and live (true or false) required"})
            code, reply = set_live(sid, live, force)
            return self.reply(code, reply)
        if self.path == "/roi/fit":                # region page: fit the handles to matched artwork
            if not self.authorised():
                return
            try:
                d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)))
                sid, reg = str(d["site_id"]), d["region"]
                _homog(reg["roi"])
            except (ValueError, KeyError, TypeError, IndexError):
                return self.reply(400, {"error": "site_id and region (roi, roi_shape, …) required"})
            except Exception:
                return self.reply(400, {"error": "the region's corners don't form a box"})
            cam = next((c for c in gateway_cameras() or [] if c.get("id") == sid), None)
            if not cam:
                return self.reply(404, {"error": f"site {sid} not found on the gateway"})
            return self.reply(200, fit_region(cam, reg, d.get("inset_pct", cam.get("inset_pct"))))
        if self.path == "/sites/rotation":         # Site page: follow Doohly's loop, or set by hand
            if not self.authorised():
                return
            try:
                d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
                sid, manual = str(d["site_id"]), bool(d["manual"])
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "site_id and manual required"})
            with ROT_LOCK:
                st = rotation_state()
                if manual:
                    st["manual"][sid] = True
                else:
                    st["manual"].pop(sid, None)
                save_rotation_state(st)
            if not manual:
                threading.Thread(target=sync_rotations, daemon=True).start()
            REPORTS["gen"] += 1
            return self.reply(200, rotation_info().get(sid) or {})
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
            n = STORE.delete_site(sid)
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
            try:
                hb = json.loads(body)
                STORE.add_heartbeat(hb, body.decode())
                note_heartbeat(hb)
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
        if meta.get("type") == "creative_change":
            cut = load_resets().get(meta.get("site_id"))
            t = iso_to_ts(meta.get("play_started_at"))
            if cut and t is not None and t < cut:   # played before the site was reset: its ID is reused now
                return self.reply(200, {"status": "dropped: played before the site's creatives were reset"})
        if self.headers.get("Idempotency-Key") != eid:
            return self.reply(400, {"error": "idempotency key mismatch"})
        if STORE.seen(eid):
            return self.reply(409, {"status": "duplicate"})
        checks = {}
        for name in ("frame", "roi"):
            data = parts.get(name)
            if data:
                ok = hashlib.sha256(data).hexdigest() == meta.get(f"{name}_sha256")
                checks[name] = "sha256 ok" if ok else "SHA256 MISMATCH"
                with open(os.path.join(ARGS.out, "images", f"{eid}.{name}.jpg"), "wb") as f:
                    f.write(data)
        meta["_received"] = {"endpoint": self.path, "images": checks}
        if not STORE.add(meta):
            return self.reply(409, {"status": "duplicate"})
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
    restored = bk.apply_staged(ARGS.out, log=lambda m: print(m, flush=True))   # a restore waiting to go in
    global STORE
    STORE = store.Store(ARGS.out, log=lambda m: print(m, flush=True))
    if restored:
        n = STORE.check_photos()
        if n:
            print(f"restore: {n} record(s) have no photos on this machine (they stay, without photos)", flush=True)
    for hb in STORE.last_heartbeats(500):                                   # last known state per gateway
        try:
            t = datetime.fromisoformat(hb.get("sent_at", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        note_heartbeat(hb, received=t)
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=bookings_loop, daemon=True).start()
    threading.Thread(target=report_warmer, daemon=True).start()
    threading.Thread(target=STORE.housekeeper, daemon=True).start()
    print(f"receiver on {ARGS.bind}:{ARGS.port}, storing in {ARGS.out}", flush=True)
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 256          # a page asks for dozens of photos at once; the default 5 drops some
    Server((ARGS.bind, ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
