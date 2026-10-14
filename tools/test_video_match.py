"""Video creatives are matched by their frames:  python3 tools/test_video_match.py

Makes a two-scene video, takes its frames the way creatives-sync does, then checks
that photos of either scene (blurred, tinted, noisy, slightly off-centre) match the
video's campaign and not a booked image, and that an unrelated creative doesn't.
Needs numpy, Pillow and ffmpeg (system or imageio-ffmpeg)."""
import os
import random
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "sync"))
import receiver as rx  # noqa: E402
import videoframes  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageEnhance
    import numpy as np
except ImportError:  # pragma: no cover
    Image = None


def artwork(seed, w=576, h=396):
    """A made-up creative: blocks of colour, bars of 'text', a logo circle."""
    r = random.Random(seed)
    im = Image.new("RGB", (w, h), tuple(r.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(6):
        x, y = r.randrange(w), r.randrange(h)
        d.rectangle([x, y, x + r.randrange(40, 220), y + r.randrange(30, 160)], fill=tuple(r.randrange(256) for _ in range(3)))
    for i in range(r.randrange(3, 6)):
        y = r.randrange(20, h - 30)
        x = r.randrange(10, w // 2)
        for j in range(r.randrange(4, 12)):
            d.rectangle([x + j * 22, y, x + j * 22 + r.randrange(8, 18), y + r.randrange(10, 22)], fill=(250, 250, 250) if i % 2 else (10, 10, 10))
    cx, cy, rr = r.randrange(60, w - 60), r.randrange(60, h - 60), r.randrange(25, 55)
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=tuple(r.randrange(256) for _ in range(3)), outline=(0, 0, 0), width=4)
    return im


def photo(im, seed, night=False):
    """A camera's view of the board: off-centre, soft, tinted, noisy."""
    r = random.Random(seed)
    w, h = im.size
    dx, dy = r.randint(-8, 8), r.randint(-6, 6)
    p = im.crop((10 + dx, 8 + dy, w - 10 + dx, h - 8 + dy)).resize((w // 2, h // 2))
    p = p.filter(ImageFilter.GaussianBlur(r.uniform(0.6, 1.4)))
    p = ImageEnhance.Brightness(p).enhance(r.uniform(1.1, 1.4) if night else r.uniform(0.7, 1.0))
    p = ImageEnhance.Contrast(p).enhance(r.uniform(0.6, 0.9))
    a = np.asarray(p, dtype=float) + np.random.default_rng(seed).normal(0, 6, (p.size[1], p.size[0], 3))
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"))


@unittest.skipIf(Image is None or not videoframes.ffmpeg_exe(), "needs numpy, Pillow and ffmpeg")
class VideoMatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        lib = os.path.join(cls.tmp, "lib")
        os.makedirs(os.path.join(lib, "beach", "images", "LUMO_2"))
        cls.scenes = [artwork(11), artwork(12)]
        for i, s in enumerate(cls.scenes):
            s.save(os.path.join(cls.tmp, f"s{i}.png"))
        # 3 s of each scene with a 1 s crossfade, like a real spot
        video = os.path.join(cls.tmp, "spot.mp4")
        subprocess.run([videoframes.ffmpeg_exe(), "-v", "error", "-y",
                        "-loop", "1", "-t", "3", "-i", os.path.join(cls.tmp, "s0.png"),
                        "-loop", "1", "-t", "3", "-i", os.path.join(cls.tmp, "s1.png"),
                        "-filter_complex", "[0][1]xfade=transition=fade:duration=1:offset=2,format=yuv420p",
                        "-r", "25", video], check=True)
        rel = "beach/frames/LUMO_1/spot.mp4"
        frames = videoframes.extract(video, os.path.join(lib, rel))
        cls.image = artwork(13)
        cls.image.save(os.path.join(lib, "beach", "images", "LUMO_2", "still.png"))
        cls.lib = [{"campaign_id": "LUMO_1", "screen": "Beach", "name": "spot.mp4", "kind": "video",
                    "frames": [f"{rel}/{f}" for f in frames], "file": f"{rel}/{frames[0]}"},
                   {"campaign_id": "LUMO_2", "screen": "Beach", "name": "still.png", "kind": "image",
                    "file": "beach/images/LUMO_2/still.png"}]
        cls.nframes = len(frames)

        out = os.path.join(cls.tmp, "out")
        os.makedirs(os.path.join(out, "images"))
        rx.ARGS = types.SimpleNamespace(out=out)
        rx.CRE.update(root=lib, mtime=1.0)
        rx.screen_creatives = lambda screen: cls.lib
        rx.save_art_state = lambda: None
        plays = []
        sources = {"c-0001": cls.scenes[0], "c-0002": cls.scenes[1], "c-0003": cls.image, "c-0004": artwork(99)}
        for k, (cid, src) in enumerate(sources.items()):
            for n in range(5):
                eid = f"e{k}{n}"
                photo(src, k * 10 + n, night=n % 2).save(os.path.join(out, "images", f"{eid}.roi.jpg"))
                plays.append((1000 + k * 100 + n, cid, False, cid in ("c-0001", "c-0002"), eid, True, True))
        rx.site_plays = lambda sid, t0, t1: plays

    def test_frames_taken(self):
        self.assertGreaterEqual(self.nframes, 10)            # 5 s at one every 0.5 s

    def test_scenes_match_the_video_and_others_dont(self):
        res = rx.artwork_matches({"id": "BEACH-01", "vault_screen": "Beach"}, 0, 2000)
        for cid in ("c-0001", "c-0002"):
            self.assertEqual((res[cid]["status"], res[cid]["campaign_id"], res[cid]["kind"]),
                             ("matched", "LUMO_1", "video"), res[cid])
            self.assertTrue(res[cid]["frame"].startswith("beach/frames/LUMO_1/spot.mp4/"))
        # the two scenes are different frames of the spot
        self.assertNotEqual(res["c-0001"]["frame"], res["c-0002"]["frame"])
        self.assertEqual((res["c-0003"]["status"], res["c-0003"]["campaign_id"]), ("matched", "LUMO_2"))
        self.assertNotEqual(res["c-0004"]["status"], "matched", res["c-0004"])     # something else entirely
        self.assertLess(res["c-0004"]["score"], rx.ART_MATCH_VIDEO)

    def test_programmatic_not_blocked_by_a_video_with_frames(self):
        ds = {"loop": {"programmatic": True},
              "bookings": [{"campaign_id": "LUMO_1", "start": "2026-10-12", "end": "2026-10-18",
                            "creatives": [{"type": "video"}]}]}
        from datetime import date
        crs = [{"creative_id": "c-0004", "blank": False, "plays": 5, "video": True}]
        art = {"c-0004": {"status": "none", "samples": 5}}
        got, why = rx.infer_programmatic(ds, date(2026, 10, 12), crs, {"c-0004": "unassigned"}, art, self.lib)
        self.assertEqual((got, why), ({"c-0004"}, ""))
        lib = [dict(self.lib[0], frames=[]), self.lib[1]]                     # frames failed: still blocked
        got, why = rx.infer_programmatic(ds, date(2026, 10, 12), crs, {"c-0004": "unassigned"}, art, lib)
        self.assertEqual(got, set())


if __name__ == "__main__":
    unittest.main()
