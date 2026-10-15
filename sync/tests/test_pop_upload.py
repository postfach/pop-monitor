import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pop_upload as pu  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")
except Exception:  # pragma: no cover
    NZ = None


def item(score, eid, version="art:a.jpg", taken="2026-10-13T14:32:10+13:00", **kw):
    d = {"site_id": "CURLETTS-IN", "site_name": "curletts-in", "screen": "Curletts In", "campaign_id": "LUMO_19702",
         "version": version, "version_label": "a.jpg", "event_id": eid, "taken_at": taken,
         "first_seen": "2026-10-12", "score": score}
    d.update(kw)
    return d


class FakeGraph:
    def __init__(self):
        self.files, self.deleted, self.n = {}, [], 0

    class R:
        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def request(self, method, url, **kw):
        if method == "PUT":
            self.n += 1
            path = url.split("root:/", 1)[1].rsplit(":/content", 1)[0]
            self.files[f"id{self.n}"] = path
            return self.R({"id": f"id{self.n}", "webUrl": "https://x/" + path})
        if method == "PATCH":
            iid = url.rsplit("/", 1)[1]
            folder = self.files[iid].rsplit("/", 1)[0]
            self.files[iid] = folder + "/" + kw["json"]["name"]
            return self.R({"id": iid, "webUrl": "https://x/" + self.files[iid]})
        if method == "DELETE":
            iid = url.rsplit("/", 1)[1]
            self.deleted.append(iid)
            self.files.pop(iid, None)
            return self.R({})
        raise AssertionError(url)


class Lib:
    def __init__(self, g):
        self.drive = "d1"

    def connect(self):
        return self


class Uploads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.g = FakeGraph()
        import json

        def atomic(path, obj):
            with open(path, "w") as f:
                json.dump(obj, f)

        def read(path, default=None):
            try:
                with open(path) as f:
                    return json.load(f)
            except OSError:
                return default
        self.notes = []
        self.u = pu.PopUploader(self.g, Lib, self.tmp, "http://recv", "k", NZ, lambda *a: None,
                                lambda text, ok=False, title=None: self.notes.append((ok, title)), atomic, read,
                                lambda p: p)
        self.u._get = lambda path, **kw: mock.Mock(content=b"jpeg")

    def run_with(self, items, phase="day"):
        resp = mock.Mock(json=lambda: {"items": items}, raise_for_status=lambda: None)
        with mock.patch.object(pu.requests, "post", return_value=resp) as post:
            ok = self.u.run(phase, 1000, 2000)
        return ok, post.call_args.kwargs["json"]

    def test_names_and_folders(self):
        self.assertEqual(pu.month_folder("2026-10-12"), "2026/10 - October")
        self.assertEqual(pu.file_name(item(87.4, "e"), "day", 1),
                         "LUMO_19702_Curletts-In_2026-10-13_1432_Day_Score87_v1.jpg")
        self.assertTrue(pu.file_name(item(91.6, "e"), "night", 2).endswith("_Night_Score92_v2.jpg"))
        self.assertEqual(pu.safe_part('Beach: "North"?'), "Beach- -North--")

    def test_first_upload_then_only_better(self):
        ok, _ = self.run_with([item(80, "e1")])
        self.assertTrue(ok)
        self.assertEqual(list(self.g.files.values()), [
            "00_Sales (NEW FOLDER)/Booked Campaigns/PoP Images/2026/10 - October/Curletts In/"
            "LUMO_19702_Curletts-In_2026-10-13_1432_Day_Score80_v1.jpg"])
        # the receiver re-scored the uploaded photo higher than the new one: keep it
        self.run_with([item(85, "e2", current_event_id="e1", current_score=88)])
        self.assertEqual(len(self.g.files), 1)
        self.assertEqual(self.g.deleted, [])
        # a better one replaces it (old file deleted), still one file
        ok, sent = self.run_with([item(90, "e3", taken="2026-10-14T09:05:00+13:00",
                                       current_event_id="e1", current_score=79)])
        self.assertEqual(sent["current"], {"CURLETTS-IN|LUMO_19702|art:a.jpg": "e1"})
        self.assertEqual(self.g.deleted, ["id1"])
        (path,) = self.g.files.values()
        self.assertTrue(path.endswith("2026/10 - October/Curletts In/LUMO_19702_Curletts-In_2026-10-14_0905_Day_Score90_v1.jpg"))
        # the same photo coming back as best: nothing to do
        self.run_with([item(90, "e3", current_event_id="e3", current_score=90)])
        self.assertEqual(len(self.g.files), 1)

    def test_beats_todays_score_of_the_photo_up_there(self):
        self.run_with([item(99.4, "e1")])                                     # up there as Score99
        # scored 96 against today's photos: a 97 is better today, and replaces it
        self.run_with([item(97.2, "e2", current_event_id="e1", current_score=96.3)])
        (path,) = self.g.files.values()
        self.assertTrue(path.endswith("_Score97_v1.jpg"), path)
        self.assertEqual(self.u.st["status"]["recent"][0]["was_now"], 96.3)
        # a tie in whole numbers doesn't
        self.run_with([item(97.4, "e3", current_event_id="e2", current_score=96.6)])
        (path2,) = self.g.files.values()
        self.assertEqual(path, path2)
        # the receiver no longer has the photo up there: its uploaded score counts
        self.run_with([item(97.4, "e4", current_event_id=None, current_score=None)])
        self.assertEqual(list(self.g.files.values()), [path])

    def test_advertiser_in_name_and_old_files_renamed(self):
        self.assertEqual(pu.adv_part("HBO Max"), "HBO.Max")
        self.assertEqual(pu.adv_part("Tui Garden Products!"), "Tui.Garden.Products")
        self.assertEqual(pu.file_name(item(98.2, "e", advertiser="HBO Max"), "day", 1),
                         "LUMO_19702_HBO.Max_Curletts-In_2026-10-13_1432_Day_Score98_v1.jpg")
        self.run_with([item(80, "e1")])                                       # uploaded before advertisers
        self.run_with([item(70, "e2", advertiser="HBO Max", current_event_id="e1", current_score=80)])
        (path,) = self.g.files.values()                                       # not replaced, but renamed
        self.assertTrue(path.endswith("/LUMO_19702_HBO.Max_Curletts-In_2026-10-13_1432_Day_Score80_v1.jpg"), path)
        self.assertEqual(self.g.deleted, [])

    def test_versions_and_phases_are_separate(self):
        self.run_with([item(80, "a1"), item(70, "b1", version="art:b.jpg")])
        self.run_with([item(60, "n1")], phase="night")
        tails = {p.rsplit("_", 3)[-3] + "_" + p.rsplit("_", 2)[-2] + "_" + p.rsplit("_", 1)[-1]
                 for p in self.g.files.values()}
        self.assertEqual(tails, {"Day_Score80_v1.jpg", "Day_Score70_v2.jpg", "Night_Score60_v1.jpg"})

    def test_folder_stays_with_first_month(self):
        self.run_with([item(50, "e1", first_seen="2026-09-28", taken="2026-09-30T10:00:00+13:00")])
        self.run_with([item(95, "e2", first_seen="2026-09-28", taken="2026-10-02T10:00:00+13:00",
                            current_event_id="e1", current_score=50)])
        (path,) = self.g.files.values()
        self.assertIn("/2026/09 - September/", path)

    def test_access_denied_stops_and_alerts_once(self):
        def boom(*a, **k):
            raise RuntimeError("Graph 403 on /drives/x: accessDenied")
        self.g.request = boom
        ok, _ = self.run_with([item(80, "e1"), item(70, "e2", version="art:b.jpg")])
        self.assertFalse(ok)
        self.assertEqual(self.notes, [(False, "Warning: PoP photo upload failed")])
        self.assertFalse(self.u.st["runs"]["day"]["ok"])
        self.assertEqual(self.u.st["runs"]["day"]["until"], 1000)      # the window is tried again
        self.run_with([item(80, "e1")])
        self.assertEqual(len(self.notes), 1)                           # not alerted again

    @unittest.skipIf(NZ is None, "no tz database")
    def test_schedule(self):
        now = datetime(2026, 10, 13, 20, 5, tzinfo=NZ)
        self.u.st["runs"] = {"day": {"at": datetime(2026, 10, 12, 20, 0, 30, tzinfo=NZ).timestamp(), "ok": True},
                             "night": {"at": datetime(2026, 10, 13, 8, 0, 30, tzinfo=NZ).timestamp(), "ok": True}}
        self.assertEqual(self.u.due(now), ["day"])
        self.assertEqual(self.u.due(now.replace(hour=19)), [])
        self.u.st["runs"]["night"] = {"at": datetime(2026, 10, 13, 8, 0, 30, tzinfo=NZ).timestamp(), "ok": False}
        self.assertEqual(self.u.due(now.replace(hour=8, minute=30)), [])
        self.assertEqual(self.u.due(now.replace(hour=9, minute=5)), ["night"])   # retried after an hour


if __name__ == "__main__":
    unittest.main()
