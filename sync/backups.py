"""Backups of the whole PoP Monitor, kept in SharePoint, and creatives-sync's own part of one.

Every night at BACKUP_AT (02:40) the receiver builds a backup (its database and settings,
the gateway's sites and learned creatives, and creatives-sync's state; no photos) and it
goes to SharePoint:

  <BACKUP_LIBRARY> / <BACKUP_ROOT> / pop-monitor-backup-2026-10-19_0240.tar.gz

The newest BACKUP_KEEP (14) are kept. The Health page lists them and can restore one. A
backup holds the camera logins, so the folder should be one only the right people see.

creatives-sync's part of a backup (state()) is what it has put in SharePoint
(pop-uploads.json: without it every PoP photo would be uploaded again) and the screen
sizes from the MIs. Restoring it (restore()) writes them back, keeping what they replace
in STATE/before-restore/, and the service then restarts.
"""
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

ENV = os.environ.get
BACKUP_AT = ENV("BACKUP_AT", "02:40")
LIBRARY = ENV("BACKUP_LIBRARY", "IT")
ROOT = ENV("BACKUP_ROOT", "PoP Monitor/Backups").strip("/")
KEEP = int(ENV("BACKUP_KEEP", "14"))
ENABLED = ENV("BACKUP_TO_SHAREPOINT", "1").lower() not in ("0", "no", "false", "off")
STATE_FILES = ("pop-uploads.json", "screen-specs.json", "artwork-picks.json", "campaign-folders.json")
CHUNK = 10 * 320 * 1024                     # upload session chunks (a multiple of 320 KiB)
NAME = re.compile(r"^pop-monitor-backup-[0-9_-]+\.tar\.gz$")


def state(state_dir):
    """creatives-sync's part of a backup."""
    files = {}
    for n in STATE_FILES:
        try:
            with open(os.path.join(state_dir, n), encoding="utf-8") as f:
                files[n] = f.read()
        except OSError:
            pass
    return {"format": "pop-sync/state", "version": 1, "files": files}


def restore(state_dir, doc):
    """Write a backup's creatives-sync part back. Raises ValueError if it isn't one."""
    if not isinstance(doc, dict) or doc.get("format") != "pop-sync/state" or not isinstance(doc.get("files"), dict):
        raise ValueError("expected creatives-sync's state from a backup")
    for n, txt in doc["files"].items():
        if n not in STATE_FILES or not isinstance(txt, str):
            raise ValueError(f"unexpected file in the backup: {n}")
        json.loads(txt)                                   # must be JSON
    before = os.path.join(state_dir, "before-restore")
    shutil.rmtree(before, ignore_errors=True)
    os.makedirs(before)
    for n in STATE_FILES:
        p = os.path.join(state_dir, n)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(before, n))
    for n, txt in doc["files"].items():
        tmp = os.path.join(state_dir, n + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(txt)
        os.replace(tmp, os.path.join(state_dir, n))
    return len(doc["files"])


class Backups:
    """The nightly backup into SharePoint, the list of them, and fetching one to restore."""

    def __init__(self, graph_factory, state_dir, receiver, api_key, local_tz, log, notify,
                 atomic_json, read_json, quote):
        self.graph_factory = graph_factory
        self.state_dir, self.receiver, self.key, self.tz = state_dir, receiver, api_key, local_tz
        self.log, self.notify, self.atomic_json, self.q = log, notify, atomic_json, quote
        self.path = os.path.join(state_dir, "backups.json")
        self.st = read_json(self.path, {}) or {}
        self.lock = threading.Lock()
        self.g = self.drive = self.folder = None

    def save(self):
        self.atomic_json(self.path, self.st)

    def status(self):
        now = datetime.now(self.tz)
        h, m = (int(x) for x in BACKUP_AT.split(":"))
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return dict(self.st, enabled=ENABLED, at=BACKUP_AT, keep=KEEP, where=f"{LIBRARY} / {ROOT}",
                    next=nxt.isoformat(timespec="minutes"), running=self.lock.locked())

    # ------------------------------------------------------------ SharePoint
    def _connect(self):
        if not self.drive:
            self.g = self.graph_factory()
            self.drive = ENV("BACKUP_DRIVE_ID") or self._drive_of()
        if not self.folder:
            self.folder = self._folder(ROOT)
        return self.drive

    def _drive_of(self):
        """The backup library's drive id."""
        site = self.g.get(f"/sites/{lib_site()}", **{"$select": "id"})
        want = LIBRARY.lower()
        for d in self.g.pages(f"/sites/{site['id']}/drives", **{"$select": "id,name,webUrl"}):
            tail = requests.utils.unquote(d.get("webUrl", "")).rstrip("/").rsplit("/", 1)[-1].lower()
            if d.get("name", "").lower() == want or tail == want:
                return d["id"]
        raise RuntimeError(f"no document library called {LIBRARY!r} for the backups (BACKUP_LIBRARY)")

    def _folder(self, rel):
        """The folder at rel (made, with its parents, if missing): its id."""
        try:
            return self.g.request("GET", f"/drives/{self.drive}/root:/{self.q(rel)}", params={"$select": "id"}).json()["id"]
        except Exception as e:
            if " 404 " not in str(e):
                raise
        parent, _, name = rel.rpartition("/")
        url = (f"/drives/{self.drive}/items/{self._folder(parent)}/children" if parent
               else f"/drives/{self.drive}/root/children")
        try:
            return self.g.request("POST", url, json={"name": name, "folder": {},
                                                     "@microsoft.graph.conflictBehavior": "fail"}).json()["id"]
        except Exception:
            return self.g.request("GET", f"/drives/{self.drive}/root:/{self.q(rel)}", params={"$select": "id"}).json()["id"]

    def items(self):
        """The backups in SharePoint, newest first."""
        self._connect()
        out = []
        for it in self.g.pages(f"/drives/{self.drive}/items/{self.folder}/children",
                               **{"$select": "id,name,size,lastModifiedDateTime,webUrl,file", "$top": "200"}):
            if "file" in it and NAME.match(it.get("name", "")):
                out.append({"id": it["id"], "name": it["name"], "size": it.get("size"),
                            "at": it.get("lastModifiedDateTime"), "url": it.get("webUrl")})
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def download(self, item_id, out):
        """Stream one backup into the file object out."""
        self._connect()
        it = self.g.request("GET", f"/drives/{self.drive}/items/{item_id}",
                            params={"$select": "id,name,@microsoft.graph.downloadUrl"}).json()
        if not NAME.match(it.get("name", "")):
            raise ValueError("not a backup")
        with requests.get(it["@microsoft.graph.downloadUrl"], stream=True, timeout=600) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                out.write(chunk)

    def _upload(self, path, name):
        size = os.path.getsize(path)
        sess = self.g.request("POST", f"/drives/{self.drive}/items/{self.folder}:/{self.q(name)}:/createUploadSession",
                              json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
        last = None
        with open(path, "rb") as f:
            i = 0
            while i < size:
                chunk = f.read(CHUNK)
                for attempt in range(4):
                    try:
                        last = requests.put(sess["uploadUrl"], data=chunk, timeout=300, headers={
                            "Content-Range": f"bytes {i}-{i + len(chunk) - 1}/{size}", "Content-Length": str(len(chunk))})
                        last.raise_for_status()
                        break
                    except requests.RequestException:
                        if attempt == 3:
                            raise
                        time.sleep(5 * (attempt + 1))
                i += len(chunk)
        return last.json() if last is not None else {}

    # ------------------------------------------------------------ the nightly run
    def due(self, now):
        h, m = (int(x) for x in BACKUP_AT.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < slot:
            slot -= timedelta(days=1)
        last = self.st.get("last_try", 0)
        return last < slot.timestamp() or (self.st.get("ok") is False and time.time() - last >= 3600)

    def loop(self, configured):
        time.sleep(120)
        while True:
            try:
                if ENABLED and configured() and self.due(datetime.now(self.tz)):
                    self.run()
            except Exception as e:                           # never kill the thread
                self.log(f"backup: {e}")
            time.sleep(60)

    def run(self):
        with self.lock:
            self.st["last_try"] = time.time()
            tmp = os.path.join(self.state_dir, "backup.part")
            t0 = time.time()
            try:
                with requests.get(f"{self.receiver}/backup", stream=True, timeout=1800,
                                  headers={"Authorization": f"Bearer {self.key}"}) as r:
                    if r.status_code != 200:
                        try:
                            err = r.json().get("error")
                        except ValueError:
                            err = r.text[:200]
                        raise RuntimeError(f"the receiver couldn't make a backup: {err}")
                    name = re.search(r'filename="([^"]+)"', r.headers.get("Content-Disposition", "")) \
                        or None
                    name = name.group(1) if name else f"pop-monitor-backup-{datetime.now(self.tz):%Y-%m-%d_%H%M}.tar.gz"
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                self._connect()
                up = self._upload(tmp, name)
                size = os.path.getsize(tmp)
                gone = 0
                for old in self.items()[KEEP:]:
                    self.g.request("DELETE", f"/drives/{self.drive}/items/{old['id']}")
                    gone += 1
                was_bad = self.st.get("ok") is False
                self.st.update(ok=True, error=None, last=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               name=name, size=size, url=up.get("webUrl"), took_s=round(time.time() - t0))
                self.log(f"backup: {name} ({size / 1e6:.0f} MB) to {LIBRARY}/{ROOT}" + (f", {gone} old one(s) removed" if gone else ""))
                if was_bad:
                    self.notify("Nightly backups are working again.", ok=True, title="Recovered: PoP Monitor backup")
            except Exception as e:
                first = self.st.get("ok") is not False
                self.st.update(ok=False, error=str(e)[:300])
                self.log(f"backup: failed: {e}")
                if first:
                    self.notify(f"The nightly PoP Monitor backup to SharePoint failed: {e}",
                                title="Warning: PoP Monitor backup failed")
                self.drive = self.folder = None
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self.save()


def lib_site():
    return ENV("SP_SITE", "lumodigital.sharepoint.com:/sites/LUMO")
