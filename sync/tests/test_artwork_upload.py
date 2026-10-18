import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import creatives_sync as cs  # noqa: E402


class Resp:
    def __init__(self, obj):
        self.obj = obj

    def json(self):
        return self.obj


class FakeGraph:
    def __init__(self):
        self.calls, self.files, self.folders = [], {}, {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params"), kw.get("json")))
        if method == "POST" and url.endswith("/children"):
            name = kw["json"]["name"]
            if name in self.folders:
                raise cs.GraphError("Graph 409: nameAlreadyExists")
            self.folders[name] = "f-" + name
            return Resp({"id": self.folders[name], "name": name})
        if method == "PUT" and url.endswith(":/content"):
            key = url.split("/items/", 1)[1].rsplit(":/content", 1)[0]
            if key in self.files:
                raise cs.GraphError("Graph 409 on x: nameAlreadyExists: exists")
            self.files[key] = len(kw["data"])
            return Resp({"id": "i1", "webUrl": "https://sp/" + key})
        raise AssertionError((method, url))

    def get(self, url, **kw):
        raise AssertionError(url)


class Lib:
    def __init__(self, g):
        self.drive = "d1"

    def connect(self):
        return self


class Upload(unittest.TestCase):
    def setUp(self):
        self.saved = (cs.OUT, cs.Graph, cs.Library, cs.SYNC)
        self.addCleanup(self.restore)
        self.tmp = tempfile.mkdtemp()
        cs.OUT = self.tmp
        self.g = FakeGraph()
        cs.Graph, cs.Library = (lambda: self.g), Lib
        self.synced = []
        cs.SYNC = type("S", (), {"request": lambda _s, why: self.synced.append(why)})()
        manifest = {"missing": [
            {"campaign_id": "LUMO_19853", "screen": "Albany Expressway In", "reason": "no file",
             "folder": {"id": "F1", "path": "Creative+MIs/2026/10. October/LUMO_19853 ECB", "url": "u"}},
            {"campaign_id": "LUMO_20001", "screen": "Beach", "reason": "no folder",
             "new_folder": {"parent_id": "M10", "name": "LUMO_20001 - New One", "path": "Creative+MIs/2026/10. October/LUMO_20001 - New One"}}]}
        with open(os.path.join(self.tmp, "vault-creatives.json"), "w") as f:
            json.dump(manifest, f)

    def restore(self):
        cs.OUT, cs.Graph, cs.Library, cs.SYNC = self.saved

    def test_into_the_campaign_folder_then_sync(self):
        code, body = cs.upload_artwork("LUMO_19853", "albany expressway IN", "Albany Expressway Billboard.jpg", b"jpeg")
        self.assertEqual(code, 201, body)
        self.assertEqual(body["path"], "Creative+MIs/2026/10. October/LUMO_19853 ECB/Albany Expressway Billboard.jpg")
        self.assertEqual(self.g.calls[-1][2], {"@microsoft.graph.conflictBehavior": "fail"})   # never replaces
        self.assertEqual(self.synced, ["artwork uploaded for LUMO_19853"])
        code, body = cs.upload_artwork("LUMO_19853", "Albany Expressway In", "Albany Expressway Billboard.jpg", b"jpeg")
        self.assertEqual(code, 409)
        self.assertIn("already in", body["error"])

    def test_new_folder_when_the_campaign_has_none(self):
        code, body = cs.upload_artwork("LUMO_20001", "Beach", "Beach_576x396.png", b"png")
        self.assertEqual(code, 201, body)
        self.assertEqual(self.g.folders, {"LUMO_20001 - New One": "f-LUMO_20001 - New One"})
        self.assertTrue(body["path"].endswith("LUMO_20001 - New One/Beach_576x396.png"))

    def test_refused(self):
        self.assertEqual(cs.upload_artwork("LUMO_1", "Beach", "a.jpg", b"x")[0], 404)          # not missing
        self.assertEqual(cs.upload_artwork("LUMO_19853", "Albany Expressway In", "a.pdf", b"x")[0], 400)
        self.assertEqual(cs.upload_artwork("LUMO_19853", "Albany Expressway In", "../a.jpg", b"x")[1].get("path", "").endswith("/a.jpg"), True)
        self.assertEqual(cs.upload_artwork("LUMO_19853", "Albany Expressway In", "b:c.jpg", b"x")[0], 400)
        self.assertEqual(cs.upload_artwork("LUMO_19853", "Albany Expressway In", "e.jpg", b"")[0], 400)
        self.assertEqual(self.synced.count("artwork uploaded for LUMO_19853"), 1)


class ChooseReport(unittest.TestCase):
    def test_not_found_reported(self):
        files = [{"name": "A.png", "folder_rel": "", "folder_url": "u", "modified": "2026-10-01", "width": 576, "height": 396}]
        rep = {}
        got, why = cs.choose("LUMO_1", "Beach", (576, 396), [date(2026, 10, 19)], files, [], ["Beach"],
                             booked_files=[{"name": "A.png", "type": "image", "from": "2026-10-19", "to": "2026-10-25"},
                                           {"name": "B.png", "type": "image", "from": "2026-10-19", "to": "2026-10-25"}],
                             report=rep)
        self.assertEqual([g["name"] for g in got], ["A.png"])
        self.assertEqual(rep["not_found"], ["B.png"])


if __name__ == "__main__":
    unittest.main()
