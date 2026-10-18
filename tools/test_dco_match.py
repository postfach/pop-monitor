"""Dynamic creatives (DCO) are matched by their rendered variants:  python3 tools/test_dco_match.py

A weather DCO has many near-identical renders (same car, different sky). Photos of one
variant match the campaign (the variants don't compete with each other), the best variant
is named, a different campaign's photos still match that campaign, and the renders let
programmatic be worked out. Needs numpy and Pillow."""
import os
import sys
import tempfile
import types
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
from test_video_match import artwork, photo, Image  # noqa: E402

if Image is not None:
    from PIL import ImageDraw


def variant(base, sky):
    """The same creative with a different sky (top third) and a weather word."""
    im = base.copy()
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, im.width, im.height // 3], fill=sky)
    d.rectangle([20, 20, 20 + 40 * (sum(sky) % 5 + 1), 50], fill=(255, 255, 255))
    return im


@unittest.skipIf(Image is None, "needs numpy and Pillow")
class DcoMatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        lib = os.path.join(tmp, "lib")
        os.makedirs(os.path.join(lib, "g", "images", "LUMO_19409"))
        os.makedirs(os.path.join(lib, "g", "images", "LUMO_2"))
        base = artwork(41)
        skies = [(40, 90, 200), (120, 130, 140), (20, 20, 40), (230, 140, 60), (90, 100, 110), (60, 160, 230)]
        cls.lib = []
        cls.vars = []
        for i, sky in enumerate(skies):
            v = variant(base, sky)
            cls.vars.append(v)
            rel = f"g/images/LUMO_19409/GRE_V{i}_0615-1813_Sky.png"
            v.save(os.path.join(lib, rel))
            cls.lib.append({"campaign_id": "LUMO_19409", "screen": "Greenlane", "name": os.path.basename(rel), "kind": "image",
                            "file": rel, "dco": True, "variant": f"Sky {i}"})
        other = artwork(42)
        other.save(os.path.join(lib, "g/images/LUMO_2/still.png"))
        cls.lib.append({"campaign_id": "LUMO_2", "screen": "Greenlane", "name": "still.png", "kind": "image",
                        "file": "g/images/LUMO_2/still.png"})
        out = os.path.join(tmp, "out")
        os.makedirs(os.path.join(out, "images"))
        rx.ARGS = types.SimpleNamespace(out=out)
        rx.CRE.update(root=lib, mtime=3.0)
        rx.screen_creatives = lambda screen: cls.lib
        rx.save_art_state = lambda: None
        rx.doohly_site = lambda sid, screen: None
        plays = []
        for k, (cid, src) in enumerate([("c-0001", cls.vars[3]), ("c-0002", other), ("c-0003", artwork(99))]):
            for n in range(5):
                eid = f"d{k}{n}"
                photo(src, 70 + k * 10 + n).save(os.path.join(out, "images", f"{eid}.roi.jpg"))
                plays.append((1000 + k * 100 + n, cid, False, False, eid, True, True))
        rx.site_plays = lambda sid, t0, t1, setup=False: plays

    def test_variant_matches_its_campaign(self):
        res = rx.artwork_matches({"id": "GREEN", "vault_screen": "Greenlane"}, 0, 2000)
        a = res["c-0001"]
        self.assertEqual((a["status"], a["campaign_id"]), ("matched", "LUMO_19409"), a)
        self.assertEqual(a["variant"], "Sky 3")
        self.assertLess(a["next"], a["score"] - rx.ART_MARGIN)          # next best is the other campaign
        self.assertEqual((res["c-0002"]["status"], res["c-0002"]["campaign_id"]), ("matched", "LUMO_2"))
        self.assertNotEqual(res["c-0003"]["status"], "matched")

    def test_renders_let_programmatic_be_worked_out(self):
        ds = {"loop": {"programmatic": True},
              "bookings": [{"campaign_id": "LUMO_19409", "start": "2026-10-19", "end": "2026-10-25",
                            "creatives": [{"type": "html"}]}]}
        crs = [{"creative_id": "c-0003", "blank": False, "plays": 5}]
        art = {"c-0003": {"status": "none", "samples": 5}}
        got, why = rx.infer_programmatic(ds, date(2026, 10, 19), crs, {"c-0003": "unassigned"}, art, self.lib)
        self.assertEqual((got, why), ({"c-0003"}, ""))
        got, why = rx.infer_programmatic(ds, date(2026, 10, 19), crs, {"c-0003": "unassigned"}, art,
                                         [x for x in self.lib if not x.get("dco")])
        self.assertEqual(got, set())
        self.assertIn("no renders", why)


if __name__ == "__main__":
    unittest.main()
