"""Working out programmatic:  python3 tools/test_programmatic.py

A creative that matches none of the booked artwork is programmatic. One that only faintly
resembles a booked campaign is too when that campaign is already delivered by its own
creatives, or isn't booked here and the resemblance is weak; a closer look-alike of a
campaign not booked here (an overrun) stays unassigned. A booking with no artwork stops
blocking this once its creative is linked by hand."""
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402

WEEK = date(2026, 10, 19)
DS = {"loop": {"programmatic": True},
      "bookings": [{"campaign_id": "A", "start": "2026-10-01", "end": "2026-10-31", "creatives": [{"type": "image"}]},
                   {"campaign_id": "B", "start": "2026-10-01", "end": "2026-10-31", "creatives": [{"type": "image"}]}]}
LIB = [{"campaign_id": "A", "file": "a.jpg"}, {"campaign_id": "B", "file": "b.jpg"}]
CAMPAIGNS = [{"campaign_id": "A", "plays_per_loop": 3}, {"campaign_id": "B", "plays_per_loop": 3}]


def cr(cid, share, plays=50):
    return {"creative_id": cid, "blank": False, "plays": plays, "share": share}


@unittest.skipUnless(rx.ART_OK, "needs numpy and Pillow")
class Programmatic(unittest.TestCase):
    def run_it(self, assigned, art, crs, ds=DS, lib=LIB, campaigns=CAMPAIGNS):
        return rx.infer_programmatic(ds, WEEK, crs, assigned, art, lib, campaigns)

    def test_faint_lookalikes(self):
        crs = [cr("c-1", 3.0), cr("c-2", 1.0), cr("c-3", 0.5), cr("c-4", 0.4), cr("c-5", 0.3), cr("c-6", 0.3)]
        assigned = {"c-1": "A", "c-2": "B", "c-3": "unassigned", "c-4": "unassigned", "c-5": "unassigned",
                    "c-6": "unassigned"}
        art = {"c-1": {"status": "matched", "campaign_id": "A", "samples": 5},
               "c-2": {"status": "matched", "campaign_id": "B", "samples": 5},
               "c-3": {"status": "possible", "campaign_id": "A", "score": 0.33, "samples": 5},   # A delivered: prog
               "c-4": {"status": "possible", "campaign_id": "B", "score": 0.33, "samples": 5},   # B short: may be B
               "c-5": {"status": "possible", "campaign_id": "X", "score": 0.30, "samples": 5},   # not booked, weak
               "c-6": {"status": "possible", "campaign_id": "X", "score": 0.40, "samples": 5}}   # not booked, close
        got, why = self.run_it(assigned, art, crs)
        self.assertEqual((got, why), ({"c-3", "c-5"}, ""))
        # without the campaigns (older callers) only clear non-matches count
        self.assertEqual(rx.infer_programmatic(DS, WEEK, crs, assigned, art, LIB)[0], set())

    def test_none_needs_enough_photos_and_plays(self):
        crs = [cr("c-1", 0.5), cr("c-2", 0.5, plays=2)]
        art = {"c-1": {"status": "none", "samples": 2}, "c-2": {"status": "none", "samples": 5}}
        got, _ = self.run_it({"c-1": "unassigned", "c-2": "unassigned"}, art, crs)
        self.assertEqual(got, set())

    def test_missing_artwork_linked_by_hand(self):
        lib = [LIB[0]]                                        # B's artwork isn't in SharePoint
        crs = [cr("c-1", 3.0), cr("c-2", 3.0), cr("c-9", 0.5)]
        art = {"c-9": {"status": "none", "samples": 5}}
        got, why = self.run_it({"c-1": "A", "c-2": "unassigned", "c-9": "unassigned"}, art, crs, lib=lib)
        self.assertEqual(got, set())
        self.assertIn("link its creative", why)
        got, why = self.run_it({"c-1": "A", "c-2": "B", "c-9": "unassigned"}, art, crs, lib=lib)   # c-2 linked to B
        self.assertEqual((got, why), ({"c-9"}, ""))


if __name__ == "__main__":
    unittest.main()
