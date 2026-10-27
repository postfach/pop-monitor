"""CPU and memory on the Health page:  python3 tools/test_system_health.py"""
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

MB = 1 << 20


def hb(t, cpu, rss, up, avail=4 << 30):
    return {"gateway_id": "gw", "sent_at": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "health": {"uptime_s": up, "process": {"cpu_pct": 30, "rss_bytes": rss},
                       "host": {"cpus": 4, "cpu_pct": cpu, "mem_total_bytes": 8 << 30, "mem_available_bytes": avail}}}


class System(unittest.TestCase):
    def setUp(self):
        out = tempfile.mkdtemp()
        saved = {k: getattr(rx, k) for k in ("STORE", "ARGS")}
        self.addCleanup(lambda: [setattr(rx, k, v) for k, v in saved.items()])
        rx.ARGS = types.SimpleNamespace(out=out, api_key="k")
        rx.STORE = store.Store(out, log=lambda m: None)
        self.now = time.time()

    def add(self, mins_ago, cpu, rss, up):
        h = hb(self.now - mins_ago * 60, cpu, rss, up)
        rx.STORE.add_heartbeat(h)
        return h

    def test_quiet_machine(self):
        for m in range(20, 0, -1):
            last = self.add(m, 40, 200 * MB, 30 * 3600)
        self.assertEqual(rx.system_warnings("gw", last["health"], self.now), [])

    def test_busy_cpu_and_low_memory(self):
        for m in range(20, 0, -1):
            last = self.add(m, 92 if m <= 15 else 40, 200 * MB, 3600)
        last["health"]["host"]["mem_available_bytes"] = 500 * MB
        w = " | ".join(rx.system_warnings("gw", last["health"], self.now))
        self.assertIn("CPU has averaged 92%", w)
        self.assertIn("low on memory", w)

    def test_memory_leak(self):
        up = 30 * 3600
        for h_ago in range(25, 0, -1):                    # 200 MB a day ago, 400 MB now
            last = self.add(h_ago * 60, 30, (200 + (25 - h_ago) * 9) * MB, up - h_ago * 3600)
        last["health"]["process"]["rss_bytes"] = 420 * MB
        self.assertIn("memory grew from 200 MB to 420 MB", " ".join(rx.system_warnings("gw", last["health"], self.now)))
        # a young gateway (restarted today) isn't judged
        last["health"]["uptime_s"] = 3 * 3600
        self.assertEqual(rx.system_warnings("gw", last["health"], self.now), [])


if __name__ == "__main__":
    unittest.main()
