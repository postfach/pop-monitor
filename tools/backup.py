"""Backup and restore of a whole PoP Monitor (receiver, gateway and creatives-sync state).

A backup is one .tar.gz:

  manifest.json          what it is: when, from where, the sites, how many records
  receiver/pop.db        every play and alert (a consistent copy of the live database)
  receiver/*.json        links, merges, artwork matches, rotations, resets, the Vault pull
  gateway.json           the gateway's sites (with regions and camera logins), learned
                         creatives and photo history (from its /api/backup)
  creatives-sync.json    which PoP photos are in SharePoint, screen sizes (its /backup)

Not in it: the photos (the last few days; the best are in SharePoint) and the artwork
library (synced again from SharePoint). It does contain the camera logins: keep it safe.

Restoring stages the receiver's files in <data>/restore-staged/ and they are put in place
when the receiver next starts, before the database is opened; what they replace goes to
<data>/before-restore/. The gateway and creatives-sync do the same with their parts.
"""
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import time

FORMAT = "pop-monitor/backup"
VERSION = 1
RECEIVER_FILE = re.compile(r"^receiver/(pop\.db|[A-Za-z0-9_.-]+\.json)$")
PARTS = {"manifest.json", "gateway.json", "creatives-sync.json"}


def receiver_files(out_dir):
    """The receiver's small state files (top level of its data folder)."""
    return sorted(n for n in os.listdir(out_dir)
                  if n.endswith(".json") and not n.startswith(".") and os.path.isfile(os.path.join(out_dir, n)))


def _add_bytes(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = time.time()
    tar.addfile(info, io.BytesIO(data))


def build(out_dir, dest, gateway=None, sync=None, extra=None):
    """Write a backup to dest (.tar.gz). gateway / sync: their state (dicts) or None.
    Returns the manifest."""
    tmp_db = dest + ".db"
    try:
        src = sqlite3.connect(os.path.join(out_dir, "pop.db"))
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)                       # consistent even while plays are arriving
        counts = dict(dst.execute("SELECT type, COUNT(*) FROM records GROUP BY type").fetchall())
        src.close()
        dst.close()
        files = receiver_files(out_dir)
        sites = []
        try:
            sites = [{"id": c.get("site_id"), "name": c.get("name") or c.get("site_id")}
                     for c in json.loads((gateway or {}).get("files", {}).get("cameras.json") or "[]")]
        except ValueError:
            pass
        manifest = dict({"format": FORMAT, "version": VERSION,
                         "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "host": socket.gethostname(), "sites": sites,
                         "plays": counts.get("creative_change", 0), "alerts": counts.get("fault", 0),
                         "receiver_files": files, "gateway": bool(gateway), "creatives_sync": bool(sync),
                         "learned_sites": len([k for k in (gateway or {}).get("files", {}) if k.startswith("creatives/")])},
                        **(extra or {}))
        with tarfile.open(dest, "w:gz", compresslevel=3) as tar:
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
            tar.add(tmp_db, "receiver/pop.db")
            for n in files:
                tar.add(os.path.join(out_dir, n), f"receiver/{n}")
            if gateway:
                _add_bytes(tar, "gateway.json", json.dumps(gateway).encode())
            if sync:
                _add_bytes(tar, "creatives-sync.json", json.dumps(sync).encode())
        return manifest
    finally:
        if os.path.exists(tmp_db):
            os.remove(tmp_db)


def read(path):
    """Check a backup and return (manifest, gateway state or None, sync state or None).
    Raises ValueError if it isn't one, or holds anything it shouldn't."""
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ValueError(f"not a PoP Monitor backup ({e})")
    with tar:
        names = []
        for m in tar.getmembers():
            if not m.isfile() or not (m.name in PARTS or RECEIVER_FILE.match(m.name)):
                raise ValueError(f"unexpected item in the backup: {m.name}")
            names.append(m.name)
        if "manifest.json" not in names or "receiver/pop.db" not in names:
            raise ValueError("not a PoP Monitor backup (no manifest or database)")
        manifest = json.load(tar.extractfile("manifest.json"))
        if manifest.get("format") != FORMAT or int(manifest.get("version", 0)) > VERSION:
            raise ValueError("not a PoP Monitor backup this version can read")
        gw = json.load(tar.extractfile("gateway.json")) if "gateway.json" in names else None
        sy = json.load(tar.extractfile("creatives-sync.json")) if "creatives-sync.json" in names else None
        return manifest, gw, sy


def stage_receiver(path, out_dir):
    """Put the backup's receiver files in <out_dir>/restore-staged/ (READY written last)."""
    staged = os.path.join(out_dir, "restore-staged")
    shutil.rmtree(staged, ignore_errors=True)
    os.makedirs(staged)
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            if not RECEIVER_FILE.match(m.name):
                continue
            target = os.path.join(staged, m.name.split("/", 1)[1])
            with tar.extractfile(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    try:                                               # check the database before anything relies on it
        db = sqlite3.connect(os.path.join(staged, "pop.db"))
        ok = db.execute("PRAGMA quick_check").fetchone()[0]
        db.close()
    except sqlite3.Error as e:
        ok = str(e)
    if ok != "ok":
        shutil.rmtree(staged, ignore_errors=True)
        raise ValueError(f"the backup's database is damaged ({ok})")
    return staged


def mark_ready(out_dir):
    with open(os.path.join(out_dir, "restore-staged", "READY"), "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def unstage(out_dir):
    shutil.rmtree(os.path.join(out_dir, "restore-staged"), ignore_errors=True)


def apply_staged(out_dir, log=print):
    """At start, before the database is opened: put a staged restore in place. The files
    it replaces go to before-restore/. Returns True if a restore was applied."""
    staged = os.path.join(out_dir, "restore-staged")
    if not os.path.exists(os.path.join(staged, "READY")):
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)  # half-written: ignore it
        return False
    before = os.path.join(out_dir, "before-restore")
    shutil.rmtree(before, ignore_errors=True)
    os.makedirs(before)
    for n in ["pop.db", "pop.db-wal", "pop.db-shm"] + receiver_files(out_dir):
        p = os.path.join(out_dir, n)
        if os.path.exists(p):
            os.replace(p, os.path.join(before, n))
    os.remove(os.path.join(staged, "READY"))
    for n in os.listdir(staged):
        os.replace(os.path.join(staged, n), os.path.join(out_dir, n))
    shutil.rmtree(staged, ignore_errors=True)
    log(f"restored from a backup; the previous data is in {before}")
    return True
