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
<nav style="display:flex;gap:16px;margin-bottom:14px"><a href="/" style="font-weight:600;text-decoration:none">Review</a><a href="/sites" style="color:var(--muted);text-decoration:none">Sites</a><a href="/bookings" style="color:var(--muted);text-decoration:none">Bookings</a><a href="/health" style="color:var(--muted);text-decoration:none">Health</a></nav>
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
      return `<tr><td><b>${esc(c.name)}</b>${c.name !== c.id ? `<br><span class="muted">${esc(c.id)}</span>` : ''}</td>
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
<label class="check" id="rotauto" hidden><input type="checkbox" name="rot_doohly"> Keep in step with Doohly's loop <span class="muted" id="rotdoohly"></span></label>
<p class="hint" id="rotnote" hidden></p>
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
  for (const k of copy ? ['model','username'] : ['site_id','display_name','model','host','username','rtsp_url_override','vapix_url_override','vault_screen'])
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
  loadRotation(c.id);
  $('save').textContent = 'Save changes'; $('test').hidden = $('region').hidden = $('del').hidden = $('dup').hidden = $('reset').hidden = false;
  sync();
}

async function loadRotation(id) {
  const all = await fetch('/sites/rotation').then(r => r.ok ? r.json() : {}).catch(() => ({}));
  rot = all[id] || null;
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
<nav style="display:flex;gap:16px;margin-bottom:14px"><a href="/" style="color:var(--muted);text-decoration:none">Review</a><a href="/sites" style="font-weight:600;text-decoration:none">Sites</a><a href="/bookings" style="color:var(--muted);text-decoration:none">Bookings</a><a href="/health" style="color:var(--muted);text-decoration:none">Health</a></nav>
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
<nav><a href="/">Review</a><a href="/sites">Sites</a><a href="/bookings">Bookings</a><a href="/health" class="on">Health</a></nav>
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
    const st = await (await fetch('/health/storage')).json().catch(() => null);
    if (st) $sub.innerHTML += `<br>This server: ${st.records.toLocaleString()} PoP and alert record(s) since ${esc(st.oldest || '–')} (${st.db_mb} MB), ` +
      `${st.photo_files.toLocaleString()} photo file(s) (${st.photo_mb} MB) · photos kept ${st.image_days} day(s), alert photos ${st.alert_image_days}, records ${st.record_days} · ` +
      `<span class="${st.free_gb < 5 ? 'bad' : ''}">${st.free_gb} GB free</span> of ${st.disk_gb} GB`;
  } catch (e) { document.getElementById('sub').innerHTML = '<span class="bad">' + esc(e.message || e) + '</span>'; }
}
let $sub; load(); setInterval(load, 15000);
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
.site > summary{list-style:none;cursor:pointer;display:flex;gap:6px 14px;align-items:center;flex-wrap:wrap;padding:11px 16px}
.site > summary::-webkit-details-marker{display:none}
.site > summary::before{content:'\25B8';color:var(--muted);width:10px} .site[open] > summary::before{content:'\25BE'}
.site[open] > summary{border-bottom:1px solid var(--line)} details.site > :not(summary){margin-left:16px;margin-right:16px}
.site[open]{padding-bottom:12px} .site.plain{padding:11px 16px;display:flex;gap:14px;flex-wrap:wrap}
.sname{font-weight:600;font-size:15px} .sname a{text-decoration:none}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:2px 9px;white-space:nowrap}
.chip.warn{border-color:var(--warn);color:var(--warn)} .chip.bad{border-color:var(--bad);color:var(--bad)} .chip.ok{border-color:var(--ok);color:var(--ok)}
.chip.warn .muted,.chip.bad .muted,.chip.ok .muted{color:inherit;opacity:.8}
button.link{background:none;border:0;padding:0 0 0 4px;color:var(--muted);cursor:pointer;font:inherit;font-size:12px}
button.link:hover{color:var(--fg);text-decoration:underline}
tr.sub td{background:var(--panel);padding-top:3px;padding-bottom:3px} tr.sub .thumb{width:54px;height:30px}
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
</style></head><body><main>
<nav><a href="/">Review</a><a href="/sites">Sites</a><a href="/bookings" class="on">Bookings</a><a href="/health">Health</a></nav>
<h1>Bookings vs delivery</h1><p class="muted" id="sub" style="margin:2px 0 0">Loading&hellip;</p>
<p class="muted" id="sync" style="margin:4px 0 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap" hidden><span id="synctxt"></span><button class="btn" id="syncnow" style="padding:2px 9px">Sync now</button></p>
<div class="muted" id="popup" style="margin:4px 0 0" hidden><span id="poptxt"></span>
 <button class="btn" data-phase="day" style="padding:2px 9px" title="Upload the best daytime photo of each campaign since the last run, now">Upload day photos now</button>
 <button class="btn" data-phase="night" style="padding:2px 9px" title="Upload the best night photo of each campaign since the last run, now">Upload night photos now</button>
 <details id="poprecent" style="margin-top:4px" hidden></details></div>
<div class="bar"><button class="btn" id="prev">&larr; Previous week</button><b id="wk"></b><button class="btn" id="next">Next week &rarr;</button>
<button class="btn" id="all">Expand / collapse all</button><span class="muted" id="built" style="font-size:12px"></span>
<span style="flex:1"></span><label class="btn" style="cursor:pointer">Upload Vault pull or creatives<input type="file" id="up" accept=".json,.zip,application/json,application/zip" hidden></label></div>
<div id="out"></div>
<details class="note muted"><summary>How this is worked out</summary>
<p>Vault lists, for each booked campaign, the screens it books with its weeks and plays per loop. For each site linked to a Vault screen, this page adds up that week's bookings and compares them with what the gateway saw.</p>
<p><b>Per loop</b> figures are slots held per loop of the site's rotation. A creative on screen for two slots back to back counts as two; a longer run than that creative usually holds (a missed change, hold-over or freeze) only counts its usual length.</p>
<p>Vault gives each booking's first and last week and how many weeks it books, but not which ones. A booking with gaps is only <b>certain</b> in its first and last week; in between it shows as <i>may be booked this week</i> and isn't counted in true coverage (it can still be matched to a creative). Week-by-week data from a Vault API would remove this.</p>
<p><b>Artwork matching.</b> The booked creatives for each screen are pulled from SharePoint (Booked Campaigns / Creative+MIs, using each campaign's Material Instructions where they name the file for the screen). Each creative the gateway sees is compared with that artwork using its PoP images: fine detail (text, edges, layout) with the camera's glare and exposure removed. A clear match (at least 40% and well ahead of every other campaign) assigns it to that campaign as <span class="pill ok">artwork match</span>; a weaker one shows <span class="pill">looks like …?</span> and stays unassigned; <span class="pill">matches no booked artwork</span> usually means programmatic, house, or a creative that isn't in SharePoint. Artwork that played outside the dates in its Material Instructions is flagged <span class="pill warn">off schedule</span>. Video creatives aren't matched yet.</p>
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
  return files.slice(0, 4).map(a => `<a href="${artUrl(a.file)}" target="_blank" title="${esc(a.name)}${a.from || a.to ? ' · ' + fmtd(a.from) + '–' + fmtd(a.to) : ''}${a.rotation ? ' · ' + a.rotation + '%' : ''}"><img class="thumb sm" loading="lazy" src="${artUrl(a.file)}" alt=""></a>`).join('') +
     (files.length > 4 ? ` <span class="muted">+${files.length - 4}</span>` : '');
}
function refThumb(cr) {
  const a = cr.art; if (!a || !a.file || a.status === 'none') return '';
  return `<a href="${artUrl(a.file)}" target="_blank" title="${esc(a.name)}"><img class="thumb" loading="lazy" src="${artUrl(a.file)}" alt="booked artwork" style="${a.status === 'possible' ? 'opacity:.55' : ''}"></a>`;
}
function artPill(cr, s, cname) {
  const a = cr.art;
  if (cr.link) return '<span class="pill">set by hand</span>';
  if (!a) return '';
  const sc = `match ${Math.round(a.score * 100)}%, next best ${Math.round(a.next * 100)}%, over ${a.samples} PoP image(s) and ${a.candidates} booked artwork(s)`;
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
  const pop = cr => cr.image ? `<a href="/images/${cr.image}.roi.jpg" target="_blank"><img class="thumb" loading="lazy" src="/images/${cr.image}.roi.jpg" alt="PoP image"></a>` : '';
  const pick = (cr, ids) => cr.blank ? '<span class="muted">blank screen</span>'
     : `<select data-site="${esc(s.site_id)}" data-ids="${esc(ids.join(','))}">${opts(cr.mixed ? '\u0000' : cr.link, cr)}</select>
       ${cr.mixed ? '<span class="pill">mixed</span>' : artPill(cr, s, cname)}${cr.assigned === 'unassigned' ? ' <span class="pill warn">unassigned</span>' : ''}${cr.off_schedule && !(cr.art || {}).off_schedule ? ' <span class="pill warn">off schedule</span>' : ''}`;
  const groups = s.groups || [];
  const crs = groups.map(g => {
    const ids = g.members.map(m => m.creative_id), many = ids.length > 1, open = OPEN_GROUPS.has(s.site_id + '|' + g.key);
    const label = many ? `<b>${esc(g.creative_id)}</b> <button type="button" class="link" data-group="${esc(s.site_id + '|' + g.key)}" title="${esc(ids.join(', '))}">${open ? '&#9662;' : '&#9656;'} ${ids.length} IDs</button>` : `<b>${esc(g.creative_id)}</b>`;
    const head = `<tr class="grp${open ? ' open' : ''}"><td>${pop(g)}</td><td>${refThumb(g)}</td>
     <td>${label}${(g.merged || []).length ? ` <span class="muted" style="font-size:12px" title="Merged into ${esc(ids.join(', '))} as the same artwork (the gateway now reports them as one ID): ${esc(g.merged.join(', '))}">+${g.merged.length} merged</span>` : ''}${g.video ? ' <span class="pill">video</span>' : ''}${g.blank ? ' <span class="pill">blank</span>' : ''}</td>
     <td class="n">${g.plays}</td><td class="n">${one(g.share)}</td><td>${pick(g, ids)}</td></tr>`;
    if (!many || !open) return head;
    return head + g.members.map(m => `<tr class="sub"><td>${pop(m)}</td><td></td><td class="muted">${esc(m.creative_id)}</td>
     <td class="n">${m.plays}</td><td class="n">${one(m.share)}</td><td>${pick(m, [m.creative_id])}</td></tr>`).join('');
  }).join('');
  const nIds = groups.reduce((n, g) => n + g.members.length, 0);
  const tc = s.true_coverage, tcls = tc == null ? '' : tc >= 0.9 ? 'ok' : tc >= 0.75 ? 'warn' : 'bad';
  const tsub = s.booked_linked_per_loop ? (s.booked_linked_per_loop < s.booked_per_loop
       ? `of ${s.booked_linked_per_loop} linked booked slot(s); ${s.booked_per_loop - s.booked_linked_per_loop} not linked yet` : 'booked slots delivered')
       : 'link creatives to campaigns to measure';
  const notSeen = s.campaigns.filter(c => c.status === 'not seen').length;
  const chip = (k, v, cls) => `<span class="chip ${cls || ''}"><span class="muted">${k}</span> ${v}</span>`;
  return `<details class="site" data-site="${esc(s.site_id)}"${OPEN_SITES.has(s.site_id) ? ' open' : ''}><summary>
     <span class="sname"><a href="/?site=${encodeURIComponent(s.site_id)}">${esc(s.name)}</a></span>
     <span class="muted">${esc(s.vault_screen)} · ${s.slot_count} × ${s.slot_s} s</span>
     ${s.doohly ? `<span class="pill ${s.doohly.online ? 'ok' : 'bad'}" title="Doohly player ${esc(s.doohly.player)}, last seen ${esc(s.doohly.alive_at || '?')}; bookings read ${esc(s.doohly.bookings_at || '?')}">player ${s.doohly.online ? 'online' : 'offline'}</span>` : ''}
     <span class="chips">${chip('True coverage', pct(tc), tcls)}${chip('Booked', `${s.booked_per_loop ?? 0} / ${s.slot_count}`, (s.booked_per_loop || 0) > s.slot_count ? 'warn' : '')}
       ${s.unassigned_per_loop > 0.05 ? chip('Unassigned', one(s.unassigned_per_loop) + ' / loop', 'warn') : ''}${notSeen ? chip('Not seen', notSeen + ' campaign(s)', 'bad') : ''}
       ${s.warnings.length ? chip('', s.warnings.length + ' warning(s)', 'warn') : ''}</span></summary>
     ${s.warnings.length ? '<ul class="w">' + s.warnings.map(w => `<li>${esc(w)}</li>`).join('') + '</ul>' : ''}
     <div class="tiles">
       ${tile('True coverage', pct(tc), tsub, tcls)}
       ${tile('Booked per loop', `${s.booked_per_loop ?? 0} / ${s.slot_count}`, `${s.campaigns.filter(c => c.certain).length} campaign(s)` + (s.campaigns.some(c => !c.certain) ? ` + ${s.campaigns.filter(c => !c.certain).length} ${s.doohly ? 'bonus if room' : 'maybe'}` : '') + (s.unsold_per_loop ? `, ${s.unsold_per_loop} unsold` : ''))}
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
  const dh = rep.doohly ? (rep.doohly.ok === false ? `<span class="bad">Doohly: ${esc(rep.doohly.error || 'failed')}</span> · `
      : `Bookings from Doohly (${esc(rep.doohly.at)}, ${rep.doohly.bookings} booking(s) on ${rep.doohly.sites} screen(s))` +
        (rep.doohly.unmatched.length ? ` · <span class="warn" title="${esc(rep.doohly.unmatched.map(u => u.site_id + ': ' + u.looked_for + ' (' + u.why + ')').join('; '))}">${rep.doohly.unmatched.length} site(s) not found in Doohly</span>` : '') + ' · ') : '';
  document.getElementById('sub').innerHTML = dh + (rep.pulled_at
    ? `${rep.doohly ? 'other screens from the ' : ''}Vault pull of ${esc(rep.pulled_at)} · ${rep.campaigns} booked campaign(s)` + (rep.source ? ` · ${esc(rep.source)}` : '') +
      (rep.creatives ? ` · artwork library of ${esc(rep.creatives.pulled_at)}, ${rep.creatives.count} file(s)` : ' · <span class="warn">no artwork library yet</span>') +
      (rep.artwork_enabled === false ? ' · <span class="warn">artwork matching unavailable (needs numpy and Pillow)</span>' : '')
    : (rep.doohly ? (rep.creatives ? `artwork library of ${esc(rep.creatives.pulled_at)}, ${rep.creatives.count} file(s)` : '<span class="warn">no artwork library yet</span>')
       : '<span class="warn">No Vault pull yet. The weekly scheduled task writes one, or upload a file.</span>'));
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
function shift(days) { const d = new Date(week + 'T00:00'); d.setDate(d.getDate() + days); week = d.toISOString().slice(0, 10);
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
    (st.missing ? ` · <span class="warn">${st.missing} without artwork</span>` : '') +
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
        ` <span class="muted">${esc(fmtT(r.at))}${r.was != null ? ` · replaced a ${Math.round(r.was)}` : ''}</span></div>`).join('');
  }
  txt.innerHTML = h;
  clearTimeout(syncTimer);
  if (st.running || (pu && pu.running)) syncTimer = setTimeout(loadSync, 4000);
  else if (wasRunning) load();
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
             record_days=store.RECORD_DAYS)
    d["oldest"] = local_time(iso(d["oldest"]), True) if d.get("oldest") else None
    STORAGE.update(at=time.time(), data=d)
    return d


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
                                   "slot_s": lp.get("slot_s")}))
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
    return sorted(names.values(), key=str.lower)


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
MERGES = {"loaded": False, "sites": {}, "mtime": None}
MERGE_MIN_PLAYS = 3                # each ID needs this many plays this week before it is merged
MERGE_LOOKALIKE = 0.75             # unassigned IDs: PoP images this alike (median of several) to merge


def _merges_path():
    return os.path.join(ARGS.out, "creative-merges.json")


def merges():
    if not MERGES["loaded"]:
        try:
            with open(_merges_path(), encoding="utf-8") as f:
                MERGES["sites"] = json.load(f).get("sites", {})
            MERGES["mtime"] = os.path.getmtime(_merges_path())
        except (OSError, ValueError):
            MERGES["sites"] = {}
        MERGES["loaded"] = True
    return MERGES["sites"]


def save_merges():
    tmp = _merges_path() + ".tmp"
    with LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sites": merges()}, f, indent=1)
        os.replace(tmp, _merges_path())
    MERGES["mtime"] = time.time()


def canon(site_id, cid):
    """The ID a creative ID was merged into (following chains), or itself."""
    m = merges().get(site_id)
    seen = 0
    while m and cid in m and seen < 20:
        cid = m[cid]["into"]
        seen += 1
    return cid


def site_plays(site_id, t0, t1):
    """A site's plays in [t0, t1), with merged IDs read as the ID they were merged into."""
    ps = STORE.plays(site_id, t0, t1)
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
                if link and not links.get(sid, {}).get(into):
                    save_link(sid, into, link)            # keep a hand-set link on the surviving ID
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
    if merges().pop(site_id, None) is not None:
        save_merges()



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
                                  "image": None, "last": 0})
        c["slots"] += r * frac
        seen_s += r * frac * S
        if s >= t0:
            c["plays"] += 1
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


def infer_programmatic(ds, week, crs, assigned, art, library):
    """Creatives that are almost certainly programmatic: the screen's loop fills its
    unbooked slots with programmatic (Doohly), Doohly says exactly what is booked,
    every booked image has its artwork here, and the creative matches none of it.
    Returns (set of creative IDs, reason it can't be done or "")."""
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
        cid, kinds = b.get("campaign_id"), {c.get("type") for c in b.get("creatives") or []}
        if "image" in kinds and cid not in have:
            return set(), f"the artwork for {b.get('campaign_name') or cid} isn't in the library, so its plays could be mistaken for programmatic"
        if "html" in kinds and cid not in taken:      # live web content can't be compared: needs a creative linked first
            return set(), (f"{b.get('campaign_name') or cid} is booked with live web content, which can't be matched: "
                           "link its creative to the campaign below and the rest can be worked out")
        if "video" in kinds and cid not in taken:
            video_block = True
    out = set()
    for cr in crs:
        a = art.get(cr["creative_id"]) or {}
        if cr["blank"] or assigned.get(cr["creative_id"]) != "unassigned":
            continue
        if a.get("status") != "none" or (a.get("samples") or 0) < PROG_MIN_SAMPLES or cr["plays"] < PROG_MIN_PLAYS:
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
ART_SAMPLES = 5                  # PoP images per creative compared
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


def _prep(img, inset=0.0):
    """Fine luma detail of an image on a fixed grid: low frequencies (glare,
    vignetting, exposure) removed, so it can be compared with a camera photo."""
    img = img.convert("RGB")
    if inset > 0:                         # the PoP image leaves out the board's edge
        w, h = img.size
        img = img.crop((w * inset, h * inset, w * (1 - inset), h * (1 - inset)))
    img = img.resize((ART_GW * 4, ART_GH * 4), _Image.BILINEAR).filter(_ImageFilter.GaussianBlur(1.2))
    img = img.resize((ART_GW, ART_GH), _Image.BOX).convert("L")
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


def _ref_fp(rel, inset):
    path = creative_file(rel)
    if not path:
        return None
    key = (path, os.path.getmtime(path), round(inset, 3))
    if key not in ART_CACHE:
        try:
            with _Image.open(path) as im:
                ART_CACHE[key] = _prep(im, inset)
        except Exception as e:
            print(f"creatives: cannot read {path}: {e}", flush=True)
            ART_CACHE[key] = None
    return ART_CACHE[key]


def _pop_fp(eid):
    key = ("pop", eid)
    if key not in ART_CACHE:
        try:
            with _Image.open(os.path.join(ARGS.out, "images", f"{eid}.roi.jpg")) as im:
                ART_CACHE[key] = _prep(im)
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
    if not ART_STATE["loaded"]:
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
    ART_STATE["dirty"] = False


def forget_art(site_id):
    """Drop a site's cached matches (its creatives were reset)."""
    if art_state().pop(site_id, None) is not None:
        ART_STATE["dirty"] = True
        save_art_state()


def _spread(ps, n):
    """Up to n plays spread evenly over the list (day and night, old and new)."""
    if len(ps) <= n:
        return ps
    return [ps[round(i * (len(ps) - 1) / (n - 1))] for i in range(n)]


def artwork_matches(cam, t0, t1):
    """For each creative the site showed in [t0, t1): the booked artwork it
    matches. A creative's match is worked out once and kept (on disk too); it
    is re-checked only when the artwork library changes or after a while, so
    new plays don't make every page load redo it. Returns {creative_id: {...}}."""
    if not ART_OK or not cam.get("vault_screen"):
        return {}
    cands = [c for c in screen_creatives(cam["vault_screen"]) if c.get("file") and c.get("kind", "image") == "image"]
    if not cands:
        return {}
    inset = max(0.0, min(0.4, float(cam.get("inset_pct") or 0) / 100.0))
    refs = [(c, _ref_fp(c["file"], inset)) for c in cands]
    refs = [(c, f) for c, f in refs if f is not None]
    if not refs:
        return {}
    stack = _np.stack([f for _, f in refs])
    lib = f"{CRE['mtime']}|{len(refs)}|{round(inset, 3)}"
    sid = cam.get("id")
    state = art_state().setdefault(sid, {})
    plays = site_plays(sid, t0, t1)
    by_cid = {}
    for p in plays:                                  # usable images in this period, oldest first
        if not p[2] and p[5] and (len(p) < 7 or p[6]):
            by_cid.setdefault(p[1], []).append(p)
    now = time.time()
    out = {}
    for cid, ps in by_cid.items():
        st = state.get(cid)
        fresh = st and st.get("lib") == lib and now - st.get("at", 0) < (
            ART_RECHECK_MATCHED if st["res"]["status"] == "matched" else ART_RECHECK_OTHER)
        if not fresh:
            sample = _spread(ps, ART_SAMPLES)
            fps = [f for f in (_pop_fp(p[4]) for p in sample) if f is not None]
            if not fps:
                continue
            per = _np.median(_np.stack([_scores(f, stack) for f in fps]), axis=0)   # per booked image
            order = _np.argsort(-per)
            best, bc = float(per[order[0]]), refs[order[0]][0]
            other = max([float(per[i]) for i in range(len(refs)) if refs[i][0]["campaign_id"] != bc["campaign_id"]] or [0.0])
            status = ("matched" if best >= ART_MATCH and best - other >= ART_MARGIN
                      else "possible" if best >= ART_POSSIBLE else "none")
            st = {"lib": lib, "at": now, "eid": sample[-1][4],
                  "res": {"status": status, "campaign_id": bc["campaign_id"], "name": bc.get("name"),
                          "file": bc.get("file"), "score": round(best, 2), "next": round(other, 2),
                          "from": bc.get("from"), "to": bc.get("to"), "samples": len(fps), "candidates": len(refs)}}
            state[cid] = st
            ART_STATE["dirty"] = True
        res = dict(st["res"])
        # played outside the dates its Material Instructions give it (e.g. a teaser left on)
        if res["status"] == "matched":
            last = datetime.fromtimestamp(ps[-1][0], LOCAL_TZ).date().isoformat()
            first = datetime.fromtimestamp(ps[0][0], LOCAL_TZ).date().isoformat()
            res["off_schedule"] = ("late" if res.get("to") and last > res["to"] else
                                   "early" if res.get("from") and first < res["from"] else "")
        res["eid"] = st.get("eid")
        out[cid] = res
    save_art_state()
    return out


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
                                                       "assigned", "art", "video", "blank")} for c in ms],
                    "plays": sum(c["plays"] for c in ms), "share": sum(c["share"] for c in ms),
                    "image": next((c["image"] for c in ms if c.get("image")), None),
                    "blank": key[0] == "blank", "video": all(c.get("video") for c in ms),
                    "assigned": head.get("assigned"), "art": head.get("art"),
                    "by": head.get("by") if len({c.get("by") for c in ms}) == 1 else "mixed",
                    "link": links.pop() if len(links) == 1 else "", "mixed": len(links) > 1,
                    "off_schedule": any((c.get("art") or {}).get("off_schedule") for c in ms),
                    "merged": sorted(a for a, m in merges().get(site_id, {}).items()
                                     if canon(site_id, a) in {c["creative_id"] for c in ms})})
    out.sort(key=lambda g: -g["share"])
    return out


def site_bookings(cam, week, links_all):
    """Booked vs delivered for one site in the week starting `week` (date)."""
    sid, S, N = cam.get("id"), (cam.get("slot_ms") or 0) / 1000.0, cam.get("slot_count") or 0
    res = {"site_id": sid, "name": cam.get("name"), "vault_screen": cam.get("vault_screen") or "",
           "slot_count": N, "slot_s": S, "campaigns": [], "creatives": [], "groups": [], "warnings": []}
    if not res["vault_screen"]:
        res["warnings"].append("No Vault screen set for this site (Settings).")
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
                           "from": x.get("from"), "to": x.get("to"), "rotation": x.get("rotation")} for x in mine])
    ds = doohly_site(sid, res["vault_screen"])
    inferred, why_not = (infer_programmatic(ds, week, crs, m, art, library) if ds else (set(), ""))
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
        c["delivery"] = c["delivered"] / c["plays_per_loop"] if c["plays_per_loop"] else None
        if not c["linked"] and not any(x["file"] for x in c["artwork"]):
            c["status"] = "not linked"      # no creative assigned and no artwork to look for: can't tell
            continue
        if not c["certain"]:
            c["status"] = "maybe" if not c["creatives"] else "seen"
            continue
        booked_linked += c["plays_per_loop"]
        c["status"] = ("not seen" if not c["creatives"] else "ok" if (c["delivery"] or 0) >= 0.9
                       else "under")
        delivered += min(c["delivered"], c["plays_per_loop"])
        if loops >= 1 and c["status"] == "not seen":
            res["warnings"].append(f"{c['name']} is booked for {c['plays_per_loop']} play(s) per loop but "
                                   "none of its creatives has been seen.")
    offs = {}
    for cr in crs:
        a = cr.get("art") or {}
        if a.get("off_schedule"):
            offs.setdefault((a["campaign_id"], a.get("name"), a["off_schedule"], a.get("from"), a.get("to")), []).append(cr["creative_id"])
    for (camp, name, kind, frm, to), ids in offs.items():
        when = f"after its end date ({to})" if kind == "late" else f"before its start date ({frm})"
        res["warnings"].append(f"{name} ({camp}) played {when} in its Material Instructions"
                               f" (seen as {', '.join(sorted(ids))}).")
    if ds and loops >= 20 and (ds.get("loop") or {}).get("programmatic") is not False:
        dbooked = sum(c["plays_per_loop"] for c in campaigns)          # sold, bonus and bonus-if-room
        room = max(0.0, N - dbooked)
        seen_p = tally["programmatic"]
        if abs(seen_p - room) > max(1.0, 0.25 * room) and not why_not:
            res["warnings"].append(f"Programmatic is taking {seen_p:.1f} slot(s) per loop, but Doohly leaves {room:.1f} "
                                   f"unbooked ({N} slots, {dbooked:g} booked)" +
                                   (": some programmatic may still be unassigned" if seen_p < room else
                                    ": something marked programmatic may belong to a booking") + ".")
        res["programmatic_room"] = round(room, 1)
    if tally["unassigned"] > 0.05 and loops >= 1:
        res["warnings"].append(f"{tally['unassigned']:.1f} slot(s) per loop show creatives not assigned yet: "
                               "mark them as a campaign, programmatic or house below.")
    if booked > N:
        res["warnings"].append(f"{'Doohly' if res.get('doohly') else 'Vault'} has {booked} plays per loop booked but the site's rotation has {N} slots: "
                               "check the slot count, or the screen runs a longer loop.")
    blank = sum(c["share"] for c in crs if c["blank"])
    elapsed = max(0.0, t1 - start)
    res["groups"] = group_creatives(sid, crs)
    res.update(campaigns=campaigns, creatives=sorted(crs, key=lambda c: -c["share"]),
               booked_per_loop=booked, unsold_per_loop=max(0, N - booked),
               programmatic_per_loop=tally["programmatic"], house_per_loop=tally["house"], unbooked_per_loop=tally["unbooked"],
               unassigned_per_loop=tally["unassigned"], blank_per_loop=blank,
               booked_linked_per_loop=booked_linked,
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


def cached_report(week, max_age=REPORT_MAX_AGE):
    """The Bookings report for a week, rebuilt at most once a minute (a background
    thread keeps this week's warm) or as soon as links or set-up change."""
    load_bookings()
    load_creatives()
    cams = gateway_cameras()
    ver = report_version(cams)
    key = week.isoformat()
    hit = REPORTS["weeks"].get(key)
    if hit and hit[1] == ver and time.time() - hit[0] < max_age:
        return hit[2]
    with REPORT_LOCK:                              # one build at a time; others wait and reuse it
        hit = REPORTS["weeks"].get(key)
        if hit and hit[1] == ver and time.time() - hit[0] < max_age:
            return hit[2]
        t = time.time()
        rep = bookings_report(week, cams)
        rep["built_at"] = local_time(iso(t), True)
        rep["build_s"] = round(time.time() - t, 2)
        REPORTS["weeks"][key] = (t, ver, rep)
        for k in [k for k, v in REPORTS["weeks"].items() if time.time() - v[0] > 3600]:
            REPORTS["weeks"].pop(k, None)
        return rep


def report_warmer():
    """Keep this week's Bookings report fresh so the page and review cards load at once."""
    while True:
        try:
            rep = cached_report(monday(datetime.now(LOCAL_TZ).date()), max_age=REPORT_MAX_AGE - 15)
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
    for cam in cams or []:
        out["sites"].append(site_bookings(cam, week, links))
    return out


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


def sync_call(method, path, timeout=5):
    """Talk to the creatives-sync service. Returns (status code, JSON)."""
    if not sync_url():
        return 404, {"configured": False, "error": "no artwork sync service (SYNC_URL is not set)"}
    req = urllib.request.Request(sync_url() + path, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {ARGS.api_key}"})
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
    return (p[0], canon(site_id, p[1]), bool(p[2]), bool(p[3]), p[4], bool(p[5]), bool(p[6]))


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
        out[sid] = {"auto": not why, "why": why, "manual": bool(st["manual"].get(sid)),
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


def bookings_loop():
    while True:
        for job in (sync_expectations, sync_rotations):
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
        recs = STORE.newest_per_site() + STORE.alerts(50)
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
        for k, c in sorted(hb.items()) if not site or k == site) or "<tr><td colspan=8 class='muted'>No heartbeat yet</td></tr>"
    cards = []
    camp_cache = {}
    for r in pops:
        q = r.get("quality", {})
        gone = r.get("_images_removed")
        img = (f"<div class='thumb'><span>photo removed after {POP_IMAGE_DAYS:g} days</span></div>" if gone else
               f"<div class='thumb'><img loading='lazy' src='/images/{esc(r['event_id'])}.roi.jpg' alt=''></div>"
               if "roi_sha256" in r else "<div class='thumb'><span>no image</span></div>")
        full = f" &middot; <a href='/images/{esc(r['event_id'])}.frame.jpg' target='_blank'>full frame</a>" if "frame_sha256" in r and not gone else ""
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
        if method in ("PUT", "POST", "DELETE") and re.match(r"/api/cameras", self.path) and 200 <= code < 300:
            nudge_sync("sites changed")           # only re-syncs if a Vault screen changed
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
                return self.reply(200, best_pops(t0, t1, ph))
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
        if path == "/records/sites":
            return self.reply(200, sorted(recorded_sites().values(), key=lambda e: e["site_id"] or ""))
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
            return self.reply(200, best_pops(t0, t1, ph, cur))
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
    global STORE
    STORE = store.Store(ARGS.out, log=lambda m: print(m, flush=True))
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
    ThreadingHTTPServer((ARGS.bind, ARGS.port), H).serve_forever()


if __name__ == "__main__":
    main()
