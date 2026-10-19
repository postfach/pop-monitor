"""A site is added: its Doohly bookings are read first (straight away), then only its own
screen's artwork is looked up; the other screens' artwork stays as it was."""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import creatives_sync as cs  # noqa: E402
import doohly  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")
except Exception:  # pragma: no cover
    NZ = None


def booking(bid, cid, day, name="A.png"):
    return {"id": bid, "name": f"{cid} booking", "status": "booked", "purchaseType": "Sold",
            "updatedAt": "2026-10-01T00:00:00.000Z", "playsPerLoop": 3, "loopsPerPlay": 1,
            "campaign": {"id": "c-" + cid, "name": f"{cid} - Campaign", "externalId": cid},
            "schedule": {"startDate": day, "endDate": day, "startTime": "00:00:00", "endTime": "23:59:00",
                         "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            "assignedCreatives": [{"order": 0, "weight": 1, "creative": {
                "id": name, "name": name, "type": "image", "file": {"width": 576, "height": 396}}}]}


class Players:
    """Doohly with two players; bookings per player, and which players' bookings were read."""

    def __init__(self, today):
        self.calls, self.read = 0, []
        self.by_frame = {"f-d1": [booking("b1", "LUMO_10001", today)], "f-d2": [booking("b2", "LUMO_10002", today)]}

    def pages(self, path, **params):
        self.calls += 1
        if path == "/v2/devices":
            yield from [{"id": "d1", "name": "LUMO-Beach"}, {"id": "d2", "name": "LUMO-Albany Expressway IN"}]
        elif path == "/v3/bookings":
            self.read.append(params["frameId"])
            for b in self.by_frame.get(params["frameId"], []):
                yield {k: v for k, v in b.items() if k != "assignedCreatives"}
        else:
            raise AssertionError(path)

    def get(self, path, **params):
        self.calls += 1
        if path.startswith("/v2/devices/"):
            d = path.rsplit("/", 1)[1]
            return {"id": d, "name": d, "isConnected": True, "frames": [
                {"id": f"f-{d}", "name": "Main", "bounds": {"width": 576, "height": 396},
                 "loopPolicy": {"slotCount": 20, "slotDuration": 8}}]}
        if path.startswith("/v3/bookings/"):
            bid = path.rsplit("/", 1)[1]
            return next(b for bs in self.by_frame.values() for b in bs if b["id"] == bid)
        if path.startswith("/v3/campaigns/"):
            return {"advertiser": {"name": "X"}}
        raise AssertionError(path)


@unittest.skipIf(NZ is None, "no tz database")
class DoohlyFirst(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.sites = [{"site_id": "BEACH", "name": "Beach", "vault_screen": "Beach"}]

        def atomic(p, o):
            with open(p, "w") as f:
                json.dump(o, f)

        def read(p, d=None):
            try:
                with open(p) as f:
                    return json.load(f)
            except OSError:
                return d
        self.w = doohly.Watcher(tmp, tmp, lambda: {"sites": self.sites}, NZ, lambda *a: None,
                                lambda *a, **k: None, atomic, read, lambda *a: None)
        self.w.c = self.p = Players(datetime.now(NZ).date().isoformat())

    def test_new_site_read_at_once_and_only_its_screen_redone(self):
        self.assertEqual(self.w.run(with_bookings=True), ["Beach"])           # first ever read
        self.assertEqual(self.w.run(with_bookings=False), [])
        self.sites.append({"site_id": "ALB", "name": "Albany", "vault_screen": "Albany Expressway In"})
        self.p.read.clear()
        # a device check, not a bookings read: the new site's bookings are read all the same
        self.assertEqual(self.w.run(with_bookings=False), ["Albany Expressway In"])
        self.assertEqual(self.p.read, ["f-d2"])                               # its player only
        self.assertEqual([b["campaign_id"] for b in self.w.doc["sites"]["ALB"]["bookings"]], ["LUMO_10002"])
        self.assertEqual(self.w.run(with_bookings=False), [])
        self.sites.append({"site_id": "NOPE", "name": "Nowhere", "vault_screen": "Nowhere"})
        self.assertEqual(self.w.run(with_bookings=False), ["Nowhere"])        # not in Doohly: not held up
        self.p.by_frame["f-d1"].append(booking("b3", "LUMO_10003", datetime.now(NZ).date().isoformat()))
        self.assertEqual(self.w.run(with_bookings=True), ["Beach"])           # only the screen that changed

    def test_screen_changed_to_another_player(self):
        self.w.run(with_bookings=True)
        self.sites[0]["vault_screen"] = "Albany Expressway In"                # now points at the other player
        self.p.read.clear()
        self.assertEqual(self.w.run(with_bookings=False), ["Albany Expressway In"])
        self.assertEqual(self.p.read, ["f-d2"])
        self.assertEqual([b["campaign_id"] for b in self.w.doc["sites"]["BEACH"]["bookings"]], ["LUMO_10002"])

    def test_wakes_when_asked(self):
        self.w.wake.set()                                                     # the loop's wait returns at once
        self.assertTrue(self.w.wake.wait(0))


class NewScreens(unittest.TestCase):
    def setUp(self):
        saved = (cs.DOOHLY, cs.STATE, doohly.token)
        self.addCleanup(lambda: (setattr(cs, "DOOHLY", saved[0]), setattr(cs, "STATE", saved[1]),
                                 setattr(doohly, "token", saved[2])))
        cs.STATE = tempfile.mkdtemp()
        self.s = cs.Syncer()
        self.s.synced = {"beach"}

    def test_waits_for_doohly_then_goes_anyway(self):
        cs.DOOHLY = type("W", (), {"wake": threading.Event()})()
        doohly.token = lambda: "t"
        inp = {"screens": ["Beach", "Greenlane"]}
        self.assertEqual(self.s.new_screens(inp, now=1000), ([], False))      # waiting for Doohly
        self.assertTrue(cs.DOOHLY.wake.is_set())
        self.assertEqual(self.s.new_screens(inp, now=1000 + cs.DOOHLY_WAIT - 1), ([], False))
        self.assertEqual(self.s.new_screens(inp, now=1000 + cs.DOOHLY_WAIT), (["Greenlane"], False))
        self.s.request("Doohly bookings read: Greenlane", ["Greenlane"])     # what the reader does
        self.assertEqual((self.s.pending, self.s.pending_full, self.s.pending_screens), (True, False, {"Greenlane"}))
        self.assertEqual(self.s.new_screens({"screens": ["Greenlane"]}, now=2000)[1], True)   # Beach removed

    def test_no_doohly_no_wait(self):
        cs.DOOHLY = None
        self.assertEqual(self.s.new_screens({"screens": ["Beach", "Greenlane"]}), (["Greenlane"], False))


class PartialRun(unittest.TestCase):
    """Greenlane is added; Beach was synced before. Only Greenlane's campaign is looked up."""

    def setUp(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(cs, k) for k in ("OUT", "STATE", "Graph", "Library", "fetch_inputs")}
        self.addCleanup(lambda: [setattr(cs, k, v) for k, v in saved.items()])
        cs.OUT, cs.STATE = out, os.path.join(out, ".sync")
        os.makedirs(cs.STATE)
        today = datetime.now(cs.LOCAL).date()
        mon = today - timedelta(days=today.weekday())
        with open(os.path.join(cs.STATE, "screen-specs.json"), "w") as f:
            json.dump({"greenlane": [1188, 288, "Greenlane"], "beach": [576, 396, "Beach"]}, f)
        sites = {}
        for sid, screen, cid in (("B", "Beach", "LUMO_10001"), ("G", "Greenlane", "LUMO_10002")):
            sites[sid] = {"screen": screen, "bookings": [
                {"campaign_id": cid, "campaign_name": f"{cid} - X", "start": mon.isoformat(),
                 "end": (mon + timedelta(days=6)).isoformat(), "start_time": "00:00", "end_time": "23:59",
                 "creatives": [{"name": f"{screen}.png", "type": "image"}]}]}
        with open(os.path.join(out, "doohly.json"), "w") as f:
            json.dump({"sites": sites}, f)
        os.makedirs(os.path.join(out, "beach", "images", "LUMO_10001"))
        with open(os.path.join(out, "beach", "images", "LUMO_10001", "Beach.png"), "wb") as f:
            f.write(b"old")
        self.beach = {"campaign_id": "LUMO_10001", "screen": "Beach", "name": "Beach.png", "kind": "image",
                      "file": "beach/images/LUMO_10001/Beach.png", "via": "doohly"}
        with open(os.path.join(out, "vault-creatives.json"), "w") as f:
            json.dump({"creatives": [self.beach], "missing": [], "screens": [{"name": "Beach", "width": 576, "height": 396}]}, f)
        with open(os.path.join(cs.STATE, "files.json"), "w") as f:
            json.dump({self.beach["file"]: "x"}, f)
        self.looked, self.connected = [], []
        test = self

        class G:
            calls = 0

        class Lib:
            def __init__(self, _g):
                self.drive = "d0"

            def connect(self):
                test.connected.append(1)
                return self

            def index_campaigns(self, today):
                return {"10001": [{"id": "F1", "_path": "2026/10/LUMO_10001"}],
                        "10002": [{"id": "F2", "_path": "2026/10/LUMO_10002"}]}

            def search_campaign(self, cid):
                return []

            def tree(self, fo):
                test.looked.append(fo["id"])
                name = "Beach.png" if fo["id"] == "F1" else "Greenlane.png"
                return [{"id": "i-" + fo["id"], "name": name, "etag": "e", "modified": "2026-10-01", "width": None,
                         "height": None, "rel": name, "folder_rel": "", "folder_url": "u", "drive": "d0"}]

            def download(self, iid, drive=None):
                return b"png"
        cs.Graph, cs.Library = G, Lib
        self.screens = ["Beach", "Greenlane"]
        cs.fetch_inputs = lambda: {"bookings": {"proposals": []}, "screens": self.screens}

    def manifest(self):
        with open(os.path.join(cs.OUT, "vault-creatives.json")) as f:
            return json.load(f)

    def test_only_the_new_screen(self):
        s = cs.Syncer()
        s.synced = {"beach"}
        res = s._run(only=["Greenlane"])
        self.assertEqual(self.looked, ["F2"])                                 # Beach's campaign not looked up
        man = self.manifest()
        self.assertEqual(sorted((c["screen"], c["name"]) for c in man["creatives"]),
                         [("Beach", "Beach.png"), ("Greenlane", "Greenlane.png")])
        self.assertIn(self.beach, man["creatives"])                          # exactly as it was
        self.assertTrue(os.path.exists(os.path.join(cs.OUT, self.beach["file"])))   # its file is kept
        self.assertEqual([x["name"] for x in man["screens"]], ["Beach", "Greenlane"])
        self.assertEqual((res["only"], res["campaigns"], res["files"], res["new_files"]), (["Greenlane"], 1, 2, 1))
        self.assertEqual(s.synced, {"beach", "greenlane"})

    def test_screen_removed_needs_no_sharepoint(self):
        s = cs.Syncer()
        s.synced = {"beach", "greenlane"}
        self.screens = ["Greenlane"]
        s._run(only=[])
        self.assertEqual((self.looked, self.connected), ([], []))
        self.assertEqual(self.manifest()["creatives"], [])
        self.assertFalse(os.path.exists(os.path.join(cs.OUT, self.beach["file"])))  # its artwork is removed
        self.assertEqual(s.synced, {"greenlane"})                           # still monitored

    def test_full_run_still_does_everything(self):
        s = cs.Syncer()
        s._run()
        self.assertEqual(sorted(self.looked), ["F1", "F2"])


if __name__ == "__main__":
    unittest.main()
