"""Fit to artwork on the region page:  python3 tools/test_region_fit.py

Draws a booked artwork onto a camera picture through a known curved (and a known
folded) region, starts from handles in the wrong places, and checks the fit
lines the photo up with the artwork again. Needs numpy and Pillow."""
import os
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
from test_video_match import artwork, Image  # noqa: E402

if Image is not None:
    import numpy as np


def draw(art, reg, W=1280, H=720):
    """The picture a camera would see: the artwork splatted through the region."""
    a = np.asarray(art.convert("RGB"), dtype=np.uint8)
    img = np.full((H, W, 3), 45, np.uint8)
    v, u = np.mgrid[0:1400, 0:3000].astype(float)
    u, v = (u + 0.5) / 3000, (v + 0.5) / 1400
    x, y = rx.region_mapper(reg)(u, v)
    xi, yi = (x * (W - 1)).round().astype(int), (y * (H - 1)).round().astype(int)
    ok = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
    img[yi[ok], xi[ok]] = a[(v[ok] * a.shape[0]).astype(int), (u[ok] * a.shape[1]).astype(int)]
    return Image.fromarray(img)


@unittest.skipIf(Image is None, "needs numpy and Pillow")
class Fit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.lib, self.out = os.path.join(tmp, "lib"), os.path.join(tmp, "out")
        os.makedirs(self.lib)
        os.makedirs(os.path.join(self.out, "images"))
        self.art = artwork(7, 960, 320)
        self.art.save(os.path.join(self.lib, "a.png"))
        rx.ARGS = types.SimpleNamespace(out=self.out)
        rx.CRE.update(root=self.lib, mtime=1.0)
        rx.board_px = lambda cam: None
        rx.artwork_matches = lambda cam, t0, t1: {"c-0001": {"status": "matched", "file": "a.png", "score": 0.7,
                                                             "campaign_id": "LUMO_1"}}
        rx.site_plays = lambda sid, t0, t1: [(time.time() - 60, "c-0001", False, False, "e1", True, True)]

    def fit(self, truth, start):
        draw(self.art, truth).save(os.path.join(self.out, "images", "e1.frame.jpg"), quality=92)
        return rx.fit_region({"id": "S"}, start, 0)

    def test_curve(self):
        C = [[0.2, 0.25], [0.8, 0.32], [0.78, 0.75], [0.22, 0.8]]
        h = rx._homog(C)
        straight = [list(map(float, rx._hmap(h, u, v))) for u, v in rx.CURVE_AT]
        bent = [[x + dx, y + dy] for (x, y), (dx, dy) in
                zip(straight, [(-0.03, -0.04), (-0.02, -0.035), (0, 0), (0, 0), (-0.03, 0.03), (-0.02, 0.025), (0, 0), (0, 0)])]
        truth = {"roi": C, "roi_shape": "curve", "roi_curve": bent}
        r = self.fit(truth, {"roi": C, "roi_shape": "curve", "roi_curve": straight})
        self.assertTrue(r["better"], r)
        self.assertGreater(r["after"], 0.9)
        err = max(abs(a - b) for p, q in zip(r["region"]["roi_curve"], bent) for a, b in zip(p, q))
        self.assertLess(err, 0.01, r["region"]["roi_curve"])

    def test_fold(self):
        C = [[0.15, 0.3], [0.85, 0.32], [0.85, 0.7], [0.15, 0.72]]
        truth = {"roi": C, "roi_shape": "fold", "roi_fold": [[0.42, 0.22], [0.42, 0.8]], "fold_pct": 40}
        r = self.fit(truth, dict(truth, roi_fold=[[0.44, 0.22], [0.41, 0.8]], fold_pct=50))
        self.assertTrue(r["better"], r)
        self.assertGreater(r["after"], 0.9)
        self.assertLess(abs(r["region"]["fold_pct"] - 40), 4)

    def test_nothing_matched(self):
        rx.artwork_matches = lambda cam, t0, t1: {}
        r = rx.fit_region({"id": "S"}, {"roi": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]}, 0)
        self.assertIn("no photo", r["error"])


if __name__ == "__main__":
    unittest.main()
