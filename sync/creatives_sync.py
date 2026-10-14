#!/usr/bin/env python3
"""creatives-sync: keeps the booked-artwork library for the Bookings page
current, straight from SharePoint, with no one in the loop.

  SharePoint (Sales / 00_Sales (NEW FOLDER) / Booked Campaigns / Creative+MIs)
        │ Microsoft Graph, app-only (Sites.Selected, read on the LUMO site)
        ▼
  creatives-sync ──► /creatives/vault-creatives.json + <screen>/images/<campaign>/<file>
        ▲                                       (read by the receiver)
        └── which campaigns and screens: GET receiver /sync/inputs
            (the latest Vault pull and the Vault screen of every monitored site)

When it runs:
  * at start, and whenever the inputs change (a new Vault pull, a site linked
    to a different Vault screen): checked every CHECK_MIN minutes;
  * every SYNC_INTERVAL_MIN minutes regardless, to pick up new or replaced artwork;
  * on demand: POST /sync (the Bookings page's "Sync now" button).

Per monitored screen and campaign booked on it this week or next:
  1. find the campaign's folders under Creative+MIs (indexed by the 5-digit
     LUMO id in the folder name; Graph search as a fallback);
  2. read every Material Instructions workbook in them: the newest one with a
     row for the screen in that week says which files play, when and at what
     rotation (and the Screen Specs tab gives every screen's pixel size);
  3. pick the files: named in the MI (typos and missing extensions tolerated),
     else named for the screen, else the screen's pixel size; weekly subfolders
     (w.c 05.10.26) narrow it to the week;
  4. download new or changed files (unchanged ones are kept), write the
     manifest atomically, and remove files no longer used.

A run that fails part-way never replaces the last good library. Failures and
recoveries are posted to the Teams/Slack webhooks.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mi  # noqa: E402
import pop_upload  # noqa: E402
import videoframes  # noqa: E402
import doohly  # noqa: E402

VERSION = "0.1.0"
ENV = os.environ.get
OUT = ENV("OUT_DIR", "/creatives")
STATE = ENV("STATE_DIR", os.path.join(OUT, ".sync"))
GRAPH = ENV("GRAPH_URL", "https://graph.microsoft.com/v1.0").rstrip("/")
SITE = ENV("SP_SITE", "lumodigital.sharepoint.com:/sites/LUMO")
LIBRARY = ENV("SP_LIBRARY", "Sales")
ROOT = ENV("SP_ROOT", "00_Sales (NEW FOLDER)/Booked Campaigns/Creative+MIs").strip("/")
RECEIVER = ENV("RECEIVER_URL", "http://receiver:8080").rstrip("/")
API_KEY = ENV("POP_API_KEY", "")
INTERVAL = float(ENV("SYNC_INTERVAL_MIN", "240")) * 60
CHECK = float(ENV("CHECK_MIN", "5")) * 60
SCREENS_OVERRIDE = [s.strip() for s in ENV("SYNC_SCREENS", "").split(",") if s.strip()]
MONTHS_BACK = int(ENV("MONTHS_BACK", "8"))    # how far back campaign folders are indexed
SKIP = re.compile(ENV("SKIP_FOLDERS", r"(?i)^(pops?|proof of play|contracts?|archive|not booked|wrong spec|old|superseded)\b"))
TZ_NAME = ENV("TZ", "Pacific/Auckland")

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover
    LOCAL = timezone.utc


def log(*a):
    print(datetime.now(LOCAL).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- Graph

class GraphError(Exception):
    pass


class Graph:
    """Minimal Microsoft Graph client: app-only token, paging, retry on throttling."""

    def __init__(self):
        self.s = requests.Session()
        self.token, self.expires = None, 0
        self.calls = 0
        self.app = None

    def _token(self):
        if self.token and time.time() < self.expires - 120:
            return self.token
        static = ENV("GRAPH_TEST_TOKEN")             # tests against a mock Graph only
        if static:
            self.token, self.expires = static, time.time() + 3600
            return self.token
        tenant, client = ENV("AZ_TENANT_ID"), ENV("AZ_CLIENT_ID")
        if not tenant or not client:
            raise GraphError("AZ_TENANT_ID and AZ_CLIENT_ID are not set (see docs/sharepoint-setup.md)")
        if self.app is None:
            import msal
            secret = ENV("AZ_CLIENT_SECRET")
            cert = ENV("AZ_CERT_PATH")
            if cert:
                with open(cert, encoding="utf-8") as f:
                    pem = f.read()
                cred = {"private_key": pem, "thumbprint": ENV("AZ_CERT_THUMBPRINT", "")}
            elif secret:
                cred = secret
            else:
                raise GraphError("set AZ_CLIENT_SECRET (or AZ_CERT_PATH and AZ_CERT_THUMBPRINT)")
            try:
                self.app = msal.ConfidentialClientApplication(
                    client, authority=f"https://login.microsoftonline.com/{tenant}", client_credential=cred)
            except Exception as e:
                raise GraphError(f"sign-in failed: check AZ_TENANT_ID ({e.__class__.__name__}: {str(e)[:160]})")
        try:
            r = self.app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        except Exception as e:
            raise GraphError(f"sign-in failed: cannot reach login.microsoftonline.com ({e})")
        if "access_token" not in r:
            raise GraphError(f"sign-in failed: {r.get('error')}: {(r.get('error_description') or '').splitlines()[0]}")
        self.token, self.expires = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.token

    def request(self, method, url, stream=False, **kw):
        if not url.startswith("http"):
            url = GRAPH + url
        for attempt in range(6):
            self.calls += 1
            try:
                r = self.s.request(method, url, headers={"Authorization": f"Bearer {self._token()}"},
                                   timeout=60, stream=stream, **kw)
            except requests.RequestException as e:
                if attempt == 5:
                    raise GraphError(f"cannot reach Microsoft Graph: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 401:
                self.token = None
                if attempt < 1:
                    continue
            if r.status_code >= 400:
                try:
                    err = r.json().get("error", {})
                    msg = f"{err.get('code')}: {err.get('message')}"
                except ValueError:
                    msg = r.text[:200]
                hint = ""
                if r.status_code in (401, 403):
                    hint = (" (the app needs Sites.Selected with admin consent and access to the LUMO site: "
                            "read for the artwork, write for the PoP photos; see docs/sharepoint-setup.md)")
                raise GraphError(f"Graph {r.status_code} on {urllib.parse.unquote(url.replace(GRAPH, ''))[:160]}: {msg}{hint}")
            return r
        raise GraphError(f"Graph kept throttling {url}")

    def get(self, url, **params):
        return self.request("GET", url, params=params or None).json()

    def pages(self, url, **params):
        d = self.get(url, **params)
        while True:
            for v in d.get("value", []):
                yield v
            nxt = d.get("@odata.nextLink")
            if not nxt:
                return
            d = self.request("GET", nxt).json()


def q(path):
    return urllib.parse.quote(path, safe="/")


# ---------------------------------------------------------------- SharePoint layout

class Library:
    """The Creative+MIs tree in the Sales library."""

    ITEM_FIELDS = "id,name,eTag,cTag,size,lastModifiedDateTime,webUrl,folder,file,image,video,parentReference"

    def __init__(self, g):
        self.g = g
        self.drive = ENV("SP_DRIVE_ID")
        self.root = None
        self.children_cache = {}

    def connect(self):
        if not self.drive:
            site = self.g.get(f"/sites/{SITE}", **{"$select": "id,webUrl,displayName"})
            want = LIBRARY.lower()
            for d in self.g.pages(f"/sites/{site['id']}/drives", **{"$select": "id,name,webUrl"}):
                tail = urllib.parse.unquote(d.get("webUrl", "")).rstrip("/").rsplit("/", 1)[-1].lower()
                if d.get("name", "").lower() == want or tail == want:
                    self.drive = d["id"]
                    break
            if not self.drive:
                raise GraphError(f"no document library called {LIBRARY!r} on {site.get('webUrl')}")
        self.root = self.g.get(f"/drives/{self.drive}/root:/{q(ROOT)}", **{"$select": self.ITEM_FIELDS})
        return self

    def children(self, item_id):
        if item_id not in self.children_cache:
            self.children_cache[item_id] = list(self.g.pages(
                f"/drives/{self.drive}/items/{item_id}/children",
                **{"$select": self.ITEM_FIELDS, "$top": "999"}))
        return self.children_cache[item_id]

    def index_campaigns(self, today):
        """{5-digit id: [folder items, newest month first]} for the recent months."""
        years = {str(today.year), str(today.year - 1)} | ({str(today.year + 1)} if today.month >= 11 else set())
        cutoff = (today.replace(day=1) - timedelta(days=31 * MONTHS_BACK))
        index = {}

        def month_key(name, year):
            m = re.match(r"\s*(\d{1,2})\s*\.", name)
            mo = int(m.group(1)) if m else next((v for k, v in mi.MONTHS.items() if k in name.lower()), None)
            return (int(year), mo) if mo and 1 <= mo <= 12 else None

        def add_month(folder, year, rank_extra=0):
            mk = month_key(folder["name"], year)
            if not mk or date(mk[0], mk[1], 28) < cutoff:
                return
            for c in self.children(folder["id"]):
                if "folder" not in c or SKIP.search(c["name"]):
                    continue
                for cid in set(re.findall(r"(?i)LUMO[\s_-]*(\d{5})", c["name"])):
                    c = dict(c, _month=mk, _path=f"{year}/{folder['name']}/{c['name']}")
                    index.setdefault(cid, []).append(c)

        for y in self.children(self.root["id"]):
            if "folder" not in y or y["name"].strip() not in years:
                continue
            year = y["name"].strip()
            for m in self.children(y["id"]):
                if "folder" not in m:
                    continue
                if re.search(r"(?i)subtle|motion|animated", m["name"]):
                    for sm in self.children(m["id"]):
                        if "folder" in sm:
                            add_month(sm, year)
                else:
                    add_month(m, year)
        for v in index.values():
            v.sort(key=lambda c: c["_month"], reverse=True)
        return index

    def search_campaign(self, cid):
        """Fallback for a campaign not in the indexed months: Graph search."""
        hits = []
        try:
            for it in self.g.pages(f"/drives/{self.drive}/root/search(q='LUMO_{cid}')",
                                   **{"$select": self.ITEM_FIELDS, "$top": "50"}):
                path = urllib.parse.unquote((it.get("parentReference") or {}).get("path", ""))
                if "/Creative+MIs/" not in path + "/" or "folder" not in it:
                    continue
                if re.search(rf"(?i)LUMO[\s_-]*{cid}", it["name"]) and not SKIP.search(it["name"]):
                    rel = path.split("/Creative+MIs/", 1)[-1] if "/Creative+MIs/" in path else ""
                    if any(SKIP.search(seg) for seg in rel.split("/") if seg):
                        continue
                    m = re.match(r"(\d{4})/(?:subtle/)?(\d{1,2})", rel, re.I)
                    it = dict(it, _month=(int(m.group(1)), int(m.group(2))) if m else (0, 0),
                              _path=f"{rel}/{it['name']}")
                    hits.append(it)
        except GraphError as e:
            log(f"search for LUMO_{cid} failed: {e}")
        hits.sort(key=lambda c: c["_month"], reverse=True)
        return hits

    def tree(self, folder, depth=0, prefix=""):
        """Every file under a campaign folder (skipping POPs, Contracts, ...)."""
        out = []
        for c in self.children(folder["id"]):
            if "folder" in c:
                if depth < 4 and not SKIP.search(c["name"]):
                    out += self.tree(c, depth + 1, f"{prefix}{c['name']}/")
            elif "file" in c:
                img = c.get("image") or {}
                out.append({"id": c["id"], "name": c["name"], "etag": c.get("eTag") or c.get("cTag") or "",
                            "modified": c.get("lastModifiedDateTime", ""), "size_b": c.get("size"),
                            "width": img.get("width"), "height": img.get("height"),
                            "rel": prefix + c["name"], "folder_rel": prefix.rstrip("/"),
                            "folder_url": c.get("webUrl", "").rsplit("/", 1)[0]})
        return out

    def download(self, item_id):
        return self.g.request("GET", f"/drives/{self.drive}/items/{item_id}/content").content


# ---------------------------------------------------------------- inputs

def fetch_inputs():
    """The latest Vault pull and the monitored screens, from the receiver."""
    r = requests.get(f"{RECEIVER}/sync/inputs", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if not isinstance((d.get("bookings") or {}).get("proposals"), list):
        if not doohly.token():
            raise RuntimeError("the receiver has no Vault pull yet (vault-bookings.json)")
        d["bookings"] = {"proposals": []}            # Doohly gives the bookings instead
    return d


def fingerprint(inputs):
    b = inputs.get("bookings") or {}
    scr = sorted(mi.screen_norm(s) for s in (inputs.get("screens") or []))
    return hashlib.sha256(json.dumps([b.get("generated_at"), len(b.get("proposals") or []), scr]).encode()).hexdigest()[:16]


def week_dates(iso):
    return mi.monday(date.fromisoformat(iso[:10]))


def bookings_by_screen(bookings, screens, weeks):
    """{screen name: [(campaign_id, proposal name, set of weeks)]} for the given weeks."""
    want = {mi.screen_norm(s): s for s in screens}
    out = {s: {} for s in screens}
    for p in bookings.get("proposals") or []:
        if p.get("status") not in (None, "Booked"):
            continue
        cid = p.get("campaign_id") or ""
        if not re.search(r"\d{5}", cid):
            continue
        for sc in p.get("screens") or []:
            key = mi.screen_norm(sc.get("name"))
            if key not in want:
                continue
            try:
                a, b = week_dates(sc["first_week"]), week_dates(sc["last_week"])
            except (KeyError, ValueError):
                continue
            hit = {w for w in weeks if a <= w <= b}
            if hit:
                e = out[want[key]].setdefault(cid, {"name": p.get("name", ""), "weeks": set()})
                e["weeks"] |= hit
    return out


def doohly_by_screen(doc, screens, weeks):
    """From doohly.json: {screen: {campaign_id: {"name", "weeks", "files"}}} for the screens
    whose player is in Doohly. "files" are the booked creatives with the dates they play."""
    want = {mi.screen_norm(s): s for s in screens}
    out = {}
    w0, w1 = min(weeks), max(weeks) + timedelta(days=6)
    for site in (doc.get("sites") or {}).values():
        screen = want.get(mi.screen_norm(site.get("screen")))
        if not screen:
            continue
        camps = out.setdefault(screen, {})
        for b in site.get("bookings") or []:
            cid = b.get("campaign_id")
            if not cid or not b.get("start") or not b.get("end"):
                continue
            a, z = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
            if z < w0 or a > w1:
                continue
            e = camps.setdefault(cid, {"name": b.get("campaign_name") or b.get("name") or "", "weeks": set(), "files": []})
            e["weeks"] |= {w for w in weeks if a <= w + timedelta(days=6) and z >= w}
            for c in b.get("creatives") or []:
                e["files"].append({"name": c.get("name") or "", "type": c.get("type"), "from": max(a, w0).isoformat(),
                                   "to": min(z, w1).isoformat(), "share": c.get("share"),
                                   "hours": c.get("hours") or (None if (b.get("start_time"), b.get("end_time")) in
                                                               (("00:00", "23:59"), ("00:00", "24:00"), ("", ""))
                                                               else f"{b['start_time']}-{b['end_time']}"),
                                   "days": c.get("days")})
    return out


# ---------------------------------------------------------------- choosing files

def choose(campaign_id, screen, size, weeks, files, mis, all_screens, cname="", booked_files=None):
    """Files for one campaign on one screen. Returns (creatives, missing_reason).
    booked_files: the creatives Doohly has booked on this screen for this campaign
    (name, type, from, to, share, hours); when given they decide the files."""
    key = mi.screen_norm(screen)
    images = [f for f in files if mi.kind_of(f["name"])]
    if not images:
        return [], ("no artwork files in the campaign folder" if files else "campaign folder is empty")
    w0, w1 = min(weeks), max(weeks) + timedelta(days=6)

    def in_week_folder(fs):
        """Narrow to weekly subfolders (w.c 05.10.26) for these weeks when there are any."""
        dated = [(mi.week_folder_date(f["folder_rel"]), f) for f in fs]
        if not any(d for d, _ in dated):
            return fs
        keep = [f for d, f in dated if d is None or (mi.monday(d) in weeks)]
        if any(mi.week_folder_date(f["folder_rel"]) for f in keep):
            return keep
        past = sorted({mi.monday(d) for d, _ in dated if d and mi.monday(d) <= w0})
        if past:                                       # latest weekly folder before this week
            return [f for d, f in dated if d is None or mi.monday(d) == past[-1]]
        return fs

    # 0. Doohly: the files actually booked on the player, found in SharePoint by name
    not_found = []
    if booked_files:
        out, seen = [], set()
        for b in booked_files:
            if b.get("type") == "html":
                continue                                   # live web content: nothing to match against
            f, how = mi.match_file(b["name"], in_week_folder(images), size)
            if not f:
                f, how = mi.match_file(b["name"], images, size)
            if not f:
                not_found.append(b["name"])
                continue
            if (f["name"], b.get("from"), b.get("hours")) in seen:
                continue
            seen.add((f["name"], b.get("from"), b.get("hours")))
            note = "booked in Doohly" + ("" if how == "exact" else f" as {b['name']} (file name {how})")
            if b.get("share") is not None:
                note += f"; {b['share']:g}% of its plays"
            if b.get("hours") or b.get("days"):
                note += f"; plays {' '.join(x for x in (b.get('hours'), ','.join(b.get('days') or [])) if x)}"
            out.append(entry(campaign_id, screen, f, "doohly", date.fromisoformat(b["from"]) if b.get("from") else None,
                             date.fromisoformat(b["to"]) if b.get("to") else None, b.get("share"), note, b.get("hours")))
        if out:
            if not_found:
                out[0]["note"] += f"; not in SharePoint: {', '.join(not_found[:3])}"
            return out, None
    got, why = _choose_mi(campaign_id, screen, size, weeks, images, mis, all_screens, key, w0, w1, in_week_folder)
    if not got and not_found:
        why = f"Doohly plays {', '.join(not_found[:3])} but no file of that name is in the SharePoint folder"
    return got, why


def _choose_mi(campaign_id, screen, size, weeks, images, mis, all_screens, key, w0, w1, in_week_folder):
    """Steps 1-3 of choose(): the Material Instructions, else file names and sizes."""
    # 1. Material Instructions: newest workbook with a row for this screen in these weeks
    rows, src = [], None
    for m in sorted(mis, key=lambda m: (m["updated"] or date.min, m["modified"]), reverse=True):
        rs = [r for r in m["rows"] if r["screen"] == key and r["start"] and r["end"]
              and r["start"] <= w1 and r["end"] >= w0]
        if rs:
            rows, src = rs, m
            break
    out, unmatched = [], []
    if rows:
        for r in rows:
            sz = r["size"] or size
            for c in r["creatives"]:
                f0, f1 = c["from"] or r["start"], c["to"] or r["end"]
                if f0 and f1 and (f1 < w0 or f0 > w1):
                    continue
                f, how = mi.match_file(c["name"], in_week_folder(images), sz)
                if not f:
                    f, how = mi.match_file(c["name"], images, sz)
                if not f:
                    unmatched.append(c["name"])
                    continue
                note = f"MI {src['name']}: {c['column'] or 'creative'}" + ("" if how == "exact" else f"; file name {how}")
                if r["hours"]:
                    note += f"; plays {r['hours']}"
                out.append(entry(campaign_id, screen, f, "mi", f0, f1, c["rotation"], note, r["hours"]))
        if out:
            if unmatched:
                out[0]["note"] += f"; not found: {', '.join(unmatched[:3])}"
            return out, None
    # 2. named for the screen; 3. the screen's pixel size
    others = list(all_screens)
    cand = in_week_folder(images)
    named = [f for f in cand if mi.names_screen(f["name"], screen, others)]
    if size:
        named = [f for f in named if fits(f, size) is not False]
    # Same-size files named for no other screen are the same campaign's artwork too: include
    # them (matching only needs the campaign right, and generic names are common).
    sized = [f for f in cand if size and fits(f, size) and f not in named
             and not any(mi.names_screen(f["name"], o) for o in others if mi.screen_norm(o) != key)]
    if named or sized:
        best = {}
        for via, fs in (("size", sized), ("filename", named)):
            for f in sorted(fs, key=lambda f: f["modified"]):
                best[mi.name_norm(f["name"])] = (f, via)          # same artwork in two places: newest copy
        why = ("MI has no row for this screen in these weeks" if mis and not rows else
               f"MI row names no file it could find ({', '.join(unmatched[:2])})" if unmatched else
               "MI names no file" if rows else "no Material Instructions")
        return [entry(campaign_id, screen, f, via, None, None, None, why) for f, via in best.values()], None
    if unmatched:
        return [], f"MI names {', '.join(unmatched[:3])} but no such file is in the folder"
    if not size:
        return [], "screen size unknown (not in any Screen Specs tab) and no file is named for the screen"
    return [], (f"no {size[0]}x{size[1]} artwork" + (" (MI has no row for this screen this week)" if mis and not rows else ""))


def fits(f, size):
    """True/False when the file's size (Graph's image facet, else its name) is
    known, None when it isn't."""
    named = mi.same_size(mi.size_in_name(f["name"]), size)
    if f.get("width"):
        return bool(mi.same_size((f["width"], f["height"]), size) or named)
    return named


def entry(cid, screen, f, via, f0, f1, rot, note, hours=None):
    e = {"campaign_id": cid, "screen": screen, "name": f["name"], "kind": mi.kind_of(f["name"]),
         "from": f0.isoformat() if f0 else None, "to": f1.isoformat() if f1 else None,
         "rotation": rot, "via": via, "folder_url": f["folder_url"], "note": note, "_item": f}
    if hours:
        e["hours"] = hours
    return e


# ---------------------------------------------------------------- one run

class Syncer:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.status = read_json(os.path.join(STATE, "status.json"), {}) or {}
        self.status.update(running=False, version=VERSION)
        self.last_fp = self.status.get("inputs")
        self.last_ok = self._ts(self.status.get("last_success"))
        self.pending = False
        self.reason = "start"

    @staticmethod
    def _ts(iso):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            return 0

    def save_status(self, **kw):
        self.status.update(kw)
        os.makedirs(STATE, exist_ok=True)
        atomic_json(os.path.join(STATE, "status.json"), self.status)

    def request(self, reason):
        self.reason, self.pending = reason, True
        self.wake.set()

    def loop(self):
        time.sleep(float(ENV("START_DELAY_S", "20")))
        self.request("start")
        while True:
            self.wake.wait(timeout=CHECK)
            self.wake.clear()
            reason = None
            if self.pending:
                reason, self.pending = self.reason, False
            else:
                try:
                    inp = fetch_inputs()
                    fp = fingerprint(inp)
                    if inp.get("screens") is not None and fp != self.last_fp:
                        reason = "new Vault pull or site screens changed"
                except Exception as e:
                    log(f"inputs check: {e}")
                if not reason and time.time() - self.last_ok >= INTERVAL:
                    reason = "scheduled refresh"
                if not reason and self.status.get("ok") is False and time.time() - self._ts(self.status.get("last_run")) >= 30 * 60:
                    reason = "retry after failure"
            if reason:
                self.run(reason)

    def run(self, reason):
        if not (ENV("AZ_CLIENT_ID") or ENV("GRAPH_TEST_TOKEN")):
            self.save_status(running=False, ok=None, last_run=now_iso(),
                             error="not set up yet: AZ_TENANT_ID, AZ_CLIENT_ID and AZ_CLIENT_SECRET are empty "
                                   "(run deploy/sharepoint/setup-sharepoint-app.ps1, see docs/sharepoint-setup.md)")
            return False
        if not self.lock.acquire(blocking=False):
            return False
        t0 = time.time()
        self.save_status(running=True, started=now_iso(), reason=reason)
        log(f"sync: starting ({reason})")
        try:
            res = self._run()
            took = round(time.time() - t0)
            self.last_ok = time.time()
            was_bad = self.status.get("ok") is False
            self.save_status(running=False, ok=True, error=None, last_run=now_iso(), last_success=now_iso(),
                             took_s=took, **res)
            log(f"sync: done in {took}s: {res['files']} file(s) for {res['pairs_ok']}/{res['pairs']} campaign-screen "
                f"pairs, {res['downloaded']} downloaded, {res['graph_calls']} Graph calls")
            if was_bad:
                notify(f"Artwork sync is working again: {res['files']} file(s) for {len(res['screens'])} screen(s).", ok=True)
            self.check_secret()
            return True
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            log(f"sync: FAILED: {msg}")
            if not isinstance(e, (GraphError, RuntimeError, requests.RequestException)):
                traceback.print_exc()
            first = self.status.get("ok") is not False
            self.save_status(running=False, ok=False, error=msg, last_run=now_iso(), took_s=round(time.time() - t0))
            if first:
                notify(f"Artwork sync from SharePoint failed: {msg}. The Bookings page keeps the last good "
                       f"library{(' from ' + self.status['last_success']) if self.status.get('last_success') else ''}.")
            return False
        finally:
            self.lock.release()

    def check_secret(self):
        """Warn (status + Teams, at most weekly) when the client secret is about to expire."""
        try:
            exp = date.fromisoformat(ENV("AZ_SECRET_EXPIRES", "")[:10])
        except ValueError:
            return
        days = (exp - datetime.now(LOCAL).date()).days
        self.save_status(secret_expires=exp.isoformat(), secret_days_left=days)
        if days <= 30 and time.time() - self._ts(self.status.get("secret_warned")) > 7 * 86400:
            notify(f"The SharePoint app secret used by the artwork sync expires on {exp:%d %b %Y} ({days} days). "
                   "Run deploy/sharepoint/setup-sharepoint-app.ps1 again to make a new one (docs/sharepoint-setup.md).")
            self.save_status(secret_warned=now_iso())

    def _run(self):
        inputs = fetch_inputs()
        fp = fingerprint(inputs) if inputs.get("screens") is not None else self.last_fp
        bookings = inputs["bookings"]
        listed = inputs.get("screens")
        if listed is None:                         # gateway unreachable: keep the last known screens
            listed = self.status.get("screens") or []
            if not listed and not SCREENS_OVERRIDE:
                raise RuntimeError("the receiver cannot reach the gateway, so the monitored screens are unknown")
        screens = SCREENS_OVERRIDE or [s for s in listed if s]
        if SCREENS_OVERRIDE == ["all"]:
            screens = sorted({sc["name"] for p in bookings["proposals"] for sc in p.get("screens") or [] if sc.get("name")})
        screens = sorted(set(screens), key=str.lower)
        today = datetime.now(LOCAL).date()
        weeks = [mi.monday(today), mi.monday(today) + timedelta(days=7)]
        booked = bookings_by_screen(bookings, screens, weeks)
        dbooked = doohly_by_screen(read_json(os.path.join(OUT, "doohly.json"), {}) or {}, screens, weeks)
        for screen, camps in dbooked.items():          # Doohly knows this screen: it decides what is booked
            booked[screen] = {cid: {"name": v["name"], "weeks": v["weeks"]} for cid, v in camps.items()}

        g = Graph()
        lib = Library(g).connect()
        index = lib.index_campaigns(today)
        specs = read_json(os.path.join(STATE, "screen-specs.json"), {}) or {}
        mi_cache_dir = os.path.join(STATE, "mi")
        os.makedirs(mi_cache_dir, exist_ok=True)
        prev = read_json(os.path.join(OUT, "vault-creatives.json"), {}) or {}

        camp_files, camp_mis, camp_folders, camp_err = {}, {}, {}, {}
        all_cids = sorted({cid for s in booked.values() for cid in s})
        for cid5 in {re.search(r"\d{5}", c).group(0) for c in all_cids}:
            folders = index.get(cid5) or lib.search_campaign(cid5)
            camp_folders[cid5] = folders
            files, mis = [], []
            try:
                for rank, fo in enumerate(folders):
                    for f in lib.tree(fo):
                        f["rank"] = rank                    # 0 = newest month's folder
                        f["folder_rel"] = f"{fo['_path']}/{f['folder_rel']}".rstrip("/")
                        files.append(f)
                for f in files:
                    if not f["name"].lower().endswith((".xlsx", ".xlsm")):
                        continue
                    if not re.search(r"(?i)material|instruction|\bmi\b|\bmis\b|booking update|_mi[ _.]", f["name"]):
                        continue
                    parsed = load_mi(lib, f, mi_cache_dir)
                    if parsed and parsed["rows"]:
                        mis.append(dict(parsed, name=f["name"], modified=f["modified"]))
                    if parsed:
                        for k, v in parsed["specs"].items():
                            specs[k] = list(v)
            except GraphError as e:
                camp_err[cid5] = str(e)
            camp_files[cid5], camp_mis[cid5] = files, mis
        atomic_json(os.path.join(STATE, "screen-specs.json"), specs)
        spec_names = [v[2] for v in specs.values()]

        creatives, missing, screen_info, pairs, pairs_ok = [], [], [], 0, 0
        for screen in screens:
            sp = specs.get(mi.screen_norm(screen))
            size = (sp[0], sp[1]) if sp else None
            screen_info.append({"name": screen, "width": size[0] if size else None, "height": size[1] if size else None})
            for cid, info in sorted(booked[screen].items()):
                pairs += 1
                cid5 = re.search(r"\d{5}", cid).group(0)
                if cid5 in camp_err:                      # keep what the last good run had
                    kept = [c for c in prev.get("creatives", []) if c.get("campaign_id") == cid and
                            mi.screen_norm(c.get("screen")) == mi.screen_norm(screen)]
                    creatives += kept
                    missing.append({"campaign_id": cid, "screen": screen,
                                    "reason": f"SharePoint error, kept {len(kept)} file(s) from the last sync: {camp_err[cid5]}"})
                    pairs_ok += bool(kept)
                    continue
                if not camp_folders[cid5]:
                    missing.append({"campaign_id": cid, "screen": screen, "name": info["name"],
                                    "reason": f"no folder for {cid} in Creative+MIs"})
                    continue
                got, why = choose(cid, screen, size, sorted(info["weeks"]), camp_files[cid5], camp_mis[cid5],
                                  [screen] + spec_names, info["name"],
                                  booked_files=(dbooked.get(screen) or {}).get(cid, {}).get("files"))
                if got:
                    pairs_ok += 1
                    creatives += got
                else:
                    missing.append({"campaign_id": cid, "screen": screen, "name": info["name"], "reason": why,
                                    "folder_url": camp_folders[cid5][0].get("webUrl", "")})

        downloaded, files_n = self.materialise(lib, creatives)
        manifest = {"format": "pop-monitor/vault-creatives", "version": 1, "generated_at": now_iso(),
                    "source": f"creatives-sync {VERSION} (Microsoft Graph, {SITE} / {LIBRARY} / {ROOT})",
                    "weeks": [w.isoformat() for w in weeks], "screens": screen_info,
                    "creatives": [{k: v for k, v in c.items() if not k.startswith("_")} for c in creatives],
                    "missing": missing}
        atomic_json(os.path.join(OUT, "vault-creatives.json"), manifest)
        self.prune(manifest)
        self.last_fp = fp
        return {"inputs": fp, "screens": screens, "pairs": pairs, "pairs_ok": pairs_ok, "files": files_n,
                "videos": sum(1 for c in creatives if c.get("kind") == "video"), "missing": len(missing),
                "downloaded": downloaded, "graph_calls": g.calls, "weeks": manifest["weeks"],
                "campaigns": len(all_cids)}

    def materialise(self, lib, creatives):
        """Download new or changed images into <screen>/images/<campaign>/<file>, and
        for videos their frames into <screen>/frames/<campaign>/<file>/f0001.jpg..."""
        state_p = os.path.join(STATE, "files.json")
        have = read_json(state_p, {}) or {}
        cache = os.path.join(STATE, "items")
        os.makedirs(cache, exist_ok=True)
        downloaded, n, seen = 0, 0, {}
        for c in creatives:
            f = c.get("_item")
            if not f:
                continue                              # kept from the last run: file already there
            if c["kind"] == "video":
                d, k = self.video_frames(lib, c, f, cache, have)
                downloaded += d
                n += k
                continue
            if c["kind"] != "image":
                c["file"] = None
                continue
            rel = f"{mi.slug(c['screen'])}/images/{safe(c['campaign_id'])}/{safe(f['name'])}"
            if rel in seen:                           # the same file listed twice (two MI rows)
                c["file"] = rel
                continue
            seen[rel] = True
            dest = os.path.join(OUT, rel)
            tag = hashlib.sha1(f"{f['id']}|{f['etag']}".encode()).hexdigest()[:20]
            cached = os.path.join(cache, tag + os.path.splitext(f["name"])[1].lower())
            if not os.path.exists(cached):
                data = lib.download(f["id"])
                with open(cached + ".part", "wb") as o:
                    o.write(data)
                os.replace(cached + ".part", cached)
                downloaded += 1
            if have.get(rel) != tag or not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(cached, dest + ".part")
                os.replace(dest + ".part", dest)
                have[rel] = tag
            os.utime(cached)
            c["file"] = rel
            n += 1
        atomic_json(state_p, have)
        return downloaded, n

    def video_frames(self, lib, c, f, cache, have):
        """Download a booked video (kept in the cache) and take its frames. The
        creative's "file" is its first frame (thumbnail), "frames" all of them."""
        rel = f"{mi.slug(c['screen'])}/frames/{safe(c['campaign_id'])}/{safe(f['name'])}"
        tag = hashlib.sha1(f"{f['id']}|{f['etag']}|{videoframes.FRAME_EVERY_S}|{videoframes.FRAME_WIDTH}".encode()).hexdigest()[:20]
        dest = os.path.join(OUT, rel)
        downloaded = 0
        if have.get(rel) != tag or not os.path.isdir(dest):
            cached = os.path.join(cache, tag + os.path.splitext(f["name"])[1].lower())
            if not os.path.exists(cached):
                data = lib.download(f["id"])
                with open(cached + ".part", "wb") as o:
                    o.write(data)
                os.replace(cached + ".part", cached)
                downloaded = 1
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                videoframes.extract(cached, dest)
            except Exception as e:
                log(f"sync: {c['campaign_id']} {f['name']}: {e}")
                c["file"], c["note"] = None, (c.get("note") or "") + f"; video frames failed: {e}"
                return downloaded, 0
            have[rel] = tag
        frames = sorted(x for x in os.listdir(dest) if x.endswith(".jpg"))
        c["frames"] = [f"{rel}/{x}" for x in frames]
        c["file"] = c["frames"][0] if frames else None
        c["frame_every_s"] = videoframes.FRAME_EVERY_S
        return downloaded, 1 if frames else 0

    def prune(self, manifest):
        """Remove library images no longer in the manifest, and stale cache entries."""
        keep = {c["file"] for c in manifest["creatives"] if c.get("file")}
        keep |= {x for c in manifest["creatives"] for x in c.get("frames") or []}
        have = read_json(os.path.join(STATE, "files.json"), {}) or {}
        for root, dirs, files in os.walk(OUT):
            if os.path.abspath(root).startswith(os.path.abspath(STATE)):
                continue
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), OUT).replace(os.sep, "/")
                if ("/images/" in rel or "/frames/" in rel) and rel not in keep:
                    os.remove(os.path.join(root, fn))
                    have.pop(rel, None)
                    have.pop(rel.rsplit("/", 1)[0], None)      # a video's frames folder
        for root, dirs, files in os.walk(OUT, topdown=False):
            if root != OUT and not os.listdir(root):
                os.rmdir(root)
        atomic_json(os.path.join(STATE, "files.json"), have)
        cache = os.path.join(STATE, "items")
        for fn in os.listdir(cache) if os.path.isdir(cache) else []:
            p = os.path.join(cache, fn)
            if time.time() - os.path.getmtime(p) > 45 * 86400:
                os.remove(p)


def safe(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")
    return name or "_"


def load_mi(lib, f, cache_dir):
    """Parse an MI workbook, cached by item id + eTag."""
    tag = hashlib.sha1(f"{f['id']}|{f['etag']}".encode()).hexdigest()[:20]
    p = os.path.join(cache_dir, tag + ".json")
    hit = read_json(p)
    if hit is not None:
        return revive(hit)
    try:
        parsed = mi.read_workbook(lib.download(f["id"]))
    except GraphError:
        raise
    except Exception as e:                        # a damaged or odd workbook: skip it, note it
        log(f"cannot read {f['rel']}: {e}")
        parsed = {"rows": [], "specs": {}, "updated": None, "error": str(e)}
    atomic_json(p, parsed)
    return revive(read_json(p))


def revive(d):
    """Dates back from the JSON cache."""
    def dd(v):
        return date.fromisoformat(v) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", v) else v
    for r in d.get("rows", []):
        r["start"], r["end"] = dd(r.get("start")), dd(r.get("end"))
        r["size"] = tuple(r["size"]) if r.get("size") else None
        for c in r.get("creatives", []):
            c["from"], c["to"] = dd(c.get("from")), dd(c.get("to"))
    d["updated"] = dd(d.get("updated"))
    d["specs"] = {k: tuple(v) for k, v in (d.get("specs") or {}).items()}
    return d


# ---------------------------------------------------------------- alerts + API

def notify(text, ok=False, title=None):
    """Teams (adaptive card, as the receiver sends) and Slack."""
    title = title or ("Recovered: artwork sync" if ok else "Warning: artwork sync failed")
    teams = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title, "wrap": True},
                             {"type": "TextBlock", "text": text, "wrap": True},
                             {"type": "TextBlock", "text": datetime.now(LOCAL).strftime("%a %d %b %H:%M %Z"),
                              "isSubtle": True, "wrap": True}]}}]}
    icon = ":white_check_mark:" if ok else ":warning:"
    slack = {"text": f"{icon} *{title}*\n{text}"}
    for url, body in ((ENV("POP_TEAMS_WEBHOOK", "").strip(), teams), (ENV("POP_SLACK_WEBHOOK", "").strip(), slack)):
        if url:
            try:
                requests.post(url, json=body, timeout=10)
            except requests.RequestException as e:
                log(f"webhook failed: {e}")


SYNC = None
POP = None
DOOHLY = None


def configured():
    return bool(ENV("AZ_CLIENT_ID") or ENV("GRAPH_TEST_TOKEN"))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def ok_auth(self):
        if not API_KEY or self.headers.get("Authorization") == f"Bearer {API_KEY}":
            return True
        self.reply(401, {"error": "bad token"})
        return False

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if not self.ok_auth():
            return
        if self.path == "/status":
            st = dict(SYNC.status)
            nxt = (SYNC.last_ok + INTERVAL) if SYNC.last_ok else None
            st.update(interval_min=INTERVAL / 60, next_refresh=datetime.fromtimestamp(nxt, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ") if nxt else None, configured=configured())
            st["pop_upload"] = POP.status() if POP else None
            st["doohly"] = DOOHLY.status() if DOOHLY else None
            return self.reply(200, st)
        self.reply(404, {})

    def do_POST(self):
        if not self.ok_auth():
            return
        if self.path == "/check":                 # receiver: inputs may have changed
            SYNC.wake.set()
            return self.reply(202, {"status": "checking"})
        if self.path == "/sync":
            if SYNC.status.get("running"):
                SYNC.request("asked again while running")
                return self.reply(202, {"status": "running"})
            SYNC.request("Sync now")
            return self.reply(202, {"status": "started"})
        m = re.fullmatch(r"/pop/run\?phase=(day|night)", self.path)
        if m and POP:                             # upload now (testing, or after a fix)
            if not configured():
                return self.reply(409, {"error": "SharePoint access is not set up"})
            if not POP.start(m.group(1)):
                return self.reply(409, {"error": "an upload is already running"})
            return self.reply(202, {"status": "started", "phase": m.group(1)})
        self.reply(404, {})


def main():
    global SYNC
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    if "--pop" in sys.argv:                        # one PoP photo upload run: --pop day|night
        phase = sys.argv[sys.argv.index("--pop") + 1]
        u = pop_upload.PopUploader(Graph(), Library, STATE, RECEIVER, API_KEY, LOCAL, log, notify,
                                   atomic_json, read_json, q)
        sys.exit(0 if u.run(phase) else 1)
    if "--once" in sys.argv:                       # one run, for testing or a cron job
        s = Syncer()
        sys.exit(0 if s.run("--once") else 1)
    SYNC = Syncer()
    threading.Thread(target=SYNC.loop, daemon=True).start()
    global POP
    POP = pop_upload.PopUploader(Graph(), Library, STATE, RECEIVER, API_KEY, LOCAL, log, notify,
                                 atomic_json, read_json, q)
    threading.Thread(target=POP.loop, args=(configured,), daemon=True).start()
    global DOOHLY
    DOOHLY = doohly.Watcher(OUT, STATE, fetch_inputs, LOCAL, log, notify, atomic_json, read_json,
                            on_change=SYNC.request)
    threading.Thread(target=DOOHLY.loop, daemon=True).start()
    port = int(ENV("PORT", "8091"))
    log(f"creatives-sync {VERSION} on :{port}; library {OUT}; refresh every {INTERVAL / 60:.0f} min, "
        f"inputs checked every {CHECK / 60:.0f} min")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
