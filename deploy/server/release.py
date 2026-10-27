#!/usr/bin/env python3
"""Release helper for the GitHub Actions workflow (.github/workflows/release.yml).

Each image is tagged with a hash of the files it is built from, so an image is only
rebuilt (and its container only restarted on the server) when its own code changed:

    plan     which images need building: prints "<service>=build|keep" and
             "<service>_image=ghcr.io/<owner>/pop-<service>:src-<hash>" lines (for $GITHUB_OUTPUT)
    render   writes the server's compose.yml with those exact images into a folder,
             with versions.json beside it

The workflow then commits that folder to the `deploy` branch, which the Portainer stack
follows: a new commit there means every image it names has been built, tested and
published, so the server never pulls an image that isn't there yet.

Run from the repository root:  python3 deploy/server/release.py plan --owner postfach
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

# what each image is built from (paths in the repo); a change to any of them rebuilds it
SERVICES = {
    "gateway": {"dockerfile": "gateway/Dockerfile",
                "inputs": ["CMakeLists.txt", "core", "gateway", "tools/popreplay.c"]},
    "receiver": {"dockerfile": "deploy/receiver.Dockerfile",
                 "inputs": ["deploy/receiver.Dockerfile", "tools/receiver.py", "tools/popscore.py",
                            "tools/store.py", "tools/backup.py", "tools/static"]},
    "creatives-sync": {"dockerfile": "sync/Dockerfile", "inputs": ["sync"]},
}
TEMPLATE = "deploy/server/compose.yml"


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else None


def source_tag(service, rev="HEAD"):
    """src-<12 hex>: a hash of the git objects of the image's inputs at `rev`."""
    h = hashlib.sha1()
    for p in SERVICES[service]["inputs"]:
        obj = git("rev-parse", f"{rev}:{p}", check=False)
        h.update(f"{p}={obj or '-'}\n".encode())
    return "src-" + h.hexdigest()[:12]


def image(owner, service, tag):
    return f"ghcr.io/{owner.lower()}/pop-{service}:{tag}"


def deployed(branch):
    """versions.json on the deploy branch (what the server runs now), or {}."""
    txt = git("show", f"{branch}:versions.json", check=False)
    try:
        return json.loads(txt or "{}").get("images", {})
    except ValueError:
        return {}


def plan(owner, branch, force=False):
    now = deployed(branch)
    out = {}
    for s in SERVICES:
        img = image(owner, s, source_tag(s))
        out[s] = {"image": img, "build": force or now.get(s) != img}
    return out


def render(template, images):
    """The compose file with each ${POP_<SERVICE>_IMAGE:-...} replaced by its exact image."""
    def sub(m):
        s = m.group(1).lower().replace("_", "-")
        if s not in images:
            raise SystemExit(f"{TEMPLATE}: no image for {m.group(0)}")
        return images[s]
    txt, n = re.subn(r"\$\{POP_([A-Z_]+)_IMAGE:-[^}]*\}", sub, template)
    if n != len(images):
        raise SystemExit(f"{TEMPLATE}: expected {len(images)} image lines, found {n}")
    txt = re.sub(r"\A(?:#[^\n]*\n)+", "", txt)              # the template's own header
    return ("# Made by the release workflow from deploy/server/compose.yml on main: do not edit here.\n"
            "# The Portainer stack follows this branch; see deploy/server/README.md.\n" + txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "render"])
    ap.add_argument("--owner", required=True)
    ap.add_argument("--branch", default="origin/deploy", help="the deploy branch as git can see it")
    ap.add_argument("--force", action="store_true", help="build every image")
    ap.add_argument("--out", help="render: folder to write compose.yml and versions.json into")
    a = ap.parse_args()
    p = plan(a.owner, a.branch, a.force)
    if a.cmd == "plan":
        for s, v in p.items():
            key = s.replace("-", "_")
            print(f"{key}={'build' if v['build'] else 'keep'}")
            print(f"{key}_image={v['image']}")
        print("any=" + ("true" if any(v["build"] for v in p.values()) else "false"))
        return
    images = {s: v["image"] for s, v in p.items()}
    with open(TEMPLATE, encoding="utf-8") as f:
        compose = render(f.read(), images)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "compose.yml"), "w", encoding="utf-8") as f:
        f.write(compose)
    with open(os.path.join(a.out, "versions.json"), "w", encoding="utf-8") as f:
        json.dump({"source_commit": git("rev-parse", "HEAD"), "images": images}, f, indent=1)
        f.write("\n")
    print(json.dumps(images, indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()
