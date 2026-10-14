import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import doohly  # noqa: E402
import creatives_sync as cs  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")
except Exception:  # pragma: no cover
    NZ = None


def booking(bid, cid, start, end, ppl=3, ptype="Sold", creatives=(), days=None, weights=False, scheds=False,
            updated="2026-10-01T00:00:00.000Z"):
    return {"id": bid, "name": f"{cid} booking", "status": "booked", "purchaseType": ptype, "updatedAt": updated,
            "externalId": "463637", "playsPerLoop": ppl, "loopsPerPlay": 1, "playConsecutively": False,
            "creativeWeightsEnabled": weights, "creativeSchedulesEnabled": scheds,
            "campaign": {"id": "c1", "name": f"{cid} - Campaign", "externalId": cid},
            "schedule": {"startDate": start, "endDate": end, "startTime": "00:00:00", "endTime": "23:59:00",
                         "days": days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            "assignedCreatives": list(creatives)}


def creative(name, weight=1, blocks=None, order=0, kind="image"):
    c = {"order": order, "weight": weight, "creative": {"id": name, "name": name, "type": kind,
                                                      "file": {"width": 576, "height": 396, "durationMs": 0}}}
    if blocks:
        c["assignedSchedules"] = [{"schedule": {"dayTimeBlocks": blocks}}]
    return c


class Helpers(unittest.TestCase):
    def test_names_and_ids(self):
        self.assertEqual(doohly.norm("LUMO-Albany Expressway IN"), doohly.norm("Albany Expressway In"))
        self.assertEqual(doohly.campaign_id({"externalId": "LUMO_19702"}), "LUMO_19702")
        self.assertEqual(doohly.campaign_id({"externalId": None, "name": "LUMO_8719 - BP Wild Bean"}), "LUMO_8719")
        self.assertIsNone(doohly.campaign_id({"name": "House ad"}))
        self.assertEqual(doohly.hours_of([{"day": "mon", "startTime": "09:00:00", "endTime": "17:00:00"}]), "09:00-17:00")
        self.assertIsNone(doohly.hours_of([{"day": "mon", "startTime": "00:00:00", "endTime": "23:59:59"}]))

    def test_slim_booking_weights_and_schedules(self):
        b = doohly.slim_booking(booking("b1", "LUMO_19519", "2026-10-12", "2026-10-18", weights=True, scheds=True,
                                        creatives=[creative("A.jpg", 15, [{"day": "mon", "startTime": "05:00:00",
                                                                           "endTime": "10:00:00"}]),
                                                   creative("B.jpg", 35, order=1)]))
        self.assertEqual(b["campaign_id"], "LUMO_19519")
        self.assertEqual(b["slot_type"], "sold")
        self.assertEqual([c["share"] for c in b["creatives"]], [30.0, 70.0])
        self.assertEqual((b["creatives"][0]["hours"], b["creatives"][0]["days"]), ("05:00-10:00", ["mon"]))

    def test_weekly_plays_split_booking(self):
        b4 = doohly.slim_booking(booking("x", "LUMO_1", "2026-10-19", "2026-10-25", ppl=1, days=["mon", "tue", "wed", "thu"]))
        b3 = doohly.slim_booking(booking("y", "LUMO_1", "2026-10-19", "2026-10-25", ppl=2, days=["fri", "sat", "sun"]))
        wk = date(2026, 10, 19)
        self.assertAlmostEqual(doohly.weekly_plays(b4, wk) + doohly.weekly_plays(b3, wk), 10 / 7)
        self.assertEqual(doohly.weekly_plays(b4, date(2026, 10, 12)), 0)


class FakeClient:
    """Answers like Doohly's API; fails the test on anything but GET-style calls."""

    def __init__(self, bookings):
        self.calls = 0
        self.b = {b["id"]: b for b in bookings}

    def pages(self, path, **params):
        self.calls += 1
        if path == "/v2/devices":
            yield from [{"id": "d1", "name": "LUMO-Beach"}, {"id": "d2", "name": "LUMO-Albany Expressway IN"},
                        {"id": "d3", "name": "LUMO-Albany Expressway OUT"}]
        elif path == "/v3/bookings":
            assert params["filter"] == "status:in:booked,reserved,paused"
            for b in self.b.values():
                yield {k: v for k, v in b.items() if k != "assignedCreatives"}
        else:
            raise AssertionError(path)

    def get(self, path, **params):
        self.calls += 1
        if path.startswith("/v2/devices/"):
            return {"id": path.rsplit("/", 1)[1], "name": "LUMO-Beach", "isConnected": False,
                    "aliveAt": "2026-10-01T00:00:00.000Z", "geoLocation": {"coordinates": [174.77, -36.85]},
                    "frames": [{"id": "nameplate", "name": "Nameplate", "bounds": {"width": 144, "height": 72}},
                               {"id": "main", "name": "Mainscreen", "bounds": {"width": 576, "height": 396},
                                "loopPolicy": {"name": "20 slots, 16s", "slotCount": 20, "slotDuration": 16}}]}
        if path.startswith("/v3/bookings/"):
            return self.b[path.rsplit("/", 1)[1]]
        raise AssertionError(path)


class WatcherRun(unittest.TestCase):
    @unittest.skipIf(NZ is None, "no tz database")
    def test_run_writes_doohly_json_and_alerts(self):
        tmp = tempfile.mkdtemp()
        notes, changes = [], []

        def atomic(p, o):
            with open(p, "w") as f:
                json.dump(o, f)

        def read(p, d=None):
            try:
                with open(p) as f:
                    return json.load(f)
            except OSError:
                return d
        sites = [{"site_id": "BEACH-01", "name": "beach", "vault_screen": "Beach"},
                 {"site_id": "PARNELL", "name": "parnell", "vault_screen": "Parnell"}]
        w = doohly.Watcher(tmp, tmp, lambda: {"sites": sites}, NZ, lambda *a: None,
                           lambda t, ok=False, title=None: notes.append(title), atomic, read, changes.append)
        today = date.today()
        w.c = FakeClient([booking("b1", "LUMO_19649", today.isoformat(), today.isoformat(), ptype="Non-Guaranteed Bonus",
                                  creatives=[creative("AUM26 LUMO-Beach (576 x 396 px) A.png")]),
                          booking("old", "LUMO_1", "2020-01-01", "2020-01-07")])
        self.assertTrue(w.run(with_bookings=True))
        d = read(os.path.join(tmp, "doohly.json"))
        s = d["sites"]["BEACH-01"]
        self.assertEqual((s["frame"]["id"], s["loop"]["slot_count"], s["device"]["lat"]), ("main", 20, -36.85))
        self.assertEqual([b["campaign_id"] for b in s["bookings"]], ["LUMO_19649"])     # the 2020 one is left out
        self.assertEqual(d["unmatched"][0]["site_id"], "PARNELL")
        self.assertEqual(notes, ["Warning: Doohly player offline at Beach"])
        calls = w.c.calls
        self.assertFalse(w.run(with_bookings=True))                                      # nothing changed
        self.assertEqual(w.c.calls - calls, 3)       # devices, device, bookings list; no detail call again
        self.assertEqual(len(notes), 1)                                                  # alerted once


class ArtworkFromDoohly(unittest.TestCase):
    def test_doohly_by_screen_and_choose(self):
        doc = {"sites": {"BEACH-01": {"screen": "Beach", "bookings": [doohly.slim_booking(booking(
            "b1", "LUMO_19649", "2026-10-12", "2026-10-18", ptype="Non-Guaranteed Bonus", weights=True,
            creatives=[creative("AUM26 LUMO-Beach (576 x 396 px) A.png", 1), creative("AUM26 LUMO-Beach (576 x 396 px) B.png", 3),
                       creative("live-weather", kind="html")]))]}}}
        weeks = [date(2026, 10, 12), date(2026, 10, 19)]
        by = cs.doohly_by_screen(doc, ["Beach", "Parnell"], weeks)
        self.assertEqual(list(by), ["Beach"])
        e = by["Beach"]["LUMO_19649"]
        self.assertEqual(e["weeks"], {date(2026, 10, 12)})
        files = [{"name": n, "folder_rel": "", "folder_url": "u", "modified": "2026-10-01", "width": 576, "height": 396}
                 for n in ("AUM26 LUMO-Beach (576 x 396 px) A.png", "AUM26 LUMO-Beach (576 x 396 px) B.png",
                           "AUM26 LUMO-Parnell (576 x 396 px) A.png")]
        got, why = cs.choose("LUMO_19649", "Beach", (576, 396), sorted(e["weeks"]), files, [], ["Beach", "Parnell"],
                             booked_files=e["files"])
        self.assertIsNone(why)
        self.assertEqual(sorted(g["name"] for g in got), ["AUM26 LUMO-Beach (576 x 396 px) A.png",
                                                          "AUM26 LUMO-Beach (576 x 396 px) B.png"])
        self.assertEqual({g["via"] for g in got}, {"doohly"})
        self.assertEqual(sorted(g["rotation"] for g in got), [20.0, 60.0])   # the html one takes the other 20%
        self.assertEqual(got[0]["from"], "2026-10-12")


if __name__ == "__main__":
    unittest.main()
