"""Keeps the server's Portainer stack on the newest release (runs as the stack's `updater`).

Portainer Community Edition redeploys a Git stack when its webhook is called, but doesn't
watch the repository itself. This checks the `deploy` branch on GitHub every few minutes
and, when it names a newer release than the one this stack is running, calls the webhook:
Portainer then pulls the branch and the new images and restarts what changed.

The release this stack is running comes in DEPLOYED_COMMIT, which the release workflow
writes into the stack's compose file, so a redeploy restarts this with the new value.

Environment:
  PORTAINER_WEBHOOK_URL   the stack's webhook (Stacks > pop-monitor > Edit stack settings > Copy link)
  GITHUB_REPO             owner/name (default postfach/pop-monitor)
  GITHUB_TOKEN            only for a private repository: a token that can read it
  UPDATE_EVERY_MIN        how often to check (default 5)
  PORTAINER_TLS_VERIFY    1 to check Portainer's certificate (default 0: it is usually self-signed)
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPO", "postfach/pop-monitor")
BRANCH = os.environ.get("DEPLOY_BRANCH", "deploy")
EVERY = max(1.0, float(os.environ.get("UPDATE_EVERY_MIN", "5"))) * 60
RETRY_AFTER = 20 * 60          # the same release isn't asked for again sooner than this


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "updater:", *a, flush=True)


def latest_release(repo=REPO, branch=BRANCH, token=None, opener=urllib.request.urlopen):
    """The source commit of the newest release on the deploy branch (its versions.json)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/versions.json?ref={branch}",
                                 headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "pop-monitor-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with opener(req, timeout=30) as r:
        return json.loads(r.read())["source_commit"]


def call_webhook(url, verify=False, opener=urllib.request.urlopen):
    ctx = None
    if url.startswith("https") and not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=b"", method="POST")
    with opener(req, timeout=120, context=ctx) if ctx else opener(req, timeout=120) as r:
        return r.status


class Updater:
    def __init__(self, deployed, webhook, token=None, verify=False, latest=latest_release, hook=call_webhook,
                 clock=time.time):
        self.deployed, self.webhook, self.token, self.verify = deployed, webhook, token, verify
        self.latest, self.hook, self.clock = latest, hook, clock
        self.asked = {}            # release -> when its redeploy was asked for

    def check(self):
        """One check. Returns what happened: 'current', 'asked', 'waiting', 'off' or 'error: ...'."""
        if not self.webhook:
            return "off"
        try:
            new = self.latest(token=self.token)
        except Exception as e:
            return f"error: can't read the deploy branch on GitHub: {e}"
        if not self.deployed or new == self.deployed:
            return "current"
        now = self.clock()
        if new in self.asked and now - self.asked[new] < RETRY_AFTER:
            return "waiting"
        self.asked[new] = now
        try:
            code = self.hook(self.webhook, verify=self.verify)
        except urllib.error.HTTPError as e:
            return f"error: Portainer refused the webhook ({e.code}): check the URL in PORTAINER_WEBHOOK_URL"
        except Exception as e:
            return f"error: can't reach Portainer's webhook: {e}"
        log(f"release {new[:7]} is out (running {self.deployed[:7]}): asked Portainer to redeploy ({code})")
        return "asked"


def main():
    deployed = os.environ.get("DEPLOYED_COMMIT", "")
    webhook = os.environ.get("PORTAINER_WEBHOOK_URL", "").strip()
    u = Updater(deployed if len(deployed) >= 7 else "", webhook, os.environ.get("GITHUB_TOKEN") or None,
                os.environ.get("PORTAINER_TLS_VERIFY", "0") == "1")
    if not webhook:
        log("PORTAINER_WEBHOOK_URL isn't set: not updating automatically (deploy/server/README.md)")
    elif not u.deployed:
        log("this stack wasn't deployed from the deploy branch (no DEPLOYED_COMMIT): not updating automatically")
    else:
        log(f"running release {deployed[:7]}; checking {REPO} {BRANCH} every {EVERY / 60:g} min")
    last = None
    while True:
        r = u.check() if u.deployed else "off"
        if r != last and r.startswith("error"):
            log(r)
        last = r
        time.sleep(EVERY)


if __name__ == "__main__":
    sys.exit(main())
