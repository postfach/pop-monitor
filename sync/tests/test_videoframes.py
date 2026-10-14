import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import videoframes  # noqa: E402
import creatives_sync as cs  # noqa: E402


def make_video(path, seconds=3):
    subprocess.run([videoframes.ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=576x396:rate=25:duration={seconds}", "-pix_fmt", "yuv420p", path], check=True)


@unittest.skipIf(not videoframes.ffmpeg_exe(), "no ffmpeg")
class VideoFrames(unittest.TestCase):
    def test_extract_and_materialise(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "spot.mp4")
        make_video(src)
        with open(src, "rb") as f:
            data = f.read()

        class Lib:
            downloads = 0

            def download(self, item_id):
                Lib.downloads += 1
                return data
        cs.OUT, cs.STATE = os.path.join(tmp, "out"), os.path.join(tmp, "out", ".sync")
        os.makedirs(cs.STATE)
        item = {"id": "i1", "etag": "e1", "name": "Spot 576x396.mp4"}
        c = {"campaign_id": "LUMO_1", "screen": "Beach", "kind": "video", "_item": item}
        s = cs.Syncer.__new__(cs.Syncer)
        got = s.materialise(Lib(), [c])
        self.assertEqual(got, (1, 1))
        self.assertEqual(len(c["frames"]), 6)                     # 3 s, one every 0.5 s
        self.assertEqual(c["file"], c["frames"][0])
        self.assertTrue(all(os.path.isfile(os.path.join(cs.OUT, f)) for f in c["frames"]))
        c2 = dict(c)
        self.assertEqual(s.materialise(Lib(), [c2]), (0, 1))      # unchanged: no download, no re-extract
        self.assertEqual(Lib.downloads, 1)
        s.prune({"creatives": [c2]})
        self.assertTrue(os.path.isfile(os.path.join(cs.OUT, c2["frames"][-1])))   # frames kept
        s.prune({"creatives": []})
        self.assertFalse(os.path.exists(os.path.join(cs.OUT, c2["frames"][0])))

    def test_bad_video(self):
        tmp = tempfile.mkdtemp()
        bad = os.path.join(tmp, "bad.mp4")
        with open(bad, "wb") as f:
            f.write(b"not a video")
        with self.assertRaises(RuntimeError):
            videoframes.extract(bad, os.path.join(tmp, "f"))
        self.assertFalse(os.path.exists(os.path.join(tmp, "f.part")))


if __name__ == "__main__":
    unittest.main()
