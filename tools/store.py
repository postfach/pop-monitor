"""The receiver's store: PoP records, alerts and heartbeats in SQLite, with
housekeeping so the disk doesn't fill up.

  out/pop.db                    records (one row per PoP or alert, with its JSON),
                                heartbeats
  out/images/<event>.frame.jpg  the photos stay files; their rows say whether they
            <event>.roi.jpg     are still there

Retention (environment, days):
  POP_IMAGE_DAYS   3    PoP photos (the best ones are in SharePoint already)
  POP_ALERT_IMAGE_DAYS 30  photos attached to alerts
  POP_RECORD_DAYS  90   PoP and alert rows (small: the details of every play)
  POP_HEARTBEAT_DAYS 7  gateway heartbeats

On first start an existing records.jsonl / heartbeats.jsonl is imported and
renamed *.imported, so nothing is lost when upgrading.
"""
import json
import os
import sqlite3
import threading
import time
import zlib
from datetime import datetime

IMAGE_DAYS = float(os.environ.get("POP_IMAGE_DAYS", "3"))
# Full camera frames are only needed to upload the best photos: once both the day and
# the night best-photo runs have covered a time (and this long has passed for the
# upload to finish), the frames from before it are removed except the best ones.
FRAME_GRACE_S = float(os.environ.get("POP_FRAME_GRACE_MIN", "120")) * 60
ALERT_IMAGE_DAYS = float(os.environ.get("POP_ALERT_IMAGE_DAYS", "30"))
RECORD_DAYS = float(os.environ.get("POP_RECORD_DAYS", "90"))
HEARTBEAT_DAYS = float(os.environ.get("POP_HEARTBEAT_DAYS", "7"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  event_id TEXT PRIMARY KEY,
  type     TEXT NOT NULL,          -- creative_change | fault
  site_id  TEXT NOT NULL DEFAULT '',
  t        REAL,                   -- play start (PoP) or alert time, unix seconds
  creative TEXT,
  blank    INTEGER NOT NULL DEFAULT 0,
  video    INTEGER NOT NULL DEFAULT 0,
  usable   INTEGER NOT NULL DEFAULT 1,
  has_roi  INTEGER NOT NULL DEFAULT 0,   -- the photos are on disk (cleared when removed)
  has_frame INTEGER NOT NULL DEFAULT 0,
  name     TEXT,
  json     BLOB NOT NULL           -- the whole record, zlib-compressed JSON
);
CREATE INDEX IF NOT EXISTS records_site_t ON records (site_id, type, t);
CREATE INDEX IF NOT EXISTS records_t ON records (t);
CREATE TABLE IF NOT EXISTS heartbeats (t REAL NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS heartbeats_t ON heartbeats (t);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- records kept with their photos whatever their age: the best photos uploaded to
-- SharePoint, so each can be scored again against new ones (tag: who pinned it)
CREATE TABLE IF NOT EXISTS pins (event_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (event_id, tag));
-- a site being set up (its region still being placed): its plays in [start, end) aren't
-- counted; end NULL = still in setup; region_at = the region's last change in the period
CREATE TABLE IF NOT EXISTS setup (site_id TEXT NOT NULL, start REAL NOT NULL, end REAL, region_at REAL);
"""


def _ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _enc(r):
    """Rows keep the full record compressed (about a third of the size)."""
    return zlib.compress(json.dumps(r, separators=(",", ":")).encode(), 6)


def _dec(v):
    if isinstance(v, (bytes, memoryview)):
        v = zlib.decompress(bytes(v)).decode()
    return json.loads(v)


def _flags(r, roi, frame):
    """Mark a record whose photo (screen crop) or full camera frame is no longer on disk."""
    if not roi and "roi_sha256" in r:
        r["_images_removed"] = True
    if not frame and "frame_sha256" in r:
        r["_frame_removed"] = True
    return r


def _name(r):
    return r.get("site_name") or r.get("camera") or ""


class Store:
    def __init__(self, out_dir, log=print, migrate=True):
        self.out = out_dir
        self.images = os.path.join(out_dir, "images")
        self.path = os.path.join(out_dir, "pop.db")
        self.log = log
        self.local = threading.local()
        self.wlock = threading.Lock()
        self.version = 0                 # bumped on every write: callers cache against it
        os.makedirs(self.images, exist_ok=True)
        db = self.db()
        db.executescript(SCHEMA)
        if migrate:
            self.migrate()

    def db(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self.local.c = c
        return c

    # ---------------------------------------------------------------- writing
    @staticmethod
    def _row(r):
        typ = r.get("type") or ""
        t = _ts(r.get("play_started_at")) if typ == "creative_change" else _ts(r.get("at") or r.get("since"))
        return (r.get("event_id"), typ, r.get("site_id") or "", t, r.get("local_creative_id"),
                int(bool(r.get("is_blank"))), int(bool(r.get("video_creative"))),
                int((r.get("quality") or {}).get("usable", True) is not False),
                int("roi_sha256" in r), int("frame_sha256" in r), _name(r), _enc(r))

    def add(self, r):
        """Store a record; False if its event ID is already stored (a retry)."""
        with self.wlock:
            cur = self.db().execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self._row(r))
            self.version += 1
            return cur.rowcount == 1

    def seen(self, event_id):
        return self.db().execute("SELECT 1 FROM records WHERE event_id=?", (event_id,)).fetchone() is not None

    def add_heartbeat(self, hb, raw=None):
        t = _ts(hb.get("sent_at")) or time.time()
        with self.wlock:
            self.db().execute("INSERT INTO heartbeats VALUES (?,?)", (t, raw or json.dumps(hb)))

    # ---------------------------------------------------------------- reading
    def plays(self, site_id, t0, t1):
        """A site's PoP plays with t0 <= start < t1, oldest first, as tuples
        (t, creative, blank, video, event_id, has_roi, usable)."""
        rows = self.db().execute(
            "SELECT t, creative, blank, video, event_id, has_roi, usable FROM records "
            "WHERE site_id=? AND type='creative_change' AND t>=? AND t<? ORDER BY t", (site_id, t0, t1)).fetchall()
        return [(t, c, bool(b), bool(v), e, bool(h), bool(u)) for t, c, b, v, e, h, u in rows]

    def last_plays(self, site_id, n):
        rows = self.db().execute(
            "SELECT t, creative, blank, video, event_id, has_roi, usable FROM records "
            "WHERE site_id=? AND type='creative_change' ORDER BY t DESC LIMIT ?", (site_id, n)).fetchall()
        return [(t, c, bool(b), bool(v), e, bool(h), bool(u)) for t, c, b, v, e, h, u in reversed(rows)]

    def play(self, event_id):
        r = self.db().execute("SELECT t, creative, blank, video, event_id, has_roi, usable, site_id FROM records "
                              "WHERE event_id=?", (event_id,)).fetchone()
        return r

    def first_play(self, site_id, creatives, before):
        """Time of the first play of any of these creatives at a site."""
        if not creatives:
            return None
        q = ",".join("?" * len(creatives))
        r = self.db().execute(f"SELECT MIN(t) FROM records WHERE site_id=? AND type='creative_change' AND blank=0 "
                              f"AND creative IN ({q}) AND t<?", (site_id, *creatives, before)).fetchone()
        return r[0] if r else None

    def recent(self, n, site_id=None):
        """The newest n records (PoPs and alerts), oldest first, as dicts."""
        if site_id:
            rows = self.db().execute("SELECT json, has_roi, has_frame FROM records WHERE site_id=? OR site_id='' "
                                     "ORDER BY t DESC LIMIT ?", (site_id, n)).fetchall()
        else:
            rows = self.db().execute("SELECT json, has_roi, has_frame FROM records ORDER BY t DESC LIMIT ?",
                                     (n,)).fetchall()
        out = []
        for j, roi, frame in reversed(rows):
            out.append(_flags(_dec(j), roi, frame))
        return out

    def newest_per_site(self, kind="creative_change", photo=False):
        """Each site's newest record; with photo=True its newest one that has a photo
        on disk (the gateway may photograph only some plays), else its newest."""
        rows = []
        if photo:
            rows = self.db().execute(
                "SELECT r.json, r.has_roi, r.has_frame FROM records r JOIN (SELECT site_id, MAX(t) mt FROM records "
                "WHERE type=? AND has_roi=1 GROUP BY site_id) m ON r.site_id=m.site_id AND r.t=m.mt "
                "WHERE r.type=? AND r.has_roi=1", (kind, kind)).fetchall()
        rows = rows + self.db().execute(
            "SELECT r.json, r.has_roi, r.has_frame FROM records r JOIN (SELECT site_id, MAX(t) mt FROM records "
            "WHERE type=? GROUP BY site_id) m ON r.site_id=m.site_id AND r.t=m.mt WHERE r.type=?", (kind, kind)).fetchall()
        out = {}
        for j, roi, frame in rows:
            r = _flags(_dec(j), roi, frame)
            out.setdefault(r.get("site_id"), r)             # the one with a photo first
        return list(out.values())

    def alerts(self, n, site_id=None):
        sql = "SELECT json FROM records WHERE type='fault'" + (" AND (site_id=? OR site_id='')" if site_id else "") + \
              " ORDER BY t DESC LIMIT ?"
        rows = self.db().execute(sql, ((site_id, n) if site_id else (n,))).fetchall()
        return [_dec(j) for (j,) in reversed(rows)]

    def sites(self):
        """Every site ID with records: counts, newest time and latest name."""
        rows = self.db().execute(
            "SELECT site_id, SUM(type='creative_change'), SUM(type='fault'), MAX(t), "
            "(SELECT name FROM records r2 WHERE r2.site_id=r.site_id ORDER BY t DESC LIMIT 1) "
            "FROM records r WHERE site_id<>'' GROUP BY site_id").fetchall()
        return [{"site_id": s, "pops": p or 0, "alerts": a or 0, "last_t": t, "name": n or ""} for s, p, a, t, n in rows]

    def last_heartbeats(self, n):
        rows = self.db().execute("SELECT json FROM heartbeats ORDER BY t DESC LIMIT ?", (n,)).fetchall()
        out = []
        for (j,) in reversed(rows):
            try:
                out.append(json.loads(j))
            except ValueError:
                pass
        return out

    def counts(self):
        db = self.db()
        n = db.execute("SELECT COUNT(*), SUM(has_roi OR has_frame), MIN(t) FROM records").fetchone()
        return {"records": n[0], "with_photos": n[1] or 0, "oldest": n[2],
                "db_mb": round(os.path.getsize(self.path) / 1e6, 1) if os.path.exists(self.path) else 0}

    # ---------------------------------------------------------------- changing history
    def _remove_images(self, eids):
        n = 0
        for e in eids:
            for kind in ("frame", "roi"):
                try:
                    os.remove(os.path.join(self.images, f"{e}.{kind}.jpg"))
                    n += 1
                except (FileNotFoundError, TypeError):
                    pass
        return n

    def delete_site(self, site_id):
        """Remove a site's records and photos. Returns how many records."""
        eids = [e for (e,) in self.db().execute("SELECT event_id FROM records WHERE site_id=?", (site_id,))]
        self._remove_images(eids)
        with self.wlock:
            self.db().execute("DELETE FROM records WHERE site_id=?", (site_id,))
            self.version += 1
        return len(eids)

    def rename_site(self, old, new, name=None):
        """Keep a site's history under its new ID (and name)."""
        rows = self.db().execute("SELECT event_id, json FROM records WHERE site_id=?", (old,)).fetchall()
        with self.wlock:
            db = self.db()
            db.execute("BEGIN")
            for e, j in rows:
                r = _dec(j)
                r["site_id"] = new
                if name:
                    r["site_name"] = r["camera"] = name
                db.execute("UPDATE records SET site_id=?, name=COALESCE(?, name), json=? WHERE event_id=?",
                           (new, name, _enc(r), e))
            db.execute("UPDATE setup SET site_id=? WHERE site_id=?", (new, old))
            db.execute("COMMIT")
            self._setup_ver = (getattr(self, "_setup_ver", 0) or 0) + 1
            self.version += 1
        return len(rows)

    # ---------------------------------------------------------------- setup periods
    def setup_periods(self):
        """{site_id: [(start, end or None, region_at or None)]}, oldest first."""
        # cached briefly: the report worker process writes nothing but must see changes soon
        key, now = getattr(self, "_setup_ver", 0), time.time()
        c = getattr(self, "_setup_cache", None)
        if not c or c[0] != key or now - c[1] > 2:
            out = {}
            for sid, a, b, r in self.db().execute("SELECT site_id, start, end, region_at FROM setup ORDER BY start"):
                out.setdefault(sid, []).append((a, b, r))
            self._setup_cache = c = (key, now, out)
        return c[2]

    def _setup_write(self, sql, args):
        with self.wlock:
            n = self.db().execute(sql, args).rowcount
            self._setup_ver = (getattr(self, "_setup_ver", 0) or 0) + 1
            self.version += 1
        return n

    def setup_start(self, site_id, t):
        """Into setup from t (nothing if it already is). True if it started now."""
        if any(b is None for _, b, _ in self.setup_periods().get(site_id, [])):
            return False
        self._setup_write("INSERT INTO setup (site_id, start) VALUES (?, ?)", (site_id, t))
        return True

    def setup_end(self, site_id, t):
        """Live from t. True if the site was in setup."""
        return self._setup_write("UPDATE setup SET end=? WHERE site_id=? AND end IS NULL", (t, site_id)) > 0

    def setup_region(self, site_id, t):
        """The region changed at t (only noted while the site is in setup)."""
        return self._setup_write("UPDATE setup SET region_at=? WHERE site_id=? AND end IS NULL", (t, site_id)) > 0

    # ---------------------------------------------------------------- pins
    def set_pins(self, tag, event_ids):
        """Keep exactly these records (and their photos) under this tag; others
        under the tag are released to the usual retention."""
        ids = sorted({str(e) for e in event_ids if e})
        with self.wlock:
            db = self.db()
            db.execute("BEGIN")
            db.execute("DELETE FROM pins WHERE tag=?", (tag,))
            db.executemany("INSERT OR IGNORE INTO pins (event_id, tag) VALUES (?, ?)", [(e, tag) for e in ids])
            db.execute("COMMIT")
        return len(ids)

    def pinned(self):
        return {e for (e,) in self.db().execute("SELECT DISTINCT event_id FROM pins")}

    # ---------------------------------------------------------------- best-photo runs
    def note_run(self, phase, until, now=None):
        """A best-photo run for this phase has looked at the photos up to `until`."""
        with self.wlock:
            self.db().execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
                              (f"run_{phase}", json.dumps({"until": until, "at": now or time.time()})))

    def frames_done_before(self, now=None):
        """Photos taken before this time have been through a day and a night best-photo
        run long enough ago that their full frames are no longer needed (None if not yet)."""
        now = now or time.time()
        done = []
        for ph in ("day", "night"):
            row = self.db().execute("SELECT v FROM meta WHERE k=?", (f"run_{ph}",)).fetchone()
            if not row:
                return None
            v = json.loads(row[0])
            if now - v["at"] < FRAME_GRACE_S:
                return None
            done.append(v["until"])
        return min(done)

    def remove_spent_frames(self, now=None):
        """Remove the full frames that no best-photo run can still pick (kept: pinned)."""
        cut = self.frames_done_before(now)
        if cut is None:
            return 0
        n = 0
        while True:
            eids = [e for (e,) in self.db().execute(
                "SELECT event_id FROM records WHERE type='creative_change' AND t<? AND has_frame=1 "
                "AND event_id NOT IN (SELECT event_id FROM pins) LIMIT 2000", (cut,))]
            if not eids:
                return n
            for e in eids:
                try:
                    os.remove(os.path.join(self.images, f"{e}.frame.jpg"))
                    n += 1
                except FileNotFoundError:
                    pass
            with self.wlock:
                db = self.db()
                db.execute("BEGIN")
                db.executemany("UPDATE records SET has_frame=0 WHERE event_id=?", [(e,) for e in eids])
                db.execute("COMMIT")
                self.version += 1

    # ---------------------------------------------------------------- housekeeping
    def housekeep(self, now=None):
        """Remove photos and rows past their retention. Returns what was removed."""
        now = now or time.time()
        done = {"photos": 0, "records": 0, "heartbeats": 0}
        for typ, days in (("creative_change", IMAGE_DAYS), ("fault", ALERT_IMAGE_DAYS)):
            cut = now - days * 86400
            while True:
                eids = [e for (e,) in self.db().execute(
                    "SELECT event_id FROM records WHERE type=? AND t<? AND (has_roi OR has_frame) "
                    "AND event_id NOT IN (SELECT event_id FROM pins) LIMIT 2000",
                    (typ, cut))]
                if not eids:
                    break
                done["photos"] += self._remove_images(eids)
                with self.wlock:
                    db = self.db()
                    db.execute("BEGIN")
                    db.executemany("UPDATE records SET has_roi=0, has_frame=0 WHERE event_id=?", [(e,) for e in eids])
                    db.execute("COMMIT")
                    self.version += 1
        with self.wlock:
            db = self.db()
            keep = "AND event_id NOT IN (SELECT event_id FROM pins)"
            old = [e for (e,) in db.execute(f"SELECT event_id FROM records WHERE t<? {keep}", (now - RECORD_DAYS * 86400,))]
            self._remove_images(old)
            done["records"] = db.execute(f"DELETE FROM records WHERE t<? {keep}", (now - RECORD_DAYS * 86400,)).rowcount
            done["heartbeats"] = db.execute("DELETE FROM heartbeats WHERE t<?", (now - HEARTBEAT_DAYS * 86400,)).rowcount
            if done["records"]:
                self.version += 1
        done["frames"] = self.remove_spent_frames(now)
        done["orphans"] = self.sweep_orphans(now)
        return done

    def sweep_orphans(self, now):
        """Photo files with no row to say they are wanted (left from before the
        database, or a crash between file and row), once well past retention."""
        cut = now - (max(IMAGE_DAYS, ALERT_IMAGE_DAYS) + 1) * 86400
        n = 0
        try:
            it = os.scandir(self.images)
        except FileNotFoundError:
            return 0
        for ent in it:
            if not ent.name.endswith(".jpg"):
                continue
            try:
                if ent.stat(follow_symlinks=False).st_mtime >= cut:
                    continue
            except OSError:
                continue
            eid = ent.name.split(".", 1)[0]
            if not self.seen(eid):
                try:
                    os.remove(ent.path)
                    n += 1
                except OSError:
                    pass
        return n

    def housekeeper(self, every_s=3600):
        while True:
            try:
                d = self.housekeep()
                if any(d.values()):
                    self.log(f"housekeeping: removed {d['photos']} photo(s) older than {IMAGE_DAYS:g} day(s), "
                             f"{d.get('frames', 0)} full frame(s) the best-photo runs are done with, "
                             f"{d['records']} record(s) older than {RECORD_DAYS:g}, {d['orphans']} stray file(s)")
            except Exception as e:                       # never stop the thread
                self.log(f"housekeeping failed: {e}")
            time.sleep(every_s)

    # ---------------------------------------------------------------- upgrade
    def migrate(self):
        """Import records.jsonl / heartbeats.jsonl from before the database."""
        for name, table in (("records.jsonl", "records"), ("heartbeats.jsonl", "heartbeats")):
            src = os.path.join(self.out, name)
            if not os.path.exists(src):
                continue
            n, batch = 0, []
            db = self.db()

            def flush():
                with self.wlock:
                    db.execute("BEGIN")
                    if table == "records":
                        db.executemany("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                    else:
                        db.executemany("INSERT INTO heartbeats VALUES (?,?)", batch)
                    db.execute("COMMIT")
            with open(src, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if table == "records":
                        row = list(self._row(r))
                        e = r.get("event_id")       # are its photos still on disk?
                        row[8] = int(row[8] and os.path.exists(os.path.join(self.images, f"{e}.roi.jpg")))
                        row[9] = int(row[9] and os.path.exists(os.path.join(self.images, f"{e}.frame.jpg")))
                        batch.append(tuple(row))
                    else:
                        batch.append((_ts(r.get("sent_at")) or 0, line.strip()))
                    n += 1
                    if len(batch) >= 5000:
                        flush()
                        batch = []
            if batch:
                flush()
            os.replace(src, src + ".imported")
            self.version += 1
            self.log(f"store: imported {n} line(s) from {name} into {os.path.basename(self.path)}")
