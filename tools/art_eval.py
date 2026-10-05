"""Check how well artwork matching separates creatives, using a creatives library.

  python3 tools/art_eval.py path/to/creatives        (folder with vault-creatives.json)

Each booked image is put through camera-like damage (edge crop, misalignment, glare,
vignetting, exposure and colour cast, blur, noise, JPEG) and matched against every
image for the same screen with the receiver's own fingerprint. Prints, per screen, how
often the right campaign comes first, the lowest score of the right artwork and the
highest score of any other campaign (the receiver's thresholds are 0.40 and 0.08 apart).
"""
import io
import json
import os
import random
import sys
import types

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import receiver as R  # noqa: E402

R.ARGS = types.SimpleNamespace(out="/nonexistent")


def damage(img, seed, inset=0.03):
    r = random.Random(seed)
    w, h = img.size
    j = 0.01
    box = (w * (inset + r.uniform(-j, j)), h * (inset + r.uniform(-j, j)),
           w * (1 - inset - r.uniform(-j, j)), h * (1 - inset - r.uniform(-j, j)))
    im = img.convert("RGB").crop(box).resize((480, int(480 * h / w)))
    a = np.asarray(im, dtype=float)
    H, W = a.shape[:2]
    yy, xx = np.mgrid[0:H, 0:W]
    cx, cy = r.uniform(0, W), r.uniform(0, H)
    glare = r.uniform(0, 60) * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * (r.uniform(.2, .5) * W) ** 2))
    vign = 1 - r.uniform(0, .3) * (((xx - W / 2) / W) ** 2 + ((yy - H / 2) / H) ** 2) * 2
    a = a * r.uniform(.6, 1.2) * np.array([r.uniform(.85, 1.15) for _ in range(3)])
    a = a * vign[..., None] + glare[..., None] + np.random.default_rng(seed).normal(0, 5, a.shape)
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r.uniform(.5, 2)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=r.randint(50, 85))
    return Image.open(b)


def main(root):
    man = json.load(open(os.path.join(root, "vault-creatives.json")))
    by = {}
    for c in man["creatives"]:
        if c.get("file"):
            by.setdefault(c["screen"], []).append(c)
    for screen, cs in by.items():
        refs = [(c, R._prep(Image.open(os.path.join(root, c["file"])), 0.03)) for c in cs]
        right = n = 0
        own, other = [], []
        for i, (c, _) in enumerate(refs):
            for s in range(3):
                f = R._prep(damage(Image.open(os.path.join(root, c["file"])), s * 100 + i))
                sc = sorted(((R._shift_corr(f, rf), rc) for rc, rf in refs), key=lambda x: -x[0])
                n += 1
                right += sc[0][1]["campaign_id"] == c["campaign_id"]
                own.append(max(v for v, rc in sc if rc is c))
                other.append(max([v for v, rc in sc if rc["campaign_id"] != c["campaign_id"]] or [0]))
        print(f"{screen}: {len(refs)} images, right campaign first {right}/{n}; "
              f"right artwork lowest {min(own):.2f}; other campaigns highest {max(other):.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "creatives")
