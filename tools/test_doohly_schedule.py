"""Doohly bookings on a schedule:  python3 tools/test_doohly_schedule.py

A booking that plays only some days (or hours) of the week is due only for the time it has
been on so far: before its first day it is "not started" (not "not seen") and isn't counted
in true coverage; while it is on, it is measured against its full rate for that time."""
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
import store  # noqa: E402


class Windows(unittest.TestCase):
    def test_days_hours_and_dates(self):
        week = date(2026, 10, 19)                       # a Monday
        b = {"start": "2026-10-01", "end": "2026-12-31", "days": ["thu", "fri", "sat", "sun"]}
        w = rx._doohly_windows(b, week)
        self.assertEqual([datetime.fromtimestamp(a, rx.LOCAL_TZ).strftime("%a %H:%M") for a, _ in w],
                         ["Thu 00:00", "Fri 00:00", "Sat 00:00", "Sun 00:00"])
        self.assertTrue(all(z - a == 86400 for a, z in w))
        b = {"start": "2026-10-21", "end": "2026-10-21", "start_time": "06:00", "end_time": "10:00"}
        w = rx._doohly_windows(b, week)
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0][1] - w[0][0], 4 * 3600)
        self.assertEqual(rx._doohly_windows({"start": "2026-11-01", "end": "2026-11-30"}, week), [])


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SiteWeek(unittest.TestCase):
    def setUp(self):
        out = tempfile.mkdtemp()
        keys = ("STORE", "ARGS", "artwork_matches", "screen_creatives", "doohly_site", "load_creatives",
                "load_bookings", "save_art_state", "infer_programmatic")
        saved = {k: getattr(rx, k) for k in keys}
        self.addCleanup(lambda: [setattr(rx, k, v) for k, v in saved.items()])
        rx.ARGS = types.SimpleNamespace(out=out, api_key="k")
        rx.STORE = store.Store(out, log=lambda m: None)
        rx.MERGES.update(loaded=True, sites={}, apart={})
        rx.artwork_matches = lambda cam, t0, t1, setup=False: {}
        rx.screen_creatives = lambda screen: []
        rx.load_creatives = lambda: {}
        rx.load_bookings = lambda: {}
        rx.save_art_state = lambda: None
        rx.infer_programmatic = lambda *a, **k: (set(), "")
        now = datetime.now(rx.LOCAL_TZ)
        self.week = now.date() - timedelta(days=now.weekday())
        self.today = now.date()
        tomorrow = self.today + timedelta(days=1)
        later = [rx.DOOHLY_DAYS[(self.today + timedelta(days=i)).weekday()] for i in (1, 2)]
        self.later_ok = tomorrow - self.week < timedelta(days=7) and len(set(later)) == 2
        self.ds = {"device": {"online": True}, "loop": {}, "bookings": [
            {"campaign_id": "LUMO_1", "campaign_name": "Always", "name": "Always", "plays_per_loop": 2,
             "start": self.week.isoformat(), "end": (self.week + timedelta(days=6)).isoformat()},
            {"campaign_id": "LUMO_2", "campaign_name": "Later", "name": "Later", "plays_per_loop": 3,
             "start": self.week.isoformat(), "end": (self.week + timedelta(days=6)).isoformat(), "days": later}]}
        rx.doohly_site = lambda sid=None, screen=None: self.ds
        # c-1 (linked to LUMO_1) twice in every loop of 4 x 10 s over the last 20 minutes
        t = now.timestamp() - 1200
        for i in range(120):
            cid = "c-1" if i % 4 < 2 else "c-9"
            rx.STORE.add({"event_id": f"e{i}", "type": "creative_change", "site_id": "S", "local_creative_id": cid,
                          "play_started_at": iso(t + i * 10), "is_blank": False, "video_creative": False,
                          "quality": {"usable": True}})

    def test_not_started_isnt_not_seen(self):
        if not self.later_ok:
            self.skipTest("needs two more days left in this week")
        cam = {"id": "S", "name": "S", "vault_screen": "S", "slot_count": 4, "slot_ms": 10000}
        res = rx.site_bookings(cam, self.week, {"S": {"c-1": "LUMO_1", "c-5": "LUMO_2"}})
        c = {x["campaign_id"]: x for x in res["campaigns"]}
        self.assertEqual(c["LUMO_2"]["status"], "not started", c["LUMO_2"])
        self.assertTrue(c["LUMO_2"]["starts"])
        self.assertEqual(c["LUMO_2"]["due_per_loop"], 0)
        self.assertNotIn("Later", " ".join(res["warnings"]))
        self.assertEqual(c["LUMO_1"]["due_per_loop"], 2)
        self.assertEqual(res["booked_linked_per_loop"], 2)        # only what was due so far
        self.assertEqual(res["booked_due_per_loop"], 2)
        self.assertGreater(res["booked_per_loop"], 2)             # the week's average still shows booked


if __name__ == "__main__":
    unittest.main()
