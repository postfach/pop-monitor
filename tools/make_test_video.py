#!/usr/bin/env python3
"""Render the synthetic billboard scene to an H.264 file for testing.

  python3 tools/make_test_video.py out.mp4 [--seconds 200] [--fps 10] [--size 1280x720]
"""
import argparse
import subprocess
import sys

import sim_scene


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=sim_scene.DURATION)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--size", default="1280x720")
    a = ap.parse_args()
    w, h = map(int, a.size.split("x"))
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", str(a.fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-g", str(a.fps * 2), a.out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = int(a.seconds * a.fps)
    for i in range(n):
        p.stdin.write(sim_scene.render(i / a.fps, w, h).tobytes())
        if i % (a.fps * 20) == 0:
            print(f"  {i / a.fps:5.0f} s", file=sys.stderr)
    p.stdin.close()
    sys.exit(p.wait())


if __name__ == "__main__":
    main()
