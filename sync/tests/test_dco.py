"""Dynamic creatives (DCO): Doohly books an HTML zip, the renders of every variant are in
LUMO Labs (a folder per campaign, a subfolder per screen)."""
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import creatives_sync as cs  # noqa: E402


def folder(name, *kids, modified="2026-09-29T00:00:00Z"):
    return {"name": name, "folder": {}, "kids": list(kids), "lastModifiedDateTime": modified}


def file(name, w=None, h=None, video=False):
    f = {"name": name, "file": {}, "eTag": "e1", "lastModifiedDateTime": "2026-09-29T00:00:00Z"}
    if w:
        f["video" if video else "image"] = {"width": w, "height": h}
    return f


def renders(code, w, h, ext="png", video=False):
    return [file(f"{code}_{v}.{ext}", w, h, video) for v in
            ("HAZE_0615-1813_Haze", "SUN-HL_1214-1510_Sunny", "NCL_1843-0545_Clear", "TW-DUSK-OC_1813-1843_Overcast")]


TREE = folder("6. LUMO Labs",
              folder("2.2025", folder("VTNZ LUMO_11868", file("a.png", 10, 10))),
              folder("3.2026",
                     folder("Q2", folder("Disclosure Day")),
                     folder("Q3",
                            folder("Bentley - Weather Triggers",
                                   file("Copy of Bentley - LUMO_19409 - Bentley Sept-Nov 2026 - Material Instructions.xlsx"),
                                   folder("Supplied Files",
                                          folder("LUMO-Greenlane", *renders("GRE", 1188, 288)),
                                          folder("LUMO-Albany Expressway In", *renders("ABI", 1188, 288))),
                                   folder("LUMO_Bentley_export_video",
                                          folder("Anzac", *renders("ANZ", 1764, 468, "mp4", True)),
                                          folder("Ports", *renders("POR", 1188, 288, "mp4", True))),
                                   folder("LUMO-Cranford", *renders("CRA", 864, 432),
                                          *renders("CRA", 864, 432, "mp4", True))))))


class FakeGraph:
    """Serves TREE: children listings by item id."""

    def __init__(self):
        self.items, self.calls = {}, 0
        self._add(TREE, "root")

    def _add(self, node, iid):
        node = dict(node, id=iid, webUrl=f"https://sp/{iid}")
        self.items[iid] = node
        node["kids"] = [self._add(k, f"{iid}/{i}") for i, k in enumerate(node.get("kids") or [])]
        return node

    def pages(self, url, **kw):
        self.calls += 1
        iid = url.split("/items/", 1)[1].rsplit("/children", 1)[0]
        return [{k: v for k, v in c.items() if k != "kids"} for c in self.items[iid]["kids"]]


class Dco(unittest.TestCase):
    def setUp(self):
        self.saved = cs.STATE
        cs.STATE = tempfile.mkdtemp()
        self.addCleanup(lambda: setattr(cs, "STATE", self.saved))
        self.g = FakeGraph()
        self.lib = self.new()

    def new(self):
        lib = cs.DcoLibrary.__new__(cs.DcoLibrary)
        cs.Library.__init__(lib, self.g, "IT", "6. LUMO Labs", "d1")
        lib._index, lib._index_from_disk = None, False
        lib.root = {k: v for k, v in self.g.items["root"].items() if k != "kids"}
        return lib

    def test_campaign_by_its_mi_then_by_advertiser(self):
        fo = self.lib.campaign_folder("19409", "", date(2026, 10, 19))
        self.assertEqual(fo["_path"], "6. LUMO Labs/3.2026/Q3/Bentley - Weather Triggers")
        fo = self.lib.campaign_folder("19999", "Bentley", date(2026, 10, 19))      # not by number: by name
        self.assertEqual(fo["name"], "Bentley - Weather Triggers")
        self.assertIsNone(self.lib.campaign_folder("19998", "Nobody", date(2026, 10, 19)))
        self.assertNotIn("11868", self.lib.index()[0])                              # 2025 isn't walked

    def test_index_kept_and_walked_again_for_a_new_campaign(self):
        self.lib.campaign_folder("19409", "", date(2026, 10, 19))
        n = self.g.calls
        lib2 = self.new()
        self.assertEqual(lib2.campaign_folder("19409", "", date(2026, 10, 19))["name"], "Bentley - Weather Triggers")
        self.assertEqual(self.g.calls, n)                                           # from the saved index
        lib2.campaign_folder("19555", "", date(2026, 10, 19))
        self.assertGreater(self.g.calls, n)                                         # unknown: walked again, once
        m = self.g.calls
        lib2.campaign_folder("19556", "", date(2026, 10, 19))
        self.assertEqual(self.g.calls, m)

    def test_variants_per_screen(self):
        fo = self.lib.campaign_folder("19409", "", date(2026, 10, 19))
        screens = ["Greenlane", "Albany Expressway In", "Albany Expressway Out", "Anzac", "Ports", "Cranford", "Kingsland In"]
        names = lambda scr, size: [f["name"] for f in self.lib.variants(fo, scr, size, screens)]
        self.assertEqual(len(names("Greenlane", (1188, 288))), 4)
        self.assertTrue(all(n.startswith("GRE_") for n in names("Greenlane", (1188, 288))))
        self.assertTrue(all(n.startswith("ABI_") for n in names("Albany Expressway In", (1188, 288))))
        self.assertEqual(names("Albany Expressway Out", (1188, 288)), [])         # its own folder isn't there
        self.assertEqual(names("Kingsland In", (1188, 288)), [])                   # same size, but in other screens' folders
        self.assertTrue(all(n.endswith(".mp4") for n in names("Anzac", (1764, 468))))
        cra = names("Cranford", (864, 432))
        self.assertEqual(len(cra), 4)
        self.assertTrue(all(n.endswith(".png") for n in cra))                     # the image over the same variant's video

    def test_labels(self):
        self.assertEqual(cs.variant_label("GRE_SUN-HL_1214-1510_Sunny.png"), "Sunny 12:14-15:10 (SUN-HL)")
        self.assertEqual(cs.variant_label("STU_UNS_0615-1813_PatchyRain.png"), "Patchy Rain 06:15-18:13 (UNS)")
        self.assertEqual(cs.variant_label("Bentley_Final.png"), "Bentley_Final")


class SyncRun(unittest.TestCase):
    """A whole sync run: Bentley is booked on Greenlane in Doohly as an HTML zip and has no
    Creative+MIs folder; its renders become the screen's artwork and nothing is missing."""

    def setUp(self):
        import json
        from datetime import datetime
        out = tempfile.mkdtemp()
        saved = {k: getattr(cs, k) for k in ("OUT", "STATE", "Graph", "Library", "DcoLibrary", "fetch_inputs")}
        self.addCleanup(lambda: [setattr(cs, k, v) for k, v in saved.items()])
        cs.OUT, cs.STATE = out, os.path.join(out, ".sync")
        os.makedirs(cs.STATE)
        today = datetime.now(cs.LOCAL).date()
        mon = today - __import__("datetime").timedelta(days=today.weekday())
        with open(os.path.join(cs.STATE, "screen-specs.json"), "w") as f:
            json.dump({"greenlane": [1188, 288, "Greenlane"], "beach": [576, 396, "Beach"]}, f)
        with open(os.path.join(out, "doohly.json"), "w") as f:
            json.dump({"sites": {"G": {"screen": "Greenlane", "bookings": [
                {"campaign_id": "LUMO_19409", "campaign_name": "LUMO_19409 - Bentley Sept-Nov 2026", "advertiser": "Bentley",
                 "start": mon.isoformat(), "end": (mon + __import__("datetime").timedelta(days=6)).isoformat(),
                 "start_time": "00:00", "end_time": "23:59", "creatives": [{"name": "bentley-weather-lumo-greenlane-1188x288-v1.zip", "type": "html"}]}]},
                "B": {"screen": "Beach", "bookings": [
                {"campaign_id": "LUMO_19410", "campaign_name": "LUMO_19410 - Other DCO", "advertiser": "Nobody",
                 "start": mon.isoformat(), "end": mon.isoformat(),
                 "start_time": "00:00", "end_time": "23:59", "creatives": [{"name": "other.zip", "type": "html"}]}]}}}, f)
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16             # the bytes aren't looked at here
        g, real_library = FakeGraph(), cs.Library

        class G:
            calls = 0

        class Lib:
            def __init__(self, _g):
                self.drive = "d0"

            def connect(self):
                return self

            def index_campaigns(self, today):
                return {}

            def search_campaign(self, cid):
                return []

            def month_folder(self, today):
                return {"id": "M10", "_path": "2026/10. October", "webUrl": "u"}

            def download(self, iid):
                return png

        class Dco(cs.DcoLibrary):
            def __init__(self, _g):
                real_library.__init__(self, g, "IT", "6. LUMO Labs", "d1")
                self._index, self._index_from_disk = None, False

            def connect(self):
                self.root = {k: v for k, v in g.items["root"].items() if k != "kids"}
                return self

            def download(self, iid):
                return png
        cs.Graph, cs.Library, cs.DcoLibrary = G, Lib, Dco
        cs.fetch_inputs = lambda: {"bookings": {"proposals": []}, "screens": ["Greenlane", "Beach"]}

    def test_run(self):
        import json
        res = cs.Syncer()._run()
        with open(os.path.join(cs.OUT, "vault-creatives.json")) as f:
            man = json.load(f)
        mine = [c for c in man["creatives"] if c["campaign_id"] == "LUMO_19409"]
        self.assertEqual(len(mine), 4)
        self.assertTrue(all(c["dco"] and c["via"] == "dco" and c["file"] for c in mine))
        self.assertIn("Sunny 12:14-15:10 (SUN-HL)", {c["variant"] for c in mine})
        self.assertTrue(os.path.exists(os.path.join(cs.OUT, mine[0]["file"])))
        self.assertEqual(res["dco"], 1)
        miss = {m["campaign_id"]: m for m in man["missing"]}
        self.assertNotIn("LUMO_19409", miss)
        m = miss["LUMO_19410"]                    # DCO with no renders anywhere: missing, and says why
        self.assertIn("DCO (other.zip in Doohly): no folder for LUMO_19410 or Nobody", m["reason"])
        self.assertEqual(m["dco"], ["other.zip"])
        self.assertEqual(m["new_folder"]["name"], "LUMO_19410 - Other DCO")     # the id isn't repeated


if __name__ == "__main__":
    unittest.main()
