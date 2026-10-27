"""The stack's updater:  python3 tools/test_updater.py"""
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater  # noqa: E402


class Updater(unittest.TestCase):
    def make(self, deployed="aaaaaaa1", latest="aaaaaaa1"):
        self.now, self.calls, self.latest = 1000.0, [], latest
        return updater.Updater(deployed, "https://192.168.0.132:9443/api/stacks/webhooks/x",
                               latest=lambda token=None: self.latest,
                               hook=lambda url, verify=False: self.calls.append(url) or 204,
                               clock=lambda: self.now)

    def test_redeploys_once_per_release(self):
        u = self.make()
        self.assertEqual(u.check(), "current")
        self.latest = "bbbbbbb2"                              # a new release on the deploy branch
        self.assertEqual(u.check(), "asked")
        self.assertEqual(len(self.calls), 1)
        self.now += 300
        self.assertEqual(u.check(), "waiting")               # Portainer is on it: not asked again at once
        self.now += updater.RETRY_AFTER
        self.assertEqual(u.check(), "asked")                 # still not running it 20 min later: ask again
        self.assertEqual(len(self.calls), 2)

    def test_off_and_errors(self):
        u = self.make(latest="bbbbbbb2")
        u.webhook = ""
        self.assertEqual(u.check(), "off")
        u = self.make(latest="bbbbbbb2")

        def boom(token=None):
            raise OSError("no network")
        u.latest = boom
        self.assertTrue(u.check().startswith("error: can't read"))
        u = self.make(latest="bbbbbbb2")

        def refused(url, verify=False):
            raise urllib.error.HTTPError(url, 404, "nope", None, None)
        u.hook = refused
        self.assertIn("refused the webhook (404)", u.check())

    def test_compose_has_the_release(self):
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, os.path.join(here, "..", "deploy", "server"))
        import release
        with open(os.path.join(here, "..", "deploy", "server", "compose.yml")) as f:
            txt = release.render(f.read(), {"gateway": "g:1", "receiver": "r:1", "creatives-sync": "c:1"}, "abc1234def")
        self.assertIn("DEPLOYED_COMMIT: abc1234def", txt)
        self.assertEqual(txt.count("image: r:1"), 2)          # the receiver and the updater
        self.assertNotIn("${POP_", txt.replace("${POP_API", "").replace("${POP_HMAC", "").replace("${POP_CAMERA", "")
                         .replace("${POP_TEAMS", "").replace("${POP_SLACK", "").replace("${POP_HOME", "")
                         .replace("${POP_VOLUME", "").replace("${POP_REVIEW", "").replace("${POP_IMAGE", "")
                         .replace("${POP_ALERT", "").replace("${POP_RECORD", "").replace("${POP_DEFAULT", "")
                         .replace("${POP_SITE", "").replace("${POP_UPLOAD", "").replace("${POP_ROOT", "")
                         .replace("${POP_DAY", "").replace("${POP_NIGHT", "").replace("_IMAGE_DAYS", ""))


if __name__ == "__main__":
    unittest.main()
