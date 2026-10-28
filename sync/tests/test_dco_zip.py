"""A DCO booking (an HTML zip in Doohly, e.g. LUMO_19035 Afterpay on Anzac): its zip
uploaded on the Bookings page puts the picture in it into the campaign folder under the
zip's name, and the sync matches that picture to the booking as the zip's variant."""
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import creatives_sync as cs  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_artwork_upload import FakeGraph, Lib  # noqa: E402

V1, V2 = "SC_doohly_28358_Afterpay_IAT_OOH_NZ_LUMO_Static_Anzac_V1.zip", "SC_doohly_28358_Afterpay_IAT_OOH_NZ_LUMO_Static_Anzac_V2.zip"


def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"rest"


def jpg(w, h):
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 +
            b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x00" * 9 + b"\xff\xd9")


def zipped(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


class Picture(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(cs.picture_size(png(1764, 468)), (1764, 468))
        self.assertEqual(cs.picture_size(jpg(1764, 468)), (1764, 468))
        self.assertIsNone(cs.picture_size(b"<html>"))

    def test_best_picture(self):
        z = zipped({"index.html": b"<html>", "img/logo.png": png(40, 40), "img/big.jpg": jpg(3528, 936),
                    "img/bg.png": png(1764, 468), "__MACOSX/img/._bg.png": png(1764, 468)})
        ext, data, member, wh = cs.zip_picture(z, [1764, 468])
        self.assertEqual((ext, member, wh), (".png", "img/bg.png", (1764, 468)))       # the screen's own size
        ext, data, member, wh = cs.zip_picture(z, [500, 500])
        self.assertEqual(member, "img/big.jpg")                                       # else the largest

    def test_video_wins(self):
        """An animated ad (SCTI on Anzac): index.html and the video it plays."""
        z = zipped({"index.html": b'<video src="1792362489969.mp4" autoplay>', "1792362489969.mp4": b"\0" * 500,
                    "other.mp4": b"\0" * 900, "poster.png": png(1764, 468)})
        ext, data, member, wh = cs.zip_picture(z, [1764, 468])
        self.assertEqual((ext, member, wh, len(data)), (".mp4", "1792362489969.mp4", None, 500))

    def test_no_picture(self):
        with self.assertRaisesRegex(ValueError, "no picture"):
            cs.zip_picture(zipped({"index.html": b"<html>", "a.js": b"x"}), [1764, 468])
        with self.assertRaisesRegex(ValueError, "isn't a zip"):
            cs.zip_picture(b"nope")


class Upload(unittest.TestCase):
    def setUp(self):
        saved = (cs.OUT, cs.Graph, cs.Library, cs.SYNC)
        self.addCleanup(lambda: setattr(cs, "OUT", saved[0]) or setattr(cs, "Graph", saved[1]) or
                        setattr(cs, "Library", saved[2]) or setattr(cs, "SYNC", saved[3]))
        cs.OUT = tempfile.mkdtemp()
        self.g = FakeGraph()
        cs.Graph, cs.Library = (lambda: self.g), Lib
        cs.SYNC = type("S", (), {"request": lambda _s, why: None})()
        with open(os.path.join(cs.OUT, "vault-creatives.json"), "w") as f:
            json.dump({"missing": [{"campaign_id": "LUMO_19035", "screen": "Anzac", "size": [1764, 468], "dco": [V1, V2],
                                    "folder": {"id": "F1", "path": "Creative+MIs/2026/10. October/LUMO_19035 Afterpay"}}]}, f)

    def test_zip_becomes_its_picture(self):
        code, res = cs.upload_artwork("LUMO_19035", "Anzac", V1, zipped({"index.html": b"<html>", "bg.jpeg": jpg(1764, 468)}))
        self.assertEqual(code, 201, res)
        self.assertEqual(res["path"].rsplit("/", 1)[1], V1[:-4] + ".jpg")
        self.assertEqual((res["from_zip"], res["size"]), ("bg.jpeg", [1764, 468]))
        self.assertEqual(list(self.g.files), ["F1:/" + V1[:-4] + ".jpg"])

    def test_refused(self):
        self.assertEqual(cs.upload_artwork("LUMO_19035", "Anzac", "other.zip", zipped({"a.png": png(1764, 468)}))[0], 400)
        code, res = cs.upload_artwork("LUMO_19035", "Anzac", V2, zipped({"index.html": b"<html>"}))
        self.assertEqual(code, 400)
        self.assertIn("render or screenshot", res["error"])

    def test_video_zip(self):
        code, res = cs.upload_artwork("LUMO_19035", "Anzac", V2, zipped({"index.html": b"<video src='a.mp4'>", "a.mp4": b"\0" * 64}))
        self.assertEqual((code, res["kind"], res["size"]), (201, "video", None), res)
        self.assertTrue(res["path"].endswith("_V2.mp4"))


def f(iid, name, w=None, h=None):
    return {"id": iid, "drive": "d0", "name": name, "etag": iid, "modified": "2026-10-01T00:00:00Z", "size_b": 10,
            "width": w, "height": h, "rel": name, "folder_rel": "", "folder_url": "https://sp/folder"}


class Sync(unittest.TestCase):
    def setUp(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(cs, k) for k in ("OUT", "STATE", "Graph", "Library", "DcoLibrary", "fetch_inputs", "SYNC")}
        self.addCleanup(lambda: [setattr(cs, k, v) for k, v in saved.items()])
        cs.OUT, cs.STATE = out, os.path.join(out, ".sync")
        os.makedirs(cs.STATE)
        today = datetime.now(cs.LOCAL).date()
        mon = today - timedelta(days=today.weekday())
        with open(os.path.join(cs.STATE, "screen-specs.json"), "w") as fh:
            json.dump({"anzac": [1764, 468, "Anzac"]}, fh)
        with open(os.path.join(out, "doohly.json"), "w") as fh:
            json.dump({"sites": {"A": {"screen": "Anzac", "bookings": [
                {"campaign_id": "LUMO_19035", "campaign_name": "LUMO_19035 - Afterpay", "advertiser": "Afterpay",
                 "start": mon.isoformat(), "end": (mon + timedelta(days=6)).isoformat(),
                 "start_time": "00:00", "end_time": "23:59",
                 "creatives": [{"name": V1, "type": "html"}, {"name": V2, "type": "html"}]}]}}}, fh)
        folder = {"id": "F", "name": "LUMO_19035 Afterpay", "_month": (today.year, today.month),
                  "_path": "2026/10. October/LUMO_19035 Afterpay", "webUrl": "https://sp/F"}
        test = self
        self.files = [f("o", "Afterpay portrait 1080x1920.jpg", 1080, 1920)]

        class G:
            calls = 0

        class Lib_:
            advertiser_folder = cs.Library.advertiser_folder
            folder_choices = cs.Library.folder_choices

            def __init__(self, _g):
                self.drive = "d0"

            def connect(self):
                return self

            def index_campaigns(self, today):
                self.month_folders = [dict(folder)]
                return {"19035": [dict(folder)]}

            def search_campaign(self, cid):
                return []

            def tree(self, fo):
                return [dict(x) for x in test.files]

            def month_folder(self, today):
                return None

            def download(self, iid, drive=None):
                return b"\xff\xd8\xff" + iid.encode()

        class NoLabs:
            def __init__(self, g):
                pass

            def connect(self):
                return self

            def campaign_folder(self, *a, **k):
                return None

        cs.Graph, cs.Library, cs.DcoLibrary = G, Lib_, NoLabs
        cs.SYNC = type("S", (), {"request": lambda *a, **k: None})()
        cs.fetch_inputs = lambda: {"bookings": {"proposals": []}, "screens": ["Anzac"]}

    def manifest(self):
        cs.Syncer()._run()
        with open(os.path.join(cs.OUT, "vault-creatives.json")) as fh:
            return json.load(fh)

    def test_before_and_after_upload(self):
        m = self.manifest()["missing"][0]
        self.assertEqual((m["dco"], m["dco_missing"]), ([V1, V2], [V1, V2]))
        self.files.append(f("v1", V1[:-4] + ".jpg", 1764, 468))
        man = self.manifest()
        self.assertEqual([(c["name"], c["dco"], c["variant"]) for c in man["creatives"]],
                         [(V1[:-4] + ".jpg", True, V1[:-4])])                     # not the other file
        m = man["missing"][0]
        self.assertTrue(m["partial"])
        self.assertEqual((m["not_found"], m["dco_missing"]), ([V2], [V2]))
        self.files.append(f("v2", V2[:-4] + ".mp4"))                              # an animated one
        man = self.manifest()
        self.assertEqual(man["missing"], [])
        self.assertEqual(sorted((c["kind"], c["variant"]) for c in man["creatives"]), [("image", V1[:-4]), ("video", V2[:-4])])


if __name__ == "__main__":
    unittest.main()
