"""doohly: what Doohly (the playout system) has booked on each monitored screen.

Read only: this module only ever sends GET requests to Doohly's public API
(https://help.dooh.ly/reference/api-endpoints), with DOOHLY_API_TOKEN.

Every DOOHLY_DEVICE_MIN minutes (5) it reads the monitored sites' players
(online, last seen, location, frames and their loop policy); every
DOOHLY_INTERVAL_MIN minutes (30) also their bookings for this week and next.
It writes OUT_DIR/doohly.json, which the receiver reads for the Bookings page
(exact weeks, plays per loop, purchase type), for day and night (the player's
coordinates) and for the player's state, and which the artwork sync reads for
the files booked on each screen.

A site is matched to its Doohly player by name: the player "LUMO-Albany
Expressway IN" is the Vault screen "Albany Expressway In". DOOHLY_DEVICES
("SITE_ID=player name;...") overrides that. The main frame is the player's
largest frame (not the name plate).

Creative files can't be downloaded through the public API (CloudFront needs
signed URLs), so the images still come from SharePoint, matched by file name.
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

ENV = os.environ.get
BASE = ENV("DOOHLY_URL", "https://api.dooh.ly/api/public").rstrip("/")
DEVICE_EVERY = float(ENV("DOOHLY_DEVICE_MIN", "5")) * 60
BOOKINGS_EVERY = float(ENV("DOOHLY_INTERVAL_MIN", "30")) * 60
OFFLINE_ALERT_MIN = float(ENV("DOOHLY_OFFLINE_ALERT_MIN", "15"))
LIVE = ("booked", "reserved", "paused")
PURCHASE = {"Sold": "sold", "Guaranteed Bonus": "bonus", "Non-Guaranteed Bonus": "ng bonus", "Filler": "filler"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def token():
    return (ENV("DOOHLY_API_TOKEN") or "").strip()


class DoohlyError(Exception):
    pass


class Client:
    """GET-only client: paging, retry on throttling. Never sends anything that changes Doohly."""

    def __init__(self):
        self.s = requests.Session()
        self.calls = 0

    def get(self, path, **params):
        url = path if path.startswith("http") else BASE + path
        for attempt in range(6):
            self.calls += 1
            try:
                r = self.s.get(url, params=params or None, timeout=60,
                               headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"})
            except requests.RequestException as e:
                if attempt == 5:
                    raise DoohlyError(f"cannot reach Doohly: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
                continue
            if r.status_code in (401, 403):
                raise DoohlyError(f"Doohly refused the API token ({r.status_code}): check DOOHLY_API_TOKEN")
            if r.status_code >= 400:
                raise DoohlyError(f"Doohly {r.status_code} on {path}: {r.text[:200]}")
            return r.json()
        raise DoohlyError(f"Doohly kept throttling {path}")

    def pages(self, path, **params):
        params.setdefault("pageSize", 100)
        while True:
            d = self.get(path, **params)
            yield from d.get("data") or []
            nxt = d.get("nextPageToken")
            if not nxt:
                return
            params = {"pageToken": nxt, "pageSize": 100}


def norm(name):
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    s = re.sub(r"^lumo ", "", s)
    return s


def campaign_id(c):
    """LUMO_xxxxx of a Doohly campaign (its external ID, else from its name)."""
    for v in ((c or {}).get("externalId"), (c or {}).get("name")):
        m = re.search(r"LUMO[_ -]?(\d{4,6})", v or "", re.I)
        if m:
            return f"LUMO_{m.group(1)}"
    return None


def overlaps(s, d0, d1):
    try:
        return s["startDate"] <= d1.isoformat() and s["endDate"] >= d0.isoformat()
    except (KeyError, TypeError):
        return False


def hours_of(blocks):
    """'09:00-17:00' when every block has the same hours and they aren't all day, else None."""
    spans = {(b.get("startTime", "")[:5], b.get("endTime", "")[:5]) for b in blocks or []}
    if len(spans) != 1:
        return None
    a, b = spans.pop()
    return None if (a, b) in (("00:00", "23:59"), ("00:00", "24:00")) else f"{a}-{b}"


def slim_booking(d):
    s = d.get("schedule") or {}
    out = {"id": d["id"], "name": d.get("name"), "status": d.get("status"),
           "purchase_type": d.get("purchaseType"), "slot_type": PURCHASE.get(d.get("purchaseType"), d.get("purchaseType")),
           "campaign_id": campaign_id(d.get("campaign")), "campaign_name": (d.get("campaign") or {}).get("name"),
           "external_id": d.get("externalId"), "updated_at": d.get("updatedAt"),
           "start": s.get("startDate"), "end": s.get("endDate"), "start_time": (s.get("startTime") or "")[:5],
           "end_time": (s.get("endTime") or "")[:5], "days": s.get("days") or DAYS,
           "plays_per_loop": d.get("playsPerLoop") or 0, "loops_per_play": d.get("loopsPerPlay") or 1,
           "consecutive": bool(d.get("playConsecutively")),
           "url": f"https://app.dooh.ly/bookings/{d['id']}", "creatives": []}
    weights = bool(d.get("creativeWeightsEnabled"))
    scheds = bool(d.get("creativeSchedulesEnabled"))
    for c in sorted(d.get("assignedCreatives") or [], key=lambda c: c.get("order") or 0):
        cr = c.get("creative") or {}
        f = cr.get("file") or {}
        blocks = []
        if scheds:
            for a in c.get("assignedSchedules") or []:
                blocks += ((a.get("schedule") or {}).get("dayTimeBlocks") or [])
        out["creatives"].append({
            "name": cr.get("name"), "type": cr.get("type"), "width": f.get("width"), "height": f.get("height"),
            "duration_ms": f.get("durationMs"), "id": cr.get("id"),
            "weight": c.get("weight") if weights else None,
            "days": sorted({b.get("day") for b in blocks}, key=DAYS.index) if blocks else None,
            "hours": hours_of(blocks) if blocks else None})
    total = sum(c["weight"] or 0 for c in out["creatives"])
    for c in out["creatives"]:
        c["share"] = round(100.0 * c["weight"] / total, 1) if weights and total else None
    return out


def weekly_plays(b, week):
    """Plays per loop a booking adds in the week starting `week`, averaged over the
    days it is on (a booking split 4 days x1 and 3 days x2 is two bookings)."""
    d0 = max(date.fromisoformat(b["start"]), week)
    d1 = min(date.fromisoformat(b["end"]), week + timedelta(days=6))
    if d1 < d0:
        return 0.0
    n = sum(1 for i in range((d1 - d0).days + 1) if DAYS[(d0 + timedelta(days=i)).weekday()] in b["days"])
    return (b["plays_per_loop"] or 0) * n / 7.0


class Watcher:
    """Keeps OUT_DIR/doohly.json current and alerts when a player goes offline."""

    def __init__(self, out_dir, state_dir, fetch_inputs, local_tz, log, notify, atomic_json, read_json, on_change):
        self.path = os.path.join(out_dir, "doohly.json")
        self.cache_p = os.path.join(state_dir, "doohly-bookings.json")
        self.fetch_inputs, self.tz, self.log, self.notify = fetch_inputs, local_tz, log, notify
        self.atomic_json, self.read_json, self.on_change = atomic_json, read_json, on_change
        self.doc = read_json(self.path, {}) or {}
        self.cache = read_json(self.cache_p, {}) or {}       # booking id -> (updatedAt, slim)
        self.lock = threading.Lock()
        self.last_bookings = 0
        self.alerted = {}                                     # site -> since (offline alert sent)
        self.c = Client()

    # ---------------------------------------------------------------- sites
    def sites(self):
        inp = self.fetch_inputs()
        sites = [s for s in (inp.get("sites") or []) if s.get("site_id")]
        over = {}
        for part in (ENV("DOOHLY_DEVICES") or "").split(";"):
            k, _, v = part.partition("=")
            if k.strip() and v.strip():
                over[k.strip()] = v.strip()
        return sites, over

    def match_devices(self, sites, over, devices):
        by_norm = {}
        for d in devices:
            by_norm.setdefault(norm(d.get("name")), []).append(d)
        out, unmatched = {}, []
        for s in sites:
            want = over.get(s["site_id"]) or s.get("vault_screen") or s.get("name")
            hits = by_norm.get(norm(want)) or []
            if len(hits) == 1:
                out[s["site_id"]] = hits[0]["id"]
            else:
                unmatched.append({"site_id": s["site_id"], "looked_for": want,
                                  "why": "no Doohly player with that name" if not hits else f"{len(hits)} players with that name"})
        return out, unmatched

    @staticmethod
    def main_frame(dev):
        frames = dev.get("frames") or []

        def area(f):
            b = f.get("bounds") or {}
            return (b.get("width") or 0) * (b.get("height") or 0)
        return max(frames, key=area) if frames else None

    # ---------------------------------------------------------------- one pass
    def run(self, with_bookings):
        with self.lock:
            sites, over = self.sites()
            devices = list(self.c.pages("/v2/devices"))
            ids, unmatched = self.match_devices(sites, over, devices)
            today = datetime.now(self.tz).date()
            week0 = today - timedelta(days=today.weekday())
            weeks = [week0, week0 + timedelta(days=7)]
            prev = self.doc.get("sites") or {}
            out_sites = {}
            for s in sites:
                sid = s["site_id"]
                if sid not in ids:
                    continue
                dev = self.c.get(f"/v2/devices/{ids[sid]}")
                fr = self.main_frame(dev)
                lp = (fr or {}).get("loopPolicy") or {}
                coords = ((dev.get("geoLocation") or {}).get("coordinates") or [None, None])
                site = {"site_id": sid, "screen": s.get("vault_screen") or s.get("name"),
                        "device": {"id": dev["id"], "name": dev.get("name"), "online": bool(dev.get("isConnected")),
                                   "alive_at": dev.get("aliveAt"), "timezone": dev.get("timezone"),
                                   "lat": coords[1], "lon": coords[0], "address": dev.get("location"),
                                   "operating_times": [{"day": o.get("dayOfWeek"), "from": (o.get("startTime") or "")[:5],
                                                        "to": (o.get("endTime") or "")[:5]} for o in dev.get("operatingTimes") or []]},
                        "frame": {"id": fr["id"], "name": fr.get("name"), "width": (fr.get("bounds") or {}).get("width"),
                                  "height": (fr.get("bounds") or {}).get("height")} if fr else None,
                        "loop": {"name": lp.get("name"), "slot_count": lp.get("slotCount"), "slot_s": lp.get("slotDuration"),
                                 "programmatic": (lp.get("programmatic") or {}).get("enabled"),
                                 "overbookable": lp.get("overbookable")} if lp else None,
                        "bookings": (prev.get(sid) or {}).get("bookings") or [],
                        "bookings_at": (prev.get(sid) or {}).get("bookings_at")}
                if with_bookings and fr:
                    site["bookings"] = self.bookings(fr["id"], weeks[0], weeks[1] + timedelta(days=6))
                    site["bookings_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                out_sites[sid] = site
            bh = hashlib.sha256(json.dumps({k: v["bookings"] for k, v in sorted(out_sites.items())},
                                           sort_keys=True).encode()).hexdigest()[:16]
            changed = bh != self.doc.get("bookings_hash")
            self.doc = {"format": "pop-monitor/doohly", "version": 1,
                        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "weeks": [w.isoformat() for w in weeks], "sites": out_sites, "unmatched": unmatched,
                        "bookings_hash": bh, "calls": self.c.calls, "ok": True, "error": None}
            self.atomic_json(self.path, self.doc)
            if with_bookings:
                self.last_bookings = time.time()
            self.check_offline(out_sites)
            return changed

    def bookings(self, frame_id, d0, d1):
        """Live bookings on a frame that overlap [d0, d1], with their creatives.
        Details are fetched only for new or changed bookings."""
        out = []
        listed = self.c.pages("/v3/bookings", frameId=frame_id, filter="status:in:" + ",".join(LIVE))
        for b in listed:
            if not overlaps(b.get("schedule"), d0, d1):
                continue
            hit = self.cache.get(b["id"])
            if hit and hit[0] == b.get("updatedAt"):
                out.append(hit[1])
                continue
            slim = slim_booking(self.c.get(f"/v3/bookings/{b['id']}"))
            self.cache[b["id"]] = [b.get("updatedAt"), slim]
            out.append(slim)
        keep = {b["id"] for b in out}
        stale = date.today() - timedelta(days=21)
        self.cache = {k: v for k, v in self.cache.items() if k in keep or (v[1].get("end") or "") >= stale.isoformat()}
        self.atomic_json(self.cache_p, self.cache)
        out.sort(key=lambda b: (b["start"] or "", b["campaign_id"] or "", b["name"] or ""))
        return out

    # ---------------------------------------------------------------- alerts
    def check_offline(self, sites):
        now = time.time()
        for sid, s in sites.items():
            d = s["device"]
            try:
                seen = datetime.fromisoformat((d.get("alive_at") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                seen = 0
            off_min = (now - seen) / 60 if seen else None
            offline = not d["online"] or (off_min is not None and off_min > OFFLINE_ALERT_MIN)
            if offline and sid not in self.alerted:
                if off_min is None or off_min >= OFFLINE_ALERT_MIN:
                    when = datetime.fromtimestamp(seen, self.tz).strftime("%a %d %b %H:%M") if seen else "unknown"
                    self.notify(f"Doohly player {d['name']} ({s['screen']}) is offline: last seen {when}. "
                                "The screen may be blank or showing old content.",
                                title=f"Warning: Doohly player offline at {s['screen']}")
                    self.alerted[sid] = now
            elif not offline and sid in self.alerted:
                self.notify(f"Doohly player {d['name']} ({s['screen']}) is back online.", ok=True,
                            title=f"Recovered: Doohly player at {s['screen']}")
                self.alerted.pop(sid, None)

    # ---------------------------------------------------------------- loop
    def status(self):
        d = self.doc or {}
        return {"enabled": bool(token()), "generated_at": d.get("generated_at"), "ok": d.get("ok"),
                "error": d.get("error"), "sites": len(d.get("sites") or {}), "unmatched": d.get("unmatched") or [],
                "bookings": sum(len(s.get("bookings") or []) for s in (d.get("sites") or {}).values()),
                "offline": [s["screen"] for s in (d.get("sites") or {}).values() if not s["device"]["online"]]}

    def loop(self):
        time.sleep(float(ENV("START_DELAY_S", "20")) + 5)
        failing = False
        while True:
            if token():
                try:
                    full = time.time() - self.last_bookings >= BOOKINGS_EVERY
                    changed = self.run(with_bookings=full)
                    if full:
                        self.log(f"doohly: {self.status()['bookings']} booking(s) on {self.status()['sites']} screen(s)"
                                 + (" (changed)" if changed else ""))
                    if changed:
                        self.on_change("Doohly bookings changed")
                    if failing:
                        self.notify("Reading Doohly works again.", ok=True, title="Recovered: Doohly sync")
                    failing = False
                except Exception as e:
                    self.doc = dict(self.doc or {}, ok=False, error=str(e)[:300])
                    try:
                        self.atomic_json(self.path, self.doc)
                    except OSError:
                        pass
                    self.log(f"doohly: {e}")
                    if not failing:
                        self.notify(f"Reading Doohly failed: {e}", title="Warning: Doohly sync failed")
                    failing = True
            time.sleep(DEVICE_EVERY)
