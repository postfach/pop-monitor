"""Backup and restore files:  python3 tools/test_backup.py"""
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backup as bk  # noqa: E402
import store  # noqa: E402
from test_store import pop  # noqa: E402

GW = {"format": "pop-gateway/state", "version": 1, "gateway_id": "gw1",
      "files": {"cameras.json": json.dumps([{"site_id": "BEACH", "name": "Beach"}, {"site_id": "ANZAC", "name": "Anzac"}]),
                "creatives/BEACH.json": "{}"}}
SY = {"format": "pop-sync/state", "version": 1, "files": {"pop-uploads.json": "{}"}}


class Backup(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.s = store.Store(self.out, log=lambda m: None)
        now = time.time()
        for i in range(5):
            self.s.add(pop(f"e{i}", "BEACH", now - 100 + i))
        with open(os.path.join(self.out, "creative-links.json"), "w") as f:
            json.dump({"BEACH": {"c-0001": "LUMO_1"}}, f)
        os.makedirs(os.path.join(self.out, "images"), exist_ok=True)
        with open(os.path.join(self.out, "images", "e1.roi.jpg"), "wb") as f:
            f.write(b"photo")
        self.dest = os.path.join(tempfile.mkdtemp(), "b.tar.gz")

    def test_round_trip(self):
        m = bk.build(self.out, self.dest, GW, SY)
        self.assertEqual((m["plays"], [s["id"] for s in m["sites"]], m["learned_sites"]), (5, ["BEACH", "ANZAC"], 1))
        with tarfile.open(self.dest) as t:
            names = sorted(t.getnames())
        self.assertEqual(names, ["creatives-sync.json", "gateway.json", "manifest.json", "receiver/creative-links.json",
                                 "receiver/pop.db"])                       # no photos
        manifest, gw, sy = bk.read(self.dest)
        self.assertEqual((gw, sy), (GW, SY))
        # things change after the backup...
        self.s.add(pop("later", "BEACH", time.time()))
        with open(os.path.join(self.out, "creative-links.json"), "w") as f:
            f.write("{}")
        # ...and the restore puts the backup back at the next start
        bk.stage_receiver(self.dest, self.out)
        self.assertFalse(bk.apply_staged(self.out, log=lambda m: None))   # not READY yet: ignored
        bk.stage_receiver(self.dest, self.out)
        bk.mark_ready(self.out)
        self.s.db().close()
        self.assertTrue(bk.apply_staged(self.out, log=lambda m: None))
        s2 = store.Store(self.out, log=lambda m: None)
        self.assertEqual(len(s2.plays("BEACH", 0, time.time() + 10)), 5)
        with open(os.path.join(self.out, "creative-links.json")) as f:
            self.assertEqual(json.load(f), {"BEACH": {"c-0001": "LUMO_1"}})
        self.assertTrue(os.path.exists(os.path.join(self.out, "before-restore", "pop.db")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "restore-staged")))
        # the photos of the records that aren't on this machine
        self.assertEqual(s2.check_photos(), 5)        # e1's crop is here (not its full frame), the rest have neither
        self.assertEqual([p[4] for p in s2.plays("BEACH", 0, time.time() + 10) if p[5]], ["e1"])

    def test_refuses_anything_else(self):
        bad = os.path.join(tempfile.mkdtemp(), "x.tar.gz")
        with tarfile.open(bad, "w:gz") as t:
            for name, data in (("manifest.json", json.dumps({"format": bk.FORMAT, "version": 1}).encode()),
                               ("receiver/pop.db", b"x"), ("../../etc/evil", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, "unexpected item"):
            bk.read(bad)
        with open(bad, "wb") as f:
            f.write(b"not a tar")
        with self.assertRaisesRegex(ValueError, "not a PoP Monitor backup"):
            bk.read(bad)

    def test_damaged_database_not_staged(self):
        bad = os.path.join(tempfile.mkdtemp(), "x.tar.gz")
        with tarfile.open(bad, "w:gz") as t:
            for name, data in (("manifest.json", json.dumps({"format": bk.FORMAT, "version": 1}).encode()),
                               ("receiver/pop.db", b"SQLite format 3\x00" + b"\x00" * 200)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        bk.read(bad)
        with self.assertRaisesRegex(ValueError, "damaged"):
            bk.stage_receiver(bad, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "restore-staged")))


if __name__ == "__main__":
    unittest.main()
