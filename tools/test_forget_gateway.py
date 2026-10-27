"""Removing a gateway that has gone (renamed, or moved to another machine):  python3 tools/test_forget_gateway.py"""
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver as rx  # noqa: E402
import store  # noqa: E402


def hb(gid, t):
    return {"gateway_id": gid, "sent_at": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "health": {"heartbeat_s": 60}}


class Forget(unittest.TestCase):
    def setUp(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(rx, k) for k in ("STORE", "ARGS")}
        self.addCleanup(lambda: [setattr(rx, k, v) for k, v in saved.items()])
        rx.ARGS = types.SimpleNamespace(out=out, api_key="k")
        rx.STORE = store.Store(out, log=lambda m: None)
        rx.GATEWAYS.clear()
        self.addCleanup(rx.GATEWAYS.clear)
        now = time.time()
        for gid, t in (("trial-gw-01", now - 6 * 3600), ("lumo-gw-01", now - 30)):
            rx.STORE.add_heartbeat(hb(gid, t))
            rx.note_heartbeat(hb(gid, t), received=t)

    def test_only_an_offline_one(self):
        self.assertEqual(rx.forget_gateway("lumo-gw-01")[0], 409)
        self.assertEqual(rx.forget_gateway("nope")[0], 404)
        code, res = rx.forget_gateway("trial-gw-01")
        self.assertEqual((code, res["heartbeats"]), (200, 1))
        self.assertEqual([g["gateway_id"] for g in rx.health_data()], ["lumo-gw-01"])
        # and it doesn't come back at the next start (heartbeats are reloaded from the database)
        self.assertEqual([h["gateway_id"] for h in rx.STORE.last_heartbeats(500)], ["lumo-gw-01"])


if __name__ == "__main__":
    unittest.main()
