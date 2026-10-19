"""A campaign whose folder carries another booking's number (LUMO_16034 Chemist Warehouse
for LUMO_19974): found by the advertiser's name when only one folder names it, then only
files named as Doohly plays them count; otherwise picked by hand from the month's folders."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import creatives_sync as cs  # noqa: E402

BOOKED = "DOOH NZ - [CWH-0926 F12-Best Buy A] 432x864.jpg"


def f(iid, name, w=None, h=None, rel=""):
    return {"id": iid, "drive": "d0", "name": name, "etag": iid, "modified": "2026-10-01T00:00:00Z", "size_b": 10,
            "width": w, "height": h, "rel": (rel + "/" if rel else "") + name, "folder_rel": rel,
            "folder_url": "https://sp/folder"}


class Folders(unittest.TestCase):
    def setUp(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(cs, k) for k in ("OUT", "STATE", "Graph", "Library", "fetch_inputs", "SYNC")}
        self.addCleanup(lambda: [setattr(cs, k, v) for k, v in saved.items()])
        cs.OUT, cs.STATE = out, os.path.join(out, ".sync")
        os.makedirs(cs.STATE)
        today = datetime.now(cs.LOCAL).date()
        mon = today - timedelta(days=today.weekday())
        mk = (today.year, today.month)
        with open(os.path.join(cs.STATE, "screen-specs.json"), "w") as fh:
            json.dump({"curletts in": [432, 864, "Curletts In"]}, fh)
        with open(os.path.join(out, "doohly.json"), "w") as fh:
            json.dump({"sites": {"C": {"screen": "Curletts In", "bookings": [
                {"campaign_id": "LUMO_19974", "campaign_name": "LUMO_19974 - CWH October", "advertiser": "Chemist Warehouse",
                 "start": mon.isoformat(), "end": (mon + timedelta(days=6)).isoformat(),
                 "start_time": "00:00", "end_time": "23:59", "creatives": [{"name": BOOKED, "type": "image"}]}]}}}, fh)
        self.month = [{"id": "F16034", "name": "LUMO_16034 Chemist Warehouse", "_month": mk,
                       "_path": "2026/10. October/LUMO_16034 Chemist Warehouse", "webUrl": "https://sp/F16034"},
                      {"id": "F18000", "name": "LUMO_18000 Kiwibank", "_month": mk,
                       "_path": "2026/10. October/LUMO_18000 Kiwibank", "webUrl": "https://sp/F18000"}]
        self.trees = {"F16034": [f("a", BOOKED, 432, 864, "BEST BUYS A"), f("b", "Summer Ready 432x864.jpg", 432, 864, "Summer Ready")],
                      "F99": [f("z", "CWH portrait 432x864.jpg", 432, 864)]}
        test = self

        class G:
            calls = 0

        class Lib:
            advertiser_folder = cs.Library.advertiser_folder
            folder_choices = cs.Library.folder_choices

            def __init__(self, _g):
                self.drive = "d0"

            def connect(self):
                return self

            def index_campaigns(self, today):
                self.month_folders = [dict(x) for x in test.month]
                return {"16034": [dict(test.month[0])], "18000": [dict(test.month[1])]}

            def search_campaign(self, cid):
                return []

            def tree(self, fo):
                return [dict(x) for x in test.trees.get(fo["id"], [])]

            def month_folder(self, today):
                return None

            def download(self, iid, drive=None):
                return b"\xff\xd8\xff" + iid.encode()

        class Sync:
            asked = []

            def request(self, reason, screens=None):
                self.asked.append(screens)

        cs.Graph, cs.Library, cs.SYNC = G, Lib, Sync()
        cs.fetch_inputs = lambda: {"bookings": {"proposals": []}, "screens": ["Curletts In"]}

    def manifest(self):
        cs.Syncer()._run()
        with open(os.path.join(cs.OUT, "vault-creatives.json")) as fh:
            return json.load(fh)

    def test_found_by_advertiser(self):
        man = self.manifest()
        self.assertEqual(man["missing"], [])
        self.assertEqual([(c["name"], c["via"]) for c in man["creatives"]], [(BOOKED, "doohly")])   # not Summer Ready

    def test_guessed_folder_only_counts_doohly_names(self):
        self.trees["F16034"] = [f("b", "Summer Ready 432x864.jpg", 432, 864, "Summer Ready")]   # right size, other name
        m = self.manifest()["missing"][0]
        self.assertIn("advertiser's name", m["reason"])
        self.assertEqual(m["folder_guessed"], f"{cs.ROOT}/2026/10. October/LUMO_16034 Chemist Warehouse")
        self.assertEqual([x["name"] for x in m["folder_files"]], ["Summer Ready 432x864.jpg"])   # can be picked
        self.assertEqual(m["folder_choices"][0]["id"], "F16034")                                 # advertiser's first

    def test_two_folders_pick_by_hand(self):
        self.month.append({"id": "F99", "name": "Chemist Warehouse (old)", "_month": self.month[0]["_month"],
                           "_path": "2026/10. October/Chemist Warehouse (old)", "webUrl": "https://sp/F99"})
        m = self.manifest()["missing"][0]
        self.assertIn("no folder for LUMO_19974", m["reason"])                 # two name it: no guess
        self.assertEqual({x["id"] for x in m["folder_choices"][:2]}, {"F16034", "F99"})
        self.assertEqual(cs.link_folder("LUMO_19974", "nope")[0], 404)
        code, res = cs.link_folder("LUMO_19974", "F99")
        self.assertEqual(code, 200)
        self.assertEqual(cs.SYNC.asked[-1], ["Curletts In"])
        man = self.manifest()
        # a folder set by hand: sizes count too (the Doohly name still isn't there: listed as partly missing)
        self.assertEqual([c["name"] for c in man["creatives"]], ["CWH portrait 432x864.jpg"])
        self.assertEqual([(m["partial"], m["folder_linked"].rsplit("/", 1)[1]) for m in man["missing"]],
                         [(True, "Chemist Warehouse (old)")])
        self.assertEqual(cs.link_folder("LUMO_19974", "")[0], 200)
        self.assertEqual(cs.link_folder("LUMO_19974", "")[0], 404)


if __name__ == "__main__":
    unittest.main()
