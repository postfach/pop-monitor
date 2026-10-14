"""pop-upload: the best day and night PoP photo of every campaign, into SharePoint.

  receiver /pop/best ──► best photo per campaign, site, artwork version and light
        │                (scored for glare, sunstrike, rain, blur, occlusion and
        │                 how clearly the creative shows: tools/popscore.py)
        ▼
  SharePoint  Sales / 00_Sales (NEW FOLDER) / Booked Campaigns / PoP Images /
              <year> / <MM - Month> / <site> /
                LUMO_19702_Curletts-In_2026-10-13_1432_Day_Score87_v1.jpg

When it runs (local time, TZ):
  * POP_DAY_AT (20:00): the daytime photos taken since the last daytime run;
  * POP_NIGHT_AT (08:00): the night photos taken since the last night run.
  A run that was missed (container down) catches up on the next start, back to
  POP_CATCHUP_DAYS at most.

For each campaign, site, version (one per booked artwork file: a campaign with
three executions gets v1, v2, v3) and light (day or night), the first run puts
up the best photo; later runs replace it only with a photo that scores higher
(the new file is uploaded, then the old one deleted). The year and month folder
is the month the campaign was first seen at the site, so a campaign's photos
stay together however long it runs. Creatives not linked to a campaign
(unassigned, programmatic, house) are never uploaded.

State (what is up there, with its score and SharePoint item) is kept in
STATE_DIR/pop-uploads.json; deleting it makes the next run upload afresh.
"""
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

ENV = os.environ.get
POP_ROOT = ENV("POP_ROOT", "00_Sales (NEW FOLDER)/Booked Campaigns/PoP Images").strip("/")
DAY_AT = ENV("POP_DAY_AT", "20:00")
NIGHT_AT = ENV("POP_NIGHT_AT", "08:00")
CATCHUP_DAYS = float(ENV("POP_CATCHUP_DAYS", "3"))   # no further back than the photos are kept
ENABLED = ENV("POP_UPLOAD", "1").lower() not in ("0", "no", "false", "off")
RETRY_S = float(ENV("POP_RETRY_MIN", "60")) * 60   # a failed run is tried again after this
SIMPLE_MAX = 4 * 1024 * 1024 - 1           # Graph's simple upload limit; larger files use an upload session
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def safe_part(name):
    """A SharePoint-safe file or folder name."""
    s = re.sub(r'["*:<>?/\\|#%\x00-\x1f]', "-", str(name or "")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s[:120] or "unnamed"


def slug(name):
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", str(name or ""))).strip("-") or "site"


def month_folder(iso_day):
    d = date.fromisoformat(iso_day[:10])
    return f"{d.year}/{d.month:02d} - {MONTHS[d.month - 1]}"


def file_name(item, phase, version_no):
    taken = datetime.fromisoformat(item["taken_at"])
    return (f"{item['campaign_id']}_{slug(item.get('screen') or item.get('site_name'))}_"
            f"{taken:%Y-%m-%d_%H%M}_{'Day' if phase == 'day' else 'Night'}_"
            f"Score{int(round(item['score']))}_v{version_no}.jpg")


def at_today(hhmm, now):
    h, m = (int(x) for x in hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


class PopUploader:
    def __init__(self, graph, library_cls, state_dir, receiver, api_key, local_tz, log, notify,
                 atomic_json, read_json, quote):
        self.g, self.Library = graph, library_cls
        self.path = os.path.join(state_dir, "pop-uploads.json")
        self.receiver, self.key, self.tz = receiver, api_key, local_tz
        self.log, self.notify, self.atomic_json, self.q = log, notify, atomic_json, quote
        self.st = read_json(self.path, {}) or {}
        for k in ("runs", "items", "versions", "folders", "status"):
            self.st.setdefault(k, {})
        self.lock = threading.Lock()
        self.drive = None

    # ------------------------------------------------------------ state
    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.atomic_json(self.path, self.st)

    def status(self):
        s = dict(self.st["status"])
        now = datetime.now(self.tz)
        s.update(enabled=ENABLED, root=POP_ROOT, day_at=DAY_AT, night_at=NIGHT_AT,
                 next_day=self._next(DAY_AT, now).isoformat(timespec="minutes"),
                 next_night=self._next(NIGHT_AT, now).isoformat(timespec="minutes"),
                 photos=len(self.st["items"]))
        return s

    @staticmethod
    def _next(hhmm, now):
        t = at_today(hhmm, now)
        return t if t > now else t + timedelta(days=1)

    # ------------------------------------------------------------ schedule
    def due(self, now):
        """Phases whose time today has passed and that haven't run since then."""
        out = []
        for phase, hhmm in (("day", DAY_AT), ("night", NIGHT_AT)):
            slot = at_today(hhmm, now)
            if now < slot:
                slot -= timedelta(days=1)
            run = self.st["runs"].get(phase, {})
            last = run.get("at", 0)
            if last < slot.timestamp() or (run.get("ok") is False and now.timestamp() - last >= RETRY_S):
                out.append(phase)
        return out

    def loop(self, configured):
        time.sleep(float(ENV("START_DELAY_S", "20")) + 10)    # let the receiver start first
        while True:
            try:
                if ENABLED and configured():
                    for phase in self.due(datetime.now(self.tz)):
                        self.run(phase)
            except Exception as e:                      # never kill the thread
                self.log(f"pop-upload: {e}")
            time.sleep(30)

    # ------------------------------------------------------------ receiver
    def _get(self, path, **params):
        r = requests.get(f"{self.receiver}{path}", params=params or None,
                         headers={"Authorization": f"Bearer {self.key}"}, timeout=900)
        r.raise_for_status()
        return r

    # ------------------------------------------------------------ SharePoint
    def _drive(self):
        if not self.drive:
            self.drive = self.Library(self.g).connect().drive
        return self.drive

    def upload(self, rel_path, data):
        """Upload (or replace) a file at rel_path in the library; returns the item."""
        drive = self._drive()
        url = f"/drives/{drive}/root:/{self.q(rel_path)}"
        if len(data) <= SIMPLE_MAX:
            return self.g.request("PUT", f"{url}:/content", params={"@microsoft.graph.conflictBehavior": "replace"},
                                  data=data).json()
        sess = self.g.request("POST", f"{url}:/createUploadSession",
                              json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
        step, last = 5 * 320 * 1024, None
        for i in range(0, len(data), step):
            chunk = data[i:i + step]
            last = requests.put(sess["uploadUrl"], data=chunk, timeout=120, headers={
                "Content-Range": f"bytes {i}-{i + len(chunk) - 1}/{len(data)}", "Content-Length": str(len(chunk))})
            last.raise_for_status()
        return last.json()

    def delete(self, item_id):
        try:
            self.g.request("DELETE", f"/drives/{self._drive()}/items/{item_id}")
        except Exception as e:
            if " 404 " not in str(e):                   # already gone (moved or deleted by hand): fine
                raise

    # ------------------------------------------------------------ one run
    def start(self, phase):
        """Run now in the background (the Bookings page button). False if one is running."""
        if self.lock.locked():
            return False
        threading.Thread(target=self.run, args=(phase,), kwargs={"manual": True}, daemon=True).start()
        return True

    def run(self, phase, since=None, until=None, manual=False):
        with self.lock:
            self.st["status"]["running"] = phase
            try:
                return self._run(phase, since, until, manual)
            finally:
                self.st["status"]["running"] = None
                self.save()

    def _run(self, phase, since, until, manual):
        now = time.time()
        last = self.st["runs"].get(phase, {}).get("until")
        until = until or now
        since = since or max(last or (until - 86400), until - CATCHUP_DAYS * 86400)
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.log(f"pop-upload: {phase} photos from {datetime.fromtimestamp(since, self.tz):%a %d %b %H:%M} "
                 f"to {datetime.fromtimestamp(until, self.tz):%a %d %b %H:%M}")
        up = kept = 0
        errors = []
        try:
            current = {f"{v.get('site_id')}|{k.split('|')[0]}|{v.get('version')}": v["event_id"]
                       for k, v in self.st["items"].items() if k.endswith("|" + phase) and v.get("event_id")}
            r = requests.post(f"{self.receiver}/pop/best", timeout=900,
                              headers={"Authorization": f"Bearer {self.key}"},
                              json={"since": since, "until": until, "phase": phase, "current": current})
            r.raise_for_status()
            best = r.json()
            if best.get("error"):
                raise RuntimeError(best["error"])
            for it in best.get("items", []):
                try:
                    if self.consider(it, phase):
                        up += 1
                    else:
                        kept += 1
                except Exception as e:
                    errors.append(f"{it.get('campaign_id')} at {it.get('screen')}: {e}")
                    self.log(f"pop-upload: {errors[-1]}")
                    if re.search(r"Graph 40[13] |not set|sign-in failed", str(e)):
                        break                       # access problem: every file would fail the same way
            ok = not errors
            self.st["runs"][phase] = {"at": now, "until": until if ok else since, "since": since, "ok": ok}
            msg = f"{up} new or better {phase} photo(s), {kept} kept" + (" (run by hand)" if manual else "")
            if not best.get("items"):
                msg = f"no {phase} photos of campaigns in this window" + (" (run by hand)" if manual else "")
            self.st["status"].update({f"last_{phase}": started, f"last_{phase}_result": msg, "ok": ok,
                                      "error": "; ".join(errors[:3]) if errors else None})
            self.log(f"pop-upload: {msg}" + (f", {len(errors)} failed" if errors else ""))
        except Exception as e:
            ok = False
            self.st["runs"][phase] = {"at": now, "until": since, "since": since, "ok": False}   # retried
            self.st["status"].update(ok=False, error=f"{phase} run: {e}", **{f"last_{phase}": started})
            self.log(f"pop-upload: {phase} run failed: {e}")
        was_ok = self.st["status"].get("notified_ok", True)
        if not ok and was_ok:
            self.notify(f"PoP photo upload ({phase}) failed: {self.st['status'].get('error')}",
                        title="Warning: PoP photo upload failed")
        elif ok and not was_ok:
            self.notify(f"PoP photo uploads are working again ({phase}: {msg}).", ok=True,
                        title="Recovered: PoP photo upload")
        self.st["status"]["notified_ok"] = ok
        self.save()
        return ok

    def consider(self, it, phase):
        """Upload this photo if it is the first or beats the one up there. True if uploaded."""
        ck = f"{it['campaign_id']}|{it['site_id']}"
        key = f"{ck}|{it['version']}|{phase}"
        cur = self.st["items"].get(key)
        if cur:
            if it["event_id"] == cur.get("event_id"):
                return False                             # what is up there is still the best
            # scored again with today's photos when the receiver still has it, else as uploaded
            ref = it.get("current_score") if it.get("current_event_id") == cur.get("event_id") else None
            if float(it["score"]) <= float(ref if ref is not None else cur.get("score", 0)):
                return False
        folder = self.st["folders"].get(ck) or month_folder(it.get("first_seen") or it["taken_at"])
        vers = self.st["versions"].setdefault(ck, {})
        vno = vers.get(it["version"]) or (max(vers.values() or [0]) + 1)
        name = file_name(it, phase, vno)
        site_dir = safe_part(it.get("screen") or it.get("site_name") or it["site_id"])
        rel = f"{POP_ROOT}/{folder}/{site_dir}/{name}"
        data = self._get(f"/pop/frame/{it['event_id']}.jpg").content
        new = self.upload(rel, data)
        if cur and cur.get("item_id") and cur.get("item_id") != new.get("id"):
            self.delete(cur["item_id"])
        self.st["folders"][ck] = folder
        vers[it["version"]] = vno
        self.st["items"][key] = {"score": it["score"], "name": name, "path": rel, "item_id": new.get("id"),
                                 "site_id": it["site_id"], "version": it["version"],
                                 "url": new.get("webUrl"), "event_id": it["event_id"], "taken_at": it["taken_at"],
                                 "version_label": it.get("version_label"), "parts": it.get("parts"),
                                 "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "replaced": (cur or {}).get("name")}
        recent = self.st["status"].setdefault("recent", [])
        recent.insert(0, {"name": name, "url": new.get("webUrl"), "score": it["score"], "site": it.get("screen"),
                          "at": self.st["items"][key]["uploaded_at"], "was": (cur or {}).get("score")})
        del recent[15:]
        self.save()                                      # after every file: a crash never orphans one
        self.log(f"pop-upload: {rel} ({it['score']}" + (f", was {cur['score']})" if cur else ")"))
        return True
