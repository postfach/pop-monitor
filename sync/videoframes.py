"""Frames from a booked video creative, for matching PoP photos of video plays.

A PoP photo of a video shows one moment, and which one isn't known, so the
receiver compares it with frames taken every FRAME_EVERY_S seconds through the
video (best frame wins). Frames are small JPEGs (FRAME_WIDTH px wide).

ffmpeg comes from the system if installed, else from the imageio-ffmpeg wheel
(a static build, no apt packages needed in the container).
"""
import glob
import os
import shutil
import subprocess

FRAME_EVERY_S = float(os.environ.get("VIDEO_FRAME_EVERY_S", "0.5"))
FRAME_WIDTH = int(os.environ.get("VIDEO_FRAME_WIDTH", "640"))
MAX_FRAMES = int(os.environ.get("VIDEO_MAX_FRAMES", "90"))


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def extract(video_path, out_dir, every_s=None, max_frames=None):
    """Write out_dir/f0000.jpg ... (one every FRAME_EVERY_S s, or every_s). Returns the
    frame file names in order, or raises RuntimeError with ffmpeg's complaint."""
    every_s, max_frames = every_s or FRAME_EVERY_S, max_frames or MAX_FRAMES
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg is not available (pip install imageio-ffmpeg)")
    tmp = out_dir + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    vf = f"fps=1/{every_s:g},scale='min({FRAME_WIDTH},iw)':-2"
    cmd = [exe, "-v", "error", "-nostdin", "-i", video_path, "-vf", vf, "-frames:v", str(max_frames),
           "-q:v", "3", os.path.join(tmp, "f%04d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    frames = sorted(os.path.basename(p) for p in glob.glob(os.path.join(tmp, "f*.jpg")))
    if r.returncode != 0 or not frames:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"no frames from the video: {(r.stderr or '').strip()[-200:] or 'ffmpeg failed'}")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.replace(tmp, out_dir)
    return frames
