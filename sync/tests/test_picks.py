"""Artwork picked by hand: a booking whose file is in its campaign folder under a name the
sync can't tie to the screen is listed as missing with the folder's files; once one is
picked it is the booking's artwork (not renamed in SharePoint), until the pick is undone."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import creatives_sync as cs  # noqa: E402


def f(iid, name, w=None, h=None, rel=""):
    return {"id": iid, "drive": "d0", "name": name, "etag": iid, "modified": "2026-10-01T00:00:00Z", "size_b": 10,
            "width": w, "height": h, "rel": (rel + "/" if rel else "") + name, "folder_rel": rel,
            "folder_url": "https://sp/folder"}


class Picks(unittest.TestCase):
    def setUp(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(cs, k) for k in ("OUT", "STATE", "Graph", "Library", "fetch_inputs", "SYNC")}
        self.addCleanup(lambda: [setattr(cs, k, v) for k, v in saved.items()])
        cs.OUT, cs.STATE = out, os.path.join(out, ".sync")
        os.makedirs(cs.STATE)
        today = datetime.now(cs.LOCAL).date()
        mon = today - timedelta(days=today.weekday())
        with open(os.path.join(cs.STATE, "screen-specs.json"), "w") as fh:
            json.dump({"alexandra park in": [1188, 288, "Alexandra Park In"]}, fh)
        with open(os.path.join(out, "doohly.json"), "w") as fh:
            json.dump({"sites": {"A": {"screen": "Alexandra Park In", "bookings": [
                {"campaign_id": "LUMO_19999", "campaign_name": "LUMO_19999 - Alexandra Park + Epsom Landowner",
                 "start": mon.isoformat(), "end": (mon + timedelta(days=6)).isoformat(),
                 "start_time": "00:00", "end_time": "23:59",
                 "creatives": [{"name": "APE_Landowner_1188x288.jpg", "type": "image"}]}]}}}, fh)
        self.files = [f("i1", "Landowner FINAL v3.jpg", 1200, 300, "Artwork"), f("i2", "Other screen 576x396.jpg", 576, 396),
                      f("i3", "brief.pdf")]
        files = self.files

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
                return [{"id": "F1", "_path": "2026/10. October/LUMO_19999 - Alexandra Park", "webUrl": "https://sp/F1"}]

            def tree(self, fo):
                return [dict(x) for x in files]

            advertiser_folder = cs.Library.advertiser_folder
            folder_choices = cs.Library.folder_choices

            def month_folder(self, today):
                return None

            def download(self, iid, drive=None):
                return b"\xff\xd8\xff" + iid.encode()

        class Sync:
            asked = []

            def request(self, reason, screens=None):
                self.asked.append(screens)

        cs.Graph, cs.Library = G, Lib
        cs.SYNC = Sync()
        cs.fetch_inputs = lambda: {"bookings": {"proposals": []}, "screens": ["Alexandra Park In"]}

    def manifest(self):
        cs.Syncer()._run()
        with open(os.path.join(cs.OUT, "vault-creatives.json")) as fh:
            return json.load(fh)

    def test_pick_and_undo(self):
        man = self.manifest()
        m = man["missing"][0]
        self.assertEqual([x["name"] for x in m["folder_files"]], ["Other screen 576x396.jpg", "Landowner FINAL v3.jpg"])
        self.assertEqual(m["folder_files"][1]["path"], "Artwork")
        self.assertEqual(m["picked"], [])
        # only a file the folder has
        self.assertEqual(cs.pick_artwork("LUMO_19999", "Alexandra Park In", ["nope.jpg"])[0], 400)
        self.assertEqual(cs.pick_artwork("LUMO_19998", "Alexandra Park In", ["Landowner FINAL v3.jpg"])[0], 404)
        code, res = cs.pick_artwork("LUMO_19999", "alexandra park in", ["Landowner FINAL v3.jpg"])
        self.assertEqual((code, res["picked"]), (200, ["Landowner FINAL v3.jpg"]))
        self.assertEqual(cs.SYNC.asked[-1], ["alexandra park in"])        # just that screen
        man = self.manifest()
        self.assertEqual(man["missing"], [])
        c = man["creatives"][0]
        self.assertEqual((c["campaign_id"], c["name"], c["via"]), ("LUMO_19999", "Landowner FINAL v3.jpg", "picked"))
        self.assertTrue(os.path.exists(os.path.join(cs.OUT, c["file"])))
        # renamed in SharePoint: still found by its ID
        self.files[0]["name"] = "Landowner FINAL v4.jpg"
        self.assertEqual(self.manifest()["creatives"][0]["name"], "Landowner FINAL v4.jpg")
        # deleted: missing again, saying so
        del self.files[0]
        m = self.manifest()["missing"][0]
        self.assertIn("no longer in the folder", m["reason"])
        # undone
        self.assertEqual(cs.pick_artwork("LUMO_19999", "Alexandra Park In", [])[0], 200)
        self.assertEqual(cs.pick_artwork("LUMO_19999", "Alexandra Park In", [])[0], 404)


if __name__ == "__main__":
    unittest.main()
