"""Setting a new site up:  python3 tools/test_setup.py

While a site is in setup its plays aren't counted (the region page still sees them),
the go-live checks pass only once the region is placed, left alone and matching booked
artwork, and going live resets the gateway's creatives before leaving setup."""
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

PLACED = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.7], [0.2, 0.7]]


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pop(eid, site, t, cid):
    return {"event_id": eid, "type": "creative_change", "site_id": site, "local_creative_id": cid,
            "play_started_at": iso(t), "is_blank": False, "video_creative": False, "quality": {"usable": True}}


class Setup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        saved = {k: getattr(rx, k) for k in ("STORE", "ARGS", "artwork_matches", "screen_creatives",
                                              "gateway_cameras", "gateway_call", "save_art_state")}
        self.addCleanup(lambda: [setattr(rx, k, v) for k, v in saved.items()])
        rx.ARGS = types.SimpleNamespace(out=tmp, api_key="k")
        rx.STORE = store.Store(tmp, log=lambda m: None)
        rx.MERGES.update(loaded=True, sites={})
        rx.save_art_state = lambda: None
        self.now = time.time()
        self.cam = {"id": "S", "name": "New site", "roi": PLACED, "vault_screen": "Beach", "setup": True}
        rx.screen_creatives = lambda screen: [{"campaign_id": "LUMO_1", "file": "a.png"},
                                              {"campaign_id": "LUMO_2", "file": "b.png"}]
        self.matches = {"c-0001": {"status": "matched", "campaign_id": "LUMO_1"},
                        "c-0002": {"status": "matched", "campaign_id": "LUMO_2"}}
        rx.artwork_matches = lambda cam, t0, t1, setup=False: self.matches
        self.calls = []

        def call(path, payload, timeout=10):
            self.calls.append((path.rsplit("/", 1)[1], payload))
            return 200, {}
        rx.gateway_call = call
        rx.gateway_cameras = lambda: [self.cam]

    def plays(self, t0, n, every=60, cids=("c-0001", "c-0002")):
        for i in range(n):
            rx.STORE.add(pop(f"e{t0}-{i}", "S", t0 + i * every, cids[i % len(cids)]))

    def test_setup_plays_not_counted(self):
        t = self.now
        rx.STORE.add(pop("a", "S", t - 300, "c-0001"))
        rx.STORE.add(pop("b", "S", t - 100, "c-0001"))
        rx.STORE.setup_start("S", t - 400)
        rx.STORE.setup_end("S", t - 200)
        self.assertEqual([p[4] for p in rx.site_plays("S", t - 1000, t)], ["b"])
        self.assertEqual([p[4] for p in rx.site_plays("S", t - 1000, t, setup=True)], ["a", "b"])
        rx.STORE.setup_start("S", t - 50)                 # back into setup: later plays aren't counted
        rx.STORE.add(pop("c", "S", t - 10, "c-0001"))
        self.assertEqual([p[4] for p in rx.site_plays("S", t - 1000, t + 1)], ["b"])

    def test_checks(self):
        t = self.now
        self.assertFalse(rx.setup_status(self.cam)["in_setup"])
        rx.STORE.setup_start("S", t - 3600)
        st = rx.setup_status(dict(self.cam, roi=rx.FULL_FRAME), now=t)
        self.assertEqual({c["key"]: c["ok"] for c in st["checks"]},
                         {"region": False, "settled": True, "plays": False, "artwork": False})
        rx.STORE.setup_region("S", t - 600)              # moved 10 min ago: plays before that don't count
        self.plays(t - 3000, 30)
        self.plays(t - 500, 5)
        st = rx.setup_status(self.cam, now=t)
        ok = {c["key"]: c["ok"] for c in st["checks"]}
        self.assertEqual(ok, {"region": True, "settled": False, "plays": False, "artwork": True})
        self.assertFalse(st["ready"])
        self.plays(t + 60, 25)                           # an hour later, with plays since the change
        st = rx.setup_status(self.cam, now=t + 3600)
        self.assertTrue(st["ready"], st)
        self.matches = {"c-0001": {"status": "matched", "campaign_id": "LUMO_1"}}   # half the plays: not enough
        st = rx.setup_status(self.cam, now=t + 3600)
        art = next(c for c in st["checks"] if c["key"] == "artwork")
        self.assertFalse(art["ok"])
        self.assertIn("53% of plays (needs 60%), 1 campaign(s) recognised (needs 2)", art["detail"])
        rx.screen_creatives = lambda screen: []          # nothing booked to compare with: by eye
        st = rx.setup_status(self.cam, now=t + 3600)
        self.assertIn("go live anyway", next(c for c in st["checks"] if c["key"] == "artwork")["detail"])

    def test_go_live_and_back(self):
        t = self.now
        rx.STORE.setup_start("S", t - 60)
        rx.art_state()["S"] = {"c-0001": {}}
        code, r = rx.set_live("S", True)
        self.assertEqual(code, 409)                      # checks not passed
        self.assertIn("not ready", r["error"])
        self.assertEqual(self.calls, [])
        code, r = rx.set_live("S", True, force=True)
        self.assertEqual(code, 200, r)
        # creatives reset while still in setup (its cleared faults stay out of Teams), then live
        self.assertEqual(self.calls, [("reset-creatives", {"restart_numbering": False}), ("setup", {"setup": False})])
        self.assertFalse(rx.in_setup("S"))
        self.assertNotIn("S", rx.art_state())
        self.assertEqual(rx.set_live("S", True)[0], 409)  # already live
        self.calls.clear()
        code, r = rx.set_live("S", False)
        self.assertEqual((code, self.calls), (200, [("setup", {"setup": True})]))
        self.assertTrue(rx.in_setup("S") and r["in_setup"])

    def test_gateway_refuses(self):
        rx.STORE.setup_start("S", self.now - 60)
        rx.gateway_call = lambda path, payload, timeout=10: (502, {"error": "cannot reach the gateway"})
        code, r = rx.set_live("S", True, force=True)
        self.assertEqual(code, 502)
        self.assertTrue(rx.in_setup("S"))                # still in setup: nothing half done

    def test_reconcile(self):
        rx.setup_reconcile([self.cam, {"id": "OLD", "setup": False}])
        self.assertTrue(rx.in_setup("S"))
        self.assertFalse(rx.in_setup("OLD"))


if __name__ == "__main__":
    unittest.main()
