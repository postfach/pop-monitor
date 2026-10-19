"""PoP photos go in <year>/<MM - Month>/<campaign>/<site>/, and photos put up in the old
site-first folders are moved there once (same files), old site folders removed when empty."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pop_upload as pu  # noqa: E402

ROOT = pu.POP_ROOT


class Drive:
    """A folder tree with items by id; enough of Graph for uploads, moves and tidying."""

    def __init__(self):
        self.items, self.n, self.calls = {}, 0, []      # id -> {"path", "folder"}

    class R:
        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def _new(self, path, folder=False):
        self.n += 1
        iid = f"i{self.n}"
        self.items[iid] = {"path": path, "folder": folder}
        return iid

    def by_path(self, path):
        return next((i for i, v in self.items.items() if v["path"] == path), None)

    def children(self, path):
        return [v for v in self.items.values() if v["path"].rpartition("/")[0] == path]

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if method == "PUT":                              # upload by path: parents appear by themselves
            path = url.split("root:/", 1)[1].rsplit(":/content", 1)[0]
            iid = self.by_path(path) or self._new(path)
            return self.R({"id": iid, "webUrl": "https://x/" + path})
        if method == "GET" and "root:/" in url:
            path = url.split("root:/", 1)[1]
            iid = self.by_path(path)
            if not iid:
                raise RuntimeError("Graph 404 on x: itemNotFound")
            return self.R({"id": iid, "folder": {"childCount": len(self.children(path))}})
        if method == "POST" and url.endswith("/children"):
            parent = "" if "/root/children" in url else self.items[url.split("/items/")[1].rsplit("/children", 1)[0]]["path"]
            path = f"{parent}/{kw['json']['name']}".lstrip("/")
            if self.by_path(path):
                raise RuntimeError("Graph 409 nameAlreadyExists")
            return self.R({"id": self._new(path, True)})
        if method == "PATCH":
            iid = url.rsplit("/", 1)[1]
            if iid not in self.items:
                raise RuntimeError("Graph 404 on x: itemNotFound")
            name = self.items[iid]["path"].rsplit("/", 1)[1]
            self.items[iid]["path"] = self.items[kw["json"]["parentReference"]["id"]]["path"] + "/" + name
            return self.R({"id": iid, "webUrl": "https://x/" + self.items[iid]["path"]})
        if method == "DELETE":
            self.items.pop(url.rsplit("/", 1)[1], None)
            return self.R({})
        raise AssertionError((method, url))


class Lib:
    def __init__(self, g):
        self.drive = "d1"

    def connect(self):
        return self


def item(cid, site, eid, adv="ACT Party", screen="Albany Expressway In", score=90):
    return {"site_id": site, "site_name": site.lower(), "screen": screen, "campaign_id": cid,
            "campaign_name": f"{cid} - ACT Oct", "advertiser": adv, "version": "art:a.jpg", "version_label": "a.jpg",
            "event_id": eid, "taken_at": "2026-10-15T11:31:00+13:00", "first_seen": "2026-10-12", "score": score}


class Layout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.g = Drive()

        def atomic(path, obj):
            with open(path, "w") as f:
                json.dump(obj, f)

        def read(path, default=None):
            try:
                with open(path) as f:
                    return json.load(f)
            except OSError:
                return default
        self.atomic, self.read = atomic, read
        self.u = self.new()

    def new(self):
        u = pu.PopUploader(self.g, Lib, self.tmp, "http://recv", "k", None, lambda *a: None,
                           lambda *a, **k: None, self.atomic, self.read, lambda p: p)
        u._get = lambda path, **kw: mock.Mock(content=b"jpeg")
        return u

    def run_with(self, u, items):
        resp = mock.Mock(json=lambda: {"items": items}, raise_for_status=lambda: None)
        with mock.patch.object(pu.requests, "post", return_value=resp):
            return u.run("day", 1000, 2000)

    def test_campaign_first(self):
        self.assertEqual(pu.campaign_dir("LUMO_17107", "ACT Party"), "LUMO_17107 - ACT Party")
        self.assertEqual(pu.campaign_dir("LUMO_17107", "", "LUMO_17107 - ACT Party Oct"), "LUMO_17107 - ACT Party Oct")
        self.assertEqual(pu.campaign_dir("LUMO_17107"), "LUMO_17107")
        self.assertTrue(self.run_with(self.u, [item("LUMO_17107", "ALB", "e1")]))
        self.assertEqual([v["path"] for v in self.g.items.values() if not v["folder"]],
                         [f"{ROOT}/2026/10 - October/LUMO_17107 - ACT Party/Albany Expressway In/"
                          "LUMO_17107_ACT.Party_Albany-Expressway-In_2026-10-15_1131_Day_Score90_v1.jpg"])

    def test_old_photos_moved_once(self):
        old = f"{ROOT}/2026/10 - October/Albany Expressway In"
        a = self.g._new(f"{old}/LUMO_17107_ACT.Party_Albany-Expressway-In_2026-10-14_0900_Day_Score88_v1.jpg")
        b = self.g._new(f"{ROOT}/2026/10 - October/Beach/LUMO_9_Old.Co_Beach_2026-10-01_0900_Day_Score70_v1.jpg")
        self.g._new(old, True)
        self.g._new(f"{ROOT}/2026/10 - October/Beach", True)
        st = {"layout": None, "folders": {"LUMO_17107|ALB": "2026/10 - October", "LUMO_9|BEACH": "2026/10 - October"},
              "versions": {"LUMO_17107|ALB": {"art:a.jpg": 1}, "LUMO_9|BEACH": {"art:x.jpg": 1}},
              "items": {"LUMO_17107|ALB|art:a.jpg|day": {"path": self.g.items[a]["path"], "item_id": a, "score": 88,
                                                          "name": self.g.items[a]["path"].rsplit("/", 1)[1],
                                                          "event_id": "old", "taken_at": "2026-10-14T09:00:00+13:00"},
                        "LUMO_9|BEACH|art:x.jpg|day": {"path": self.g.items[b]["path"], "item_id": b, "score": 70,
                                                       "name": self.g.items[b]["path"].rsplit("/", 1)[1]}}}
        self.atomic(os.path.join(self.tmp, "pop-uploads.json"), st)
        u = self.new()
        self.assertTrue(self.run_with(u, [item("LUMO_17107", "ALB", "old", score=50)]))   # nothing better today
        self.assertEqual(self.g.items[a]["path"], f"{ROOT}/2026/10 - October/LUMO_17107 - ACT Party/Albany Expressway In/"
                                                  "LUMO_17107_ACT.Party_Albany-Expressway-In_2026-10-14_0900_Day_Score88_v1.jpg")
        # a campaign that has ended (not in today's photos): its advertiser comes from the file name
        self.assertEqual(self.g.items[b]["path"], f"{ROOT}/2026/10 - October/LUMO_9 - Old Co/Beach/"
                                                  "LUMO_9_Old.Co_Beach_2026-10-01_0900_Day_Score70_v1.jpg")
        self.assertIsNone(self.g.by_path(old))                                  # the empty old folders are gone
        self.assertIsNone(self.g.by_path(f"{ROOT}/2026/10 - October/Beach"))
        self.assertEqual(u.st["items"]["LUMO_17107|ALB|art:a.jpg|day"]["path"], self.g.items[a]["path"])
        self.assertEqual(u.st["layout"], 2)
        n = len(self.g.calls)
        self.run_with(u, [item("LUMO_17107", "ALB", "old", score=50)])
        self.assertFalse(any(m == "PATCH" for m, _ in self.g.calls[n:]))       # once only
        # a better photo later replaces it in the campaign folder
        self.run_with(u, [item("LUMO_17107", "ALB", "new", score=99)])
        files = [v["path"] for v in self.g.items.values() if not v["folder"] and "LUMO_17107" in v["path"]]
        self.assertEqual(len(files), 1)
        self.assertIn("/LUMO_17107 - ACT Party/Albany Expressway In/", files[0])
        self.assertIn("Score99", files[0])

    def test_folder_with_other_files_kept(self):
        old = f"{ROOT}/2026/10 - October/Greenlane"
        a = self.g._new(f"{old}/LUMO_5_X_Greenlane_2026-10-02_0900_Day_Score60_v1.jpg")
        self.g._new(f"{old}/someone's notes.docx")
        self.g._new(old, True)
        self.atomic(os.path.join(self.tmp, "pop-uploads.json"), {
            "folders": {"LUMO_5|G": "2026/10 - October"},
            "items": {"LUMO_5|G|v|day": {"path": self.g.items[a]["path"], "item_id": a, "name": "x"}}})
        self.run_with(self.new(), [])
        self.assertIsNotNone(self.g.by_path(old))                               # not empty: left alone


if __name__ == "__main__":
    unittest.main()
