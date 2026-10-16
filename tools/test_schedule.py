"""Off-schedule warnings use every booking of a file:  python3 tools/test_schedule.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import receiver as rx  # noqa: E402


def e(frm, to, name="a.png", camp="LUMO_19660"):
    return {"campaign_id": camp, "name": name, "from": frm, "to": to}


class Schedule(unittest.TestCase):
    def test_this_and_next_weeks_booking_of_the_same_file(self):
        # Alexandra Park In: LUMO_19660 booked 12-18 Oct and again 19-25 Oct with the same files
        lib = [e("2026-10-19", "2026-10-25"), e("2026-10-12", "2026-10-18"), e("2026-10-12", "2026-10-18", name="b.png")]
        periods = rx.book_periods(lib, "LUMO_19660", "a.png")
        self.assertEqual(periods, [("2026-10-12", "2026-10-25")])
        self.assertEqual(rx.off_schedule(["2026-10-15", "2026-10-16"], periods), "")

    def test_real_early_late_and_gap(self):
        periods = rx.book_periods([e("2026-10-12", "2026-10-18"), e("2026-11-02", "2026-11-08")], "LUMO_19660", "a.png")
        self.assertEqual(rx.off_schedule(["2026-10-11"], periods), "early")
        self.assertEqual(rx.off_schedule(["2026-10-27"], periods), "between")
        self.assertEqual(rx.off_schedule(["2026-11-09"], periods), "late")
        self.assertEqual(rx.off_schedule(["2026-10-12"], rx.book_periods([e(None, None)], "LUMO_19660", "a.png")), "")


if __name__ == "__main__":
    unittest.main()
