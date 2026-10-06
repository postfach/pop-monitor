"""Unit tests for Material Instructions parsing and file choice.

  python3 -m unittest discover -s sync/tests -v
"""
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mi  # noqa: E402
import fixtures  # noqa: E402
import creatives_sync as cs  # noqa: E402


def f(name, w=None, h=None, folder="", modified="2026-10-01T00:00:00Z", rank=0):
    return {"id": name, "name": name, "etag": "1", "modified": modified, "width": w, "height": h,
            "rel": f"{folder}/{name}".strip("/"), "folder_rel": folder, "folder_url": "https://x/" + folder, "rank": rank}


WEEK = [date(2026, 10, 5)]


class Parse(unittest.TestCase):
    def test_standard_with_header_dates(self):
        data = fixtures.standard("LUMO_19023", "Pals", [
            ("LUMO-Beach", "05.10.2026", "11.10.2026", "Paid",
             ["PAL020 Raspberry Launch Billboards_Teaser_Beach 576x396",
              "PAL020 Raspberry Launch Billboards_Beach 576x396"], 576, 396),
            ("LUMO-Curletts In", "05.10.2026", "11.10.2026", "Bonus",
             ["PAL020 Raspberry Launch Billboards_Teaser_Curlettes In 432x864",
              "PAL020 Raspberry Launch Billboards_Curlettes In 432x864.png"], 432, 864),
            ("LUMO-Beach", "19.10.2026", "25.10.2026", "Paid", ["PAL020 Raspberry Launch Billboards_Beach 576x396"], 576, 396),
        ], headers=("Creative #1 File Name: 5th Oct - 7th October", "Creative #2 File Name:  8th October - 11th October"))
        wb = mi.read_workbook(data)
        self.assertEqual(len(wb["rows"]), 3)
        r = wb["rows"][0]
        self.assertEqual(r["screen"], "beach")
        self.assertEqual((r["start"], r["end"]), (date(2026, 10, 5), date(2026, 10, 11)))
        self.assertEqual([(c["from"], c["to"]) for c in r["creatives"]],
                         [(date(2026, 10, 5), date(2026, 10, 7)), (date(2026, 10, 8), date(2026, 10, 11))])
        self.assertEqual(wb["updated"], date(2026, 8, 17))
        self.assertEqual(wb["specs"]["tamaki"][:2], (1368, 324))
        self.assertEqual(wb["specs"]["albany expressway in"][:2], (1188, 288))

    def test_anchor_layout(self):
        data = fixtures.anchor_style("LUMO_19519", "Anchor", [
            ("LUMO-Albany Expressway In", "05.10.2026", "11.10.2026", "Paid", 1188, 288,
             [("FON0218A - WAKE UP TO MILK OOH - 1188x288px_AlbanGood.jpg", "15%"),
              ("FON0218A - WAKE UP TO MILK OOH - 1188x288px_GenZGood.jpg", "15%"),
              ("FON0218A - WAKE UP TO MILK OOH - 1188x288px_GenZLegit.jpg", "35%"),
              ("FON0218A - WAKE UP TO MILK OOH - 1188x288px_LanceSmooth.jpg", "35%"),
              ("FON0218A_WUTM_OOH_1188x288px_Sam.jpg", 50), ("FON0218A_WUTM_OOH_1188x288px_Sophie.jpg", 50)],
             "9am-5pm: Real Good 30%"),
        ])
        wb = mi.read_workbook(data)
        self.assertEqual(len(wb["rows"]), 1)              # the group-label row is not a booking
        r = wb["rows"][0]
        self.assertEqual(r["size"], (1188, 288))
        self.assertEqual([c["rotation"] for c in r["creatives"]], [15, 15, 35, 35, 50, 50])
        self.assertEqual(r["start"], date(2026, 10, 5))

    def test_helpers(self):
        self.assertEqual(mi.rotation(0.15), 15)
        self.assertEqual(mi.rotation("70%"), 70)
        self.assertEqual(mi.screen_norm("LUMO-Blenheim Road"), mi.screen_norm("Blenheim Rd"))
        self.assertEqual(mi.screen_norm("LUMO-Tāmaki"), "tamaki")
        self.assertEqual(mi.header_range("Creative #1 File Name: 28th Dec - 3rd Jan", 2026),
                         (date(2026, 12, 28), date(2027, 1, 3)))
        self.assertEqual(mi.week_folder_date("Wolfbrook/w.c 05.10.26"), date(2026, 10, 5))
        self.assertTrue(mi.names_screen("PAL020_Curlettes In 432x864.png", "Curletts In"))
        self.assertFalse(mi.names_screen("PAL020_Curletts Out 432x864.png", "Curletts In"))
        self.assertFalse(mi.names_screen("X_Albany Expressway Out 1188x288.jpg", "Albany Expressway In"))
        self.assertTrue(mi.names_screen("LUMO-Beach_576x396px_OCT-15.jpg", "Beach"))


class Choose(unittest.TestCase):
    def mis(self, rows, updated=date(2026, 9, 1), name="MI.xlsx"):
        return [{"rows": rows, "updated": updated, "modified": "2026-09-01", "name": name}]

    def row(self, screen, creatives, start=date(2026, 10, 5), end=date(2026, 10, 11), size=None, hours=None):
        return {"site": "LUMO-" + screen, "screen": mi.screen_norm(screen), "start": start, "end": end, "type": "Paid",
                "size": size, "hours": hours, "notes": "",
                "creatives": [{"name": n, "rotation": r, "from": None, "to": None, "column": "Creative #1"} for n, r in creatives]}

    def test_mi_typos_and_missing_extension(self):
        files = [f("PAL020 Raspberry Launch Billboards_Teaser_Beach 576x396.png", 576, 396),
                 f("PAL020 Raspberry Launch Billboards_Teaser_Curlettes In 432x864.png", 432, 864),
                 f("Pals - Material Instructions.xlsx")]
        got, why = cs.choose("LUMO_19023", "Beach", (576, 396), WEEK, files,
                             self.mis([self.row("Beach", [("PAL020 Raspberry Launch Billboards_Teaser_Beach 576x396", None)])]), [])
        self.assertIsNone(why)
        self.assertEqual([g["name"] for g in got], ["PAL020 Raspberry Launch Billboards_Teaser_Beach 576x396.png"])
        self.assertEqual(got[0]["via"], "mi")
        got, _ = cs.choose("LUMO_19023", "Curletts In", (432, 864), WEEK, files,
                           self.mis([self.row("Curletts In", [("PAL020 Raspberry Launch Billboards_Teaser_Curletts In 432x864", None)])]), [])
        self.assertEqual(len(got), 1)

    def test_newest_mi_wins(self):
        files = [f("old_1188x288.jpg", 1188, 288), f("new_1188x288.jpg", 1188, 288)]
        mis = self.mis([self.row("Khyber", [("old_1188x288.jpg", None)])], updated=date(2026, 8, 1), name="old") + \
            self.mis([self.row("Khyber", [("new_1188x288.jpg", None)])], updated=date(2026, 9, 20), name="new")
        got, _ = cs.choose("LUMO_1", "Khyber", (1188, 288), WEEK, files, mis, [])
        self.assertEqual([g["name"] for g in got], ["new_1188x288.jpg"])

    def test_size_fallback_excludes_other_screens(self):
        files = [f("KFC_576x396px.png", 576, 396), f("KFC_Customhouse Quay_576x396.png", 576, 396),
                 f("KFC_432x864.png", 432, 864)]
        got, _ = cs.choose("LUMO_17314", "Beach", (576, 396), WEEK, files, [], ["Beach", "Customhouse Quay"])
        self.assertEqual([g["name"] for g in got], ["KFC_576x396px.png"])
        self.assertEqual(got[0]["via"], "size")

    def test_size_tolerance(self):
        self.assertTrue(mi.same_size((2376, 576), (1188, 288)))      # supplied at 2x
        self.assertTrue(mi.same_size((577, 397), (576, 396)))        # a pixel over
        self.assertFalse(mi.same_size((1368, 324), (1188, 288)))     # a different screen shape
        self.assertFalse(mi.same_size((1152, 384), (1188, 288)))
        files = [f("1188x288.png", 2376, 576), f("NIAS Amova_LUMO-Beach_576 x 396.jpg", 577, 397)]
        got, _ = cs.choose("LUMO_17346", "Albany Expressway In", (1188, 288), WEEK, files, [], ["Beach"])
        self.assertEqual([g["name"] for g in got], ["1188x288.png"])
        got, _ = cs.choose("LUMO_18796", "Beach", (576, 396), WEEK, files, [], ["Beach"])
        self.assertEqual([g["name"] for g in got], ["NIAS Amova_LUMO-Beach_576 x 396.jpg"])

    def test_named_for_screen(self):
        files = [f("LUMO-Beach_576x396px_OCT-15.jpg", 576, 396), f("LUMO-Parnell_468x756.jpg", 468, 756)]
        got, _ = cs.choose("LUMO_17559", "Beach", (576, 396), WEEK, files, [], ["Beach", "Parnell"])
        self.assertEqual(got[0]["via"], "filename")

    def test_week_folders(self):
        files = [f("A_1188x288.jpg", 1188, 288, folder="w.c 28.09.26"), f("B_1188x288.jpg", 1188, 288, folder="w.c 05.10.26"),
                 f("C_1188x288.jpg", 1188, 288, folder="w.c 12.10.26")]
        got, _ = cs.choose("LUMO_19762", "Khyber", (1188, 288), WEEK, files, [], [])
        self.assertEqual([g["name"] for g in got], ["B_1188x288.jpg"])

    def test_missing_reasons(self):
        got, why = cs.choose("LUMO_1", "Beach", (576, 396), WEEK, [f("x.mp4")], [], [])
        self.assertEqual(got, [])
        self.assertIn("576x396", why)
        got, why = cs.choose("LUMO_1", "Beach", (576, 396), WEEK, [], [], [])
        self.assertIn("empty", why)

    def test_rows_outside_week_ignored(self):
        files = [f("Oct_576x396.jpg", 576, 396), f("Nov_576x396.jpg", 576, 396)]
        mis = self.mis([self.row("Beach", [("Nov_576x396.jpg", None)], start=date(2026, 11, 2), end=date(2026, 11, 8))])
        got, _ = cs.choose("LUMO_1", "Beach", (576, 396), WEEK, files, mis, [])
        self.assertEqual(got[0]["via"], "size")           # MI has no row this week -> by size


if __name__ == "__main__":
    unittest.main()
