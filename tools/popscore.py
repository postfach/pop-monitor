"""PoP image quality: how good a proof-of-play photo is of the creative on screen.

score(roi, ref) -> {"score": 0-100, ...parts}

The photo is the camera's crop of the screen (the region of interest). What makes
one PoP better than another of the same creative:

  clarity   fine detail (text, edges, layout) agrees with the reference: the
            booked artwork, or the typical look of this creative in its other
            PoP images. Glare, sunstrike, rain on the lens, haze and anything in
            front of the screen all wipe out or distort detail.
  coverage  the worst part of the screen still agrees (4 x 2 tiles): catches a
            local hot spot, a raindrop or a truck covering one corner, which an
            overall figure would average away.
  layout    the coarse light and dark areas agree: a sun reflection or veiling
            glare across the face changes these even when edges survive.
  sharpness Laplacian energy, against the median of this creative's photos in
            the same light (rain, fog, focus, motion).
  contrast  2nd to 98th percentile spread, against the same median (washout).
  clipping  share of blown-out pixels (sunstrike, night bloom).

Scores are only compared between photos of the same creative in the same light
(day with day, night with night), so the parts are relative where they can be.
Without numpy/Pillow nothing here runs (the receiver checks ART_OK first).
"""
import math

import numpy as np
from PIL import Image, ImageFilter

GW, GH = 96, 48            # detail grid (finer than the matcher's 48 x 32: rain, small text)
TW, TH = 4, 2              # coverage tiles
LW, LH = 12, 6             # coarse layout grid
SW, SH = 256, 128          # sharpness / contrast / clipping grid

WEIGHTS = {"clarity": 0.32, "coverage": 0.18, "layout": 0.14, "sharpness": 0.14, "contrast": 0.10, "clipping": 0.12}
VIDEO_WEIGHTS = {"sharpness": 0.4, "contrast": 0.3, "clipping": 0.3}   # frames of a video differ: no reference


def _hp(t, k=3):
    p = np.pad(t, k, mode="edge")
    c = np.cumsum(np.cumsum(p, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    n = 2 * k + 1
    box = c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]
    return t - box / (n * n)


def prep(img, inset=0.0):
    """Arrays for one image (PIL): detail map, coarse layout, and the photo stats."""
    img = img.convert("RGB")
    if inset > 0:
        w, h = img.size
        img = img.crop((w * inset, h * inset, w * (1 - inset), h * (1 - inset)))
    g = img.convert("L")
    tall = g.height > g.width * 1.15               # portrait boards: same grids, turned
    gw, gh, lw, lh, sw, sh = (GH, GW, LH, LW, SH, SW) if tall else (GW, GH, LW, LH, SW, SH)
    big = g.resize((gw * 4, gh * 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.0))
    detail = _hp(np.asarray(big.resize((gw, gh), Image.BOX), dtype=float))
    layout = np.asarray(g.resize((lw, lh), Image.BOX), dtype=float)
    s = np.asarray(g.resize((sw, sh), Image.BILINEAR), dtype=float)
    lap = 4 * s[1:-1, 1:-1] - s[:-2, 1:-1] - s[2:, 1:-1] - s[1:-1, :-2] - s[1:-1, 2:]
    full = np.asarray(g, dtype=np.uint8)
    p2, p98 = np.percentile(s, (2, 98))
    return {"detail": detail, "layout": layout,
            "sharp": float(lap.var()), "contrast": float(p98 - p2),
            "clipped": float((full >= 250).mean()), "mean": float(s.mean())}


def _corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum()) / d if d > 1e-9 else 0.0


def _aligned(a, b, r=3):
    """Best overall correlation over small offsets, and the overlapping parts at that offset."""
    H, W = a.shape
    best = (-2.0, a, b)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            A = a[max(0, dy):H + min(0, dy), max(0, dx):W + min(0, dx)]
            B = b[max(0, -dy):H + min(0, -dy), max(0, -dx):W + min(0, -dx)]
            c = _corr(A, B)
            if c > best[0]:
                best = (c, A, B)
    return best


def _tiles(a, b, r=3):
    """Correlation of each tile, each aligned on its own (the crop is rarely
    exactly the reference's shape, so edges drift by a cell or two). Tiles that
    are nearly flat in the reference (plain background) give no evidence."""
    H, W = a.shape
    th, tw = (TW, TH) if H > W else (TH, TW)
    spans = [(ty * H // th, (ty + 1) * H // th, tx * W // tw, (tx + 1) * W // tw)
             for ty in range(th) for tx in range(tw)]
    energy = [float(b[y0:y1, x0:x1].std()) for y0, y1, x0, x1 in spans]
    floor = max(1.0, 0.5 * float(np.median(energy)))
    out = []
    for (y0, y1, x0, x1), e in zip(spans, energy):
        if e < floor:
            continue
        ref = b[y0:y1, x0:x1]
        best = -1.0
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                ys, xs = y0 + dy, x0 + dx
                if ys < 0 or xs < 0 or ys + (y1 - y0) > H or xs + (x1 - x0) > W:
                    continue
                best = max(best, _corr(a[ys:ys + (y1 - y0), xs:xs + (x1 - x0)], ref))
        out.append(best)
    return out


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def consensus(preps):
    """Reference from a creative's own photos (no booked artwork): the
    pixel-wise median, which the odd glare or raindrop doesn't survive."""
    shapes = [p["detail"].shape for p in preps]
    common = max(set(shapes), key=shapes.count)
    preps = [p for p in preps if p["detail"].shape == common]
    return {"detail": np.median(np.stack([p["detail"] for p in preps]), axis=0),
            "layout": np.median(np.stack([p["layout"] for p in preps]), axis=0)}


def norms(preps):
    """Typical sharpness and contrast of a creative's photos in one light."""
    sh = [p["sharp"] for p in preps]
    co = [p["contrast"] for p in preps]
    cl = [p["clipped"] for p in preps]
    return {"sharp": float(np.median(sh)) if sh else 0.0, "contrast": float(np.median(co)) if co else 0.0,
            "clipped": float(np.median(cl)) if cl else 0.0}


def score(p, ref=None, norm=None, video=False):
    """p: prep() of the photo; ref: prep()/consensus() of the reference or None;
    norm: norms() of comparable photos. Returns the score and its parts (0..1)."""
    norm = norm or {}
    parts = {}
    if ref is not None and ref["detail"].shape != p["detail"].shape:   # portrait vs landscape: not comparable
        ref = None
    rs = norm.get("sharp") or 0
    rc = norm.get("contrast") or 0
    parts["sharpness"] = clamp(p["sharp"] / (1.15 * rs)) if rs > 0 else clamp(math.log1p(p["sharp"]) / 7)
    parts["contrast"] = clamp(p["contrast"] / (1.1 * rc)) if rc > 0 else clamp(p["contrast"] / 160)
    # blown-out pixels beyond what is usual for this creative in this light (at
    # night some bloom is normal; a sunstrike adds a lot more)
    parts["clipping"] = clamp(1 - max(0.0, p["clipped"] - max(0.01, norm.get("clipped") or 0)) / 0.12)
    if not video and ref is not None:
        c, _, _ = _aligned(p["detail"], ref["detail"])
        parts["clarity"] = clamp((c - 0.10) / 0.70)
        t = _tiles(p["detail"], ref["detail"])
        worst = sum(sorted(t)[:2]) / len(sorted(t)[:2]) if t else None   # the two worst tiles
        parts["coverage"] = clamp(worst / 0.70) if t else parts["clarity"]
        parts["layout"] = clamp((_corr(p["layout"], ref["layout"]) - 0.30) / 0.65)
        w = WEIGHTS
        raw = {"clarity": round(c, 3), "worst_tiles": round(worst, 3) if t else None}
    else:
        w = VIDEO_WEIGHTS
        raw = {}
    s = sum(w[k] * parts[k] for k in w) / sum(w.values())
    # one bad aspect ruins a PoP however good the rest is: the weakest part caps the score
    weakest = min(parts[k] for k in w)
    s = min(s, 0.35 + 0.75 * weakest)
    return {"score": round(100 * s, 1), "parts": {k: round(v, 2) for k, v in parts.items()},
            "raw": dict(raw, sharp=round(p["sharp"], 1), contrast=round(p["contrast"], 1),
                        clipped=round(p["clipped"], 4))}


# ---------------------------------------------------------------- day or night

def sun_elevation(ts, lat, lon):
    """Solar elevation in degrees at unix time ts (NOAA approximation, ~0.5 deg)."""
    d = ts / 86400.0 + 2440587.5 - 2451545.0          # days since J2000
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    L = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    e = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(e) * math.sin(L), math.cos(L))
    dec = math.asin(math.sin(e) * math.sin(L))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians((gmst * 15 + lon) % 360) - ra
    la = math.radians(lat)
    s = math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


DAY_SUN, NIGHT_SUN = 4.0, -8.0      # degrees: twilight in between is neither (light changes by the minute)
DAY_LUMA, NIGHT_LUMA = 45.0, 75.0   # median brightness of the whole frame


def frame_luma(img):
    g = img.convert("L").resize((64, 36), Image.BOX)
    return float(np.median(np.asarray(g, dtype=float)))


def phase(ts, lat, lon, luma=None):
    """'day', 'night' or None (twilight, or the picture disagrees with the clock)."""
    el = sun_elevation(ts, lat, lon)
    if el >= DAY_SUN and (luma is None or luma >= DAY_LUMA):
        return "day"
    if el <= NIGHT_SUN and (luma is None or luma <= NIGHT_LUMA):
        return "night"
    return None
