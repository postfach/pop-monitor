"""Tests for the receiver's store:  python3 tools/test_store.py"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pop(eid, site, t, cid="c-0001", photos=True):
    r = {"event_id": eid, "type": "creative_change", "site_id": site, "site_name": site.lower(),
         "local_creative_id": cid, "play_started_at": iso(t), "is_blank": False, "video_creative": False,
         "quality": {"usable": True}}
    if photos:
        r["roi_sha256"] = r["frame_sha256"] = "x" * 64
    return r


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = store.Store(self.dir, log=lambda m: None)
        self.now = time.time()

    def photo(self, eid):
        for k in ("roi", "frame"):
            with open(os.path.join(self.dir, "images", f"{eid}.{k}.jpg"), "wb") as f:
                f.write(b"jpeg")

    def test_add_is_idempotent_and_plays_read_back(self):
        self.assertTrue(self.s.add(pop("a", "S1", self.now - 100)))
        self.assertFalse(self.s.add(pop("a", "S1", self.now - 100)))         # a retry
        self.s.add(pop("b", "S1", self.now - 50, "c-0002", photos=False))
        self.s.add(pop("c", "S2", self.now - 10))
        ps = self.s.plays("S1", self.now - 1000, self.now)
        self.assertEqual([(p[1], p[4], p[5]) for p in ps], [("c-0001", "a", True), ("c-0002", "b", False)])
        self.assertEqual([p[4] for p in self.s.last_plays("S1", 1)], ["b"])
        self.assertTrue(self.s.seen("c") and not self.s.seen("zz"))
        self.assertEqual({e["site_id"]: e["pops"] for e in self.s.sites()}, {"S1": 2, "S2": 1})
        self.assertEqual(self.s.recent(5, "S1")[-1]["event_id"], "b")

    def test_housekeeping_removes_old_photos_but_keeps_the_rows(self):
        old, new = self.now - 4 * 86400, self.now - 3600
        for eid, t in (("old", old), ("new", new)):
            self.s.add(pop(eid, "S1", t))
            self.photo(eid)
        alert = {"event_id": "al", "type": "fault", "site_id": "S1", "fault": "blank", "at": iso(old),
                 "frame_sha256": "x" * 64}
        self.s.add(alert)
        self.photo("al")
        done = self.s.housekeep(self.now)
        self.assertEqual(done["photos"], 2)
        imgs = sorted(os.listdir(os.path.join(self.dir, "images")))
        self.assertEqual(imgs, ["al.frame.jpg", "al.roi.jpg", "new.frame.jpg", "new.roi.jpg"])   # alerts keep 30 days
        ps = {p[4]: p[5] for p in self.s.plays("S1", 0, self.now)}
        self.assertEqual(ps, {"old": False, "new": True})        # the play is still counted, without a photo
        self.assertTrue(next(r for r in self.s.recent(10, "S1") if r["event_id"] == "old").get("_images_removed"))

    def test_rows_past_retention_and_orphans(self):
        self.s.add(pop("ancient", "S1", self.now - 200 * 86400))
        stray = os.path.join(self.dir, "images", "nobody.roi.jpg")
        with open(stray, "wb") as f:
            f.write(b"x")
        os.utime(stray, (self.now - 60 * 86400, self.now - 60 * 86400))
        done = self.s.housekeep(self.now)
        self.assertEqual((done["records"], done["orphans"]), (1, 1))
        self.assertFalse(self.s.seen("ancient") or os.path.exists(stray))

    def test_rename_and_delete_site(self):
        self.s.add(pop("a", "OLD", self.now - 10))
        self.photo("a")
        self.assertEqual(self.s.rename_site("OLD", "NEW", "New name"), 1)
        r = self.s.recent(1, "NEW")[0]
        self.assertEqual((r["site_id"], r["site_name"]), ("NEW", "New name"))
        self.assertEqual(self.s.delete_site("NEW"), 1)
        self.assertEqual(os.listdir(os.path.join(self.dir, "images")), [])

    def test_migrates_jsonl(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "images"))
        with open(os.path.join(d, "records.jsonl"), "w") as f:
            f.write(json.dumps(pop("m1", "S1", self.now - 30)) + "\n" + "not json\n" + json.dumps(pop("m2", "S1", self.now - 20)) + "\n")
        with open(os.path.join(d, "images", "m2.roi.jpg"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(d, "heartbeats.jsonl"), "w") as f:
            f.write(json.dumps({"gateway_id": "gw", "sent_at": iso(self.now)}) + "\n")
        s = store.Store(d, log=lambda m: None)
        self.assertEqual([(p[4], p[5]) for p in s.plays("S1", 0, self.now)], [("m1", False), ("m2", True)])
        self.assertEqual(s.last_heartbeats(1)[0]["gateway_id"], "gw")
        self.assertTrue(os.path.exists(os.path.join(d, "records.jsonl.imported")))
        store.Store(d, log=lambda m: None)                       # starting again imports nothing twice
        self.assertEqual(len(s.plays("S1", 0, self.now)), 2)


if __name__ == "__main__":
    unittest.main()
