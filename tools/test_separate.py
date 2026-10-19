"""Undoing a wrong merge:  python3 tools/test_separate.py

Two different artworks merged as one ID by mistake: separating the merged ID gives it
its own ID again (and tells the gateway, naming the IDs merged into it before), puts
the plays since the merge whose photos look like it back under it, restores its link,
and the pair is never merged again. Needs numpy and Pillow for the photo part."""
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
import store  # noqa: E402
from test_video_match import artwork, photo, Image  # noqa: E402


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pop(eid, t, cid, roi=True):
    r = {"event_id": eid, "type": "creative_change", "site_id": "S", "local_creative_id": cid,
         "play_started_at": iso(t), "is_blank": False, "video_creative": False, "quality": {"usable": True}}
    if roi:
        r["roi_sha256"] = "x"
    return r


class Separate(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.out, "images"))
        saved = {k: getattr(rx, k) for k in ("STORE", "ARGS", "gateway_call", "save_art_state")}
        self.addCleanup(lambda: [setattr(rx, k, v) for k, v in saved.items()])
        rx.ARGS = types.SimpleNamespace(out=self.out, api_key="k")
        rx.STORE = store.Store(self.out, log=lambda m: None)
        rx.MERGES.update(loaded=True, sites={}, apart={})
        rx.ART_STATE.update(loaded=True, sites={"S": {"c-0001": {}, "c-0002": {}}})
        rx.save_art_state = lambda: None
        rx.ART_CACHE.clear()
        self.calls = []

        def call(path, payload, timeout=10):
            self.calls.append((path, payload))
            return 200, {"queued": True}
        rx.gateway_call = call
        self.now = time.time()
        t = self.now - 86400
        # c-0003 had been merged into c-0002 (right), then c-0002 into c-0001 (wrong)
        rx.MERGES["sites"]["S"] = {
            "c-0003": {"into": "c-0002", "at": t - 3600, "pushed": t - 3500, "why": "PoP images look alike"},
            "c-0002": {"into": "c-0001", "at": t, "pushed": t + 60, "why": "same booked artwork: a.png",
                       "link": "LUMO_2", "link_moved": True}}
        rx.save_link("S", "c-0001", "LUMO_2")       # moved there by the merge

    def shots(self):
        """Photos: c-0001's artwork and c-0002's before the merge, then plays reported as
        c-0001 since it, some of each artwork."""
        a, b = artwork(11), artwork(12)
        t0 = self.now - 86400
        k = 0
        plan = [("c-0001", a, t0 - 7200 + i * 300) for i in range(4)] + \
               [("c-0002", b, t0 - 7000 + i * 300) for i in range(4)] + \
               [("c-0001", a if i % 2 else b, t0 + 600 + i * 300) for i in range(8)]
        truth = {}
        for cid, art, t in plan:
            eid = f"e{k}"
            photo(art, k).save(os.path.join(self.out, "images", f"{eid}.roi.jpg"))
            rx.STORE.add(pop(eid, t, cid))
            truth[eid] = art is b
            k += 1
        return truth

    @unittest.skipIf(Image is None, "needs numpy and Pillow")
    def test_separate_gives_plays_back(self):
        truth = self.shots()
        code, res = rx.separate("S", "c-0002")
        self.assertEqual(code, 200, res)
        self.assertEqual(self.calls, [("/api/cameras/S/split-creative", {"from": "c-0002", "into": "c-0001", "with": ["c-0003"]})])
        self.assertEqual(res["plays_moved"], 4)
        after = {p[4]: p[1] for p in rx.STORE.plays("S", 0, self.now)}
        for eid, is_b in truth.items():
            if int(eid[1:]) >= 8:                       # the plays since the merge
                self.assertEqual(after[eid], "c-0002" if is_b else "c-0001", eid)
        self.assertNotIn("c-0002", rx.merges()["S"])
        self.assertEqual(rx.canon("S", "c-0003"), "c-0002")        # the right merge stays
        self.assertTrue(rx.kept_apart("S", "c-0001", "c-0002"))
        links = rx.load_links()["S"]
        self.assertEqual((links.get("c-0001"), links.get("c-0002")), (None, "LUMO_2"))
        self.assertEqual(rx.art_state()["S"], {})                # both matched again
        # the record keeps what it was reported as
        r = rx.STORE.db().execute("SELECT json FROM records WHERE creative='c-0002' AND event_id='e8'").fetchone()
        self.assertEqual(store._dec(r[0])["merged_as"], "c-0001")

    def test_not_merged_and_gateway_refusing(self):
        self.assertEqual(rx.separate("S", "c-0009")[0], 404)
        rx.gateway_call = lambda path, payload, timeout=10: (502, {"error": "cannot reach the gateway"})
        code, res = rx.separate("S", "c-0002")
        self.assertEqual(code, 502)
        self.assertIn("c-0002", rx.merges()["S"])                # nothing changed

    def test_not_merged_again(self):
        rx.separate("S", "c-0002")
        rx.ART_OK, ok = True, rx.ART_OK
        self.addCleanup(lambda: setattr(rx, "ART_OK", ok))
        rep = {"sites": [{"site_id": "S", "groups": [{"key": "a|LUMO_1|a.png", "art": {"name": "a.png"},
                                                      "members": [{"creative_id": "c-0001", "plays": 9},
                                                                  {"creative_id": "c-0002", "plays": 9}]}]}]}
        self.assertEqual(rx.plan_merges(rep), 0)
        self.assertNotIn("c-0002", rx.merges()["S"])

    def test_merged_info(self):
        rx.STORE.add(pop("m1", self.now - 90000, "c-0002"))
        info = rx.merged_info("S", "c-0002")
        self.assertEqual((info["canon"], info["plays"], info["image"]), ("c-0001", 1, "m1"))


class OtherProcess(unittest.TestCase):
    """The report worker keeps its own copy of the merges and artwork matches: a change
    made by the web process (a merge separated) must reach it."""

    def test_worker_sees_changes(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(rx, k) for k in ("ARGS",)}
        self.addCleanup(lambda: [setattr(rx, k, v) for k, v in saved.items()])
        rx.ARGS = types.SimpleNamespace(out=out, api_key="k")
        rx.MERGES.clear()
        rx.MERGES.update(loaded=False, sites={}, apart={}, mtime=None)
        rx.merges().setdefault("S", {})["c-0002"] = {"into": "c-0001", "at": 1, "pushed": None, "why": "x"}
        rx.save_merges()
        self.assertEqual(rx.canon("S", "c-0002"), "c-0001")
        # the other process separates it
        time.sleep(0.05)
        with open(rx._merges_path(), "w") as f:
            f.write('{"sites": {"S": {}}, "apart": {"S": [["c-0001", "c-0002"]]}}')
        os.utime(rx._merges_path(), (time.time() + 5, time.time() + 5))
        rx.MERGES["checked"] = 0
        self.assertEqual(rx.canon("S", "c-0002"), "c-0002")
        self.assertTrue(rx.kept_apart("S", "c-0001", "c-0002"))
        # artwork matches the same way
        rx.ART_STATE.clear()
        rx.ART_STATE.update(loaded=False, dirty=False, sites={})
        rx.art_state()["S"] = {"c-0001": {"status": "matched"}}
        rx.ART_STATE["dirty"] = True
        rx.save_art_state()
        with open(rx._art_state_path(), "w") as f:
            f.write('{"sites": {"S": {}}}')
        os.utime(rx._art_state_path(), (time.time() + 5, time.time() + 5))
        rx.ART_STATE["checked"] = 0
        self.assertEqual(rx.art_state()["S"], {})


if __name__ == "__main__":
    unittest.main()
