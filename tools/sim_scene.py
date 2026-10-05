"""Synthetic billboard scene shared by make_test_video.py and fake_camera.py.

render(t, w, h) returns an RGB uint8 frame of a street scene with a digital
billboard seen at an angle. The rotation is 4 slots x 8 s:

  A static, B static (enters with a 0.8 s crossfade), C static with a small
  animated element, D full-motion video creative.

Fault injections (seconds from start):
  96-160 s  slot B is replaced by black (blank slot / missing creative)
  160-176 s screen black (black fault)
  176-   s  normal rotation again (recovery)
"""
import numpy as np

import os

SLOT = 8.0
ROT = ["A", "B", "C", "D"]
# Share-of-time variant for bookings tests: SIM_ROTATION=A,A,B,C,D,B gives a
# 6-slot loop where A holds two slots back to back and B two slots spread out.
# Custom rotations have no fault injections. An entry can also be the path of an
# image file, shown as that real creative (artwork-matching tests).
CUSTOM = bool(os.environ.get("SIM_ROTATION"))
if CUSTOM:
    ROT = [x.strip() for x in os.environ["SIM_ROTATION"].split(",") if x.strip()]
# normalised board corners: TL, TR, BR, BL (also used in the gateway config)
QUAD = [(0.18, 0.16), (0.84, 0.22), (0.82, 0.80), (0.19, 0.86)]
DURATION = 200.0

_maps = {}


def _homography(quad, w, h):
    """unit square (u,v) -> pixel; returns inverse (pixel -> u,v)."""
    src = [(0, 0), (1, 0), (1, 1), (0, 1)]
    A, b = [], []
    for (u, v), (x, y) in zip(src, quad):
        X, Y = x * (w - 1), y * (h - 1)
        A.append([u, v, 1, 0, 0, 0, -u * X, -v * X]); b.append(X)
        A.append([0, 0, 0, u, v, 1, -u * Y, -v * Y]); b.append(Y)
    hv = np.linalg.solve(np.array(A, float), np.array(b, float))
    H = np.append(hv, 1).reshape(3, 3)
    return np.linalg.inv(H)


def _uv(w, h):
    key = (w, h)
    if key not in _maps:
        Hi = _homography(QUAD, w, h)
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
        d = Hi[2, 0] * xs + Hi[2, 1] * ys + Hi[2, 2]
        u = (Hi[0, 0] * xs + Hi[0, 1] * ys + Hi[0, 2]) / d
        v = (Hi[1, 0] * xs + Hi[1, 1] * ys + Hi[1, 2]) / d
        inside = (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
        # street background: gradient sky + road, static
        bg = np.zeros((h, w, 3), np.float32)
        bg[..., 0] = 70 + 40 * (ys / h)
        bg[..., 1] = 80 + 30 * (ys / h)
        bg[..., 2] = 95 - 20 * (ys / h)
        _maps[key] = (u.astype(np.float32), v.astype(np.float32), inside, bg)
    return _maps[key]


_imgs = {}


def _artwork(path, u, v):
    """A real creative (image file) on the board, as a camera sees it: a little
    soft, with a warm cast and some sun glare towards the top left."""
    if path not in _imgs:
        from PIL import Image
        with Image.open(path) as im:
            _imgs[path] = np.asarray(im.convert("RGB"), np.float32)
    a = _imgs[path]
    h, w = a.shape[:2]
    img = a[np.clip((v * h).astype(int), 0, h - 1), np.clip((u * w).astype(int), 0, w - 1)]
    img = img * np.array([1.05, 0.98, 0.9], np.float32) * 0.85
    glare = 60 * np.exp(-((u - 0.2) ** 2 + (v - 0.15) ** 2) / 0.08)
    return img + glare[..., None]


def _creative(name, u, v, t):
    if "/" in name or name.lower().endswith((".png", ".jpg", ".jpeg")):
        return _artwork(name, u, v)
    seed = {"A": 11, "B": 23, "C": 37, "D": 51, "E": 67, "F": 79}[name]
    rng = np.random.RandomState(seed)
    pal = rng.randint(30, 230, size=(4, 8, 3)).astype(np.float32)
    bx = np.clip((u * 8).astype(int), 0, 7)
    by = np.clip((v * 4).astype(int), 0, 3)
    img = pal[by, bx]
    freq = 6 + seed % 9
    img = img + 35 * np.sin((u * freq + v * (seed % 5 + 2)) * 6.283)[..., None]
    # "headline" bar
    bar = (v > 0.12) & (v < 0.22) & (u > 0.08) & (u < 0.75)
    img[bar] = [245, 245, 240]
    if name == "C":   # small animated element: a pulsing badge
        r = 0.08 + 0.02 * np.sin(t * 5)
        badge = (u - 0.85) ** 2 + ((v - 0.7) * 0.5) ** 2 < (r * 0.5) ** 2
        img[badge] = [255, 210, 0]
    if name == "D":   # full-motion video
        m = 70 * np.sin(t * 4.0 + bx * 1.3 + by * 2.1)
        img = img + np.stack([m, -m, m * 0.5], -1)
    return img


def scene_at(t):
    """(current creative or None for black, previous creative, crossfade weight)."""
    if CUSTOM:
        k = int(t // SLOT) % len(ROT)
        return ROT[k], None, 1.0
    if 160.0 <= t < 176.0:
        return None, None, 0.0
    k = int(t // SLOT) % len(ROT)
    name = ROT[k]
    if 96.0 <= t < 160.0 and name == "B":
        return None, None, 0.0
    prev = ROT[(k - 1) % len(ROT)]
    local = t % SLOT
    if name == "B" and local < 0.8 and not (96.0 <= t < 160.0):
        return name, prev, local / 0.8
    return name, None, 1.0


def render(t, w=1280, h=720, noise=True):
    u, v, inside, bg = _uv(w, h)
    frame = bg.copy()
    name, prev, mix = scene_at(t)
    local = t % SLOT                 # creatives play from their own start, like real ads
    if CUSTOM and name is not None:  # a creative holding slots back to back plays on
        k = int(t // SLOT) % len(ROT)
        while k > 0 and ROT[k - 1] == name:
            k -= 1
            local += SLOT
    if name is None:
        board = np.full(u.shape + (3,), 3.0, np.float32)
    else:
        board = _creative(name, u, v, local)
        if prev is not None:
            board = _creative(prev, u, v, SLOT) * (1 - mix) + board * mix
    frame[inside] = board[inside]
    if noise:
        frame += np.random.randint(-3, 4, size=frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def expected_plays(duration=DURATION):
    """Ground truth: list of (start_s, label) for each play after the first."""
    out, last = [], None
    t = 0.0
    while t < duration:
        name, _, _ = scene_at(t + 1.0)          # sample mid-slot
        label = name or "black"
        if label != last and t > 0:
            out.append((t, label))
        last = label
        t += SLOT
    return out
