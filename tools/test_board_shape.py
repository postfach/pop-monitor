"""Screen shape for artwork matching:  python3 tools/test_board_shape.py

A tall screen's fingerprints use a tall grid (from the site's resolution, Doohly's,
or the artwork's), and PoP images match whether or not the gateway straightened
them to the screen's shape. Needs numpy and Pillow."""
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
from test_video_match import artwork, photo, Image  # noqa: E402


@unittest.skipIf(Image is None, "needs numpy and Pillow")
class BoardShape(unittest.TestCase):
    def test_grid(self):
        self.assertEqual(rx.art_grid(None), (48, 32))
        self.assertEqual(rx.art_grid(576 / 396), (48, 32))       # near 3:2: unchanged
        gw, gh = rx.art_grid(9 / 16)
        self.assertGreater(gh, gw)
        self.assertLess(abs(gw * gh - 48 * 32), 60)

    def test_board_px_order(self):
        rx.doohly_site = lambda sid, screen: {"frame": {"width": 396, "height": 704}}
        self.assertEqual(rx.board_px({"id": "S", "board_width": 1080, "board_height": 1920}), (1080, 1920))
        self.assertEqual(rx.board_px({"id": "S", "board_width": 0}), (396, 704))
        rx.doohly_site = lambda sid, screen: None
        self.assertIsNone(rx.board_px({"id": "S"}))

    def test_tall_screen_matches_straightened_or_squeezed(self):
        tmp = tempfile.mkdtemp()
        lib = os.path.join(tmp, "lib")
        os.makedirs(os.path.join(lib, "tall"))
        arts = {"LUMO_1": artwork(21, 396, 704), "LUMO_2": artwork(22, 396, 704)}
        cands = []
        for cid, im in arts.items():
            im.save(os.path.join(lib, "tall", cid + ".png"))
            cands.append({"campaign_id": cid, "screen": "Tall", "name": cid, "kind": "image",
                          "file": f"tall/{cid}.png", "width": 396, "height": 704})
        out = os.path.join(tmp, "out")
        os.makedirs(os.path.join(out, "images"))
        rx.ARGS = types.SimpleNamespace(out=out)
        rx.CRE.update(root=lib, mtime=2.0)
        rx.screen_creatives = lambda screen: cands
        rx.save_art_state = lambda: None
        rx.doohly_site = lambda sid, screen: None          # the grid comes from the artwork's shape
        plays = []
        for k, (cid, src) in enumerate([("c-0001", arts["LUMO_1"]), ("c-0002", arts["LUMO_2"])]):
            for n in range(4):
                p = photo(src, 50 + k * 10 + n)
                if n % 2:                                    # as seen from an angle: squeezed sideways
                    p = p.resize((int(p.width * 0.55), p.height))
                eid = f"t{k}{n}"
                p.save(os.path.join(out, "images", f"{eid}.roi.jpg"))
                plays.append((1000 + k * 100 + n, cid, False, False, eid, True, True))
        rx.site_plays = lambda sid, t0, t1, setup=False: plays
        res = rx.artwork_matches({"id": "TALL", "vault_screen": "Tall"}, 0, 2000)
        self.assertEqual((res["c-0001"]["status"], res["c-0001"]["campaign_id"]), ("matched", "LUMO_1"))
        self.assertEqual((res["c-0002"]["status"], res["c-0002"]["campaign_id"]), ("matched", "LUMO_2"))
        self.assertIn("(29, 53)", rx.art_state()["TALL"]["c-0001"]["lib"])


if __name__ == "__main__":
    unittest.main()
