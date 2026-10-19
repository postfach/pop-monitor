"""Backups: creatives-sync's part of one, and the nightly copy kept in SharePoint."""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import backups  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")
except Exception:  # pragma: no cover
    NZ = None


class State(unittest.TestCase):
    def test_state_and_restore(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "pop-uploads.json"), "w") as f:
            f.write('{"items": {"a": 1}}')
        st = backups.state(d)
        self.assertEqual(list(st["files"]), ["pop-uploads.json"])
        with open(os.path.join(d, "pop-uploads.json"), "w") as f:
            f.write('{"items": {}}')
        self.assertEqual(backups.restore(d, st), 1)
        with open(os.path.join(d, "pop-uploads.json")) as f:
            self.assertEqual(json.load(f), {"items": {"a": 1}})
        with open(os.path.join(d, "before-restore", "pop-uploads.json")) as f:
            self.assertEqual(json.load(f), {"items": {}})               # what it replaced is kept
        with self.assertRaises(ValueError):
            backups.restore(d, {"format": "pop-sync/state", "files": {"../../etc/x": "{}"}})
        with self.assertRaises(ValueError):
            backups.restore(d, {"format": "something else", "files": {}})


class FakeGraph:
    """One drive with a folder of backups; upload sessions and downloads by URL."""

    def __init__(self):
        self.files, self.n, self.sessions = {}, 0, {}

    class R:
        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def get(self, url, **kw):
        if url.startswith("/sites/"):
            return {"id": "site1"}
        raise AssertionError(url)

    def pages(self, url, **kw):
        if url.endswith("/drives"):
            return [{"id": "drv", "name": "IT", "webUrl": "https://sp/sites/LUMO/IT"}]
        if url.endswith("/children"):
            return [{"id": i, "name": f["name"], "size": len(f["data"]), "file": {}} for i, f in self.files.items()]
        raise AssertionError(url)

    def request(self, method, url, **kw):
        if method == "GET" and "root:/" in url:
            return self.R({"id": "folder1"})
        if method == "POST" and url.endswith(":/createUploadSession"):
            name = url.rsplit(":/createUploadSession", 1)[0].rsplit(":/", 1)[1]
            self.sessions["https://upload/1"] = name
            return self.R({"uploadUrl": "https://upload/1"})
        if method == "DELETE":
            self.files.pop(url.rsplit("/", 1)[1])
            return self.R({})
        if method == "GET" and "/items/" in url:
            iid = url.rsplit("/", 1)[1]
            return self.R({"id": iid, "name": self.files[iid]["name"], "@microsoft.graph.downloadUrl": f"https://dl/{iid}"})
        raise AssertionError((method, url))


class Nightly(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.g = FakeGraph()
        for k in range(15):                                     # 15 older backups already up there
            self.g.files[f"old{k}"] = {"name": f"pop-monitor-backup-2026-10-{k + 1:02d}_0240.tar.gz", "data": b"x"}
        self.notes = []

        def atomic(p, o):
            with open(p, "w") as f:
                json.dump(o, f)
        self.b = backups.Backups(lambda: self.g, self.d, "http://recv", "k", NZ, lambda *a: None,
                                 lambda *a, **k: self.notes.append(k.get("title")), atomic,
                                 lambda p, d=None: d, lambda p: p)
        self.uploaded = bytearray()

    def fake_requests(self, data, status=200):
        test = self

        class Resp:
            def __init__(self, url, **kw):
                self.status_code = status
                self.url = url
                self.headers = {"Content-Disposition": 'attachment; filename="pop-monitor-backup-2026-10-20_0240.tar.gz"'}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, n):
                body = data if "recv" in self.url else test.g.files[self.url.rsplit("/", 1)[1]]["data"]
                return [body[i:i + n] for i in range(0, len(body), n)]

            def json(self):
                return {"error": "the gateway is down"}

            def raise_for_status(self):
                pass

        def put(url, data=None, headers=None, timeout=None):
            test.uploaded.extend(data)
            name = test.g.sessions[url]
            rng = headers["Content-Range"]
            end, total = (int(x) for x in rng.split(" ")[1].split("-")[1].split("/"))
            if end + 1 == total:
                test.g.n += 1
                test.g.files[f"new{test.g.n}"] = {"name": name, "data": bytes(test.uploaded)}
            r = mock.Mock(raise_for_status=lambda: None)
            r.json = lambda: {"id": f"new{test.g.n}", "webUrl": f"https://sp/{name}"}
            return r
        return mock.patch.multiple(backups.requests, get=lambda url, **kw: Resp(url, **kw), put=put)

    def test_upload_and_keep_the_newest(self):
        data = os.urandom(backups.CHUNK * 2 + 1234)            # three chunks
        with self.fake_requests(data):
            self.b.run()
        self.assertTrue(self.b.st["ok"], self.b.st)
        names = sorted(f["name"] for f in self.g.files.values())
        self.assertEqual(len(names), backups.KEEP)
        self.assertEqual(names[-1], "pop-monitor-backup-2026-10-20_0240.tar.gz")
        new = next(f for f in self.g.files.values() if f["name"].endswith("10-20_0240.tar.gz"))
        self.assertEqual(new["data"], data)
        self.assertFalse(os.path.exists(os.path.join(self.d, "backup.part")))
        # and one can be fetched back to restore
        out = io.BytesIO()
        iid = next(i for i, f in self.g.files.items() if f["name"].endswith("10-20_0240.tar.gz"))
        with self.fake_requests(b""):
            self.b.download(iid, out)
        self.assertEqual(out.getvalue(), data)
        self.assertEqual(self.b.items()[0]["name"], "pop-monitor-backup-2026-10-20_0240.tar.gz")

    def test_failure_is_reported_once(self):
        with self.fake_requests(b"", status=502):
            self.b.run()
            self.b.run()
        self.assertFalse(self.b.st["ok"])
        self.assertIn("gateway is down", self.b.st["error"])
        self.assertEqual(self.notes, ["Warning: PoP Monitor backup failed"])

    def test_due(self):
        self.b.st = {"last_try": datetime(2026, 10, 19, 2, 41, tzinfo=NZ).timestamp(), "ok": True}
        self.assertFalse(self.b.due(datetime(2026, 10, 19, 23, 0, tzinfo=NZ)))
        self.assertTrue(self.b.due(datetime(2026, 10, 20, 2, 41, tzinfo=NZ)))


if __name__ == "__main__":
    unittest.main()
