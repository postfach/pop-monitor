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

            def item(self, iid):
                f = next((x for x in test.month if x["id"] == iid), None)
                return dict(f, _path=f["_path"]) if f else None

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

    def test_doohly_only_booking(self):
        """Made in Doohly with no LUMO number (the Alexandra Park landowner): no folder is looked
        for or guessed; its folder is picked by hand, from every indexed month."""
        path = os.path.join(cs.OUT, "doohly.json")
        with open(path) as fh:
            doc = json.load(fh)
        b = doc["sites"]["C"]["bookings"][0]
        doc["sites"]["C"]["bookings"] = [dict(b, campaign_id=None, name="Curletts IN 3",
                                              campaign_name="Landowner", advertiser="",
                                              creatives=[{"name": "landowner 432x864.jpg", "type": "image"}])]
        with open(path, "w") as fh:
            json.dump(doc, fh)
        self.month.append({"id": "OLD", "name": "Landowner artwork", "_month": (2026, 3),
                           "_path": "2026/03. March/Landowner artwork", "webUrl": "https://sp/OLD"})
        self.trees["OLD"] = [f("l", "landowner 432x864.jpg", 432, 864)]
        m = self.manifest()["missing"][0]
        self.assertEqual(m["campaign_id"], "D:Curletts IN 3")
        self.assertIn("without a LUMO number", m["reason"])
        self.assertNotIn("new_folder", m)                                      # nothing to name one with
        self.assertIn("OLD", [x["id"] for x in m["folder_choices"]])           # older months offered too
        self.assertEqual(cs.link_folder("D:Curletts IN 3", "OLD")[0], 200)
        man = self.manifest()
        self.assertEqual(man["missing"], [])
        self.assertEqual([(c["campaign_id"], c["name"]) for c in man["creatives"]], [("D:Curletts IN 3", "landowner 432x864.jpg")])


class Browse(unittest.TestCase):
    """Choosing a folder like a download location: starts at this year's folder, goes up as
    far as Creative+MIs and no further, and only folders inside it can be used."""

    def setUp(self):
        root = cs.ROOT.strip("/")
        year = str(datetime.now(cs.LOCAL).date().year)
        P = lambda rel: {"path": "/drives/d0/root:/" + (root + "/" + rel).strip("/")}    # noqa: E731
        self.items = {
            "ROOT": {"id": "ROOT", "name": root.rsplit("/", 1)[-1], "folder": {"childCount": 2}, "webUrl": "u"},
            "Y": {"id": "Y", "name": year, "folder": {"childCount": 1}, "parentReference": dict(P(""), id="ROOT")},
            "M": {"id": "M", "name": "10. October", "folder": {"childCount": 1}, "parentReference": dict(P(year), id="Y")},
            "C": {"id": "C", "name": "LUMO_16034 Chemist Warehouse", "folder": {"childCount": 2},
                  "parentReference": dict(P(f"{year}/10. October"), id="M"), "webUrl": "https://sp/C"},
            "OUT": {"id": "OUT", "name": "Elsewhere", "folder": {}, "parentReference": {"path": "/drives/d0/root:/Other", "id": "X"}},
            "F": {"id": "F", "name": "a.jpg", "file": {}, "parentReference": dict(P(year), id="Y")}}
        kids = {"ROOT": ["Y", "OUT"], "Y": ["M"], "M": ["C"], "C": ["F"]}
        items = self.items

        class G:
            calls = 0

            def get(self, url, **kw):
                if ":/" in url and "/root:" in url:
                    return dict(items["ROOT"])
                iid = url.rsplit("/", 1)[1]
                if iid not in items:
                    raise cs.GraphError("itemNotFound")
                return dict(items[iid])

            def pages(self, url, **kw):
                return [dict(items[k]) for k in kids.get(url.split("/items/")[1].split("/")[0], [])]
        saved = {k: getattr(cs, k) for k in ("Graph", "Library", "OUT", "STATE", "SYNC")}
        self.addCleanup(lambda: [setattr(cs, k, v) for k, v in saved.items()])
        cs.Graph, cs.Library = G, saved["Library"]
        os.environ["SP_DRIVE_ID"] = "d0"
        self.addCleanup(lambda: os.environ.pop("SP_DRIVE_ID", None))
        out = tempfile.mkdtemp()
        cs.OUT, cs.STATE = out, os.path.join(out, ".sync")
        os.makedirs(cs.STATE)

        class Sync:
            def request(self, reason, screens=None):
                pass
        cs.SYNC = Sync()

    def test_browse_and_choose(self):
        code, b = cs.browse()
        self.assertEqual((code, b["folder"]["id"], b["up"], [f["name"] for f in b["folders"]]), (200, "Y", "ROOT", ["10. October"]))
        code, b = cs.browse("ROOT")
        self.assertTrue(b["is_root"])
        self.assertIsNone(b["up"])                                   # no further up than Creative+MIs
        code, b = cs.browse("C")
        self.assertEqual((b["folder"]["path"].split("/", 1)[1], b["up"], b["files"]), ("10. October/LUMO_16034 Chemist Warehouse", "M", ["a.jpg"]))
        self.assertEqual(cs.browse("OUT")[0], 404)                  # outside Creative+MIs
        self.assertEqual(cs.browse("F")[0], 404)                    # a file
        self.assertEqual(cs.link_folder("D:Alexandra Park IN", "OUT")[0], 404)
        self.assertEqual(cs.link_folder("D:Alexandra Park IN", "ROOT")[0], 404)
        code, res = cs.link_folder("D:Alexandra Park IN", "C")
        self.assertEqual((code, res["folder"]["path"].split("/", 1)[1]), (200, "10. October/LUMO_16034 Chemist Warehouse"))


if __name__ == "__main__":
    unittest.main()
