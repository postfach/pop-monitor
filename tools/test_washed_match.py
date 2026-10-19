"""Washed-out PoP photos are still matched:  python3 tools/test_washed_match.py

White-heavy artwork photographed in strong light comes out with much of the board blown
out; the gateway marks those photos unusable ("blown out"), but the detail that is left
still says which campaign it is. They are compared when there aren't enough clean
photos, with the same thresholds; unrelated creatives must still not match. Needs numpy and Pillow."""
import os
import random
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
from test_video_match import artwork, photo, Image  # noqa: E402

if Image is not None:
    import numpy as np
    from PIL import ImageDraw


def white_art(seed, w=576, h=396):
    """Mostly white artwork (like Star Insure's): a white board, a dark headline,
    a photo block and a logo."""
    r = random.Random(seed)
    im = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(im)
    x0, y0 = r.randrange(20, w // 2), r.randrange(20, h // 2)
    d.rectangle([x0, y0, x0 + r.randrange(120, 220), y0 + r.randrange(80, 160)],
                fill=tuple(r.randrange(40, 200) for _ in range(3)))
    for i in range(r.randrange(3, 6)):
        y = r.randrange(20, h - 40)
        x = r.randrange(10, w // 2)
        for j in range(r.randrange(5, 12)):
            d.rectangle([x + j * 20, y, x + j * 20 + r.randrange(8, 16), y + r.randrange(10, 20)], fill=(20, 20, 30))
    cx, cy = r.randrange(60, w - 60), r.randrange(60, h - 60)
    d.ellipse([cx - 30, cy - 30, cx + 30, cy + 30], fill=(200, 30, 40))
    return im


def washed(im, seed, gain=1.9):
    """A camera photo with the exposure pushed until the white blows out."""
    p = photo(im, seed)
    a = np.asarray(p, dtype=float) * gain + 25
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"))


def blown(img):
    a = np.asarray(img)
    return float((a.max(axis=2) >= 248).mean())


@unittest.skipIf(Image is None, "needs numpy and Pillow")
class Washed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        lib = os.path.join(tmp, "lib")
        os.makedirs(lib)
        cls.arts = {"LUMO_1": white_art(1), "LUMO_2": white_art(2), "LUMO_3": artwork(3), "LUMO_4": artwork(4)}
        cls.lib = []
        for cid, im in cls.arts.items():
            im.save(os.path.join(lib, f"{cid}.png"))
            cls.lib.append({"campaign_id": cid, "screen": "S", "name": f"{cid}.png", "kind": "image", "file": f"{cid}.png"})
        cls.out = os.path.join(tmp, "out")
        os.makedirs(os.path.join(cls.out, "images"))
        rx.ARGS = types.SimpleNamespace(out=cls.out)
        rx.CRE.update(root=lib, mtime=7.0)
        rx.screen_creatives = lambda screen: cls.lib
        rx.save_art_state = lambda: None
        rx.doohly_site = lambda sid, screen: None
        cls.plays = []
        cls.blown = {}

        def add(cid, img, n, washed_out, seed0):
            for k in range(n):
                eid = f"{cid}-{k}"
                im = washed(img, seed0 + k) if washed_out else photo(img, seed0 + k)
                cls.blown[eid] = blown(im)
                im.save(os.path.join(cls.out, "images", f"{eid}.roi.jpg"), quality=90)
                # (t, creative, blank, video, event, has photo, usable, washed out)
                cls.plays.append((1000 + len(cls.plays), cid, False, False, eid, True, not washed_out, washed_out))
        add("c-0001", cls.arts["LUMO_1"], 4, True, 10)      # white artwork, every photo blown out
        add("c-0002", cls.arts["LUMO_3"], 3, True, 20)      # colourful artwork, washed out too
        add("c-0003", white_art(99), 4, True, 30)           # unbooked white artwork, washed out
        add("c-0004", artwork(98), 4, True, 40)             # unbooked colourful artwork, washed out
        add("c-0005", cls.arts["LUMO_2"], 2, False, 50)     # clean photos...
        add("c-0005", cls.arts["LUMO_2"], 3, True, 60)      # ...and washed-out ones
        rx.site_plays = lambda sid, t0, t1, setup=False: cls.plays

    def setUp(self):
        rx.art_state().pop("W", None)

    def test_photos_really_are_blown_out(self):
        self.assertGreater(min(v for k, v in self.blown.items() if k.startswith("c-0001")), 0.5)

    def test_washed_out_white_artwork_matches(self):
        res = rx.artwork_matches({"id": "W", "vault_screen": "S"}, 0, 9999)
        a = res["c-0001"]
        self.assertEqual((a["status"], a["campaign_id"]), ("matched", "LUMO_1"), a)
        self.assertEqual(a["washed"], 4)
        a = res["c-0002"]
        self.assertEqual((a["status"], a["campaign_id"]), ("matched", "LUMO_3"), a)

    def test_unbooked_creatives_still_dont_match(self):
        res = rx.artwork_matches({"id": "W", "vault_screen": "S"}, 0, 9999)
        for cid in ("c-0003", "c-0004"):
            self.assertNotEqual(res[cid]["status"], "matched", res[cid])

    def test_clean_photos_come_first(self):
        res = rx.artwork_matches({"id": "W", "vault_screen": "S"}, 0, 9999)
        a = res["c-0005"]
        self.assertEqual((a["status"], a["campaign_id"]), ("matched", "LUMO_2"), a)
        self.assertEqual((a["samples"], a["washed"]), (5, 3))   # 2 clean, then 3 washed out to make 5

    def test_a_board_blown_out_completely_matches_nothing(self):
        """Sun strike: nothing left to compare, so it can't make a match on its own."""
        Image.new("RGB", (288, 198), (255, 255, 255)).save(os.path.join(self.out, "images", "sun-0.roi.jpg"))
        Image.new("RGB", (288, 198), (254, 255, 253)).save(os.path.join(self.out, "images", "sun-1.roi.jpg"))
        plays = [(5000, "c-0009", False, False, "sun-0", True, False, True),
                 (5001, "c-0009", False, False, "sun-1", True, False, True)]
        saved = rx.site_plays
        rx.site_plays = lambda sid, t0, t1, setup=False: plays
        try:
            res = rx.artwork_matches({"id": "W", "vault_screen": "S"}, 0, 9999)
        finally:
            rx.site_plays = saved
        self.assertNotEqual(res.get("c-0009", {}).get("status"), "matched", res.get("c-0009"))


if __name__ == "__main__":
    unittest.main()
